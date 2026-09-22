# Hospital 1 Evaluation

Evaluated 913 labelled invoices. Classification accuracy: **100.0%**; precision: **100.0%**; recall: **100.0%**; F1: **100.0%**. Exact expected-total accuracy is **98.7% overall (901/913)** and **79.3% among erroneous invoices (46/58)**.

## Confusion matrix

| TP | FP | TN | FN |
|---:|---:|---:|---:|
| 58 | 0 | 855 | 0 |

## Per-category performance

| Category | TP | FP | FN | Precision | Recall |
|---|---:|---:|---:|---:|---:|
| bundle_not_applied | 5 | 0 | 0 | 100.0% | 100.0% |
| contract_number_mismatch | 5 | 0 | 0 | 100.0% | 100.0% |
| cross_invoice_duplicate | 4 | 0 | 0 | 100.0% | 100.0% |
| daily_cap_exceeded | 4 | 0 | 0 | 100.0% | 100.0% |
| duplicate_invoice_id | 5 | 0 | 0 | 100.0% | 100.0% |
| exclusion_window_violation | 4 | 0 | 0 | 100.0% | 100.0% |
| invoice_total_mismatch | 6 | 5 | 0 | 54.5% | 100.0% |
| line_total_arithmetic | 6 | 0 | 0 | 100.0% | 100.0% |
| malformed_service_date | 6 | 0 | 0 | 100.0% | 100.0% |
| premium_incorrectly_applied | 6 | 10 | 0 | 37.5% | 100.0% |
| premium_omitted | 3 | 0 | 0 | 100.0% | 100.0% |
| service_date_after_invoice_date | 5 | 2 | 0 | 71.4% | 100.0% |
| service_date_out_of_window | 5 | 0 | 0 | 100.0% | 100.0% |
| unit_price_mismatch | 10 | 18 | 0 | 35.7% | 100.0% |
| unknown_service | 9 | 0 | 3 | 100.0% | 75.0% |
| volume_discount_incorrectly_applied | 4 | 4 | 0 | 50.0% | 100.0% |
| volume_discount_omitted | 4 | 1 | 0 | 80.0% | 100.0% |
| wrong_unit_basis | 11 | 3 | 0 | 78.6% | 100.0% |

## Systematic failure modes

1. **Semantic collision:** highly abbreviated descriptions can omit the specialty or care setting. Weak candidates now abstain as `unknown_service`; stronger but close candidates remain explicitly uncertain with reduced confidence.
2. **Category overlap:** one monetary discrepancy can support both a generic `unit_price_mismatch` and a specific adjustment category. The detailed audit preserves both for review.
3. **Invalid-date pricing:** malformed dates cannot select a dated amendment or weekend rule. The engine preserves the billed amount for an unpriceable service and flags the date.
4. **Ordering sensitivity:** cumulative discounts and duplicate allocation depend on Service Date then line identifier, as required by the contracts; source rows with duplicate identifiers remain review-sensitive.
