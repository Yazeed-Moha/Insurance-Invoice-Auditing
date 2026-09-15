"""Parse heterogeneous Markdown contracts into a consistent JSON schema.

The extractor is deterministic by default. An LLM can produce the same schema using
the versioned prompt, but its output must pass ``validate_contract`` before use.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Iterable

from .schema import decimal_to_ratio, money_to_cents, normalise_unit, percent_to_basis_points, validate_contract


DATE_FORMAT = "%d %B %Y"
NUMBER_WORDS = {
    "four": 4, "six": 6, "seven": 7, "eight": 8, "ten": 10, "twelve": 12,
    "fourteen": 14, "sixteen": 16, "twenty": 20, "twenty-four": 24,
    "thirty": 30, "sixty": 60, "eighty": 80, "one hundred": 100,
    "one hundred and twenty": 120, "one hundred and eighty": 180,
    "two hundred and forty": 240, "three hundred": 300,
}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _number(text: str) -> int:
    paren = re.search(r"\((\d+)\)", text)
    if paren:
        return int(paren.group(1))
    digit = re.search(r"\d+", text.replace(",", ""))
    if digit:
        return int(digit.group())
    lower = text.lower().strip()
    for word, value in sorted(NUMBER_WORDS.items(), key=lambda item: -len(item[0])):
        if word in lower:
            return value
    raise ValueError(f"cannot parse number: {text}")


def _metadata(texts: Iterable[str], hospital_id: str) -> dict[str, Any]:
    joined = "\n".join(texts)
    def field(label: str) -> str | None:
        match = re.search(rf"\*\*{label}:\*\*\s*([^\n]+)", joined, re.I)
        return match.group(1).strip() if match else None
    def iso(label: str) -> str | None:
        value = field(label)
        if not value:
            return None
        return datetime.strptime(value, DATE_FORMAT).date().isoformat()
    return {
        "contract_number": field("Contract number"),
        "provider": field("Provider"),
        "payer": field("Payer"),
        "effective_from": iso("Effective from"),
        "effective_to": iso("Effective to"),
        "currency": field("Currency") or "GBP",
        "rounding": "half_up_cent",
        "hospital_id": hospital_id,
    }


def _tables(text: str) -> list[tuple[str, list[str], list[list[str]]]]:
    section = ""
    lines = text.splitlines()
    output: list[tuple[str, list[str], list[list[str]]]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#"):
            section = line.lstrip("# ").strip()
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|$", lines[i + 1]):
            header = [x.strip() for x in line.strip().strip("|").split("|")]
            rows: list[list[str]] = []
            i += 2
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([x.strip() for x in lines[i].strip().strip("|").split("|")])
                i += 1
            output.append((section, header, rows))
            continue
        i += 1
    return output


def _blank_rules() -> dict[str, list[dict[str, Any]]]:
    return {key: [] for key in (
        "threshold_premiums", "weekend_uplifts", "volume_discounts", "daily_caps", "bundles",
        "exclusions", "facility_multipliers", "plan_multipliers"
    )}


def _add_daily_cap(rules: dict[str, list[dict[str, Any]]], service: str, limit: int) -> None:
    if not any(rule["service"] == service and rule["limit"] == limit for rule in rules["daily_caps"]):
        rules["daily_caps"].append({"service": service, "limit": limit})


def _service(services: dict[str, dict[str, Any]], name: str, unit: str, rate: int,
             start: str | None, end: str | None, cap: int | None = None) -> None:
    item = services.setdefault(name, {
        "service_id": _slug(name), "name": name, "aliases": [], "unit": unit,
        "rates": [], "daily_cap": cap,
    })
    item["unit"] = unit
    if cap is not None:
        item["daily_cap"] = cap
    version = {"effective_from": start, "effective_to": end, "rate_cents": rate}
    if version not in item["rates"]:
        item["rates"].append(version)


def _parse_table_contract(paths: list[Path], hospital_id: str) -> dict[str, Any]:
    texts = [p.read_text(encoding="utf-8") for p in paths]
    meta = _metadata(texts, hospital_id)
    services: dict[str, dict[str, Any]] = {}
    rules = _blank_rules()
    for path, text in zip(paths, texts):
        for section, header, rows in _tables(text):
            h = [x.lower() for x in header]
            for row in rows:
                if len(row) != len(header):
                    continue
                record = dict(zip(h, row))
                if "unit basis" in h and ("rate" in h or "base rate" in h):
                    name = record["service"]
                    rate = money_to_cents(record.get("rate") or record["base rate"])
                    cap_raw = record.get("daily cap", "—")
                    cap = None if cap_raw in {"—", "-", ""} else _number(cap_raw)
                    _service(services, name, normalise_unit(record["unit basis"]), rate,
                             meta["effective_from"], meta["effective_to"], cap)
                    if cap is not None:
                        _add_daily_cap(rules, name, cap)
                elif "rate to 31 december 2024" in h:
                    name = record["service"]
                    unit = normalise_unit(record["unit basis"])
                    _service(services, name, unit, money_to_cents(record["rate to 31 december 2024"]),
                             meta["effective_from"], "2024-12-31")
                    _service(services, name, unit, money_to_cents(record["rate from 1 january 2025"]),
                             "2025-01-01", meta["effective_to"])
                elif "service a" in h and "service b" in h:
                    a, b = record["service a"], record["service b"]
                    ra = record.get("bundled rate a") or record.get("substituted rate a")
                    rb = record.get("bundled rate b") or record.get("substituted rate b")
                    rules["bundles"].append({"service_a": a, "service_b": b,
                                               "rate_a_cents": money_to_cents(ra), "rate_b_cents": money_to_cents(rb)})
                elif "service" in h and any("f-main" == x for x in h):
                    for facility in ("f-main", "f-north", "f-coast"):
                        num, den = decimal_to_ratio(record[facility])
                        rules["facility_multipliers"].append({"service": record["service"], "facility": facility.upper(), "numerator": num, "denominator": den})
                elif "service" in h and all(x in h for x in ("bronze", "silver", "gold")):
                    for tier in ("bronze", "silver", "gold"):
                        num, den = decimal_to_ratio(record[tier])
                        rules["plan_multipliers"].append({"service": record["service"], "plan_tier": tier.upper(), "numerator": num, "denominator": den})
                elif "service" in h and any("not billable" in x or "excluded by" in x for x in h):
                    window_key = next(x for x in h if "not billable" in x or x == "window")
                    other_key = next(x for x in h if "of this service" in x or "excluded by" in x)
                    rules["exclusions"].append({"service_a": record["service"], "service_b": record[other_key], "window_days": _number(record[window_key])})
                elif "service" in h and any("maximum" in x for x in h):
                    key = next(x for x in h if "maximum" in x)
                    if record["service"] in services:
                        limit = _number(record[key])
                        services[record["service"]]["daily_cap"] = limit
                        _add_daily_cap(rules, record["service"], limit)
                elif "service" in h and any("cumulative utilisation" in x for x in h):
                    threshold_key = next(x for x in h if "cumulative utilisation" in x)
                    discount_key = next(x for x in h if "discount" in x)
                    rules["volume_discounts"].append({"service": record["service"], "threshold": _number(record[threshold_key]), "basis_points": percent_to_basis_points(record[discount_key])})
                elif "service" in h and any("threshold" in x or "daily quantity exceeds" in x for x in h):
                    threshold_key = next(x for x in h if "threshold" in x or "daily quantity exceeds" in x)
                    uplift_key = next(x for x in h if "uplift" in x or "premium" in x)
                    rules["threshold_premiums"].append({"service": record["service"], "threshold": _number(record[threshold_key]), "basis_points": percent_to_basis_points(record[uplift_key])})
                elif "service" in h and any("uplift" in x for x in h):
                    uplift_key = next(x for x in h if "uplift" in x)
                    rules["weekend_uplifts"].append({"service": record["service"], "basis_points": percent_to_basis_points(record[uplift_key])})
    # Added services in Hospital 3 only apply from the amendment date.
    if hospital_id == "H3":
        amendment_text = next((t for p, t in zip(paths, texts) if "amendment" in p.name), "")
        for section, header, rows in _tables(amendment_text):
            if section.startswith("A1.2"):
                for row in rows:
                    name = row[0]
                    services[name]["rates"] = [
                        rate for rate in services[name]["rates"]
                        if not (rate["effective_from"] == meta["effective_from"]
                                and rate["effective_to"] == meta["effective_to"])
                    ]
            if section.startswith("A1.3"):
                for row in rows:
                    name, unit, rate = row
                    services.pop(name, None)
                    _service(services, name, normalise_unit(unit), money_to_cents(rate), "2025-01-01", meta["effective_to"])
    result = {
        "schema_version": "2.0", "hospital_id": hospital_id, "metadata": meta,
        "services": sorted(services.values(), key=lambda x: x["name"]), "rules": rules,
        "calculation_order": ["bundle", "facility_multiplier", "plan_multiplier", "premium", "volume_discount"],
        "extraction": {"method": "deterministic_markdown_tables", "source_files": [str(p) for p in paths], "confidence": 0.99, "warnings": []},
    }
    result["extraction"]["warnings"] = validate_contract(result)
    return result


def _parse_prose_contract(path: Path, hospital_id: str) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    meta = _metadata([text], hospital_id)
    services: dict[str, dict[str, Any]] = {}
    rules = _blank_rules()
    clauses = re.findall(r"\n(?=\d+\.\d+ In respect of )", text)
    chunks = re.split(r"\n(?=\d+\.\d+ In respect of )", text)
    for chunk in chunks:
        m = re.match(r"\d+\.\d+ In respect of (.*?), the Provider shall invoice the Payer at the rate of (GBP [\d,.]+) (per [^.]+)\.", chunk)
        if not m:
            continue
        name, rate_raw, unit_raw = m.groups()
        unit_raw = unit_raw.strip()
        _service(services, name, normalise_unit(unit_raw), money_to_cents(rate_raw), meta["effective_from"], meta["effective_to"])
        cap = re.search(r"(?:may not exceed|maximum of|No more than|shall not bill more than)[^.]*(\(\d+\)|\d+)[^.]*(?:per Patient per Service Day|on a single Service Day)", chunk, re.I)
        if cap:
            limit = _number(cap.group(0))
            services[name]["daily_cap"] = limit
            _add_daily_cap(rules, name, limit)
        for dm in re.finditer(r"cumulative utilisation.*?exceeds .*?\((\d+)\).*?discount of .*?\((\d+)%\)", chunk, re.I):
            rules["volume_discounts"].append({"service": name, "threshold": int(dm.group(1)), "basis_points": int(dm.group(2)) * 100})
        threshold = re.search(r"aggregate quantity.*?exceeds .*?\((\d+)\).*?(?:increased by|premium of|uplift of).*?\((\d+)%\)", chunk, re.I)
        if threshold:
            rules["threshold_premiums"].append({"service": name, "threshold": int(threshold.group(1)), "basis_points": int(threshold.group(2)) * 100})
        weekend = re.search(r"(?:not fall on a Business Day|Saturday or Sunday).*?(?:increased by|premium of|uplift of).*?\((\d+)%\)", chunk, re.I)
        if weekend:
            rules["weekend_uplifts"].append({"service": name, "basis_points": int(weekend.group(1)) * 100})
        exclusion = re.search(r"This Service is not billable where (.*?) has been delivered.*?within .*?\((\d+)\) days", chunk, re.I)
        if exclusion:
            rules["exclusions"].append({"service_a": name, "service_b": exclusion.group(1), "window_days": int(exclusion.group(2))})
        bundle = re.search(r"Where this Service and (.*?) are both delivered.*?bundle, this Service at (GBP [\d,.]+) .*? and .*? at (GBP [\d,.]+) ", chunk, re.I)
        if bundle:
            rules["bundles"].append({"service_a": name, "service_b": bundle.group(1), "rate_a_cents": money_to_cents(bundle.group(2)), "rate_b_cents": money_to_cents(bundle.group(3))})
    result = {
        "schema_version": "2.0", "hospital_id": hospital_id, "metadata": meta,
        "services": sorted(services.values(), key=lambda x: x["name"]), "rules": rules,
        "calculation_order": ["bundle", "facility_multiplier", "plan_multiplier", "premium", "volume_discount"],
        "extraction": {"method": "deterministic_clause_regex", "source_files": [str(path)], "confidence": 0.92, "warnings": []},
    }
    result["extraction"]["warnings"] = validate_contract(result)
    return result


def parse_contract(contract_dir: Path, hospital_id: str) -> dict[str, Any]:
    paths = sorted(contract_dir.glob("*.md"))
    if not paths:
        raise FileNotFoundError(f"no Markdown contracts in {contract_dir}")
    if hospital_id == "H2":
        return _parse_prose_contract(paths[0], hospital_id)
    return _parse_table_contract(paths, hospital_id)


def write_contract(contract: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(contract, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
