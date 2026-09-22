"""Auditable service matching with confidence and explicit abstention."""

from __future__ import annotations

from dataclasses import dataclass, asdict
import re
from typing import Any


ALIASES = {
    # care setting / timing
    "adv": "advanced", "amb": "ambulatory", "asst": "assisted", "beds": "bedside",
    "compr": "comprehensive", "comp": "comprehensive", "cont": "continuous",
    "elect": "elective", "emer": "emergency", "ext": "extended", "foc": "focused",
    "inpt": "inpatient", "intens": "intensive", "interm": "intermittent",
    "outpt": "outpatient", "postop": "postoperative", "preop": "preoperative",
    "rtn": "routine", "spclst": "specialist", "spec": "specialist", "std": "standard",
    "supv": "supervised",
    # specialties
    "card": "cardiac", "derm": "dermatologic", "endo": "endocrine", "ent": "otolaryngologic",
    "gastro": "gastrointestinal", "gi": "gastrointestinal", "ger": "geriatric",
    "haem": "haematology", "hematology": "haematology", "hep": "hepatic",
    "immun": "immunologic", "infect": "infectious", "metab": "metabolic",
    "msk": "musculoskeletal", "neuro": "neurological", "obst": "obstetric",
    "onc": "oncology", "ophth": "ophthalmic", "ortho": "orthopaedic",
    "paed": "paediatric", "pall": "palliative", "psych": "psychiatric",
    "pulm": "pulmonary", "ren": "renal", "rheum": "rheumatologic", "urol": "urologic",
    "vasc": "vascular",
    # service concepts
    "admin": "administration", "anaes": "anaesthesia", "anly": "analysis", "bd": "bed", "biop": "biopsy",
    "conf": "conference", "cs": "case", "crit": "critical", "diag": "diagnostic",
    "dial": "dialysis", "disch": "discharge", "disp": "dispensing", "endosc": "endoscopic",
    "fract": "fraction", "hm": "home", "img": "imaging", "inf": "infusion",
    "interp": "interpretation", "isol": "isolation", "lab": "laboratory", "nutr": "nutritional",
    "obs": "observation", "occ": "occupancy", "pharm": "pharmaceutical", "physio": "physiotherapy",
    "pnl": "panel", "proc": "procedure", "prog": "programme", "radiother": "radiotherapy",
    "recov": "recovery", "rehab": "rehabilitation", "rm": "room", "sess": "session",
    "spcm": "specimen", "steril": "sterilisation", "supp": "support", "svc": "service",
    "tele": "telemetry", "thtr": "theatre", "transf": "transfusion", "transp": "transport",
    "vent": "ventilation", "vst": "visit", "wd": "ward", "wnd": "wound",
}

NOISE = {"charge", "fee", "service", "the", "for", "and", "of"}


def tokens(text: str) -> list[str]:
    text = re.sub(r"/[A-Z]{1,4}-\d+", " ", text, flags=re.I)
    raw = re.findall(r"[a-z0-9]+", text.lower())
    return [ALIASES.get(x, x) for x in raw if x not in NOISE and not x.isdigit()]


def _token_similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)):
        return 0.88
    return 0.0


def _coverage(description: list[str], canonical: list[str]) -> float:
    if not description or not canonical:
        return 0.0
    remaining = list(canonical)
    matched = 0.0
    for token in description:
        candidates = [(_token_similarity(token, other), i) for i, other in enumerate(remaining)]
        score, index = max(candidates, default=(0.0, -1))
        if score:
            matched += score
            remaining.pop(index)
    precision = matched / len(description)
    recall = matched / len(canonical)
    return 0.58 * recall + 0.42 * precision


@dataclass(frozen=True)
class Match:
    service_id: str | None
    service_name: str | None
    confidence: float
    runner_up: str | None
    margin: float
    uncertainty: str | None
    text_score: float
    method: str = "token_alias_v1"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ServiceMatcher:
    def __init__(self, contract: dict[str, Any], abstain_below: float = 0.58, margin_below: float = 0.06):
        self.services = contract["services"]
        self.abstain_below = abstain_below
        self.margin_below = margin_below
        self.rate_candidates: dict[str, set[int]] = {
            service["name"]: {rate["rate_cents"] for rate in service["rates"]} for service in self.services
        }
        for bundle in contract.get("rules", {}).get("bundles", []):
            self.rate_candidates.setdefault(bundle["service_a"], set()).add(bundle["rate_a_cents"])
            self.rate_candidates.setdefault(bundle["service_b"], set()).add(bundle["rate_b_cents"])

    def match(self, description: str, billed_unit: str | None = None,
              billed_rate_cents: int | None = None, service_date: str | None = None) -> Match:
        query = tokens(description)
        scored: list[tuple[float, dict[str, Any]]] = []
        text_scores: dict[str, float] = {}
        for service in self.services:
            text_score = _coverage(query, tokens(service["name"]))
            text_scores[service["name"]] = text_score
            score = text_score
            if billed_unit and billed_unit == service["unit"]:
                score += 0.035
            # Price is only a weak tie-breaker: rates themselves may be erroneous.
            if billed_rate_cents is not None and any(r["rate_cents"] == billed_rate_cents for r in service["rates"]):
                score += 0.025
            scored.append((min(score, 1.0), service))
        scored.sort(key=lambda x: (x[0], x[1]["name"]), reverse=True)
        best_score, best = scored[0]
        second_score, second = scored[1] if len(scored) > 1 else (0.0, {"name": None})
        margin = best_score - second_score
        uncertainty = None
        service_id: str | None = best["service_id"]
        service_name: str | None = best["name"]
        text_score = text_scores[best["name"]]
        confidence = max(0.0, min(0.99, 0.15 + 0.70 * best_score + 0.55 * margin))
        if best_score < self.abstain_below:
            uncertainty = "no_service_match"
            service_id = service_name = None
            confidence = min(confidence, 0.44)
        elif margin < self.margin_below:
            uncertainty = "ambiguous_service_match"
            confidence = min(confidence, 0.64)
        return Match(service_id, service_name, round(confidence, 4), second["name"], round(margin, 4), uncertainty, round(text_score, 4))
