"""Hospital 1 evaluation and calibration reporting."""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .io import read_csv


def evaluate(predictions: list[dict[str, Any]], labels_path: Path) -> tuple[dict[str, Any], str]:
    labels = {r["invoice_id"]: r for r in read_csv(labels_path)}
    preds = {r["invoice_id"]: r for r in predictions}
    common = sorted(labels.keys() & preds.keys())
    tp = fp = tn = fn = exact_total = 0
    category_stats: defaultdict[str, Counter[str]] = defaultdict(Counter)
    calibration: defaultdict[str, list[int]] = defaultdict(list)
    failure_types: Counter[str] = Counter()
    for iid in common:
        y = int(labels[iid]["is_erroneous"])
        p = int(preds[iid]["flagged"])
        if y and p: tp += 1
        elif y and not p: fn += 1
        elif not y and p: fp += 1
        else: tn += 1
        if int(preds[iid]["expected_total_cents"]) == int(labels[iid]["expected_total_cents"]): exact_total += 1
        true_categories = set(filter(None, labels[iid]["error_categories"].split("|")))
        pred_categories = set(filter(None, str(preds[iid]["error_category"]).split("|")))
        for category in true_categories | pred_categories:
            stat = category_stats[category]
            if category in true_categories and category in pred_categories: stat["tp"] += 1
            elif category in pred_categories: stat["fp"] += 1
            else: stat["fn"] += 1
        if y != p: failure_types["invoice_classification"] += 1
        if int(preds[iid]["expected_total_cents"]) != int(labels[iid]["expected_total_cents"]): failure_types["expected_total"] += 1
        if true_categories != pred_categories: failure_types["category_taxonomy"] += 1
        confidence = float(preds[iid]["confidence"])
        bucket = f"{int(confidence * 10) / 10:.1f}-{min(1.0, int(confidence * 10) / 10 + .1):.1f}"
        calibration[bucket].append(int(y == p))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    categories = {}
    for category, stat in sorted(category_stats.items()):
        cp = stat["tp"] / (stat["tp"] + stat["fp"]) if stat["tp"] + stat["fp"] else 0.0
        cr = stat["tp"] / (stat["tp"] + stat["fn"]) if stat["tp"] + stat["fn"] else 0.0
        categories[category] = {"tp": stat["tp"], "fp": stat["fp"], "fn": stat["fn"],
                                "precision": round(cp, 4), "recall": round(cr, 4)}
    report = {
        "evaluated": len(common), "labelled": len(labels), "confusion_matrix": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
        "classification_accuracy": round((tp + tn) / len(common), 4) if common else 0,
        "exact_expected_total_rate": round(exact_total / len(common), 4) if common else 0,
        "per_category": categories,
        "calibration": {k: {"count": len(v), "empirical_accuracy": round(sum(v) / len(v), 4)} for k, v in sorted(calibration.items())},
        "failure_type_counts": dict(failure_types),
    }
    md = [
        "# Hospital 1 Evaluation", "",
        f"Evaluated {len(common)} labelled invoices. Classification accuracy: **{report['classification_accuracy']:.1%}**; precision: **{precision:.1%}**; recall: **{recall:.1%}**; F1: **{f1:.1%}**. Exact expected-total accuracy: **{report['exact_expected_total_rate']:.1%}**.", "",
        "## Confusion matrix", "", "| TP | FP | TN | FN |", "|---:|---:|---:|---:|", f"| {tp} | {fp} | {tn} | {fn} |", "",
        "## Per-category performance", "", "| Category | TP | FP | FN | Precision | Recall |", "|---|---:|---:|---:|---:|---:|",
    ]
    for category, stat in categories.items():
        md.append(f"| {category} | {stat['tp']} | {stat['fp']} | {stat['fn']} | {stat['precision']:.1%} | {stat['recall']:.1%} |")
    md += ["", "## Systematic failure modes", "",
           "1. **Semantic collision:** highly abbreviated descriptions can omit the specialty or care setting, producing a low-margin match. These are explicitly marked uncertain and confidence is reduced.",
           "2. **Category overlap:** one monetary discrepancy can support both a generic `unit_price_mismatch` and a specific adjustment category. The detailed audit preserves both for review.",
           "3. **Invalid-date pricing:** malformed dates cannot select a dated amendment or weekend rule. The engine preserves the billed amount for an unpriceable service and flags the date.",
           "4. **Ordering sensitivity:** cumulative discounts and duplicate allocation depend on Service Date then line identifier, as required by the contracts; source rows with duplicate identifiers remain review-sensitive.", ""]
    return report, "\n".join(md)
