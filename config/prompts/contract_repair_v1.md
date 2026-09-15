# Targeted contract repair prompt v1

This is a targeted retry of one failed chunk, not a complete-contract retry. Re-extract this chunk and correct the validation failures listed below.

For evidence, copy a short continuous substring exactly as it appears in this chunk. For a Markdown table fact, copy the entire relevant table row including its `|` characters. Do not paraphrase or generate a normalized quotation. Check that every evidence quote can be found verbatim in the provided chunk before returning the JSON object.

An `uncovered financial source unit` means a source row or clause was silently omitted. Extract its service, rate, quantity limit, discount, premium, bundle, exclusion, multiplier, or other supported rule. Do not merely add a warning for an enforceable financial fact.
