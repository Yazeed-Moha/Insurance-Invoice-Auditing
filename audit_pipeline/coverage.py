"""Deterministic source coverage for financial contract clauses."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .chunking import ContractChunk


TABLE_HEADER_TERMS = re.compile(
    r"service|rate|unit basis|discount|uplift|premium|threshold|maximum|daily cap|"
    r"billable|excluded|window|bundled|substituted|multiplier|bronze|silver|gold|f-main",
    re.I,
)
PROSE_TERMS = re.compile(
    r"rate of\s+GBP|cumulative utilisation[^.]*discount|discount[^.]*cumulative utilisation|"
    r"premium of|uplift of|increased by[^.]*%|"
    r"not billable|both delivered|maximum of|may not exceed|shall not bill more than|"
    r"daily quantity|non-business day[^.]*%",
    re.I,
)
VALUE_MARKER = re.compile(r"GBP|\d|%", re.I)


def _normalise(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def source_units(chunk: ContractChunk) -> list[dict[str, str]]:
    """Identify source rows/clauses that can change invoice pricing or validity."""
    lines = chunk.content.splitlines()
    raw_units: list[tuple[str, str]] = []
    prose: list[str] = []

    def flush_prose() -> None:
        text = "\n".join(prose).strip()
        prose.clear()
        text = "\n".join(line for line in text.splitlines() if not line.startswith("#")).strip()
        sentences = re.split(r"(?<=[.!?])\s+(?=(?:\d+\.\d+\s+)?[A-Z\"])", text)
        for sentence in sentences:
            # Clause numbers such as ``3.5`` are identifiers, not financial
            # values. An abstract definition with no amount/threshold must not
            # be treated as an extractable rule.
            value_text = re.sub(r"^\d+\.\d+\s+", "", sentence)
            if sentence and PROSE_TERMS.search(sentence) and VALUE_MARKER.search(value_text):
                raw_units.append(("prose_clause", sentence))

    index = 0
    while index < len(lines):
        if lines[index].lstrip().startswith("|"):
            flush_prose()
            table: list[str] = []
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                table.append(lines[index].strip())
                index += 1
            if len(table) >= 3 and TABLE_HEADER_TERMS.search(table[0]):
                for row in table[2:]:
                    raw_units.append(("table_row", row))
            continue
        if not lines[index].strip():
            flush_prose()
        else:
            prose.append(lines[index])
        index += 1
    flush_prose()

    units = []
    for index, (kind, text) in enumerate(raw_units, start=1):
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
        units.append({
            "source_unit_id": f"{chunk.chunk_id}-u{index:03d}-{digest}",
            "kind": kind,
            "text": text,
        })
    return units


def coverage_report(fragment: dict[str, Any], chunk: ContractChunk) -> tuple[list[dict[str, Any]], list[str]]:
    """Prove that every financial source unit supports at least one extracted fact."""
    evidence: list[tuple[str, str]] = []
    for service in fragment.get("services", []):
        for index, rate in enumerate(service.get("rates", [])):
            item = rate.get("evidence", {})
            evidence.append((f"service:{service.get('name')}:rate:{index}", _normalise(str(item.get("text", "")))))
    for kind, rules in fragment.get("rules", {}).items():
        for index, rule in enumerate(rules):
            item = rule.get("evidence", {})
            evidence.append((f"rule:{kind}:{index}", _normalise(str(item.get("text", "")))))

    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    for unit in source_units(chunk):
        text = _normalise(unit["text"])
        matches = [label for label, quote in evidence if quote and (quote in text or text in quote)]
        status = "covered" if matches else "uncovered"
        records.append({**unit, "status": status, "matched_facts": matches})
        if not matches:
            warnings.append(f"uncovered financial source unit {unit['source_unit_id']}: {text[:240]}")
    return records, warnings
