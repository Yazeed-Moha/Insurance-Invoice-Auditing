# File Guide

## Application package

- `audit_pipeline/cli.py` — command-line entry point and complete hospital-processing workflow.
- `audit_pipeline/contract_agent.py` — strict contract-agent orchestration: hashing, model-aware caching, chunking, resumable LLM extraction, per-chunk grounding, targeted repair, and response logging.
- `audit_pipeline/chunking.py` — general Markdown-aware chunking; preserves headings, splits prose at sentence boundaries, and splits tables only between rows while repeating their headers.
- `audit_pipeline/coverage.py` — identifies financial table rows and prose clauses and proves that each one produced an evidence-backed fact.
- `audit_pipeline/llm.py` — provider-neutral structured-LLM interface, the default GLM-5.3 JSON-mode client with local schema validation, plus optional Groq and Ollama clients.
- `audit_pipeline/contract_parser.py` — deterministic reference parser retained for tests, diagnostics, and future fallback experiments; it is not used by the runtime CLI.
- `audit_pipeline/schema.py` — canonical units/calculation steps plus date, range, amendment, numeric, reference, and business validation.
- `audit_pipeline/matcher.py` — maps abbreviated invoice descriptions to canonical services and reports confidence, runner-up, margin, and uncertainty.
- `audit_pipeline/engine.py` — deterministic pricing and business-rule enforcement at line, invoice, cross-line, and historical levels.
- `audit_pipeline/evaluation.py` — compares Hospital 1 predictions with labels and calculates classification, total, category, and calibration metrics.
- `audit_pipeline/io.py` — CSV and JSON input/output helpers and the required submission columns.
- `audit_pipeline/__init__.py` — package identity and version.

## Contracts, prompts, and schemas

- `config/schemas/contract.schema.json` — stable interchange format between contract understanding and deterministic validation.
- `config/prompts/contract_extraction_v5.md` — active completeness-aware extraction prompt using canonical units and evidence-backed daily caps.
- `config/prompts/contract_repair_v2.md` — targeted missing-facts prompt used only for chunks that fail evidence validation; its delta is merged deterministically.
- `config/prompts/contract_extraction_v1.md`, `v2.md`, and `v3.md` — retained prompt history showing iteration.
- `config/prompts/service_matching_v1.md` — optional LLM ambiguity-resolver contract; deterministic matching remains the first stage.

## Reproducibility and documentation

- `tests/test_pipeline.py` — rounding, parsing, amendments, matching, grounding, and LLM-workflow tests.
- `pyproject.toml` — Python package metadata and CLI installation entry point.
- `README.md` — setup, commands, outputs, and assessment instructions.
- `DECISION_LOG.md` — assumptions, ambiguity decisions, limitations, and AI-assistance disclosure.
- `EVALUATION_REPORT.md` — current Hospital 1 development-set results.
- `submission.csv` — current Hospitals 2–5 prediction deliverable.

## Supplied assessment data

- `data/contracts/` — source contract documents for the five hospitals.
- `data/invoices/` — invoice and line-item datasets.
- `data/labels/hospital_1_labels.csv` — development labels used only for evaluation.
- `data/submission_template.csv` — required prediction format.
