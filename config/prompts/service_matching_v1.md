# Service matching prompt v1 (optional ambiguity resolver)

Map one free-text invoice description to at most one canonical service from the supplied contract-service shortlist. Use the description as primary evidence. Billing unit and billed price are weak supporting evidence because either can itself be erroneous.

Return JSON only:

```json
{
  "matched_service_id": "string or null",
  "confidence": 0.0,
  "runner_up_service_id": "string or null",
  "reason": "brief evidence",
  "uncertainty": "null, no_match, or ambiguous_match"
}
```

Abstain when the description omits a distinguishing specialty, care setting, or service concept. Never decide whether the line is correctly priced and never perform arithmetic.

