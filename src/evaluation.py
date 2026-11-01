"""Evaluation metrics: anomaly detection, score validity vs hidden labels, distributions.

Ground truth is read here (evaluation side) and never by the scoring engine.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as C
from .export import load_exceptions, load_ground_truth, load_tables, write_json


def _auc(pos: np.ndarray, neg: np.ndarray) -> float | None:
    """P(score_pos > score_neg) with ties = 0.5 (Mann-Whitney U / n1 n2)."""
    if len(pos) == 0 or len(neg) == 0:
        return None
    ranks = pd.Series(np.concatenate([pos, neg])).rank().to_numpy()
    u = ranks[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2
    return round(float(u / (len(pos) * len(neg))), 4)


def _plain(o):
    """Recursively convert numpy scalars / non-string keys into JSON-safe values."""
    if isinstance(o, dict):
        return {str(k): _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_plain(v) for v in o]
    if hasattr(o, "item"):
        return o.item()
    return o


def _r(x, nd=2):
    return None if x is None or pd.isna(x) else round(float(x), nd)


def evaluate(data_dir: str | Path, write_report: bool = True, verbose: bool = True) -> dict:
    data_dir = Path(data_dir)
    t = load_tables(data_dir)
    gt, labels = load_ground_truth(data_dir)
    exc = load_exceptions(data_dir)
    validation = json.loads((data_dir / "validation_summary.json").read_text()) if (data_dir / "validation_summary.json").exists() else {}

    # 1. Engine quarantine vs labelled source anomalies (score anomalies are audit-side).
    src_gt = gt[gt["issue_type"] != "SCORE_USES_FUTURE_DATA"]
    quarantined = set(exc["record_id"])
    per_issue = {}
    for issue, grp in src_gt.groupby("issue_type"):
        hit = grp["record_id"].isin(quarantined)
        per_issue[issue] = {"injected": int(len(grp)), "quarantined": int(hit.sum()), "recall": _r(hit.mean(), 4)}
    tp = int(exc["record_id"].isin(src_gt["record_id"]).sum())
    quarantine_metrics = {
        "precision": _r(tp / len(exc), 4) if len(exc) else None,
        "recall": _r(src_gt["record_id"].isin(quarantined).mean(), 4),
        "exceptions_logged": int(len(exc)),
        "by_rule": exc["rule_id"].value_counts().sort_index().to_dict(),
        "by_assigned_role": exc["assigned_role"].value_counts().sort_index().to_dict(),
        "per_issue_type": per_issue,
    }
    audit = validation.get("anomaly_detection", {})
    total_injected = sum(d["injected"] for d in audit.values())
    validation_metrics = {
        "status": validation.get("status"),
        "recall": _r(sum(d["detected"] for d in audit.values()) / total_injected, 4) if total_injected else None,
        "precision": _r(1 - validation.get("unexpected_total", 0) / max(1, sum(c["findings"] for c in validation.get("checks", {}).values())), 4),
        "per_issue_type": {k: {"injected": v["injected"], "detected": v["detected"], "recall": v["recall"]} for k, v in audit.items()},
    }

    # 2. Score validity against hidden profiles (official runs only).
    sc = t["SupplierScore"]
    sc = sc[sc["score_run_id"].str.startswith("RUN-")].merge(labels, on="supplier_id")
    final = sc[sc["score_timestamp"] == sc["score_timestamp"].max()]
    scored = final[final["reliability_tier"] != "INSUFFICIENT_DATA"]
    stable = scored.loc[scored["profile"] == "STABLE", "reliability_score"].to_numpy()
    at_risk = scored.loc[scored["risk_class"] == "AT_RISK", "reliability_score"].to_numpy()
    allowed = {"APPROVED_OR_MONITOR": {"APPROVED", "MONITOR"}, "MONITOR_OR_ESCALATE": {"MONITOR", "ESCALATE"},
               "REVIEW_DATA": {"REVIEW_DATA"}}
    agree = final.apply(lambda r: r["decision"] in allowed[r["expected_final_decision"]], axis=1)
    by_profile = final.groupby("profile").agg(
        suppliers=("supplier_id", "size"), mean_reliability=("reliability_score", "mean"),
        mean_coverage_pct=("data_coverage_pct", "mean"),
        insufficient_rate=("reliability_tier", lambda s: (s == "INSUFFICIENT_DATA").mean()))
    by_profile["decision_agreement"] = agree.groupby(final["profile"]).mean()
    trend = sc.pivot_table(index="profile", columns="score_timestamp", values="delivery_score", aggfunc="mean")
    trend.columns = [str(pd.Timestamp(c).date()) for c in trend.columns]
    det = sc[sc["profile"] == "DETERIORATING"].pivot(index="supplier_id", columns="score_timestamp", values="delivery_score")
    first_c, last_c = det.columns.min(), det.columns.max()
    both = det[[first_c, last_c]].dropna()
    qi = sc.groupby(["profile", "supplier_id"])["quality_score"].min().groupby("profile").mean()
    score_validity = {
        "final_cutoff": str(final["score_timestamp"].max().date()),
        "auc_stable_vs_at_risk": _auc(stable, at_risk),
        "mean_reliability_stable": _r(stable.mean() if len(stable) else None),
        "mean_reliability_at_risk": _r(at_risk.mean() if len(at_risk) else None),
        "decision_agreement_with_expected": _r(agree.mean(), 4),
        "by_profile": {p: {k: _r(v, 4) if isinstance(v, float) else int(v) for k, v in row.items()} for p, row in by_profile.iterrows()},
        "decision_matrix": pd.crosstab(final["profile"], final["decision"]).to_dict("index"),
        "tier_matrix": pd.crosstab(final["profile"], final["reliability_tier"]).to_dict("index"),
        "mean_delivery_score_by_cutoff": {p: {c: _r(v) for c, v in row.items()} for p, row in trend.iterrows()},
        "deteriorating_share_declining": _r((both[last_c] < both[first_c]).mean(), 4) if len(both) else None,
        "mean_worst_quality_score_by_profile": {p: _r(v) for p, v in qi.items()},
    }

    # 3. Missing-data handling.
    cp = t["SupplierScoreComponent"].merge(t["SupplierScore"][["score_id", "supplier_id", "score_run_id"]], on="score_id")
    cp = cp[cp["score_run_id"].str.startswith("RUN-")].merge(labels[["supplier_id", "profile"]], on="supplier_id")
    missing = {
        "component_missing_rate": {k: _r(v, 4) for k, v in (cp["status"] == "MISSING").groupby(cp["component"]).mean().items()},
        "component_missing_rate_by_profile": {k: _r(v, 4) for k, v in (cp["status"] == "MISSING").groupby(cp["profile"]).mean().items()},
        "scores_insufficient_data": int((sc["reliability_tier"] == "INSUFFICIENT_DATA").sum()),
        "imputation": "none - MISSING components carry effective_weight 0 and are listed in components_missing",
    }

    # 4. Realism / distribution summary of source data.
    sh, po, ins, inv = t["Shipment"], t["PurchaseOrder"], t["Inspection"], t["Invoice"]
    d = sh[sh["delivery_date"].notna() & sh["po_id"].isin(po["po_id"])].merge(po[["po_id", "order_date", "required_date"]], on="po_id")
    lead = (d["delivery_date"] - d["order_date"]).dt.days
    lateness = (d["delivery_date"] - d["required_date"]).dt.days
    distributions = {
        "sites_per_supplier": t["Site"].groupby("supplier_id").size().value_counts().sort_index().to_dict(),
        "lines_per_po": po["line_count"].value_counts().sort_index().to_dict(),
        "shipments_per_po": sh.groupby("po_id").size().value_counts().sort_index().to_dict(),
        "shipment_status": sh["status"].value_counts().to_dict(),
        "po_status": po["status"].value_counts().to_dict(),
        "on_time_rate": _r((lateness <= 0).mean(), 4),
        "lead_time_days": {"mean": _r(lead.mean()), "p10": _r(lead.quantile(0.1)), "p50": _r(lead.median()), "p90": _r(lead.quantile(0.9))},
        "days_late_when_late": {"mean": _r(lateness[lateness > 0].mean()), "p90": _r(lateness[lateness > 0].quantile(0.9))},
        "unit_reject_rate": _r(ins["rejected_qty"].sum() / ins["inspected_qty"].sum(), 4),
        "inspection_result": ins["result"].value_counts().to_dict(),
        "invoice_match_status": inv["match_status"].value_counts().to_dict(),
        "invoice_coverage_of_delivered": _r(len(inv) / max(1, sh["delivery_date"].notna().sum()), 4),
        "risk_severity": t["RiskSignal"]["severity"].value_counts().to_dict(),
        "po_total_amount_usd": {"p50": _r(po["total_amount"].median()), "p90": _r(po["total_amount"].quantile(0.9))},
    }

    report = {"quarantine_vs_ground_truth": quarantine_metrics, "validation_vs_ground_truth": validation_metrics,
              "score_validity": score_validity, "missing_data": missing, "distributions": distributions}
    report = _plain(report)
    if write_report:
        write_json(data_dir / "evaluation_report.json", report)
    if verbose:
        sv = score_validity
        print("\nEVALUATION")
        print(f"  quarantine precision={quarantine_metrics['precision']} recall={quarantine_metrics['recall']} "
              f"({quarantine_metrics['exceptions_logged']} exceptions)")
        print(f"  validation recall={validation_metrics['recall']} precision={validation_metrics['precision']}")
        print(f"  AUC stable vs at-risk={sv['auc_stable_vs_at_risk']}  mean score stable={sv['mean_reliability_stable']} "
              f"at-risk={sv['mean_reliability_at_risk']}  decision agreement={sv['decision_agreement_with_expected']}")
        print(f"  deteriorating suppliers with declining delivery score: {sv['deteriorating_share_declining']}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate scores and anomaly detection against ground truth.")
    parser.add_argument("--data-dir", default="data/full")
    evaluate(parser.parse_args().data_dir)


if __name__ == "__main__":
    main()
