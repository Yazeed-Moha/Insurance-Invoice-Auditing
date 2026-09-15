"""Strict LLM contract-understanding workflow with deterministic validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any

from jsonschema import ValidationError, validate

from .chunking import ContractChunk, chunk_contract, save_chunks
from .coverage import coverage_report
from .llm import LLMError, StructuredLLMClient, recover_failed_generation
from .schema import RULE_KINDS, normalise_calculation_order, normalise_unit, validate_contract


LOG = logging.getLogger("insurance_audit.contract_agent")
PROMPT_VERSION = "contract_extraction_v5"
REPAIR_PROMPT_VERSION = "contract_repair_v2"
SCHEMA_VERSION = "2.0"


EVIDENCE_SCHEMA = {
    "type": "object",
    "required": ["source_file", "section", "text"],
    "properties": {
        "source_file": {"type": "string"},
        "section": {"type": "string"},
        "text": {"type": "string"},
    },
}
RATE_SCHEMA = {
    "type": "object",
    "required": ["effective_from", "effective_to", "rate_cents", "evidence"],
    "properties": {
        "effective_from": {"type": ["string", "null"]},
        "effective_to": {"type": ["string", "null"]},
        "rate_cents": {"type": "integer"},
        "evidence": EVIDENCE_SCHEMA,
    },
}
SERVICE_SCHEMA = {
    "type": "object",
    "required": ["service_id", "name", "aliases", "unit", "rates", "daily_cap"],
    "properties": {
        "service_id": {"type": "string"},
        "name": {"type": "string"},
        "aliases": {"type": "array", "items": {"type": "string"}},
        "unit": {"type": "string", "enum": [
            "per_visit", "per_procedure", "per_hour", "per_day", "per_item",
            "per_night", "per_test", "per_unit_dispensed", "per_hour_per_item",
        ]},
        "rates": {"type": "array", "items": RATE_SCHEMA},
        "daily_cap": {"type": ["integer", "null"]},
    },
}


def _rule_schema(required: list[str], properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object", "required": [*required, "evidence"],
        "properties": {**properties, "evidence": EVIDENCE_SCHEMA},
    }


RULE_SCHEMAS = {
    "threshold_premiums": _rule_schema(["service", "threshold", "basis_points"], {
        "service": {"type": "string"}, "threshold": {"type": "integer"}, "basis_points": {"type": "integer"},
    }),
    "weekend_uplifts": _rule_schema(["service", "basis_points"], {
        "service": {"type": "string"}, "basis_points": {"type": "integer"},
    }),
    "volume_discounts": _rule_schema(["service", "threshold", "basis_points"], {
        "service": {"type": "string"}, "threshold": {"type": "integer"}, "basis_points": {"type": "integer"},
    }),
    "daily_caps": _rule_schema(["service", "limit"], {
        "service": {"type": "string"}, "limit": {"type": "integer"},
    }),
    "bundles": _rule_schema(["service_a", "service_b", "rate_a_cents", "rate_b_cents"], {
        "service_a": {"type": "string"}, "service_b": {"type": "string"},
        "rate_a_cents": {"type": "integer"}, "rate_b_cents": {"type": "integer"},
    }),
    "exclusions": _rule_schema(["service_a", "service_b", "window_days"], {
        "service_a": {"type": "string"}, "service_b": {"type": "string"}, "window_days": {"type": "integer"},
    }),
    "facility_multipliers": _rule_schema(["service", "facility", "numerator", "denominator"], {
        "service": {"type": "string"}, "facility": {"type": "string"},
        "numerator": {"type": "integer"}, "denominator": {"type": "integer"},
    }),
    "plan_multipliers": _rule_schema(["service", "plan_tier", "numerator", "denominator"], {
        "service": {"type": "string"}, "plan_tier": {"type": "string"},
        "numerator": {"type": "integer"}, "denominator": {"type": "integer"},
    }),
}


def normalise_fragment(fragment: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize harmless representation differences before validation."""
    output = json.loads(json.dumps(fragment))
    rules = output.setdefault("rules", {})
    for kind in RULE_KINDS:
        rules.setdefault(kind, [])
    known_caps = {rule.get("service") for rule in rules["daily_caps"]}
    for service in output.get("services", []):
        service["unit"] = normalise_unit(str(service.get("unit", "")))
        cap = service.get("daily_cap")
        if isinstance(cap, int) and cap > 0 and service.get("name") not in known_caps:
            evidence = next((rate.get("evidence") for rate in service.get("rates", [])
                             if isinstance(rate.get("evidence"), dict)), None)
            if evidence is not None:
                rules["daily_caps"].append({
                    "service": service.get("name"), "limit": cap, "evidence": evidence,
                })
                known_caps.add(service.get("name"))
    # Some models use the canonical service_id in rule references even though
    # the rule schema expects the canonical service name. Both identify the
    # same extracted service, so normalize this before reference validation.
    service_names = {
        service.get("service_id"): service.get("name")
        for service in output.get("services", [])
        if service.get("service_id") and service.get("name")
    }
    for incoming_rules in rules.values():
        for rule in incoming_rules:
            for field in ("service", "service_a", "service_b"):
                if rule.get(field) in service_names:
                    rule[field] = service_names[rule[field]]
    output["calculation_order"] = normalise_calculation_order(output.get("calculation_order", []))
    return output


FRAGMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["metadata", "services", "rules", "calculation_order", "warnings"],
    "properties": {
        "metadata": {
            "type": "object",
            "properties": {
                "contract_number": {"type": ["string", "null"]}, "provider": {"type": ["string", "null"]},
                "payer": {"type": ["string", "null"]}, "effective_from": {"type": ["string", "null"]},
                "effective_to": {"type": ["string", "null"]}, "currency": {"type": ["string", "null"]},
                "rounding": {"type": ["string", "null"]},
            },
        },
        "services": {"type": "array", "items": SERVICE_SCHEMA},
        "rules": {
            "type": "object",
            "required": list(RULE_KINDS),
            "properties": {key: {"type": "array", "items": schema} for key, schema in RULE_SCHEMAS.items()},
        },
        "calculation_order": {"type": "array", "items": {"type": "string", "enum": [
            "bundle", "facility_multiplier", "plan_multiplier", "premium", "volume_discount",
        ]}},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
}


def source_fingerprint(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _empty_rules() -> dict[str, list[dict[str, Any]]]:
    return {key: [] for key in FRAGMENT_SCHEMA["properties"]["rules"]["required"]}


def merge_fragment_patch(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a targeted missing-facts response into one existing chunk fragment."""
    base = normalise_fragment(base)
    patch = normalise_fragment(patch)
    metadata = base.setdefault("metadata", {})
    for key, value in patch.get("metadata", {}).items():
        if value not in (None, ""):
            metadata[key] = value
    services = {item.get("name"): item for item in base.get("services", []) if item.get("name")}
    for incoming in patch.get("services", []):
        name = incoming.get("name")
        if not name:
            continue
        if name not in services:
            services[name] = incoming
            continue
        existing = services[name]
        rates = existing.setdefault("rates", [])
        rate_positions = {
            (item.get("effective_from"), item.get("effective_to"), item.get("rate_cents")): index
            for index, item in enumerate(rates)
        }
        for item in incoming.get("rates", []):
            identity = (item.get("effective_from"), item.get("effective_to"), item.get("rate_cents"))
            if identity in rate_positions:
                rates[rate_positions[identity]] = item
            else:
                rate_positions[identity] = len(rates)
                rates.append(item)
        for alias in incoming.get("aliases", []):
            if alias not in existing.setdefault("aliases", []):
                existing["aliases"].append(alias)
        if existing.get("daily_cap") is None and incoming.get("daily_cap") is not None:
            existing["daily_cap"] = incoming["daily_cap"]
    base["services"] = list(services.values())
    for kind in RULE_KINDS:
        existing_rules = base["rules"][kind]
        def identity(item: dict[str, Any]) -> str:
            return json.dumps({key: value for key, value in item.items() if key != "evidence"}, sort_keys=True)
        positions = {identity(item): index for index, item in enumerate(existing_rules)}
        for rule in patch["rules"][kind]:
            encoded = identity(rule)
            if encoded in positions:
                existing_rules[positions[encoded]] = rule
            else:
                positions[encoded] = len(existing_rules)
                existing_rules.append(rule)
    if patch.get("calculation_order"):
        base["calculation_order"] = patch["calculation_order"]
    for warning in patch.get("warnings", []):
        if warning not in base.setdefault("warnings", []):
            base["warnings"].append(warning)
    return normalise_fragment(base)


def reconcile_rate_schedules(services: list[dict[str, Any]]) -> list[str]:
    """Remove a broad rate shadowed by one explicit multi-period schedule.

    Contracts split across appendices and amendments can state the original
    full-term rate in one document and the authoritative before/after schedule
    in another. A schedule is authoritative only when at least two rates with
    different values share the same exact evidence quotation and together
    cover the candidate's entire interval. All other overlaps remain intact so
    ``validate_contract`` rejects them.
    """
    notes: list[str] = []

    def bounds(rate: dict[str, Any]) -> tuple[date, date] | None:
        try:
            start = date.fromisoformat(rate["effective_from"]) if rate.get("effective_from") else date.min
            end = date.fromisoformat(rate["effective_to"]) if rate.get("effective_to") else date.max
        except (TypeError, ValueError):
            return None
        return start, end

    def covers(intervals: list[tuple[date, date]], target: tuple[date, date]) -> bool:
        cursor, target_end = target
        for start, end in sorted(intervals):
            if end < cursor:
                continue
            if start > cursor:
                return False
            if end >= target_end:
                return True
            cursor = end + timedelta(days=1)
        return False

    for service in services:
        rates = service.get("rates", [])
        retained: list[dict[str, Any]] = []
        for index, candidate in enumerate(rates):
            target = bounds(candidate)
            if target is None:
                retained.append(candidate)
                continue
            candidate_evidence = candidate.get("evidence", {})
            candidate_key = (
                candidate_evidence.get("source_file"), candidate_evidence.get("text"),
            )
            groups: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
            for other_index, other in enumerate(rates):
                if other_index == index:
                    continue
                evidence = other.get("evidence", {})
                key = (evidence.get("source_file"), evidence.get("text"))
                if key != candidate_key:
                    groups.setdefault(key, []).append(other)
            shadowed_by: tuple[Any, Any] | None = None
            for key, schedule in groups.items():
                schedule_bounds = [item for rate in schedule if (item := bounds(rate)) is not None]
                if (len(schedule_bounds) >= 2
                        and len({rate.get("rate_cents") for rate in schedule}) >= 2
                        and covers(schedule_bounds, target)):
                    shadowed_by = key
                    break
            if shadowed_by is None:
                retained.append(candidate)
                continue
            notes.append(
                f"removed shadowed broad rate for {service.get('name')} from "
                f"{candidate_key[0]}; complete multi-period schedule is evidenced in {shadowed_by[0]}"
            )
        service["rates"] = retained
    return notes


def merge_fragments(fragments: list[dict[str, Any]], hospital_id: str,
                    source_files: list[str], manifest: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {"hospital_id": hospital_id}
    services: dict[str, dict[str, Any]] = {}
    rules = _empty_rules()
    order: list[str] = []
    model_warnings: list[str] = []
    warnings: list[str] = []
    for raw_fragment in fragments:
        fragment = normalise_fragment(raw_fragment)
        for key, value in fragment.get("metadata", {}).items():
            if value not in (None, ""):
                metadata.setdefault(key, value)
        for incoming in fragment.get("services", []):
            name = incoming.get("name")
            if not name:
                warnings.append("service without a name was discarded")
                continue
            if name not in services:
                services[name] = incoming
            else:
                existing = services[name]
                for rate in incoming.get("rates", []):
                    if rate not in existing.setdefault("rates", []):
                        existing["rates"].append(rate)
                for alias in incoming.get("aliases", []):
                    if alias not in existing.setdefault("aliases", []):
                        existing["aliases"].append(alias)
                if existing.get("daily_cap") is None and incoming.get("daily_cap") is not None:
                    existing["daily_cap"] = incoming["daily_cap"]
        for kind, incoming_rules in fragment.get("rules", {}).items():
            if kind not in rules:
                continue
            known = {json.dumps(item, sort_keys=True) for item in rules[kind]}
            for rule in incoming_rules:
                encoded = json.dumps(rule, sort_keys=True)
                if encoded not in known:
                    rules[kind].append(rule)
                    known.add(encoded)
        incoming_order = fragment.get("calculation_order", [])
        # Chunks containing one rule often return only the locally relevant
        # subset. Keep the most complete explicit order instead of allowing a
        # later one-step subset to overwrite the contract-wide convention.
        if len(incoming_order) > len(order):
            order = incoming_order
        for warning in fragment.get("warnings", []):
            if warning not in model_warnings:
                model_warnings.append(warning)
    sorted_services = sorted(services.values(), key=lambda item: item["name"])
    normalization_notes = reconcile_rate_schedules(sorted_services)
    contract = {
        "schema_version": SCHEMA_VERSION,
        "hospital_id": hospital_id,
        "metadata": metadata,
        "services": sorted_services,
        "rules": rules,
        "calculation_order": order or ["bundle", "facility_multiplier", "plan_multiplier", "premium", "volume_discount"],
        # Model notes describe local chunk ambiguity or intentionally omitted
        # facts. They remain visible for review but are not validation errors.
        # Python-generated schema, reference, coverage, and grounding failures
        # are kept in ``warnings`` and therefore still block strict LLM mode.
        "extraction": {
            **manifest,
            "source_files": source_files,
            "model_warnings": model_warnings,
            "normalization_notes": normalization_notes,
            "warnings": warnings,
        },
    }
    contract["extraction"]["warnings"].extend(validate_contract(contract))
    return contract


def grounding_warnings(contract: dict[str, Any], paths: list[Path]) -> list[str]:
    """Reject LLM facts whose claimed quotation is absent from the source."""
    sources = {path.name: re.sub(r"\s+", " ", path.read_text(encoding="utf-8")).strip() for path in paths}
    warnings: list[str] = []

    def check(evidence: Any, label: str) -> None:
        if not isinstance(evidence, dict):
            warnings.append(f"missing evidence for {label}")
            return
        filename = Path(str(evidence.get("source_file", ""))).name
        quote = re.sub(r"\s+", " ", str(evidence.get("text", ""))).strip()
        if filename not in sources:
            warnings.append(f"evidence file not found for {label}: {filename}")
        elif not quote or quote not in sources[filename]:
            warnings.append(f"evidence quote not grounded for {label}")

    for service in contract.get("services", []):
        for index, rate in enumerate(service.get("rates", [])):
            check(rate.get("evidence"), f"{service.get('name')} rate {index}")
    for kind, rules in contract.get("rules", {}).items():
        for index, rule in enumerate(rules):
            check(rule.get("evidence"), f"{kind}[{index}]")
    return warnings


def chunk_grounding_warnings(fragment: dict[str, Any], chunk: ContractChunk) -> list[str]:
    """Return evidence failures attributable to one model input chunk."""
    source = re.sub(r"\s+", " ", chunk.content).strip()
    warnings: list[str] = []

    def check(evidence: Any, label: str) -> None:
        if not isinstance(evidence, dict):
            warnings.append(f"missing evidence for {label}")
            return
        filename = Path(str(evidence.get("source_file", ""))).name
        quote = re.sub(r"\s+", " ", str(evidence.get("text", ""))).strip()
        if filename != chunk.source_file:
            warnings.append(f"evidence file must be {chunk.source_file} for {label}")
        elif not quote or quote not in source:
            warnings.append(f"evidence quote must be copied verbatim from this chunk for {label}")

    for service in fragment.get("services", []):
        for index, rate in enumerate(service.get("rates", [])):
            check(rate.get("evidence"), f"{service.get('name')} rate {index}")
    for kind, rules in fragment.get("rules", {}).items():
        for index, rule in enumerate(rules):
            check(rule.get("evidence"), f"{kind}[{index}]")
    return warnings


@dataclass
class ContractUnderstandingAgent:
    root: Path
    cache_dir: Path
    prompt_path: Path
    llm_client: StructuredLLMClient
    max_chunk_chars: int = 12_000
    max_table_rows: int = 20
    chunks_dir: Path | None = None
    llm_runs_dir: Path | None = None

    def parse(self, contract_dir: Path, hospital_id: str) -> dict[str, Any]:
        paths = sorted(contract_dir.glob("*.md"))
        if not paths:
            raise FileNotFoundError(f"no Markdown contracts in {contract_dir}")
        fingerprint = source_fingerprint(paths)
        provider_limit = getattr(self.llm_client, "recommended_max_chunk_chars", self.max_chunk_chars)
        effective_max_chars = min(self.max_chunk_chars, provider_limit)
        chunks = chunk_contract(paths, effective_max_chars, self.max_table_rows)
        chunks_dir = self.chunks_dir or self.root / "artifacts" / "chunks"
        saved_at = save_chunks(chunks, chunks_dir, hospital_id, fingerprint,
                               effective_max_chars, self.max_table_rows)
        relative_chunks = str(saved_at.relative_to(self.root)) if saved_at.is_relative_to(self.root) else str(saved_at)
        LOG.info("saved %d contract chunks for %s to %s", len(chunks), hospital_id, saved_at)
        cache_path = self.cache_dir / f"hospital_{hospital_id.removeprefix('H')}.json"
        cached = self._read_cache(cache_path, fingerprint)
        if cached is not None:
            cached["extraction"]["chunks_directory"] = relative_chunks
            LOG.info("using validated LLM contract cache for %s", hospital_id)
            return cached
        try:
            contract = self._extract_with_llm(paths, hospital_id, fingerprint, chunks)
            contract["extraction"]["chunks_directory"] = relative_chunks
            if contract["extraction"]["warnings"]:
                raise LLMError("; ".join(contract["extraction"]["warnings"][:5]))
            self._write_cache(cache_path, contract)
            return contract
        except LLMError as exc:
            LOG.warning("LLM extraction failed for %s: %s", hospital_id, exc)
            raise

    def _extract_with_llm(self, paths: list[Path], hospital_id: str, fingerprint: str,
                          chunks: list[ContractChunk]) -> dict[str, Any]:
        assert self.llm_client is not None
        system_prompt = self.prompt_path.read_text(encoding="utf-8")
        generated_at = datetime.now(timezone.utc)
        model_slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", self.llm_client.model).strip("-") or "model"
        run_id = generated_at.strftime("%Y%m%dT%H%M%S.%fZ")
        model_root = (self.llm_runs_dir or self.root / "artifacts" / "llm_runs") / \
            f"hospital_{hospital_id.removeprefix('H')}" / fingerprint[:12] / model_slug
        run_root = model_root / run_id
        fragments: list[dict[str, Any]] = []
        failures: dict[int, list[str]] = {}
        resumed_ids: list[str] = []
        incomplete_resumed_ids: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            saved = self._find_saved_fragment(model_root, index, chunk)
            if saved is not None:
                fragment, saved_issues = saved
                if saved_issues:
                    LOG.info("reusing schema-valid incomplete response for %s chunk %d/%d id=%s; queued for targeted repair",
                             hospital_id, index, len(chunks), chunk.chunk_id)
                    incomplete_resumed_ids.append(chunk.chunk_id)
                else:
                    LOG.info("reusing validated saved response for %s chunk %d/%d id=%s",
                             hospital_id, index, len(chunks), chunk.chunk_id)
                    resumed_ids.append(chunk.chunk_id)
                self._save_llm_record(run_root / "resumed", f"{index:03d}-{chunk.chunk_id}", fragment, None)
            else:
                fragment = self._generate_chunk(hospital_id, index, len(chunks), chunk,
                                                system_prompt, run_root, attempt=1)
            fragment = normalise_fragment(fragment)
            fragments.append(fragment)
            _, coverage_warnings = coverage_report(fragment, chunk)
            chunk_warnings = [*chunk_grounding_warnings(fragment, chunk), *coverage_warnings]
            if chunk_warnings:
                failures[index - 1] = chunk_warnings

        repaired_ids: list[str] = []
        if failures:
            LOG.warning("validation requested targeted repair of %d/%d chunks for %s: %s",
                        len(failures), len(chunks), hospital_id,
                        ", ".join(chunks[index].chunk_id for index in failures))
            repair_template = (
                self.root / "config" / "prompts" / "contract_repair_v2.md"
            ).read_text(encoding="utf-8")
            for zero_index, feedback in failures.items():
                chunk = chunks[zero_index]
                repair_prompt = system_prompt + "\n\n" + repair_template + \
                    "\n\nVALIDATION FAILURES:\n- " + "\n- ".join(feedback)
                repair_fragment = self._generate_chunk(
                    hospital_id, zero_index + 1, len(chunks), chunk,
                    repair_prompt, run_root, attempt=2,
                )
                fragments[zero_index] = merge_fragment_patch(
                    fragments[zero_index], repair_fragment,
                )
                self._save_llm_record(
                    run_root / "merged",
                    f"{zero_index + 1:03d}-{chunk.chunk_id}",
                    fragments[zero_index], None,
                )
                repaired_ids.append(chunk.chunk_id)
        source_coverage: list[dict[str, Any]] = []
        local_warnings: list[str] = []
        for fragment, chunk in zip(fragments, chunks):
            records, warnings = coverage_report(fragment, chunk)
            source_coverage.extend(records)
            local_warnings.extend(chunk_grounding_warnings(fragment, chunk))
            local_warnings.extend(warnings)
        manifest = {
            "method": "llm", "provider": self.llm_client.provider, "model": self.llm_client.model,
            "prompt_version": PROMPT_VERSION, "schema_version": SCHEMA_VERSION,
            "source_hash": fingerprint, "generated_at": generated_at.isoformat(),
            "temperature": getattr(self.llm_client, "temperature", None), "chunk_count": len(chunks),
            "max_chunk_chars": min(
                self.max_chunk_chars,
                getattr(self.llm_client, "recommended_max_chunk_chars", self.max_chunk_chars),
            ),
            "max_completion_tokens": getattr(self.llm_client, "max_completion_tokens", None),
            "confidence": 0.95, "validation_attempts": 2 if repaired_ids else 1,
            "repair_prompt_version": REPAIR_PROMPT_VERSION if repaired_ids else None,
            "repaired_chunks": repaired_ids,
            "resumed_chunks": resumed_ids,
            "incomplete_resumed_chunks": incomplete_resumed_ids,
            "source_coverage": source_coverage,
            "llm_run_directory": str(run_root.relative_to(self.root)) if run_root.is_relative_to(self.root) else str(run_root),
        }
        contract = merge_fragments(fragments, hospital_id, [str(path.relative_to(self.root)) for path in paths], manifest)
        contract["extraction"]["warnings"].extend(local_warnings)
        contract["extraction"]["warnings"].extend(grounding_warnings(contract, paths))
        return contract

    def _generate_chunk(self, hospital_id: str, index: int, total: int,
                        chunk: ContractChunk, system_prompt: str, run_root: Path,
                        attempt: int) -> dict[str, Any]:
        assert self.llm_client is not None
        LOG.info("%s %s chunk %d/%d id=%s source=%s chars=%d sections=%s with %s",
                 "repairing" if attempt > 1 else "extracting", hospital_id, index, total,
                 chunk.chunk_id, chunk.source_file, chunk.char_count,
                 " | ".join(chunk.sections), self.llm_client.model)
        attempt_dir = run_root / f"attempt_{attempt}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{index:03d}-{chunk.chunk_id}"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Hospital: {hospital_id}\nChunk {index} of {total}. Extract only facts present in this chunk.\n\n{chunk.render()}"},
        ]
        try:
            fragment = self.llm_client.generate(messages, FRAGMENT_SCHEMA)
        except LLMError as exc:
            self._save_llm_record(attempt_dir, stem, None, str(exc))
            raise
        self._save_llm_record(attempt_dir, stem, fragment, None)
        return fragment

    def _save_llm_record(self, directory: Path, stem: str,
                         fragment: dict[str, Any] | None, error: str | None) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        record = {
            "provider": getattr(self.llm_client, "provider", None),
            "model": getattr(self.llm_client, "model", None),
            "prompt_version": PROMPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "fragment": fragment,
            "error": error,
        }
        (directory / f"{stem}.response.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        raw = getattr(self.llm_client, "last_raw_response", None)
        if raw:
            provider = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(getattr(self.llm_client, "provider", "llm")))
            (directory / f"{stem}.{provider}-raw.json").write_text(raw, encoding="utf-8")

    def _find_saved_fragment(self, model_root: Path, index: int,
                             chunk: ContractChunk) -> tuple[dict[str, Any], list[str]] | None:
        """Return a saved schema-valid fragment and its local validation issues.

        Complete fragments are preferred. If none exists, the best incomplete
        fragment is returned so the targeted repair prompt can use its exact
        coverage/grounding failures instead of spending quota on a generic retry.
        """
        stem = f"{index:03d}-{chunk.chunk_id}.response.json"
        saved_fragments: list[tuple[Path, dict[str, Any], list[str]]] = []

        def issues_for(fragment: dict[str, Any]) -> list[str]:
            _, coverage_warnings = coverage_report(fragment, chunk)
            return [*chunk_grounding_warnings(fragment, chunk), *coverage_warnings]

        for run_dir in sorted(model_root.glob("*"), reverse=True):
            candidates = sorted(run_dir.glob(f"attempt_*/{stem}"), reverse=True)
            candidates.extend(sorted(run_dir.glob(f"resumed/{stem}"), reverse=True))
            candidates.extend(sorted(run_dir.glob(f"merged/{stem}"), reverse=True))
            for path in candidates:
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                    fragment = record.get("fragment")
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(fragment, dict):
                    for raw_path in path.parent.glob(path.name.removesuffix(".response.json") + ".*-raw.json"):
                        try:
                            body = json.loads(raw_path.read_text(encoding="utf-8"))
                        except (OSError, json.JSONDecodeError):
                            continue
                        fragment = recover_failed_generation(body, FRAGMENT_SCHEMA)
                        if fragment is not None:
                            LOG.info("recovered a schema-valid fragment from saved rejected output: %s", raw_path)
                            break
                if not isinstance(fragment, dict):
                    continue
                if record.get("model") != self.llm_client.model:
                    continue
                fragment = normalise_fragment(fragment)
                try:
                    validate(instance=fragment, schema=FRAGMENT_SCHEMA)
                except ValidationError:
                    continue
                issues = issues_for(fragment)
                if not issues:
                    return fragment, []
                saved_fragments.append((path, fragment, issues))
        if not saved_fragments:
            return None

        # Older interrupted runs may predate persisted merged responses. Rebuild
        # them from the best full/base response plus chronological attempt_2
        # deltas, but keep a merge only when deterministic validation improves.
        bases = [item for item in saved_fragments if item[0].parent.name != "attempt_2"]
        current_path, current, current_issues = min(
            bases or saved_fragments, key=lambda item: len(item[2]),
        )
        patches = sorted(
            (item for item in saved_fragments if item[0].parent.name == "attempt_2"),
            key=lambda item: item[0].stat().st_mtime,
        )
        for _, patch, _ in patches:
            candidate = merge_fragment_patch(current, patch)
            candidate_issues = issues_for(candidate)
            if len(candidate_issues) < len(current_issues):
                current, current_issues = candidate, candidate_issues
                if not current_issues:
                    return current, []
        best_individual = min(saved_fragments, key=lambda item: len(item[2]))
        if len(best_individual[2]) < len(current_issues):
            return best_individual[1], best_individual[2]
        return current, current_issues

    def _read_cache(self, path: Path, fingerprint: str) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            contract = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        extraction = contract.get("extraction", {})
        if (extraction.get("source_hash") != fingerprint
                or extraction.get("schema_version") != SCHEMA_VERSION
                or extraction.get("prompt_version") != PROMPT_VERSION
                or extraction.get("provider") != self.llm_client.provider
                or extraction.get("model") != self.llm_client.model
                or validate_contract(contract)):
            return None
        contract["extraction"]["method"] = "validated_cache"
        return contract

    @staticmethod
    def _write_cache(path: Path, contract: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(contract, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(path)
