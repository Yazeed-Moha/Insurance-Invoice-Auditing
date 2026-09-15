# Decision Log

## Scope and sequencing

The runtime uses LLM extraction for all five supplied Markdown contracts, then validates behaviour on Hospital 1 before generating unseen-hospital predictions. The PDFs are retained as verification sources, while Markdown is the reproducible machine input because the repository supplies equivalent text.

## Decisions

- The LLM is restricted to contract understanding. It emits structured, evidence-backed facts; Python validates those facts and performs matching, arithmetic, and business-rule enforcement. A failed extraction stops the run rather than silently switching implementations.
- All money remains integer cents. Multipliers use rational integers and every adjustment uses decimal half-up rounding immediately after that step.
- A service match uses normalized tokens and a curated abbreviation vocabulary. Unit and price contribute only weak tie-breakers because they may be the error under investigation. Low-score matches abstain; low-margin matches stay visible as uncertain and lower invoice confidence.
- Cumulative utilisation is processed by Service Date then line identifier, across patients and invoices. The threshold is tested before adding the current line, following the contracts' “prior to” wording.
- Threshold premiums use aggregate patient/service/date quantity. A daily cap limits payable quantity. Later duplicate delivery lines and directional exclusion violations have zero payable quantity.
- Invalid contract numbers, dates, units, and invoice arithmetic are audit errors but do not automatically erase otherwise payable clinical service. Unknown or not-yet-effective services preserve the billed line amount and receive low confidence rather than inventing a price.
- If a threshold and weekend uplift both appear for one service, the baseline applies the deeper premium once. This follows the “not compounded within a single class” language explicit in Hospital 4 and is recorded as an assumption for the other contracts.

## Known ambiguity and limitations

- Hospital 2's intentionally repetitive prose increases semantic matching risk. Every model note and deterministic validation result is written to the generated contract artifact.
- The deterministic matcher cannot recover a specialty omitted entirely from a description. These cases are marked `ambiguous_service_match`; an optional LLM resolver could review only that small queue.
- Duplicate and cap allocation assumes the earliest line identifier is payable when the contract does not specify which duplicate to retain.
- Category labels are deliberately compositional. A line may carry both `unit_price_mismatch` and a more specific premium/discount diagnosis; this improves audit traceability but may differ from a single-label taxonomy.

## AI assistance disclosure

AI assistance was used to inspect the supplied repository, reason about the architecture, draft the contract agent/rule engine/tests, and document the approach. No Hospital 2–5 ground truth or external domain data was used. Runtime LLM outputs must pass the same schema, reference, coverage, and evidence checks before pricing.

## What I would do with another week

I would benchmark a smaller, cheaper contract-understanding model and add a deterministic fallback for availability. The fallback would run only after an LLM/API failure, would produce the same canonical schema, and would be held to the same source-coverage, evidence, reference, date, numeric, and amendment validation gates. Its lower-confidence provenance would be explicit in the artifact and observable in monitoring; it would never silently replace a failed model result.
