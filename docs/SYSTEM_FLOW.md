# System Flow

## Inputs

The assessment uses repository files as its input interface:

```text
data/contracts/hospital_N/*.md
data/invoices/hospital_N_invoices.csv
data/invoices/hospital_N_line_items.csv
```

No frontend is required. A future upload API can replace the filesystem loader without changing the contract, matching, or pricing components.

## Contract processing

1. Read every Markdown document belonging to a hospital.
2. Hash the source files to identify the exact contract version.
3. Load a validated LLM extraction only when source hash, provider, model, prompt, and schema versions match.
4. Otherwise split the documents by headings and complete sections.
5. Ask GLM-5.3 (default), Groq, or Ollama for JSON rule fragments at temperature zero; Python validates each result against the same strict schema.
6. Normalize units and calculation-step names into canonical values.
7. Prove that every financial table row or enforceable prose clause produced an evidence-backed fact.
8. Merge fragments deterministically and validate schema, references, dates, numeric ranges, amendments, calculation order, and exact source evidence.
9. Retry only incomplete or invalid chunks with precise validation feedback.
10. Cache only a complete schema-v2 contract. If LLM extraction or deterministic validation fails, stop the run visibly.

## Invoice processing

1. Load invoices and join their line items by `invoice_id`.
2. Match each free-text description to a canonical contracted service.
3. Select the service rate effective on the Service Date.
4. Apply bundle, facility, plan, premium, and discount steps in contractual order.
5. Apply payable-quantity limits and calculate each expected line total using integer cents.
6. Check duplicates, exclusions, cumulative thresholds, dates, identifiers, units, and arithmetic across all relevant invoices.
7. Sum expected lines, compare with the billed invoice total, and emit the prediction plus detailed evidence and uncertainty.

## Outputs

```text
artifacts/contract_cache/hospital_N.json validated reusable LLM extraction
artifacts/contracts/hospital_N.json  contract used by the run
artifacts/audit_details/hospital_N.json
artifacts/hospital_N_predictions.csv
artifacts/hospital_1_evaluation.md
artifacts/submission.csv
```
