"""Canonical contract schema helpers and validation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import re
from typing import Any


UNIT_MAP = {
    "per visit": "per_visit",
    "per procedure": "per_procedure",
    "per hour": "per_hour",
    "per day": "per_day",
    "per day of service": "per_day",
    "per item": "per_item",
    "per item supplied": "per_item",
    "per night": "per_night",
    "per night of occupancy": "per_night",
    "per test": "per_test",
    "per unit": "per_unit_dispensed",
    "per unit dispensed": "per_unit_dispensed",
    "per hour, per item": "per_hour_per_item",
}
CANONICAL_UNITS = set(UNIT_MAP.values())
CALCULATION_STEP_MAP = {
    "bundle": "bundle", "bundles": "bundle",
    "facility_multiplier": "facility_multiplier", "facility_multipliers": "facility_multiplier",
    "plan_multiplier": "plan_multiplier", "plan_multipliers": "plan_multiplier",
    "premium": "premium", "premiums": "premium", "threshold_premiums": "premium",
    "weekend_uplifts": "premium", "non_business_day_uplifts": "premium",
    "volume_discount": "volume_discount", "volume_discounts": "volume_discount",
}
RULE_KINDS = (
    "threshold_premiums", "weekend_uplifts", "volume_discounts", "daily_caps",
    "bundles", "exclusions", "facility_multipliers", "plan_multipliers",
)


def money_to_cents(value: str) -> int:
    cleaned = re.sub(r"[^0-9.]", "", value.replace(",", ""))
    return int((Decimal(cleaned) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def percent_to_basis_points(value: str) -> int:
    return int((Decimal(re.sub(r"[^0-9.]", "", value)) * 100).quantize(Decimal("1")))


def decimal_to_ratio(value: str) -> tuple[int, int]:
    d = Decimal(value.strip())
    return int(d * 10000), 10000


def normalise_unit(value: str) -> str:
    value = re.sub(r"\s+", " ", value.strip().lower())
    return UNIT_MAP.get(value, value.replace(" ", "_"))


def normalise_calculation_order(values: list[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        canonical = CALCULATION_STEP_MAP.get(str(value).strip().lower())
        if canonical and canonical not in output:
            output.append(canonical)
    return output


def parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def validate_contract(contract: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    required = {"schema_version", "hospital_id", "metadata", "services", "rules", "calculation_order"}
    missing = required - set(contract)
    if missing:
        warnings.append(f"missing top-level fields: {sorted(missing)}")
    metadata = contract.get("metadata", {})
    contract_start = parse_date(metadata.get("effective_from"))
    contract_end = parse_date(metadata.get("effective_to"))
    if metadata.get("effective_from") and contract_start is None:
        warnings.append("invalid contract effective_from date")
    if metadata.get("effective_to") and contract_end is None:
        warnings.append("invalid contract effective_to date")
    if contract_start and contract_end and contract_start > contract_end:
        warnings.append("contract effective_from is after effective_to")

    seen: set[str] = set()
    seen_ids: set[str] = set()
    for service in contract.get("services", []):
        name = service.get("name", "")
        if name in seen:
            warnings.append(f"duplicate service: {name}")
        seen.add(name)
        service_id = service.get("service_id", "")
        if not service_id:
            warnings.append(f"service has no service_id: {name}")
        elif service_id in seen_ids:
            warnings.append(f"duplicate service_id: {service_id}")
        seen_ids.add(service_id)
        if not service.get("rates"):
            warnings.append(f"service has no rate: {name}")
        if not service.get("unit"):
            warnings.append(f"service has no unit: {name}")
        elif service.get("unit") not in CANONICAL_UNITS:
            warnings.append(f"unsupported canonical unit for {name}: {service.get('unit')}")
        ranges: list[tuple[date, date, int]] = []
        for index, rate in enumerate(service.get("rates", [])):
            if not isinstance(rate.get("rate_cents"), int) or rate.get("rate_cents", 0) <= 0:
                warnings.append(f"invalid rate_cents for {name} rate {index}")
            start_raw, end_raw = rate.get("effective_from"), rate.get("effective_to")
            parsed_start = parse_date(start_raw) if start_raw else None
            parsed_end = parse_date(end_raw) if end_raw else None
            if start_raw and parsed_start is None:
                warnings.append(f"invalid effective_from for {name} rate {index}")
            if end_raw and parsed_end is None:
                warnings.append(f"invalid effective_to for {name} rate {index}")
            start = parsed_start or date.min
            end = parsed_end or date.max
            if start > end:
                warnings.append(f"rate effective_from is after effective_to for {name} rate {index}")
            ranges.append((start, end, index))
        for left in range(len(ranges)):
            for right in range(left + 1, len(ranges)):
                if max(ranges[left][0], ranges[right][0]) <= min(ranges[left][1], ranges[right][1]):
                    warnings.append(f"overlapping rate amendments for {name}: rates {ranges[left][2]} and {ranges[right][2]}")
    names = seen
    rules = contract.get("rules", {})
    for kind in RULE_KINDS:
        if kind not in rules:
            warnings.append(f"missing rule collection: {kind}")
    for kind in ("threshold_premiums", "weekend_uplifts", "volume_discounts", "daily_caps"):
        for rule in contract.get("rules", {}).get(kind, []):
            if rule.get("service") not in names:
                warnings.append(f"{kind} references unknown service: {rule.get('service')}")
    for kind in ("threshold_premiums", "volume_discounts"):
        for rule in rules.get(kind, []):
            if not isinstance(rule.get("threshold"), int) or rule.get("threshold", -1) < 0:
                warnings.append(f"invalid threshold in {kind} for {rule.get('service')}")
    for kind in ("threshold_premiums", "weekend_uplifts", "volume_discounts"):
        for rule in rules.get(kind, []):
            value = rule.get("basis_points")
            if not isinstance(value, int) or not 0 < value <= 10000:
                warnings.append(f"invalid basis_points in {kind} for {rule.get('service')}")
    cap_services: dict[str, int] = {}
    for rule in rules.get("daily_caps", []):
        service = rule.get("service")
        if service in cap_services and cap_services[service] != rule.get("limit"):
            warnings.append(f"conflicting daily caps for service: {service}")
        elif isinstance(rule.get("limit"), int):
            cap_services[service] = rule["limit"]
        if not isinstance(rule.get("limit"), int) or rule.get("limit", 0) <= 0:
            warnings.append(f"invalid daily cap for {service}")
    for kind in ("bundles", "exclusions"):
        for rule in contract.get("rules", {}).get(kind, []):
            for key in ("service_a", "service_b"):
                if rule.get(key) not in names:
                    warnings.append(f"{kind} references unknown service: {rule.get(key)}")
    for rule in rules.get("bundles", []):
        if any(not isinstance(rule.get(key), int) or rule.get(key, 0) <= 0
               for key in ("rate_a_cents", "rate_b_cents")):
            warnings.append(f"invalid bundled rates: {rule.get('service_a')} + {rule.get('service_b')}")
    for rule in rules.get("exclusions", []):
        if not isinstance(rule.get("window_days"), int) or rule.get("window_days", -1) < 0:
            warnings.append(f"invalid exclusion window: {rule.get('service_a')} + {rule.get('service_b')}")
    for kind in ("facility_multipliers", "plan_multipliers"):
        for rule in rules.get(kind, []):
            if rule.get("service") not in names:
                warnings.append(f"{kind} references unknown service: {rule.get('service')}")
            if (not isinstance(rule.get("numerator"), int) or rule.get("numerator", -1) < 0
                    or not isinstance(rule.get("denominator"), int) or rule.get("denominator", 0) <= 0):
                warnings.append(f"invalid multiplier in {kind} for {rule.get('service')}")
    order = contract.get("calculation_order", [])
    if not order:
        warnings.append("missing calculation order")
    elif normalise_calculation_order(order) != order:
        warnings.append(f"unsupported or duplicate calculation order values: {order}")
    return warnings
