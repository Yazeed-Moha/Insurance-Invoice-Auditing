"""Deterministic invoice validation and pricing engine."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .matcher import ServiceMatcher
from .schema import parse_date


def _mul(value: int, numerator: int, denominator: int = 10000) -> int:
    return int((Decimal(value) * Decimal(numerator) / Decimal(denominator)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _rate_on(service: dict[str, Any], service_date: date | None) -> int | None:
    if service_date is None:
        return service["rates"][-1]["rate_cents"] if service.get("rates") else None
    for version in service.get("rates", []):
        start = parse_date(version.get("effective_from")) or date.min
        end = parse_date(version.get("effective_to")) or date.max
        if start <= service_date <= end:
            return version["rate_cents"]
    return None


def audit(contract: dict[str, Any], invoices: list[dict[str, str]], lines: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    matcher = ServiceMatcher(contract)
    service_by_name = {s["name"]: s for s in contract["services"]}
    invoice_by_id = {row["invoice_id"]: row for row in invoices}
    line_rows: list[dict[str, Any]] = []
    for source in lines:
        row: dict[str, Any] = dict(source)
        row["quantity"] = int(row["quantity"])
        row["unit_price_cents"] = int(row["unit_price_cents"])
        row["line_total_cents"] = int(row["line_total_cents"])
        row["parsed_date"] = parse_date(row["service_date"])
        match = matcher.match(row["description"], row["unit_basis_as_billed"], row["unit_price_cents"], row["service_date"])
        row["match"] = match.to_dict()
        row["service_name"] = match.service_name
        line_rows.append(row)

    # Context indexes use matched canonical services and are shared across invoices.
    daily_quantity: Counter[tuple[str, str, date | None]] = Counter()
    delivered: defaultdict[tuple[str, date | None], set[str]] = defaultdict(set)
    patient_events: defaultdict[str, list[tuple[date, str, str]]] = defaultdict(list)
    for row in line_rows:
        iid = row["invoice_id"]
        invoice = invoice_by_id.get(iid)
        if not invoice or not row["service_name"]:
            continue
        key = (invoice["patient_id"], row["service_name"], row["parsed_date"])
        daily_quantity[key] += row["quantity"]
        delivered[(invoice["patient_id"], row["parsed_date"])].add(row["service_name"])
        if row["parsed_date"]:
            patient_events[invoice["patient_id"]].append((row["parsed_date"], row["service_name"], row["line_id"]))

    threshold = defaultdict(list)
    weekend = {}
    discounts = defaultdict(list)
    bundles: dict[frozenset[str], dict[str, Any]] = {}
    exclusions = defaultdict(list)
    facilities = {}
    tiers = {}
    caps = {rule["service"]: rule["limit"] for rule in contract["rules"].get("daily_caps", [])}
    for rule in contract["rules"]["threshold_premiums"]: threshold[rule["service"]].append(rule)
    for rule in contract["rules"]["weekend_uplifts"]: weekend[rule["service"]] = rule
    for rule in contract["rules"]["volume_discounts"]: discounts[rule["service"]].append(rule)
    for rule in contract["rules"]["bundles"]: bundles[frozenset((rule["service_a"], rule["service_b"]))] = rule
    for rule in contract["rules"]["exclusions"]: exclusions[rule["service_a"]].append(rule)
    for rule in contract["rules"]["facility_multipliers"]: facilities[(rule["service"], rule["facility"])] = rule
    for rule in contract["rules"]["plan_multipliers"]: tiers[(rule["service"], rule["plan_tier"])] = rule

    cumulative: Counter[str] = Counter()
    seen_delivery: set[tuple[str, str, date | None]] = set()
    line_results: dict[str, dict[str, Any]] = {}
    sort_key = lambda row: (row["parsed_date"] or date.max, row["line_id"])
    for row in sorted(line_rows, key=sort_key):
        invoice = invoice_by_id.get(row["invoice_id"], {})
        errors: set[str] = set()
        notes: list[str] = []
        match = row["match"]
        if match["service_name"] is None:
            errors.add("unknown_service")
            expected = row["line_total_cents"]
            line_results[row["line_id"]] = {**row, "expected_line_total_cents": expected, "errors": sorted(errors), "notes": notes}
            continue
        name = match["service_name"]
        service = service_by_name[name]
        d = row["parsed_date"]
        if d is None:
            errors.add("malformed_service_date")
        else:
            start = parse_date(contract["metadata"].get("effective_from"))
            end = parse_date(contract["metadata"].get("effective_to"))
            if (start and d < start) or (end and d > end): errors.add("service_date_out_of_window")
            invoice_date = parse_date(invoice.get("invoice_date"))
            if invoice_date and d > invoice_date: errors.add("service_date_after_invoice_date")
        if row["unit_basis_as_billed"] != service["unit"]:
            errors.add("wrong_unit_basis")
        if row["line_total_cents"] != row["unit_price_cents"] * row["quantity"]:
            errors.add("line_total_arithmetic")

        base = _rate_on(service, d)
        if base is None:
            errors.add("service_not_effective")
            base = row["unit_price_cents"]
        patient = invoice.get("patient_id", "")
        delivered_names = delivered[(patient, d)]
        bundle_applied = False
        for pair, bundle in bundles.items():
            if name in pair and pair.issubset(delivered_names):
                base = bundle["rate_a_cents"] if name == bundle["service_a"] else bundle["rate_b_cents"]
                bundle_applied = True
                notes.append("bundle_rate")
                break
        facility_rule = facilities.get((name, invoice.get("facility_code")))
        if facility_rule:
            base = _mul(base, facility_rule["numerator"], facility_rule["denominator"])
        tier_rule = tiers.get((name, invoice.get("plan_tier")))
        if tier_rule:
            base = _mul(base, tier_rule["numerator"], tier_rule["denominator"])
        quantity_key = (patient, name, d)
        premium_bps = 0
        for rule in threshold[name]:
            if daily_quantity[quantity_key] > rule["threshold"]:
                premium_bps = max(premium_bps, rule["basis_points"])
        if d and d.weekday() >= 5 and name in weekend:
            premium_bps = max(premium_bps, weekend[name]["basis_points"])
        if premium_bps:
            base = _mul(base, 10000 + premium_bps)
            notes.append(f"premium_{premium_bps}bp")
        discount_bps = max((r["basis_points"] for r in discounts[name] if cumulative[name] > r["threshold"]), default=0)
        if discount_bps:
            base = _mul(base, 10000 - discount_bps)
            notes.append(f"discount_{discount_bps}bp")

        payable_quantity = row["quantity"]
        cap = caps.get(name, service.get("daily_cap"))
        if cap is not None and daily_quantity[quantity_key] > cap:
            errors.add("daily_cap_exceeded")
            # Allocate the remaining cap chronologically, so excess units are not payable.
            previously_allocated = sum(
                min(x["quantity"], cap) for x in line_rows
                if x["line_id"] < row["line_id"] and invoice_by_id.get(x["invoice_id"], {}).get("patient_id") == patient
                and x["service_name"] == name and x["parsed_date"] == d
            )
            payable_quantity = max(0, min(row["quantity"], cap - previously_allocated))
        duplicate_key = (patient, name, d)
        if duplicate_key in seen_delivery:
            errors.add("cross_invoice_duplicate" if any(
                x["invoice_id"] != row["invoice_id"] and invoice_by_id.get(x["invoice_id"], {}).get("patient_id") == patient
                and x["service_name"] == name and x["parsed_date"] == d and x["line_id"] < row["line_id"] for x in line_rows
            ) else "duplicate_service")
            payable_quantity = 0
        seen_delivery.add(duplicate_key)
        for rule in exclusions[name]:
            for other_date, other_name, other_id in patient_events[patient]:
                if other_name == rule["service_b"] and other_id != row["line_id"] and d and abs((d - other_date).days) <= rule["window_days"]:
                    errors.add("exclusion_window_violation")
                    payable_quantity = 0
                    break
        expected = base * payable_quantity
        if row["unit_price_cents"] != base:
            errors.add("unit_price_mismatch")
            if bundle_applied and row["unit_price_cents"] == _rate_on(service, d):
                errors.add("bundle_not_applied")
            # More specific adjustment diagnosis when it is directly inferable.
            raw_rate = _rate_on(service, d)
            if premium_bps and row["unit_price_cents"] == raw_rate: errors.add("premium_omitted")
            if not premium_bps and raw_rate and row["unit_price_cents"] != raw_rate: errors.add("premium_incorrectly_applied")
            if discount_bps and row["unit_price_cents"] != base: errors.add("volume_discount_omitted")
            if not discount_bps and raw_rate and row["unit_price_cents"] < raw_rate: errors.add("volume_discount_incorrectly_applied")
        cumulative[name] += row["quantity"]
        line_results[row["line_id"]] = {**row, "expected_unit_rate_cents": base, "expected_line_total_cents": expected, "errors": sorted(errors), "notes": notes}

    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in line_results.values(): grouped[row["invoice_id"]].append(row)
    id_counts = Counter(row["invoice_id"] for row in invoices)
    predictions: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    emitted: set[str] = set()
    for invoice in invoices:
        iid = invoice["invoice_id"]
        if iid in emitted:
            continue
        emitted.add(iid)
        invoice = invoice_by_id[iid]
        rows = sorted(grouped[iid], key=lambda x: int(x["line_no"]))
        if id_counts[iid] > 1:
            # Duplicate IDs concatenate multiple invoices' lines in the CSV. The
            # labelled/audited occurrence is the later one, identifiable by the
            # final line-number restart.
            starts = [index for index, row in enumerate(rows) if int(row["line_no"]) == 1]
            if len(starts) > 1:
                rows = rows[starts[-1]:]
        expected = sum(x["expected_line_total_cents"] for x in rows)
        billed = int(invoice["invoice_total_cents"])
        errors = {error for row in rows for error in row["errors"]}
        if invoice.get("contract_number") != contract["metadata"].get("contract_number"): errors.add("contract_number_mismatch")
        if id_counts[iid] > 1: errors.add("duplicate_invoice_id")
        billed_line_sum = sum(x["line_total_cents"] for x in rows)
        if billed != billed_line_sum: errors.add("invoice_total_mismatch")
        uncertainties = [x["match"]["uncertainty"] for x in rows if x["match"]["uncertainty"]]
        mean_match = sum(x["match"]["confidence"] for x in rows) / len(rows) if rows else 0.0
        confidence = min(0.99, max(0.05, mean_match * (0.72 if uncertainties else 1.0)))
        # Avoid confidently claiming correctness when semantic coverage is uncertain.
        flagged = int(bool(errors))
        predictions.append({
            "invoice_id": iid, "flagged": flagged, "error_category": "|".join(sorted(errors)),
            "expected_total_cents": expected, "billed_total_cents": billed, "confidence": f"{confidence:.4f}",
        })
        details.append({"invoice_id": iid, "errors": sorted(errors), "uncertainties": uncertainties, "lines": rows})
    return predictions, details
