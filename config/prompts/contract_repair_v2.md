# Targeted contract repair prompt v2

This is a targeted missing-facts extraction for one previously processed chunk. Return only the services, rates, limits, discounts, premiums, bundles, exclusions, multipliers, or other supported facts named in the validation failures below. Leave unrelated arrays empty. The system will deterministically merge this response into the existing validated fragment; do not re-extract unrelated parts of the chunk.

For evidence, copy a short continuous substring exactly as it appears in this chunk. Do not paraphrase. Check that every evidence quote can be found verbatim in the provided chunk before returning the JSON object.

An `uncovered financial source unit` is a row or clause that was silently omitted. Extract each listed enforceable fact into the correct schema object. Do not merely add a warning.
