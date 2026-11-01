"""Explainable, point-in-time supplier reliability scoring.

The engine reads only canonical source tables. It never opens ground_truth/.
Records that fail its own integrity rules are quarantined into the
ExceptionLog and excluded before any metric is computed (exception path).
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from . import config as C
from . import ids

DAY = pd.Timedelta(days=1)

DENOMINATORS = {
    "delivery": "Shipments delivered in window (valid PO)",
    "quality": "Units inspected in window",
    "cost": "Expected invoice amount in window (USD)",
    "capacity": "Supplier-months with dispatches in window",
    "compliance": "Active contracts with an audit on file",
    "disruption": "Risk signals in look-back window",
}
FORMULAS = {
    "delivery": "score = on_time / delivered x 100",
    "quality": f"score = max(0, 100 - {C.QUALITY_POINTS_PER_REJECT_PCT:g} x reject%)",
    "cost": f"score = max(0, 100 - {C.COST_POINTS_PER_VARIANCE_PCT:g} x |variance|%)",
    "capacity": "score = months_within_capacity / active_months x 100",
    "compliance": f"score = clip(mean audit - {C.CERT_LAPSED_PENALTY:g} if cert lapsed - {C.REGULATORY_SIGNAL_PENALTY:g} x regulatory signals, 0, 100)",
    "disruption": f"score = max(0, 100 - sum(severity pts {C.SEVERITY_POINTS} x (1 open | {C.RESOLVED_SIGNAL_FACTOR} resolved)))",
}


# --------------------------------------------------------------------------
# Exception path: quarantine
# --------------------------------------------------------------------------
def quarantine(tables: dict[str, pd.DataFrame]) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Apply engine integrity rules; return (clean tables, ExceptionLog)."""
    site, po, sh = tables["Site"], tables["PurchaseOrder"], tables["Shipment"]
    ev, rs = tables["DeliveryEvent"], tables["RiskSignal"]
    rows: list[dict] = []

    def log(rule, entity, frame, key, reason, role):
        for rec, sup in zip(frame[key], frame["supplier_id"]):
            rows.append({"rule_id": rule, "entity": entity, "record_id": rec, "supplier_id": sup,
                         "reason": reason, "assigned_role": role})

    # Site conflation: embedded supplier ID differs, or address shared across suppliers.
    embedded = site["site_id"].map(ids.supplier_of)
    shared_addr = site.groupby("address")["supplier_id"].transform("nunique") > 1
    bad_site = (embedded != site["supplier_id"]) | (shared_addr & site.duplicated("address", keep="first"))
    log("SITE_CONFLATION", "Site", site[bad_site], "site_id",
        "Site ID/address attributed to more than one supplier", "OPERATIONS_MANAGER")
    site_owner = site[~bad_site].set_index("site_id")["supplier_id"]

    bad_po = po["site_id"].map(site_owner) != po["supplier_id"]
    log("SITE_CONFLATION", "PurchaseOrder", po[bad_po], "po_id",
        "PO site does not belong to PO supplier", "PLANNER")
    po_ok = po[~bad_po]
    po_lookup = po_ok.set_index("po_id")

    no_po = sh["po_id"].isna() | ~sh["po_id"].isin(po["po_id"])
    log("SHP_NO_PO", "Shipment", sh[no_po], "shipment_id",
        "Shipment has no valid purchase order", "LOGISTICS_ANALYST")
    linked = ~no_po & sh["po_id"].isin(po_ok["po_id"])
    mismatch = linked & ((sh["po_id"].map(po_lookup["supplier_id"]) != sh["supplier_id"])
                         | (sh["po_id"].map(po_lookup["site_id"]) != sh["site_id"]))
    log("SITE_CONFLATION", "Shipment", sh[mismatch], "shipment_id",
        "Shipment supplier/site differs from its PO", "LOGISTICS_ANALYST")

    stamp = ev.pivot_table(index="shipment_id", columns="event_type", values="event_timestamp", aggfunc="min")
    for col in ("DISPATCHED", "DELIVERED"):
        if col not in stamp:
            stamp[col] = pd.NaT
    ev_bad = stamp.index[stamp["DELIVERED"] < stamp["DISPATCHED"]]
    date_bad = (sh["delivery_date"] < sh["dispatch_date"]) | sh["shipment_id"].isin(ev_bad)
    log("SHP_DATE_ORDER", "Shipment", sh[date_bad & ~no_po], "shipment_id",
        "Delivery recorded before dispatch", "LOGISTICS_ANALYST")

    bad_rs = rs["source"].isna() | rs["signal_timestamp"].isna()
    log("RSK_PROVENANCE", "RiskSignal", rs[bad_rs], "risk_signal_id",
        "Risk signal missing source and/or timestamp", "LOGISTICS_ANALYST")

    sh_ok = sh[linked & ~mismatch & ~date_bad]
    ok_ship_ids = set(sh_ok["shipment_id"])
    clean = dict(tables)
    clean["Site"] = site[~bad_site]
    clean["Contract"] = tables["Contract"][tables["Contract"]["site_id"].isin(clean["Site"]["site_id"])]
    clean["PurchaseOrder"] = po_ok
    clean["Shipment"] = sh_ok
    clean["Inspection"] = tables["Inspection"][tables["Inspection"]["shipment_id"].isin(ok_ship_ids)]
    clean["Invoice"] = tables["Invoice"][tables["Invoice"]["shipment_id"].isin(ok_ship_ids)]
    clean["RiskSignal"] = rs[~bad_rs]

    exceptions = pd.DataFrame(rows, columns=["rule_id", "entity", "record_id", "supplier_id", "reason", "assigned_role"])
    exceptions = exceptions.drop_duplicates(["rule_id", "record_id"]).sort_values(["rule_id", "record_id"], kind="mergesort")
    prefix, width = ids.COUNTERS["ExceptionLog"]
    exceptions.insert(0, "exception_id", ids.sequence(prefix, 1, len(exceptions), width))
    exceptions["action"] = "EXCLUDED_FROM_SCORING"
    exceptions["detected_at"] = C.EXTRACT_DATE
    exceptions["status"] = "OPEN"
    exceptions = exceptions[["exception_id", "rule_id", "entity", "record_id", "supplier_id", "reason",
                             "action", "assigned_role", "detected_at", "status"]].reset_index(drop=True)
    return clean, exceptions


