from decimal import Decimal
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from audit_pipeline.contract_agent import (
    ContractUnderstandingAgent, FRAGMENT_SCHEMA, merge_fragment_patch, merge_fragments,
    normalise_fragment, numeric_evidence_warnings, reconcile_rate_schedules,
)
from audit_pipeline.chunking import ContractChunk, chunk_contract
from audit_pipeline.coverage import coverage_report
from audit_pipeline.contract_parser import parse_contract
from audit_pipeline.engine import _mul
from audit_pipeline.llm import GLMClient, GroqClient, LLMError, recover_failed_generation, strict_json_schema
from audit_pipeline.matcher import ServiceMatcher
from audit_pipeline.schema import RULE_KINDS, validate_contract


ROOT = Path(__file__).resolve().parents[1]


class PipelineTests(unittest.TestCase):
    def test_numeric_evidence_rejects_rate_scaled_by_100_twice(self):
        fragment = {
            "services": [{
                "name": "Emergency Transport",
                "rates": [{
                    "rate_cents": 967500,
                    "evidence": {"text": "The rate is GBP 96.75 per visit."},
                }],
            }],
            "rules": {},
        }
        warnings = numeric_evidence_warnings(fragment)
        self.assertEqual(len(warnings), 1)
        self.assertIn("evidence supports [9675]", warnings[0])

    def test_numeric_evidence_accepts_rate_and_rule_values(self):
        evidence = {
            "text": "The rate is GBP 96.75. After 60 visits, a discount of ten percent (10%) applies."
        }
        fragment = {
            "services": [{"name": "Emergency Transport", "rates": [{
                "rate_cents": 9675, "evidence": evidence,
            }]}],
            "rules": {"volume_discounts": [{
                "service": "Emergency Transport", "threshold": 60,
                "basis_points": 1000, "evidence": evidence,
            }]},
        }
        self.assertEqual(numeric_evidence_warnings(fragment), [])

    def test_numeric_evidence_checks_multiplier_ratio(self):
        evidence = {"text": "| Service A | 1 | 1.10 | 0.92 |"}
        accepted = {
            "services": [],
            "rules": {"facility_multipliers": [{
                "service": "Service A", "facility": "F-NORTH",
                "numerator": 11, "denominator": 10, "evidence": evidence,
            }]},
        }
        self.assertEqual(numeric_evidence_warnings(accepted), [])

        rejected = json.loads(json.dumps(accepted))
        rejected["rules"]["facility_multipliers"][0]["numerator"] = 12
        warnings = numeric_evidence_warnings(rejected)
        self.assertEqual(len(warnings), 1)
        self.assertIn("extracted 12/10", warnings[0])

    def test_targeted_patch_replaces_same_evidenced_rate_correction(self):
        rules = {kind: [] for kind in RULE_KINDS}
        evidence = {"source_file": "contract.md", "section": "Rates", "text": "Rate GBP 96.75"}
        service = {"service_id": "a", "name": "A", "aliases": [], "unit": "per_visit",
                   "daily_cap": None, "rates": [{"effective_from": None, "effective_to": None,
                                                   "rate_cents": 967500, "evidence": evidence}]}
        base = {"metadata": {}, "services": [service], "rules": rules,
                "calculation_order": [], "warnings": []}
        corrected = json.loads(json.dumps(service))
        corrected["rates"][0]["rate_cents"] = 9675
        patch_fragment = {"metadata": {}, "services": [corrected],
                          "rules": {kind: [] for kind in RULE_KINDS},
                          "calculation_order": [], "warnings": []}
        merged = merge_fragment_patch(base, patch_fragment)
        self.assertEqual([rate["rate_cents"] for rate in merged["services"][0]["rates"]], [9675])

    def test_complete_amendment_schedule_shadows_broad_original_rate(self):
        service = {
            "name": "Service A",
            "rates": [
                {"effective_from": "2024-01-01", "effective_to": "2025-12-31",
                 "rate_cents": 10000,
                 "evidence": {"source_file": "appendix.md", "text": "original"}},
                {"effective_from": None, "effective_to": "2024-12-31", "rate_cents": 10000,
                 "evidence": {"source_file": "amendment.md", "text": "before | after"}},
                {"effective_from": "2025-01-01", "effective_to": None, "rate_cents": 12000,
                 "evidence": {"source_file": "amendment.md", "text": "before | after"}},
            ],
        }
        notes = reconcile_rate_schedules([service])
        self.assertEqual([rate["rate_cents"] for rate in service["rates"]], [10000, 12000])
        self.assertEqual(len(notes), 1)

    def test_model_notes_are_preserved_without_becoming_validation_failures(self):
        fragment = {
            "metadata": {"effective_from": None, "effective_to": None},
            "services": [{
                "service_id": "service_a", "name": "Service A", "aliases": [],
                "unit": "per_visit", "daily_cap": None,
                "rates": [{
                    "effective_from": None, "effective_to": None, "rate_cents": 10000,
                    "evidence": {"source_file": "contract.md", "section": "Rates", "text": "Service A"},
                }],
            }],
            "rules": {kind: [] for kind in RULE_KINDS},
            "calculation_order": [],
            "warnings": ["Another service is defined in a different chunk."],
        }
        contract = merge_fragments([fragment], "H1", ["contract.md"], {})
        self.assertEqual(contract["extraction"]["warnings"], [])
        self.assertEqual(
            contract["extraction"]["model_warnings"],
            ["Another service is defined in a different chunk."],
        )

    def test_merge_keeps_most_complete_calculation_order(self):
        base = {
            "metadata": {}, "services": [],
            "rules": {kind: [] for kind in RULE_KINDS}, "warnings": [],
        }
        full = {
            **base,
            "calculation_order": [
                "bundle", "facility_multiplier", "plan_multiplier", "premium", "volume_discount",
            ],
        }
        local = {**base, "calculation_order": ["volume_discount"]}
        contract = merge_fragments([full, local], "H1", ["contract.md"], {})
        self.assertEqual(contract["calculation_order"], full["calculation_order"])

    def test_glm_client_uses_zai_json_mode_and_validates_schema_locally(self):
        captured = {}

        class Completions:
            def create(self, **kwargs):
                captured.update(kwargs)
                message = types.SimpleNamespace(content='{"value":"ok"}')
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

        fake_client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=Completions()),
        )
        fake_module = types.SimpleNamespace(ZaiClient=lambda api_key: fake_client)
        schema = {"type": "object", "properties": {"value": {"type": "string"}}}
        with patch.dict("sys.modules", {"zai": fake_module}), \
                patch.dict("os.environ", {"GLM_API_KEY": "secret-for-test"}):
            result = GLMClient().generate([{"role": "user", "content": "extract"}], schema)
        self.assertEqual(result, {"value": "ok"})
        self.assertEqual(captured["model"], "glm-5.3")
        self.assertEqual(captured["response_format"], {"type": "json_object"})
        self.assertEqual(captured["thinking"], {"type": "enabled"})
        self.assertEqual(captured["reasoning_effort"], "low")
        self.assertIn("JSON Schema", captured["messages"][0]["content"])

    def test_groq_profile_fits_restricted_cloud_request_budget(self):
        client = GroqClient()
        self.assertEqual(client.max_completion_tokens, 3072)
        self.assertEqual(client.recommended_max_chunk_chars, 6000)

    def test_contract_normalization_uses_canonical_units(self):
        fragment = {
            "services": [{"name": "Consultation", "unit": "per visit", "rates": [], "daily_cap": None}],
            "rules": {}, "calculation_order": ["bundles", "weekend_uplifts", "volume_discounts"],
        }
        result = normalise_fragment(fragment)
        self.assertEqual(result["services"][0]["unit"], "per_visit")
        self.assertEqual(result["calculation_order"], ["bundle", "premium", "volume_discount"])
        self.assertIn("daily_caps", result["rules"])

    def test_coverage_rejects_silently_omitted_financial_row(self):
        chunk = ContractChunk(
            "discounts-001", "contract.md", ("Discounts",), ("table",),
            "| Service | Threshold | Discount |\n|---|---|---|\n"
            "| Service A | 60 visits | 10% |\n| Service B | 80 visits | 15% |",
        )
        fragment = {
            "services": [],
            "rules": {"volume_discounts": [{
                "service": "Service A", "threshold": 60, "basis_points": 1000,
                "evidence": {"source_file": "contract.md", "section": "Discounts",
                             "text": "| Service A | 60 visits | 10% |"},
            }]},
        }
        records, warnings = coverage_report(fragment, chunk)
        self.assertEqual([record["status"] for record in records], ["covered", "uncovered"])
        self.assertEqual(len(warnings), 1)

    def test_coverage_ignores_abstract_numbered_discount_definition(self):
        chunk = ContractChunk(
            "definitions-001", "contract.md", ("Conventions",), ("prose",),
            "3.5 A cumulative volume discount applies to a line item where cumulative "
            "utilisation of the Service prior to that line item exceeds the stated threshold.",
        )
        records, warnings = coverage_report({"services": [], "rules": {}}, chunk)
        self.assertEqual(records, [])
        self.assertEqual(warnings, [])

    def test_targeted_fragment_patch_preserves_existing_and_adds_missing_facts(self):
        empty_rules = {kind: [] for kind in RULE_KINDS}
        base = {"metadata": {}, "services": [{"service_id": "a", "name": "A", "aliases": [],
                "unit": "per_visit", "rates": [], "daily_cap": None}],
                "rules": empty_rules, "calculation_order": [], "warnings": []}
        patch_rules = {kind: [] for kind in RULE_KINDS}
        patch_rules["daily_caps"] = [{"service": "A", "limit": 3, "evidence": {
            "source_file": "contract.md", "section": "Caps", "text": "cap is 3"}}]
        patch = {"metadata": {}, "services": [{"service_id": "b", "name": "B", "aliases": [],
                 "unit": "per_test", "rates": [], "daily_cap": None}],
                 "rules": patch_rules, "calculation_order": [], "warnings": []}
        merged = merge_fragment_patch(base, patch)
        self.assertEqual({item["name"] for item in merged["services"]}, {"A", "B"})
        self.assertEqual(merged["rules"]["daily_caps"][0]["limit"], 3)

    def test_groq_strict_schema_adapter_closes_every_object(self):
        schema = strict_json_schema({
            "type": "object",
            "properties": {
                "metadata": {"type": "object", "properties": {"name": {"type": ["string", "null"]}}},
            },
        })
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["required"], ["metadata"])
        metadata = schema["properties"]["metadata"]
        self.assertFalse(metadata["additionalProperties"])
        self.assertEqual(metadata["required"], ["name"])

    def test_groq_failed_generation_repairs_only_misnested_top_level_fields(self):
        schema = {
            "type": "object",
            "properties": {
                "rules": {"type": "object", "properties": {}},
                "calculation_order": {"type": "array", "items": {"type": "string"}},
                "warnings": {"type": "array", "items": {"type": "string"}},
            },
        }
        body = {"code": "json_validate_failed", "failed_generation": json.dumps({
            "rules": {"calculation_order": [], "warnings": []},
        })}
        recovered = recover_failed_generation(body, schema)
        self.assertEqual(recovered, {"rules": {}, "calculation_order": [], "warnings": []})

        unsafe = {"code": "json_validate_failed", "failed_generation": json.dumps({
            "rules": {"unexpected": 1, "calculation_order": [], "warnings": []},
        })}
        self.assertIsNone(recover_failed_generation(unsafe, schema))

    def test_groq_failed_generation_fills_only_empty_structural_collections(self):
        schema = {
            "type": "object",
            "properties": {
                "rules": {"type": "object", "properties": {
                    "premiums": {"type": "array", "items": {"type": "object"}},
                    "discounts": {"type": "array", "items": {"type": "object"}},
                }},
                "calculation_order": {"type": "array", "items": {"type": "string"}},
                "warnings": {"type": "array", "items": {"type": "string"}},
            },
        }
        body = {"failed_generation": json.dumps({"rules": {"premiums": []}})}
        recovered = recover_failed_generation(body, schema)
        self.assertEqual(recovered, {
            "rules": {"premiums": [], "discounts": []},
            "calculation_order": [], "warnings": [],
        })

    def test_groq_failed_generation_exposes_empty_fragment_to_coverage(self):
        body = {"error": {"failed_generation": json.dumps({
            "metadata": {field: None for field in FRAGMENT_SCHEMA["properties"]["metadata"]["properties"]},
        })}}
        recovered = recover_failed_generation(body, FRAGMENT_SCHEMA)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["services"], [])
        self.assertEqual(set(recovered["rules"]), set(RULE_KINDS))

    def test_fragment_normalizes_rule_service_ids_to_names(self):
        fragment = {
            "services": [{"service_id": "service_a", "name": "Service A",
                          "unit": "per visit", "rates": [], "daily_cap": None}],
            "rules": {"weekend_uplifts": [{"service": "service_a", "basis_points": 1000}]},
            "calculation_order": [],
        }
        result = normalise_fragment(fragment)
        self.assertEqual(result["rules"]["weekend_uplifts"][0]["service"], "Service A")

    def test_markdown_chunking_splits_tables_between_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "contract.md"
            rows = "\n".join(f"| Service {i} | GBP {i}.00 |" for i in range(1, 6))
            path.write_text(f"# Agreement\n\n## Rates\n\n| Service | Rate |\n|---|---|\n{rows}\n", encoding="utf-8")
            chunks = chunk_contract([path], max_chars=500, max_table_rows=2)
            rendered = "\n".join(chunk.render() for chunk in chunks)
            self.assertGreaterEqual(len(chunks), 3)
            for i in range(1, 6):
                self.assertEqual(rendered.count(f"| Service {i} |"), 1)
            self.assertTrue(all(chunk.char_count <= 500 for chunk in chunks))

    def test_half_up_rounding(self):
        self.assertEqual(_mul(14125, 12000), 16950)
        self.assertEqual(_mul(101, 5000), 51)

    def test_hospital_1_contract_parses(self):
        contract = parse_contract(ROOT / "data/contracts/hospital_1", "H1")
        self.assertGreater(len(contract["services"]), 100)
        service = next(s for s in contract["services"] if s["name"] == "Advanced Neurological Consultation")
        self.assertEqual(service["rates"][0]["rate_cents"], 14125)
        self.assertEqual(service["unit"], "per_visit")
        self.assertGreater(len(contract["rules"]["daily_caps"]), 0)
        self.assertFalse(contract["extraction"]["warnings"])

    def test_abbreviated_match(self):
        contract = parse_contract(ROOT / "data/contracts/hospital_1", "H1")
        match = ServiceMatcher(contract).match("Consult Advanced Neuro", "per_visit", 14125)
        self.assertEqual(match.service_name, "Advanced Neurological Consultation")
        self.assertGreater(match.confidence, .7)

    def test_weak_cross_specialty_match_abstains(self):
        contract = parse_contract(ROOT / "data/contracts/hospital_1", "H1")
        match = ServiceMatcher(contract).match("Extended Haem Anaes Admin")
        self.assertIsNone(match.service_name)
        self.assertEqual(match.uncertainty, "no_service_match")

    def test_hospital_3_amendment(self):
        contract = parse_contract(ROOT / "data/contracts/hospital_3", "H3")
        service = next(s for s in contract["services"] if s["name"] == "Assisted Urologic Endoscopic Procedure")
        rates = {r["effective_from"]: r["rate_cents"] for r in service["rates"]}
        self.assertEqual(rates["2024-01-01"], 94250)
        self.assertEqual(rates["2025-01-01"], 111225)

    def test_business_validation_rejects_overlap_and_invalid_percentage(self):
        contract = parse_contract(ROOT / "data/contracts/hospital_1", "H1")
        service = contract["services"][0]
        service["rates"].append({
            "effective_from": "2025-01-01", "effective_to": "2025-12-31",
            "rate_cents": service["rates"][0]["rate_cents"],
        })
        contract["rules"]["weekend_uplifts"][0]["basis_points"] = 12000
        warnings = validate_contract(contract)
        self.assertTrue(any("overlapping rate amendments" in warning for warning in warnings))
        self.assertTrue(any("invalid basis_points" in warning for warning in warnings))

    def test_llm_contract_agent_caches_grounded_schema(self):
        class FakeClient:
            provider = "test"
            model = "fake-model"
            temperature = 0

            def generate(self, messages, response_schema):
                return {
                    "metadata": {"contract_number": "C-1", "effective_from": "2024-01-01", "effective_to": "2025-12-31", "currency": "GBP", "rounding": "half_up_cent"},
                    "services": [{
                        "service_id": "consultation", "name": "Consultation", "aliases": [],
                        "unit": "per_visit", "daily_cap": None,
                        "rates": [{"effective_from": "2024-01-01", "effective_to": "2025-12-31", "rate_cents": 10000,
                                   "evidence": {"source_file": "contract.md", "section": "Rates", "text": "Consultation | per visit | GBP 100.00"}}],
                    }],
                    "rules": {key: [] for key in ("threshold_premiums", "weekend_uplifts", "volume_discounts", "bundles", "exclusions", "facility_multipliers", "plan_multipliers")},
                    "calculation_order": ["bundle", "facility_multiplier", "plan_multiplier", "premium", "volume_discount"],
                    "warnings": [],
                }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract_dir = root / "contracts/hospital_1"
            contract_dir.mkdir(parents=True)
            (contract_dir / "contract.md").write_text("# Contract\n\n## Rates\n\nConsultation | per visit | GBP 100.00\n", encoding="utf-8")
            prompt = root / "prompt.md"
            prompt.write_text("Extract the contract.", encoding="utf-8")
            agent = ContractUnderstandingAgent(root, root / "cache", prompt, FakeClient())
            result = agent.parse(contract_dir, "H1")
            self.assertEqual(result["extraction"]["method"], "llm")
            self.assertEqual(result["services"][0]["rates"][0]["rate_cents"], 10000)
            self.assertTrue((root / "cache/hospital_1.json").exists())
            chunk_dirs = list((root / "artifacts/chunks/hospital_1").glob("*/manifest.json"))
            self.assertEqual(len(chunk_dirs), 1)

    def test_llm_contract_agent_repairs_only_failed_chunk(self):
        class RepairingClient:
            provider = "test"
            model = "repairing-model"
            temperature = 0

            def __init__(self):
                self.calls = []

            def generate(self, messages, response_schema):
                source = messages[-1]["content"]
                is_second = "| Service 2 |" in source
                self.calls.append("service_2" if is_second else "service_1")
                name = "Service 2" if is_second else "Service 1"
                rate = 20000 if is_second else 10000
                evidence = f"| {name} | GBP {rate // 100}.00 |"
                if is_second and self.calls.count("service_2") == 1:
                    evidence = "Service 2 costs GBP 200.00"  # deliberately paraphrased
                return {
                    "metadata": {},
                    "services": [{
                        "service_id": name.lower().replace(" ", "_"), "name": name,
                        "aliases": [], "unit": "per_visit", "daily_cap": None,
                        "rates": [{"effective_from": None, "effective_to": None,
                                   "rate_cents": rate,
                                   "evidence": {"source_file": "contract.md", "section": "Rates", "text": evidence}}],
                    }],
                    "rules": {key: [] for key in (
                        "threshold_premiums", "weekend_uplifts", "volume_discounts",
                        "bundles", "exclusions", "facility_multipliers", "plan_multipliers")},
                    "calculation_order": [], "warnings": [],
                }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract_dir = root / "contracts/hospital_1"
            contract_dir.mkdir(parents=True)
            (contract_dir / "contract.md").write_text(
                "# Contract\n\n## Rates\n\n| Service | Rate |\n|---|---|\n"
                "| Service 1 | GBP 100.00 |\n| Service 2 | GBP 200.00 |\n", encoding="utf-8")
            prompts = root / "config/prompts"
            prompts.mkdir(parents=True)
            prompt = prompts / "contract_extraction_v5.md"
            prompt.write_text("Extract the contract.", encoding="utf-8")
            (prompts / "contract_repair_v2.md").write_text("Repair only this chunk verbatim.", encoding="utf-8")
            client = RepairingClient()
            agent = ContractUnderstandingAgent(
                root, root / "cache", prompt, client,
                max_table_rows=1,
            )
            result = agent.parse(contract_dir, "H1")
            self.assertEqual(client.calls, ["service_1", "service_2", "service_2"])
            self.assertEqual(result["extraction"]["validation_attempts"], 2)
            self.assertEqual(len(result["extraction"]["repaired_chunks"]), 1)
            response_files = list((root / "artifacts/llm_runs").glob("**/*.response.json"))
            self.assertEqual(len(response_files), 4)  # two initial, repair delta, persisted merge
            resumed = agent.parse(contract_dir, "H1")
            self.assertEqual(client.calls, ["service_1", "service_2", "service_2"])
            self.assertEqual(resumed["extraction"]["method"], "validated_cache")
            self.assertEqual(len(resumed["extraction"]["repaired_chunks"]), 1)

    def test_completed_targeted_repair_persists_merged_fragment_for_resume(self):
        class Client:
            provider = "test"
            model = "resume-model"
            temperature = 0

            def __init__(self):
                self.calls = 0

            def generate(self, messages, response_schema):
                self.calls += 1
                include = "VALIDATION FAILURES" in messages[0]["content"]
                services = []
                if include:
                    services = [{
                        "service_id": "service_1", "name": "Service 1", "aliases": [],
                        "unit": "per_visit", "daily_cap": None,
                        "rates": [{"effective_from": None, "effective_to": None,
                                   "rate_cents": 10000,
                                   "evidence": {"source_file": "contract.md", "section": "Rates",
                                                "text": "In respect of Service 1, the Provider shall invoice the Payer at the rate of GBP 100.00 per visit."}}],
                    }]
                return {"metadata": {}, "services": services,
                        "rules": {kind: [] for kind in RULE_KINDS},
                        "calculation_order": [], "warnings": []}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract_dir = root / "contracts/hospital_1"
            contract_dir.mkdir(parents=True)
            (contract_dir / "contract.md").write_text(
                "# Rates\n\nIn respect of Service 1, the Provider shall invoice the Payer at the rate of GBP 100.00 per visit.\n",
                encoding="utf-8")
            prompts = root / "config/prompts"
            prompts.mkdir(parents=True)
            prompt = prompts / "contract_extraction_v5.md"
            prompt.write_text("Extract.", encoding="utf-8")
            (prompts / "contract_repair_v2.md").write_text("Repair.", encoding="utf-8")
            client = Client()
            agent = ContractUnderstandingAgent(root, root / "cache", prompt, client)
            agent.parse(contract_dir, "H1")
            client.calls = 0
            result = agent.parse(contract_dir, "H1")
            self.assertEqual(client.calls, 0)
            self.assertEqual(result["extraction"]["method"], "validated_cache")
            self.assertEqual(len(result["extraction"]["repaired_chunks"]), 1)

    def test_contract_agent_has_no_runtime_fallback(self):
        class FailingClient:
            provider = "test"
            model = "failing-model"
            recommended_max_chunk_chars = 12_000

            def generate(self, messages, response_schema):
                raise LLMError("provider unavailable")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract_dir = root / "contracts/hospital_1"
            contract_dir.mkdir(parents=True)
            (contract_dir / "contract.md").write_text(
                "# Contract\n\n## Rates\n\nService A is GBP 100.00 per visit.\n",
                encoding="utf-8",
            )
            prompts = root / "config/prompts"
            prompts.mkdir(parents=True)
            prompt = prompts / "contract_extraction_v5.md"
            prompt.write_text("Extract.", encoding="utf-8")
            agent = ContractUnderstandingAgent(root, root / "cache", prompt, FailingClient())
            with self.assertRaisesRegex(LLMError, "provider unavailable"):
                agent.parse(contract_dir, "H1")


if __name__ == "__main__":
    unittest.main()
