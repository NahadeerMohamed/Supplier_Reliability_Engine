"""Validate an exported dataset and reconcile findings against ground truth.

Every check emits record-level findings (check, entity, record_id). A run
passes when (a) no finding lands on a record outside ground truth and (b)
every injected anomaly is detected by at least one check.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as C
from . import ids
from .export import load_ground_truth, load_tables, write_json
from .schema import (BY_NAME, ENTITIES, EVENT_TO_SHIPMENT_STATUS, PO_TRANSITIONS,
                     SHIPMENT_TRANSITIONS, foreign_keys, id_pattern)
from .scoring import quarantine, score_snapshot

CHECKS = {
    "SCHEMA": "Required columns present.",
    "PK_UNIQUE": "Primary key non-null and unique.",
    "ID_FORMAT": "Primary key matches the ID convention.",
    "REQUIRED_FIELD": "Required fields are non-null.",
    "ENUM": "Values belong to the allowed set.",
    "FK_INTEGRITY": "Foreign-key values exist in the parent table.",
    "DUPLICATE": "Business keys / full rows are not duplicated.",
    "RELATIONSHIP": "Denormalised supplier/site/PO references agree across entities.",
    "SITE_CONFLATION": "A site ID/address belongs to exactly one supplier.",
    "DATE_ORDER": "Chronology between related dates holds.",
    "FUTURE_DATED": "No source timestamp after the extract date.",
    "EVENT_SEQUENCE": "Event histories follow legal lifecycle transitions in time order.",
    "LIFECYCLE_STATE": "Current status equals the latest event; derived dates match events.",
    "ARITHMETIC": "Derived amounts, quantities and statuses reconcile to source rows.",
    "SCORE_RECONCILIATION": "Score equals the sum of component contributions; tier/decision follow rules.",
    "SCORE_LEAKAGE": "No source data after score_timestamp was used.",
    "SCORE_REPRODUCIBILITY": "Re-scoring the source data at the same cutoff reproduces the score.",
}


# Forward-looking plan dates may legitimately fall after the extract date.
PLANNED_DATES = {"required_date", "end_date", "certification_expiry_date", "due_date"}


class Findings:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []

    def add(self, check: str, entity: str, record_ids, detail: str) -> None:
        for rid in pd.Series(record_ids, dtype=object).dropna().astype(str).unique():
            self.rows.append((check, entity, rid, detail))

    def frame(self) -> pd.DataFrame:
        df = pd.DataFrame(self.rows, columns=["check", "entity", "record_id", "detail"])
        return df.drop_duplicates(["check", "record_id", "detail"]).reset_index(drop=True)


def _check_generic(t: dict[str, pd.DataFrame], fx: Findings) -> None:
    for e in ENTITIES:
        df, pk = t[e.name], e.primary_key
        missing = [c for c in e.columns if c not in df.columns]
        if missing:
            fx.add("SCHEMA", e.name, [e.name], f"missing columns {missing}")
            continue
        fx.add("PK_UNIQUE", e.name, df.loc[df[pk].duplicated(keep="first"), pk], "duplicate primary key")
        if df[pk].isna().any():
            fx.add("PK_UNIQUE", e.name, [f"{e.name}:null-pk"], "null primary key")
        pattern = id_pattern(e.name)
        if pattern:
            bad = ~df[pk].astype(str).str.match(pattern)
            fx.add("ID_FORMAT", e.name, df.loc[bad, pk], f"does not match {pattern}")
        for f in e.fields:
            if f.required:
                fx.add("REQUIRED_FIELD", e.name, df.loc[df[f.name].isna(), pk], f"{f.name} is null")
            if f.allowed:
                bad = df[f.name].notna() & ~df[f.name].isin(f.allowed)
                fx.add("ENUM", e.name, df.loc[bad, pk], f"{f.name} not in allowed values")
        for col, parent, pcol in foreign_keys(e):
            bad = df[col].notna() & ~df[col].isin(t[parent][pcol])
            fx.add("FK_INTEGRITY", e.name, df.loc[bad, pk], f"{col} not found in {parent}.{pcol}")
        rest = [c for c in e.columns if c != pk]
        fx.add("DUPLICATE", e.name, df.loc[df.duplicated(rest, keep="first"), pk], "duplicate row (all non-key fields)")

    for name, keys in [("POLine", ["po_id", "line_number"]), ("ShipmentLine", ["shipment_id", "po_line_id"]),
                       ("Invoice", ["shipment_id"]), ("Inspection", ["shipment_id", "lot_number"]),
("DeliveryEvent", ["shipment_id", "event_seq"]),
                       ("POStatusEvent", ["po_id", "event_seq"]),
                       ("SupplierScore", ["supplier_id", "score_timestamp", "score_run_id"]),
                       ("SupplierScoreComponent", ["score_id", "component"])]:
        df = t[name]
        fx.add("DUPLICATE", name, df.loc[df.duplicated(keys, keep="first"), BY_NAME[name].primary_key], f"duplicate {keys}")


def _check_relationships(t: dict[str, pd.DataFrame], fx: Findings) -> None:
    site, con, po, sh = t["Site"], t["Contract"], t["PurchaseOrder"], t["Shipment"]
    embedded = site["site_id"].map(ids.supplier_of)
    fx.add("SITE_CONFLATION", "Site", site.loc[embedded != site["supplier_id"], "site_id"],
           "site_id embeds a different supplier than supplier_id")
    shared = site.groupby("address")["supplier_id"].transform("nunique") > 1
    fx.add("SITE_CONFLATION", "Site", site.loc[shared & site.duplicated("address", keep="first"), "site_id"],
           "address already registered to another supplier")

    owner = site.drop_duplicates("site_id").set_index("site_id")["supplier_id"]
    fx.add("RELATIONSHIP", "Contract", con.loc[con["site_id"].map(owner) != con["supplier_id"], "contract_id"],
           "contract site belongs to another supplier")
    c = con.set_index("contract_id")
    bad = ((po["site_id"].map(owner) != po["supplier_id"]) | (po["contract_id"].map(c["supplier_id"]) != po["supplier_id"])
           | (po["contract_id"].map(c["site_id"]) != po["site_id"]))
    fx.add("RELATIONSHIP", "PurchaseOrder", po.loc[bad, "po_id"], "PO supplier/site/contract disagree")
    p = po.set_index("po_id")
    has_po = sh["po_id"].isin(p.index)
    bad = has_po & ((sh["po_id"].map(p["supplier_id"]) != sh["supplier_id"]) | (sh["po_id"].map(p["site_id"]) != sh["site_id"]))
    fx.add("RELATIONSHIP", "Shipment", sh.loc[bad, "shipment_id"], "shipment supplier/site differ from PO")

    sl = t["ShipmentLine"].merge(sh[["shipment_id", "po_id"]], on="shipment_id", how="left") \
        .merge(t["POLine"][["po_line_id", "po_id"]].rename(columns={"po_id": "line_po"}), on="po_line_id", how="left")
    fx.add("RELATIONSHIP", "ShipmentLine", sl.loc[sl["po_id"] != sl["line_po"], "shipment_line_id"],
           "PO line belongs to a different PO than the shipment")
    s = sh.set_index("shipment_id")
    ins, inv = t["Inspection"], t["Invoice"]
    fx.add("RELATIONSHIP", "Inspection", ins.loc[ins["shipment_id"].map(s["supplier_id"]) != ins["supplier_id"], "inspection_id"],
           "inspection supplier differs from shipment")
    bad = (inv["shipment_id"].map(s["supplier_id"]) != inv["supplier_id"]) | (inv["shipment_id"].map(s["po_id"]) != inv["po_id"])
    fx.add("RELATIONSHIP", "Invoice", inv.loc[bad, "invoice_id"], "invoice supplier/PO differ from shipment")
    rs = t["RiskSignal"]
    fx.add("RELATIONSHIP", "RiskSignal", rs.loc[rs["risk_signal_id"].map(ids.supplier_of) != rs["supplier_id"], "risk_signal_id"],
           "risk_signal_id embeds a different supplier")


def _check_dates(t: dict[str, pd.DataFrame], fx: Findings) -> None:
    sup = t["Supplier"].set_index("supplier_id")
    site, con, po, sh = t["Site"], t["Contract"], t["PurchaseOrder"], t["Shipment"]

    def rule(entity, df, pk, mask, detail):
        fx.add("DATE_ORDER", entity, df.loc[mask.fillna(False).astype(bool), pk], detail)

    rule("Site", site, "site_id", site["effective_from"] < site["supplier_id"].map(sup["onboarding_date"]), "effective_from before supplier onboarding")
    rule("Contract", con, "contract_id", con["start_date"] >= con["end_date"], "start_date >= end_date")
    c = con.set_index("contract_id")
    rule("PurchaseOrder", po, "po_id", po["required_date"] <= po["order_date"], "required_date <= order_date")
    rule("PurchaseOrder", po, "po_id", (po["order_date"] < po["contract_id"].map(c["start_date"]))
         | (po["order_date"] > po["contract_id"].map(c["end_date"])), "order_date outside contract effective dates")
    p = po.set_index("po_id")
    rule("Shipment", sh, "shipment_id", sh["created_date"] < sh["po_id"].map(p["order_date"]), "created before PO order_date")
    rule("Shipment", sh, "shipment_id", sh["dispatch_date"] < sh["created_date"], "dispatch before created")
    rule("Shipment", sh, "shipment_id", sh["delivery_date"] < sh["dispatch_date"], "delivery_date precedes dispatch_date")
    rule("Shipment", sh, "shipment_id", sh["delivery_date"].notna() & sh["dispatch_date"].isna(), "delivered without dispatch")
    s = sh.set_index("shipment_id")
    ins, inv, rs = t["Inspection"], t["Invoice"], t["RiskSignal"]
    rule("Inspection", ins, "inspection_id", ins["inspection_date"] < ins["shipment_id"].map(s["delivery_date"]), "inspection before delivery")
    rule("Inspection", ins, "inspection_id", ins["shipment_id"].map(s["delivery_date"]).isna(), "inspection of undelivered shipment")
    rule("Invoice", inv, "invoice_id", inv["invoice_date"] < inv["shipment_id"].map(s["delivery_date"]), "invoice before delivery")
    rule("RiskSignal", rs, "risk_signal_id", rs["resolved_timestamp"] < rs["signal_timestamp"], "resolved before signalled")

    end = C.EXTRACT_DATE + pd.Timedelta(days=1)
    for e in ENTITIES:
        if e.kind == "derived":
            continue
        df = t[e.name]
        for f in e.fields:
            if f.dtype in ("DATE", "DATETIME") and f.name not in PLANNED_DATES:
                fx.add("FUTURE_DATED", e.name, df.loc[df[f.name] >= end, e.primary_key], f"{f.name} after extract date")


def _check_events(t: dict[str, pd.DataFrame], fx: Findings) -> None:
    def sequence(df, key, type_col, transitions):
        df = df.sort_values([key, "event_seq"], kind="mergesort")
        prev = df.groupby(key)[type_col].shift()
        prev_ts = df.groupby(key)["event_timestamp"].shift()
        legal = [cur in transitions.get(None if pd.isna(p) else p, set()) for p, cur in zip(prev, df[type_col])]
        bad = ~np.array(legal, dtype=bool) | (df["event_timestamp"] < prev_ts).to_numpy()
        return df, bad

    ev, bad = sequence(t["DeliveryEvent"], "shipment_id", "event_type", SHIPMENT_TRANSITIONS)
    fx.add("EVENT_SEQUENCE", "Shipment", ev.loc[bad, "shipment_id"], "illegal or out-of-order delivery event")
    sh = t["Shipment"]
    last = ev.groupby("shipment_id").last()
    derived = sh["shipment_id"].map(last["event_type"]).map(EVENT_TO_SHIPMENT_STATUS)
    fx.add("LIFECYCLE_STATE", "Shipment", sh.loc[derived != sh["status"], "shipment_id"], "status differs from latest event")
    first = ev.groupby(["shipment_id", "event_type"])["event_timestamp"].min().unstack()
    for col, ev_type in (("dispatch_date", "DISPATCHED"), ("delivery_date", "DELIVERED")):
        ev_date = sh["shipment_id"].map(first[ev_type]).dt.normalize() if ev_type in first else pd.Series(pd.NaT, index=sh.index)
        mismatch = ~((ev_date == sh[col]) | (ev_date.isna() & sh[col].isna()))
        fx.add("LIFECYCLE_STATE", "Shipment", sh.loc[mismatch, "shipment_id"], f"{col} differs from {ev_type} event")

    pe, bad = sequence(t["POStatusEvent"], "po_id", "status", PO_TRANSITIONS)
    fx.add("EVENT_SEQUENCE", "PurchaseOrder", pe.loc[bad, "po_id"], "illegal or out-of-order PO status event")
    po = t["PurchaseOrder"]
    fx.add("LIFECYCLE_STATE", "PurchaseOrder", po.loc[po["po_id"].map(pe.groupby("po_id")["status"].last()) != po["status"], "po_id"],
           "status differs from latest POStatusEvent")
    rs = t["RiskSignal"]
    expected = np.where(rs["resolved_timestamp"].notna(), "RESOLVED", "OPEN")
    fx.add("LIFECYCLE_STATE", "RiskSignal", rs.loc[rs["status"] != expected, "risk_signal_id"], "status inconsistent with resolved_timestamp")


def _check_arithmetic(t: dict[str, pd.DataFrame], fx: Findings) -> None:
    tol = 0.011
    ln, po, sh, sl = t["POLine"], t["PurchaseOrder"], t["Shipment"], t["ShipmentLine"]
    fx.add("ARITHMETIC", "POLine", ln.loc[((ln["ordered_qty"] * ln["unit_price"]).round(2) - ln["line_total"]).abs() > tol, "po_line_id"],
           "line_total != ordered_qty x unit_price")
    g = ln.groupby("po_id")
    bad = ((po["po_id"].map(g["line_total"].sum()) - po["total_amount"]).abs() > tol) | (po["po_id"].map(g.size()) != po["line_count"])
    fx.add("ARITHMETIC", "PurchaseOrder", po.loc[bad, "po_id"], "total_amount/line_count do not reconcile to POLine")

    shipped = sl.groupby("shipment_id")["shipped_qty"].sum()
    fx.add("ARITHMETIC", "Shipment", sh.loc[sh["shipment_id"].map(shipped).fillna(0) != sh["shipped_qty_total"], "shipment_id"],
           "shipped_qty_total != sum(ShipmentLine.shipped_qty)")
    fx.add("ARITHMETIC", "ShipmentLine", sl.loc[sl["shipped_qty"] <= 0, "shipment_line_id"], "shipped_qty must be positive")
    per_line = sl.groupby("po_line_id")["shipped_qty"].sum()
    over = ln["po_line_id"].map(per_line).fillna(0) > ln["ordered_qty"]
    fx.add("ARITHMETIC", "POLine", ln.loc[over, "po_line_id"], "shipped more than ordered")
    received = po["status"].isin(["RECEIVED", "CLOSED"])
    undelivered = sh[sh["delivery_date"].isna() & sh["po_id"].notna()].groupby("po_id").size()
    fx.add("ARITHMETIC", "PurchaseOrder", po.loc[received & po["po_id"].isin(undelivered.index), "po_id"],
           "PO RECEIVED/CLOSED with undelivered shipments")
    short = ln["po_id"].isin(po.loc[received, "po_id"]) & (ln["po_line_id"].map(per_line).fillna(0) != ln["ordered_qty"])
    fx.add("ARITHMETIC", "PurchaseOrder", ln.loc[short, "po_id"], "PO RECEIVED/CLOSED but line not fully shipped")

    ins = t["Inspection"]
    bad = ((ins["accepted_qty"] + ins["rejected_qty"]) != ins["inspected_qty"]) | (ins["inspected_qty"] <= 0) | (ins["rejected_qty"] < 0)
    fx.add("ARITHMETIC", "Inspection", ins.loc[bad, "inspection_id"], "accepted + rejected != inspected")
    fx.add("ARITHMETIC", "Inspection", ins.loc[ins["inspected_qty"] > ins["shipment_id"].map(sh.set_index("shipment_id")["shipped_qty_total"]), "inspection_id"],
           "inspected more than shipped")
    rate = ins["rejected_qty"] / ins["inspected_qty"]
    expected = np.select([ins["rejected_qty"] == 0, rate <= C.INSPECTION_DEVIATION_LIMIT], ["ACCEPTED", "ACCEPTED_WITH_DEVIATION"], "REJECTED")
    bad = (ins["result"] != expected) | ((ins["defect_code"] == "NONE") != (ins["rejected_qty"] == 0))
    fx.add("ARITHMETIC", "Inspection", ins.loc[bad, "inspection_id"], "result/defect_code inconsistent with rejected_qty")

    inv = t["Invoice"]
    amt = sl.merge(ln[["po_line_id", "unit_price"]], on="po_line_id")
    expected_amt = (amt["shipped_qty"] * amt["unit_price"]).groupby(amt["shipment_id"]).sum().round(2)
    fx.add("ARITHMETIC", "Invoice", inv.loc[(inv["shipment_id"].map(expected_amt) - inv["expected_amount"]).abs() > tol, "invoice_id"],
           "expected_amount != sum(shipped_qty x unit_price)")
    fx.add("ARITHMETIC", "Invoice", inv.loc[(inv["invoiced_amount"] - inv["expected_amount"] - inv["variance_amount"]).abs() > tol, "invoice_id"],
           "variance_amount != invoiced - expected")
    fx.add("ARITHMETIC", "Invoice", inv.loc[((inv["variance_amount"] / inv["expected_amount"] * 100).round(3) - inv["variance_pct"]).abs() > 0.0011, "invoice_id"],
           "variance_pct != variance / expected x 100")
    tol_pct = inv["po_id"].map(po.set_index("po_id")["contract_id"]).map(t["Contract"].set_index("contract_id")["price_tolerance_pct"])
    expected_match = np.where(inv["variance_pct"].abs() <= tol_pct, "MATCHED", "PRICE_VARIANCE")
    fx.add("ARITHMETIC", "Invoice", inv.loc[inv["match_status"] != expected_match, "invoice_id"], "match_status inconsistent with tolerance")
    terms = inv["po_id"].map(po.set_index("po_id")["contract_id"]).map(t["Contract"].set_index("contract_id")["payment_terms_days"])
    due = inv["invoice_date"] + pd.to_timedelta(terms.astype("float64"), unit="D")
    fx.add("ARITHMETIC", "Invoice", inv.loc[due != inv["due_date"], "invoice_id"], "due_date != invoice_date + payment terms")


def _check_scores(t: dict[str, pd.DataFrame], fx: Findings) -> None:
    sc, cp = t["SupplierScore"], t["SupplierScoreComponent"]
    comps = list(C.WEIGHTS)
    g = cp.groupby("score_id")
    fx.add("SCORE_RECONCILIATION", "SupplierScore", sc.loc[sc["score_id"].map(g.size()) != len(comps), "score_id"], "score lacks six components")
    total = sc["score_id"].map(g["weighted_contribution"].sum())
    bad = sc["reliability_score"].notna() & ((total - sc["reliability_score"]).abs() > 0.011)
    fx.add("SCORE_RECONCILIATION", "SupplierScore", sc.loc[bad, "score_id"], "reliability_score != sum(weighted_contribution)")
    ok = cp[cp["status"] == "OK"]
    eff = ok.groupby("score_id")["effective_weight"].sum()
    fx.add("SCORE_RECONCILIATION", "SupplierScore", eff.index[(eff - 1).abs() > 1e-4], "effective weights do not sum to 1")
    cov = (ok.groupby("score_id")["base_weight"].sum() * 100).round(2)
    fx.add("SCORE_RECONCILIATION", "SupplierScore", sc.loc[(sc["score_id"].map(cov).fillna(0) - sc["data_coverage_pct"]).abs() > 0.011, "score_id"],
           "data_coverage_pct != sum of available weights")
    wide = cp.pivot(index="score_id", columns="component", values="component_score")
    for comp in comps:
        a, b = sc[f"{comp}_score"], sc["score_id"].map(wide[comp])
        fx.add("SCORE_RECONCILIATION", "SupplierScore", sc.loc[~((a - b).abs() <= 0.005) & ~(a.isna() & b.isna()), "score_id"],
               f"{comp}_score differs from component row")
    n_ok = sc["score_id"].map(ok.groupby("score_id").size()).fillna(0)
    insufficient = (sc["data_coverage_pct"] < C.MIN_COVERAGE_PCT) | (n_ok < C.MIN_COMPONENTS)
    tier = np.select([insufficient, sc["reliability_score"] >= C.TIER_HIGH, sc["reliability_score"] >= C.TIER_MEDIUM],
                     ["INSUFFICIENT_DATA", "HIGH", "MEDIUM"], "LOW")
    decision = np.select([insufficient, (tier == "LOW") | (sc["open_high_risks"] > 0), tier == "MEDIUM"],
                         ["REVIEW_DATA", "ESCALATE", "MONITOR"], "APPROVED")
    fx.add("SCORE_RECONCILIATION", "SupplierScore", sc.loc[(sc["reliability_tier"] != tier) | (sc["decision"] != decision), "score_id"],
           "tier/decision inconsistent with documented thresholds")

    fx.add("SCORE_LEAKAGE", "SupplierScore", sc.loc[sc["max_source_date"] > sc["score_timestamp"], "score_id"],
           "max_source_date after score_timestamp")
    fx.add("SCORE_LEAKAGE", "SupplierScore", sc.loc[sc["score_timestamp"] > C.EXTRACT_DATE, "score_id"], "score_timestamp after extract date")

    clean, _ = quarantine(t)
    cols = [f"{c}_score" for c in comps] + ["reliability_score", "data_coverage_pct"]
    for cutoff in sorted(sc["score_timestamp"].dropna().unique()):
        recomputed, _ = score_snapshot(clean, pd.Timestamp(cutoff))
        rows = sc[sc["score_timestamp"] == cutoff]
        merged = rows.merge(recomputed[["supplier_id"] + cols], on="supplier_id", how="left", suffixes=("", "_re"))
        diff = np.zeros(len(merged), dtype=bool)
        for c in cols:
            a, b = merged[c], merged[f"{c}_re"]
            diff |= ~(((a - b).abs() <= 0.011) | (a.isna() & b.isna())).to_numpy()
        fx.add("SCORE_REPRODUCIBILITY", "SupplierScore", merged.loc[diff, "score_id"],
               f"re-scoring at {pd.Timestamp(cutoff).date()} gives a different result")


def validate_data(data_dir: str | Path, write_report: bool = True, verbose: bool = True) -> dict:
    data_dir = Path(data_dir)
    t = load_tables(data_dir)
    gt, _ = load_ground_truth(data_dir)
    fx = Findings()
    _check_generic(t, fx)
    _check_relationships(t, fx)
    _check_dates(t, fx)
    _check_events(t, fx)
    _check_arithmetic(t, fx)
    _check_scores(t, fx)
    findings = fx.frame()

    gt_ids = set(gt["record_id"]) if gt is not None else set()
    findings["expected"] = findings["record_id"].isin(gt_ids)
    unexpected = findings[~findings["expected"]]
    detection = {}
    if gt is not None:
        hit = findings[findings["expected"]].groupby("record_id")["check"].apply(lambda s: sorted(set(s)))
        for issue, grp in gt.groupby("issue_type", sort=False):
            found = grp["record_id"].isin(hit.index)
            checks = sorted({c for r in grp["record_id"] if r in hit.index for c in hit[r]})
            detection[issue] = {"injected": int(len(grp)), "detected": int(found.sum()),
                                "recall": round(float(found.mean()), 4), "detected_by": checks,
                                "missed": grp.loc[~found, "record_id"].tolist()}
    all_detected = all(d["detected"] == d["injected"] for d in detection.values())
    status = "PASS" if unexpected.empty and all_detected else "FAIL"

    per_check = {}
    for check, desc in CHECKS.items():
        sub = findings[findings["check"] == check]
        per_check[check] = {"description": desc, "findings": int(len(sub)),
                            "on_ground_truth_records": int(sub["expected"].sum()),
                            "unexpected": int((~sub["expected"]).sum())}
    report = {
        "status": status,
        "rule": "PASS = zero findings outside ground truth AND every injected anomaly detected",
        "row_counts": {name: int(len(df)) for name, df in t.items()},
        "checks": per_check,
        "anomaly_detection": detection,
        "unexpected_findings": unexpected.head(50).drop(columns="expected").to_dict("records"),
        "unexpected_total": int(len(unexpected)),
    }
    if write_report:
        write_json(data_dir / "validation_summary.json", report)
        findings.to_csv(data_dir / "outputs" / "validation_findings.csv", index=False, lineterminator="\n")
    if verbose:
        print("\nVALIDATION SUMMARY")
        for check, r in per_check.items():
            flag = "ok " if r["unexpected"] == 0 else "ERR"
            print(f"  [{flag}] {check:<22} findings={r['findings']:>5}  on-ground-truth={r['on_ground_truth_records']:>4}  unexpected={r['unexpected']}")
        for issue, d in detection.items():
            print(f"  anomaly {issue:<26} detected {d['detected']}/{d['injected']} via {', '.join(d['detected_by'])}")
        print(f"Validation {status}.")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate generated supplier reliability data.")
    parser.add_argument("--data-dir", default="data/full")
    args = parser.parse_args()
    report = validate_data(args.data_dir)
    raise SystemExit(0 if report["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