# --------------------------------------------------------------------------
# Point-in-time snapshot
# --------------------------------------------------------------------------
def _in(series: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    return (series >= start) & (series < end)


def score_snapshot(clean: dict[str, pd.DataFrame], cutoff: pd.Timestamp,
                   data_cutoff: pd.Timestamp | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Score every supplier at `cutoff` using only data dated on/before it.

    `data_cutoff` exists solely so the anomaly injector can reproduce the
    "score uses future data" defect; production runs leave it as None.
    """
    data_cutoff = cutoff if data_cutoff is None else data_cutoff
    end = data_cutoff + DAY                                   # exclusive
    start = max(cutoff - pd.Timedelta(days=C.SCORING_WINDOW_DAYS - 1), C.OBSERVATION_START)
    risk_start = end - pd.Timedelta(days=C.RISK_WINDOW_DAYS)
    sup = clean["Supplier"]["supplier_id"]
    po = clean["PurchaseOrder"].set_index("po_id")
    sh, ins, inv = clean["Shipment"], clean["Inspection"], clean["Invoice"]
    con, rs = clean["Contract"], clean["RiskSignal"]
    comp: dict[str, pd.DataFrame] = {}

    def frame(**cols) -> pd.DataFrame:
        cols = {k: v if pd.api.types.is_datetime64_any_dtype(v) else v.astype("float64") for k, v in cols.items()}
        return pd.DataFrame(cols).reindex(sup)

    # Delivery: on-time / delivered
    d = sh[_in(sh["delivery_date"], start, end)]
    on_time = d["delivery_date"] <= d["po_id"].map(po["required_date"])
    g = d.assign(on_time=on_time).groupby("supplier_id")
    f = frame(num=g["on_time"].sum(), den=g.size(), max_date=g["delivery_date"].max())
    f["ok"] = f["den"] >= C.MIN_DELIVERED_SHIPMENTS
    f["metric"] = f["num"] / f["den"] * 100
    f["score"] = f["metric"]
    comp["delivery"] = f

    # Quality: rejected units / inspected units
    q = ins[_in(ins["inspection_date"], start, end)]
    g = q.groupby("supplier_id")
    f = frame(num=g["rejected_qty"].sum(), den=g["inspected_qty"].sum(), n=g.size(),
              max_date=g["inspection_date"].max())
    f["ok"] = (f["n"] >= C.MIN_INSPECTIONS) & (f["den"] >= C.MIN_INSPECTED_UNITS)
    f["metric"] = f["num"] / f["den"] * 100
    f["score"] = (100 - C.QUALITY_POINTS_PER_REJECT_PCT * f["metric"]).clip(lower=0)
    comp["quality"] = f

    # Cost: |invoice variance| / expected amount
    c = inv[_in(inv["invoice_date"], start, end)]
    g = c.assign(abs_var=c["variance_amount"].abs()).groupby("supplier_id")
    f = frame(num=g["abs_var"].sum(), den=g["expected_amount"].sum(), n=g.size(),
              max_date=g["invoice_date"].max())
    f["ok"] = (f["n"] >= C.MIN_INVOICES) & (f["den"] > 0)
    f["metric"] = f["num"] / f["den"] * 100
    f["score"] = (100 - C.COST_POINTS_PER_VARIANCE_PCT * f["metric"]).clip(lower=0)
    comp["cost"] = f

    # Capacity: months within contracted monthly capacity / active months
    active = con[(con["start_date"] <= cutoff) & (con["end_date"] >= cutoff)]
    cap = active.groupby("supplier_id")["capacity_units_month"].sum()
    k = sh[_in(sh["dispatch_date"], start, end)]
    monthly = k.assign(month=k["dispatch_date"].dt.to_period("M")).groupby(["supplier_id", "month"]).agg(
        units=("shipped_qty_total", "sum"), max_date=("dispatch_date", "max")).reset_index()
    monthly["within"] = monthly["units"] <= monthly["supplier_id"].map(cap).fillna(0)
    g = monthly.groupby("supplier_id")
    f = frame(num=g["within"].sum(), den=g.size(), max_date=g["max_date"].max())
    f["ok"] = f["den"] >= C.MIN_CAPACITY_MONTHS
    f["metric"] = f["num"] / f["den"] * 100
    f["score"] = f["metric"]
    comp["capacity"] = f

    # Compliance: audit score less certification and regulatory penalties
    audited = active[active["last_audit_score"].notna() & (active["last_audit_date"] < end)]
    g = audited.groupby("supplier_id")
    lapsed = active[active["certification_expiry_date"] < end].groupby("supplier_id").size() > 0
    reg = rs[(rs["risk_type"] == "REGULATORY") & _in(rs["signal_timestamp"], risk_start, end)]
    reg_g = reg.groupby("supplier_id")
    f = frame(num=g["last_audit_score"].sum(), den=g.size(), max_audit=g["last_audit_date"].max())
    f["lapsed"] = lapsed.reindex(sup).fillna(False).astype(bool)
    f["n_reg"] = reg_g.size().reindex(sup).fillna(0)
    f["max_date"] = pd.concat([f["max_audit"], reg_g["signal_timestamp"].max().reindex(sup).dt.normalize()], axis=1).max(axis=1)
    f["ok"] = f["den"] >= 1
    f["metric"] = f["num"] / f["den"]
    f["adjustment"] = -(C.CERT_LAPSED_PENALTY * f["lapsed"] + C.REGULATORY_SIGNAL_PENALTY * f["n_reg"])
    f["score"] = (f["metric"] + f["adjustment"]).clip(0, 100)
    comp["compliance"] = f

    # Disruption: severity-weighted penalty, resolved signals count half
    r = rs[_in(rs["signal_timestamp"], risk_start, end)]
    is_open = r["resolved_timestamp"].isna() | (r["resolved_timestamp"] >= end)
    pts = r["severity"].map(C.SEVERITY_POINTS) * np.where(is_open, 1.0, C.RESOLVED_SIGNAL_FACTOR)
    g = r.assign(pts=pts).groupby("supplier_id")
    f = frame(num=g["pts"].sum(), den=g.size(), max_date=g["signal_timestamp"].max().dt.normalize())
    f[["num", "den"]] = f[["num", "den"]].fillna(0)
    f["ok"] = True                    # continuous monitoring: zero signals is evidence, not missing
    f["metric"] = f["num"]
    f["score"] = (100 - f["metric"]).clip(lower=0)
    comp["disruption"] = f

    known = rs[rs["signal_timestamp"] < end]
    open_high = known[(known["severity"] == "HIGH") & (known["resolved_timestamp"].isna() | (known["resolved_timestamp"] >= end))]
    open_high_n = open_high.groupby("supplier_id").size().reindex(sup).fillna(0).astype(int)

    # Assemble components (long) and score rows (wide)
    long_rows = []
    for name, f in comp.items():
        f = f.copy()
        f["ok"] = f["ok"].fillna(False).astype(bool)
        if "adjustment" not in f:
            f["adjustment"] = 0.0
        long_rows.append(pd.DataFrame({
            "supplier_id": sup.to_numpy(), "component": name,
            "status": np.where(f["ok"], "OK", "MISSING"),
            "numerator": f["num"].to_numpy(dtype=float), "denominator": f["den"].to_numpy(dtype=float),
            "denominator_definition": DENOMINATORS[name],
            "metric_value": f["metric"].round(4).to_numpy(dtype=float),
            "adjustment": f["adjustment"].fillna(0.0).to_numpy(dtype=float),
            "formula": FORMULAS[name],
            "component_score": np.where(f["ok"], f["score"].round(2), np.nan),
            "base_weight": C.WEIGHTS[name],
            "max_date": np.where(f["ok"], f["max_date"], pd.NaT),
        }))
    components = pd.concat(long_rows, ignore_index=True)
    components["max_date"] = pd.to_datetime(components["max_date"])
    ok_w = components["base_weight"].where(components["status"] == "OK", 0.0)
    total_w = ok_w.groupby(components["supplier_id"]).transform("sum")
    components["effective_weight"] = np.where(total_w > 0, ok_w / total_w.replace(0, np.nan), 0.0).round(6)
    contrib = components["effective_weight"] * components["component_score"].fillna(0.0)
    components["weighted_contribution"] = contrib.round(4)

    g = components.groupby("supplier_id", sort=False)
    wide = components.pivot(index="supplier_id", columns="component", values="component_score").reindex(sup)
    coverage = (ok_w.groupby(components["supplier_id"]).sum() * 100).round(2).reindex(sup)
    n_ok = (components["status"] == "OK").groupby(components["supplier_id"]).sum().reindex(sup)
    reliability = contrib.groupby(components["supplier_id"]).sum().round(2).reindex(sup)
    reliability = reliability.where(n_ok > 0)
    insufficient = (coverage < C.MIN_COVERAGE_PCT) | (n_ok < C.MIN_COMPONENTS)
    tier = np.select([insufficient, reliability >= C.TIER_HIGH, reliability >= C.TIER_MEDIUM],
                     ["INSUFFICIENT_DATA", "HIGH", "MEDIUM"], "LOW")
    decision = np.select([insufficient, (tier == "LOW") | (open_high_n.to_numpy() > 0), tier == "MEDIUM"],
                         ["REVIEW_DATA", "ESCALATE", "MONITOR"], "APPROVED")

    lost = (components["effective_weight"] * (100 - components["component_score"])).where(components["status"] == "OK")
    ranked = components.assign(lost=lost.round(6), order=components["component"].map({c: i for i, c in enumerate(C.WEIGHTS)}))
    ranked = ranked[ranked["lost"] > 0].sort_values(["supplier_id", "lost", "order"], ascending=[True, False, True], kind="mergesort")
    drivers = ranked.groupby("supplier_id")["component"].apply(lambda s: ";".join(s.head(2))).reindex(sup)
    missing = components[components["status"] == "MISSING"].groupby("supplier_id")["component"].apply(";".join).reindex(sup)
    max_source = components.groupby("supplier_id")["max_date"].max().reindex(sup)

    counts = pd.DataFrame({
        "delivered_shipments": comp["delivery"]["den"], "inspections": comp["quality"]["n"],
        "invoices": comp["cost"]["n"], "dispatch_months": comp["capacity"]["den"],
        "audited_contracts": comp["compliance"]["den"], "risk_signals": comp["disruption"]["den"],
    }).fillna(0).astype(int)

    scores = pd.DataFrame({
        "supplier_id": sup.to_numpy(),
        "model_version": C.MODEL_VERSION,
        "score_timestamp": cutoff,
        "observation_start": start,
        "max_source_date": max_source.to_numpy(),
        **{f"{c}_score": wide[c].to_numpy() for c in C.WEIGHTS},
        "data_coverage_pct": coverage.to_numpy(),
        "components_missing": missing.to_numpy(),
        "reliability_score": reliability.to_numpy(),
        "reliability_tier": tier,
        "decision": decision,
        "top_drivers": drivers.to_numpy(),
        "open_high_risks": open_high_n.to_numpy(),
        "source_record_counts": [json.dumps(r, sort_keys=True) for r in counts.to_dict("records")],
    })
    return scores, components.drop(columns="max_date")


def assign_ids(scores: pd.DataFrame, components: pd.DataFrame, run_id: str,
               score_start: int, component_start: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    sp, sw = ids.COUNTERS["SupplierScore"]
    cp, cw = ids.COUNTERS["SupplierScoreComponent"]
    scores = scores.copy()
    scores.insert(0, "score_id", ids.sequence(sp, score_start, len(scores), sw))
    scores.insert(1, "score_run_id", run_id)
    comps = components.merge(scores[["supplier_id", "score_id"]], on="supplier_id", how="left")
    comps["_order"] = comps["component"].map({c: i for i, c in enumerate(C.WEIGHTS)})
    comps = comps.sort_values(["score_id", "_order"], kind="mergesort").drop(columns=["_order", "supplier_id"])
    comps.insert(0, "score_component_id", ids.sequence(cp, component_start, len(comps), cw))
    cols = ["score_component_id", "score_id", "component", "status", "numerator", "denominator",
            "denominator_definition", "metric_value", "adjustment", "formula", "component_score",
            "base_weight", "effective_weight", "weighted_contribution"]
    return scores, comps[cols].reset_index(drop=True)


def run_scoring(tables: dict[str, pd.DataFrame],
                cutoffs: list[pd.Timestamp] = C.SCORE_CUTOFFS) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Quarantine, then score every supplier at every cutoff."""
    clean, exceptions = quarantine(tables)
    all_scores, all_comps = [], []
    s_next, c_next = 1, 1
    for cutoff in cutoffs:
        scores, comps = score_snapshot(clean, cutoff)
        scores, comps = assign_ids(scores, comps, f"RUN-{cutoff.date()}", s_next, c_next)
        s_next += len(scores)
        c_next += len(comps)
        all_scores.append(scores)
        all_comps.append(comps)
    return pd.concat(all_scores, ignore_index=True), pd.concat(all_comps, ignore_index=True), exceptions
