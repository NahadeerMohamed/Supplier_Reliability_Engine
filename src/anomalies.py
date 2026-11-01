"""Inject labelled edge cases into a valid baseline and record ground truth.

Anomalies are either NEW rows (fresh IDs continuing the counters) or targeted
MUTATED rows whose original value is preserved in ground truth. Unrelated rows
are never touched. Ground truth is written to ground_truth/ only.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config as C
from . import ids
from .schema import BY_NAME
from .scoring import assign_ids, quarantine, score_snapshot

EXPECTED = {
    "SHIPMENT_NO_PO": ("REQUIRED_FIELD|FK_INTEGRITY",
                       "QUARANTINE shipment (SHP_NO_PO); exclude from delivery metric; analyst matches to a PO or rejects receipt"),
    "DELIVERY_BEFORE_DISPATCH": ("DATE_ORDER|EVENT_SEQUENCE",
                                 "QUARANTINE shipment (SHP_DATE_ORDER); exclude from delivery metric; carrier/warehouse corrects the event"),
    "RISK_NO_SOURCE_OR_TIME": ("REQUIRED_FIELD",
                               "QUARANTINE risk signal (RSK_PROVENANCE); exclude from disruption score until provenance supplied"),
    "SITE_CONFLATION": ("SITE_CONFLATION",
                        "BLOCK site (SITE_CONFLATION); excluded from scoring; master-data owner separates the suppliers' sites"),
    "SCORE_USES_FUTURE_DATA": ("SCORE_LEAKAGE|SCORE_REPRODUCIBILITY",
                               "REJECT score; fails point-in-time audit; rerun with the correct cutoff"),
}


def _gt(issue, entity, record_id, supplier, method, field, original, injected) -> dict:
    detection, decision = EXPECTED[issue]
    return {"issue_type": issue, "entity": entity, "record_id": record_id, "supplier_id": supplier,
            "injection_method": method, "field": field,
            "original_value": None if original is None else str(original),
            "injected_value": None if injected is None else str(injected),
            "expected_detection": detection, "expected_decision": decision}


def _next_ids(frame: pd.DataFrame, entity: str, count: int) -> list[str]:
    prefix, width = ids.COUNTERS[entity]
    return ids.sequence(prefix, ids.max_counter(frame[BY_NAME[entity].primary_key]) + 1, count, width)


def inject_source_anomalies(tables: dict[str, pd.DataFrame], k: int, seed: int) -> tuple[dict[str, pd.DataFrame], list[dict]]:
    rng = np.random.default_rng(seed + 7)
    t = {name: df.copy() for name, df in tables.items()}
    gt: list[dict] = []
    sh, ev = t["Shipment"], t["DeliveryEvent"]

    delivered = sh[sh["status"] == "DELIVERED"]
    delayed_ids = set(ev.loc[ev["event_type"] == "DELAYED", "shipment_id"])
    picks = rng.choice(len(delivered), 2 * k, replace=False)
    templates = delivered.iloc[picks[:k]]
    mutate_pool = delivered[~delivered["shipment_id"].isin(delayed_ids) & ~delivered["shipment_id"].isin(templates["shipment_id"])]
    mutate = mutate_pool.iloc[np.sort(rng.choice(len(mutate_pool), k, replace=False))]

    # 1) SHIPMENT_NO_PO: new shipments with a null or non-existent PO reference.
    new_sh = templates.copy()
    new_sh["shipment_id"] = _next_ids(sh, "Shipment", k)
    new_sh["po_id"] = [None if i % 2 == 0 else f"PO-2026-{900001 + i:06d}" for i in range(k)]
    new_sh["shipped_qty_total"] = 0
    new_ev = ev[ev["shipment_id"].isin(templates["shipment_id"])].copy()
    new_ev["shipment_id"] = new_ev["shipment_id"].map(dict(zip(templates["shipment_id"], new_sh["shipment_id"])))
    new_ev = new_ev.sort_values(["shipment_id", "event_seq"], kind="mergesort")
    new_ev["delivery_event_id"] = _next_ids(ev, "DeliveryEvent", len(new_ev))
    for row in new_sh.itertuples():
        gt.append(_gt("SHIPMENT_NO_PO", "Shipment", row.shipment_id, row.supplier_id, "NEW_ROW",
                      "po_id", None, row.po_id if row.po_id else "NULL"))

    # 2) DELIVERY_BEFORE_DISPATCH: mutate delivery date + DELIVERED event to precede dispatch.
    shift = pd.to_timedelta(rng.integers(1, 6, k), unit="D")
    new_delivery = mutate["dispatch_date"].to_numpy() - shift.to_numpy()
    sh_idx = sh.index[sh["shipment_id"].isin(mutate["shipment_id"])]
    sh.loc[sh_idx, "delivery_date"] = pd.Series(new_delivery, index=mutate.index).reindex(sh_idx).to_numpy()
    by_ship = dict(zip(mutate["shipment_id"], new_delivery))
    ev_idx = ev.index[ev["shipment_id"].isin(mutate["shipment_id"]) & (ev["event_type"] == "DELIVERED")]
    for i in ev_idx:
        old = ev.at[i, "event_timestamp"]
        ev.at[i, "event_timestamp"] = pd.Timestamp(by_ship[ev.at[i, "shipment_id"]]) + (old - old.normalize())
    for row, nd in zip(mutate.itertuples(), new_delivery):
        gt.append(_gt("DELIVERY_BEFORE_DISPATCH", "Shipment", row.shipment_id, row.supplier_id, "MUTATED_ROW",
                      "delivery_date;DeliveryEvent[DELIVERED].event_timestamp",
                      row.delivery_date.date(), pd.Timestamp(nd).date()))

    t["Shipment"] = pd.concat([sh, new_sh], ignore_index=True)
    t["DeliveryEvent"] = pd.concat([ev, new_ev], ignore_index=True)

    # 3) RISK_NO_SOURCE_OR_TIME: new signals missing source, timestamp, or both.
    rs = t["RiskSignal"]
    sup_ids = t["Supplier"]["supplier_id"].to_numpy()
    rs_sup = np.sort(rng.choice(sup_ids, k, replace=False))
    seq_max = rs.groupby("supplier_id")["risk_signal_id"].apply(lambda s: ids.max_counter(s))
    secs = rng.integers(0, ((C.EXTRACT_DATE - C.OBSERVATION_START).days + 1) * 86400, k)
    new_rs = pd.DataFrame({
        "risk_signal_id": [ids.risk_signal_id(s, int(seq_max.get(s, 0)) + 1) for s in rs_sup],
        "supplier_id": rs_sup,
        "risk_type": rng.choice(["PORT_CONGESTION", "FINANCIAL_DISTRESS", "LABOR_STRIKE"], k),
        "severity": rng.choice(["MEDIUM", "HIGH"], k),
        "signal_timestamp": [pd.NaT if i % 3 != 0 else C.OBSERVATION_START + pd.Timedelta(seconds=int(s))
                             for i, s in enumerate(secs)],
        "source": [None if i % 3 != 1 else "NEWS_FEED" for i in range(k)],
        "resolved_timestamp": pd.NaT,
        "status": "OPEN",
    })
    new_rs["signal_timestamp"] = pd.to_datetime(new_rs["signal_timestamp"])
    new_rs["resolved_timestamp"] = pd.to_datetime(new_rs["resolved_timestamp"])
    for row in new_rs.itertuples():
        missing = [f for f, v in (("source", row.source), ("signal_timestamp", row.signal_timestamp)) if pd.isna(v)]
        gt.append(_gt("RISK_NO_SOURCE_OR_TIME", "RiskSignal", row.risk_signal_id, row.supplier_id, "NEW_ROW",
                      ";".join(missing), None, "NULL"))
    t["RiskSignal"] = pd.concat([rs, new_rs], ignore_index=True)

    # 4) SITE_CONFLATION: supplier A's physical site re-registered under supplier B.
    site = t["Site"]
    first_sites = site[site["site_seq"] == 1]
    pair = rng.choice(len(sup_ids), (k, 2), replace=False) if 2 * k <= len(sup_ids) else \
        np.stack([rng.permutation(len(sup_ids))[:2] for _ in range(k)])
    seq_next = site.groupby("supplier_id")["site_seq"].max()
    rows = []
    for a, b in pair:
        a_id, b_id = sup_ids[a], sup_ids[b]
        src = first_sites[first_sites["supplier_id"] == a_id].iloc[0]
        seq_next[a_id] += 1
        rows.append({**src.to_dict(), "site_id": ids.site_id(a_id, int(seq_next[a_id])),
                     "supplier_id": b_id, "site_seq": int(seq_next[a_id])})
    new_site = pd.DataFrame(rows)
    for row, (a, _) in zip(new_site.itertuples(), pair):
        gt.append(_gt("SITE_CONFLATION", "Site", row.site_id, row.supplier_id, "NEW_ROW",
                      "supplier_id;address", sup_ids[a], f"{row.supplier_id} @ {row.address}"))
    t["Site"] = pd.concat([site, new_site], ignore_index=True)
    return t, gt


def inject_score_leakage(tables: dict[str, pd.DataFrame], scores: pd.DataFrame, comps: pd.DataFrame,
                         k: int, seed: int) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    """Append ad-hoc backfill scores computed with data after their cutoff."""
    rng = np.random.default_rng(seed + 11)
    cutoff = C.SCORE_CUTOFFS[0]
    clean, _ = quarantine(tables)
    honest, _ = score_snapshot(clean, cutoff)
    leaky, leaky_comps = score_snapshot(clean, cutoff, data_cutoff=C.EXTRACT_DATE)
    diff = (leaky["reliability_score"] - honest["reliability_score"]).abs()
    eligible = np.flatnonzero((diff >= 1.0).to_numpy() & (leaky["max_source_date"] > cutoff).to_numpy())
    chosen = np.sort(rng.choice(eligible, min(k, len(eligible)), replace=False))
    leaky = leaky.iloc[chosen].reset_index(drop=True)
    leaky_comps = leaky_comps[leaky_comps["supplier_id"].isin(leaky["supplier_id"])]
    new_scores, new_comps = assign_ids(
        leaky, leaky_comps, f"ADHOC-{cutoff.date()}-BACKFILL",
        ids.max_counter(scores["score_id"]) + 1, ids.max_counter(comps["score_component_id"]) + 1)
    gt = [_gt("SCORE_USES_FUTURE_DATA", "SupplierScore", r.score_id, r.supplier_id, "NEW_ROW",
              "max_source_date", None, f"{pd.Timestamp(r.max_source_date).date()} > {cutoff.date()}")
          for r in new_scores.itertuples()]
    return (pd.concat([scores, new_scores], ignore_index=True),
            pd.concat([comps, new_comps], ignore_index=True), gt)


def ground_truth_frame(rows: list[dict]) -> pd.DataFrame:
    order = {t: i for i, t in enumerate(EXPECTED)}
    df = pd.DataFrame(rows)
    df = df.assign(_o=df["issue_type"].map(order)).sort_values(["_o", "record_id"], kind="mergesort").drop(columns="_o")
    prefix, width = ids.COUNTERS["GroundTruth"]
    df.insert(0, "gt_id", ids.sequence(prefix, 1, len(df), width))
    return df.reset_index(drop=True)
