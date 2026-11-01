"""Deterministic synthetic baseline generator (clean records only).

Order of generation: Supplier -> Site -> Contract -> PurchaseOrder -> POLine ->
Shipment -> ShipmentLine -> DeliveryEvent -> Inspection -> Invoice ->
POStatusEvent -> RiskSignal. Every child row is built from valid parent IDs and
every derived value (totals, statuses, variances) is computed from source rows.

Dates are handled internally as integer day offsets from OBSERVATION_START.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config as C
from . import ids
from .config import Scale
from .schema import CATEGORIES, COUNTRIES, DEFECT_CODES, RISK_SOURCES, RISK_TYPES, SITE_TYPES

BASE = C.OBSERVATION_START
EXTRACT_DAY = (C.EXTRACT_DATE - BASE).days
LAST_ORDER_DAY = (C.LAST_ORDER_DATE - BASE).days

CITIES = {
    "IN": ["Chennai", "Pune", "Bengaluru"], "US": ["Columbus", "Reno", "Austin"],
    "DE": ["Stuttgart", "Leipzig", "Bremen"], "SG": ["Jurong", "Tuas", "Woodlands"],
    "VN": ["Haiphong", "Bac Ninh", "Binh Duong"], "MX": ["Monterrey", "Queretaro", "Tijuana"],
    "CN": ["Suzhou", "Dongguan", "Ningbo"], "PL": ["Poznan", "Wroclaw", "Lodz"],
}

# Profile-specific generation parameters (documented in docs/SCORING.md and
# echoed per supplier in ground_truth/supplier_profiles.csv).
PO_WEIGHT = {"STABLE": 1.0, "DETERIORATING": 1.0, "QUALITY_INCIDENT": 1.0, "SPARSE": 0.12, "CAPACITY_STRAINED": 2.5}
DELAY_MEAN = {"STABLE": -3.0, "DETERIORATING": -3.0, "QUALITY_INCIDENT": -2.0, "SPARSE": -2.5, "CAPACITY_STRAINED": -1.0}
DETERIORATION_DAYS_PER_MONTH = 1.3
DELAY_SD = 2.5
REJECT_P = {"STABLE": 0.008, "DETERIORATING": 0.02, "QUALITY_INCIDENT": 0.02, "SPARSE": 0.015, "CAPACITY_STRAINED": 0.03}
INCIDENT_REJECT_P = 0.22
INVOICE_VARIANCE_P = {"STABLE": 0.08, "DETERIORATING": 0.20, "QUALITY_INCIDENT": 0.20, "SPARSE": 0.15, "CAPACITY_STRAINED": 0.25}
RISK_RATE = {"STABLE": 0.8, "DETERIORATING": 2.5, "QUALITY_INCIDENT": 1.5, "SPARSE": 0.6, "CAPACITY_STRAINED": 2.0}
CERT_LAPSE_P = {"STABLE": 0.03, "DETERIORATING": 0.15, "QUALITY_INCIDENT": 0.15, "SPARSE": 0.10, "CAPACITY_STRAINED": 0.15}
AUDIT_MEAN = {"STABLE": 90.0, "DETERIORATING": 80.0, "QUALITY_INCIDENT": 76.0, "SPARSE": 82.0, "CAPACITY_STRAINED": 80.0}
SPARSE_INSPECTION_WEIGHT = 0.1
SPARSE_INVOICE_DROP_P = 0.65
SPARSE_AUDIT_MISSING_P = 0.60


def day(ts: str) -> int:
    return (pd.Timestamp(ts) - BASE).days


def to_date(days: np.ndarray) -> pd.Series:
    return pd.Series(BASE + pd.to_timedelta(np.asarray(days, dtype="float64"), unit="D"))


def to_ts(days: np.ndarray, minutes: np.ndarray) -> pd.Series:
    return pd.Series(BASE + pd.to_timedelta(np.asarray(days, dtype="int64") * 1440 + np.asarray(minutes, dtype="int64"), unit="min"))


def _map(values: np.ndarray, table: dict) -> np.ndarray:
    return np.array([table[v] for v in values])


def _group_starts(counts: np.ndarray) -> np.ndarray:
    return np.concatenate([[0], np.cumsum(counts)[:-1]]).astype(int)


def generate_baseline(scale: Scale, seed: int = C.SEED) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Return (canonical tables, hidden supplier profile labels)."""
    if min(scale.suppliers, scale.pos, scale.shipments, scale.inspections) < 1:
        raise ValueError("All entity counts must be positive integers.")
    rng = np.random.default_rng(seed)

    # ---------------- Supplier ----------------
    n = scale.suppliers
    sup_ids = np.array([ids.supplier_id(i) for i in range(1, n + 1)])
    profile = rng.choice(C.PROFILES, n, p=C.PROFILE_PROBS)
    sup_country = rng.choice(COUNTRIES, n)
    onboarding = rng.integers(day("2015-01-01"), day("2025-06-30") + 1, n)
    suppliers = pd.DataFrame({
        "supplier_id": sup_ids,
        "supplier_name": [f"Synthetic Supplier {i:06d}" for i in range(1, n + 1)],
        "category": rng.choice(CATEGORIES, n),
        "country": sup_country,
        "supplier_tier": rng.choice(["STRATEGIC", "PREFERRED", "APPROVED"], n, p=[0.15, 0.35, 0.50]),
        "onboarding_date": to_date(onboarding),
        "status": rng.choice(["ACTIVE", "ON_HOLD"], n, p=[0.95, 0.05]),
    })

    # ---------------- Site (1-3 per supplier) ----------------
    n_sites = rng.choice([1, 2, 3], n, p=[0.6, 0.3, 0.1])
    site_start = _group_starts(n_sites)
    site_sup = np.repeat(np.arange(n), n_sites)
    m = len(site_sup)
    site_seq = np.arange(m) - site_start[site_sup] + 1
    site_country = np.where(site_seq == 1, sup_country[site_sup], rng.choice(COUNTRIES, m))
    city_pick = rng.integers(0, 3, m)
    cities = [CITIES[c][k] for c, k in zip(site_country, city_pick)]
    effective_from = np.minimum(onboarding[site_sup] + rng.integers(0, 365, m), day("2025-12-01"))
    site_ids = np.array([ids.site_id(sup_ids[s], q) for s, q in zip(site_sup, site_seq)])
    site_type = np.where(site_seq == 1, "MANUFACTURING", rng.choice(SITE_TYPES, m))
    sites = pd.DataFrame({
        "site_id": site_ids,
        "supplier_id": sup_ids[site_sup],
        "site_seq": site_seq,
        "site_name": [f"{city} {t.title()} {q:02d}" for city, t, q in zip(cities, site_type, site_seq)],
        "site_type": site_type,
        "country": site_country,
        "address": [f"Unit {k + 1:05d}, {city} Industrial Estate, {c}" for k, (city, c) in enumerate(zip(cities, site_country))],
        "effective_from": to_date(effective_from),
        "status": "ACTIVE",
    })

    # ---------------- Contract (one per site) ----------------
    site_profile = profile[site_sup]
    strained_site = site_profile == "CAPACITY_STRAINED"
    contract_start = np.minimum(effective_from + rng.integers(0, 31, m), day("2025-12-31"))
    contract_end = rng.choice([day("2027-12-31"), day("2028-06-30")], m)
    capacity = np.where(strained_site, rng.integers(120, 301, m), rng.integers(1500, 4001, m))
    lapsed = rng.random(m) < _map(site_profile, CERT_LAPSE_P)
    cert_expiry = np.where(lapsed, rng.integers(day("2026-02-01"), day("2026-10-15") + 1, m),
                           rng.integers(day("2027-01-15"), day("2028-12-31") + 1, m))
    audit_score = np.clip(rng.normal(_map(site_profile, AUDIT_MEAN), 6.0), 40, 100).round(1)
    audit_date = rng.integers(day("2025-01-15"), day("2025-12-15") + 1, m).astype("float64")
    no_audit = (site_profile == "SPARSE") & (rng.random(m) < SPARSE_AUDIT_MISSING_P)
    audit_score = np.where(no_audit, np.nan, audit_score)
    audit_date[no_audit] = np.nan
    payment_terms = rng.choice([30, 45, 60], m)
    contracts = pd.DataFrame({
        "contract_id": ids.sequence("CON-2026", 1, m, 6),
        "supplier_id": sup_ids[site_sup],
        "site_id": site_ids,
        "start_date": to_date(contract_start),
        "end_date": to_date(contract_end),
        "currency": "USD",
        "payment_terms_days": payment_terms,
        "price_tolerance_pct": C.INVOICE_TOLERANCE_PCT,
        "capacity_units_month": capacity,
        "certification_type": "ISO9001",
        "certification_expiry_date": to_date(cert_expiry),
        "last_audit_date": to_date(audit_date),
        "last_audit_score": audit_score,
        "status": "ACTIVE",
    })

    # ---------------- PurchaseOrder ----------------
    n_pos = scale.pos
    w = _map(profile, PO_WEIGHT)
    po_sup = rng.choice(n, n_pos, p=w / w.sum())
    po_site = site_start[po_sup] + (rng.random(n_pos) * n_sites[po_sup]).astype(int)
    order_day = rng.integers(0, LAST_ORDER_DAY + 1, n_pos)
    lead = rng.integers(10, 31, n_pos)
    planner = rng.integers(1, 21, n_pos)
    order = np.lexsort((po_site, order_day))           # chronological PO numbering
    po_sup, po_site, order_day, lead, planner = (a[order] for a in (po_sup, po_site, order_day, lead, planner))
    required_day = order_day + lead
    po_ids = np.array(ids.sequence("PO-2026", 1, n_pos, 6))

    # ---------------- POLine ----------------
    n_lines = rng.choice([1, 2, 3, 4], n_pos, p=[0.4, 0.3, 0.2, 0.1])
    line_po = np.repeat(np.arange(n_pos), n_lines)
    L = len(line_po)
    line_number = np.arange(L) - _group_starts(n_lines)[line_po] + 1
    strained_line = profile[po_sup[line_po]] == "CAPACITY_STRAINED"
    ordered_qty = np.where(strained_line, rng.integers(150, 601, L), rng.integers(20, 401, L))
    unit_price = rng.uniform(2, 250, L).round(2)
    line_total = (ordered_qty * unit_price).round(2)
    po_line_ids = np.array(ids.sequence("POL-2026", 1, L, 7))
    polines = pd.DataFrame({
        "po_line_id": po_line_ids,
        "po_id": po_ids[line_po],
        "line_number": line_number,
        "item_id": [f"ITEM-{i:05d}" for i in rng.integers(1, 5001, L)],
        "ordered_qty": ordered_qty,
        "unit_price": unit_price,
        "line_total": line_total,
    })

    # ---------------- Shipment ----------------
    n_ship = scale.shipments
    if n_ship >= n_pos:
        ship_cnt = 1 + np.bincount(rng.integers(0, n_pos, n_ship - n_pos), minlength=n_pos)
    else:
        ship_cnt = np.zeros(n_pos, dtype=int)
        ship_cnt[rng.choice(n_pos, n_ship, replace=False)] = 1
    ship_start = _group_starts(ship_cnt)
    sh_po = np.repeat(np.arange(n_pos), ship_cnt)
    S = len(sh_po)
    k = np.arange(S) - ship_start[sh_po]                # split-delivery index within PO
    sh_prof = profile[po_sup[sh_po]]
    month_idx = order_day[sh_po] // 30
    delay_mean = _map(sh_prof, DELAY_MEAN) + np.where(sh_prof == "DETERIORATING", DETERIORATION_DAYS_PER_MONTH * month_idx, 0.0)
    delay = np.rint(rng.normal(delay_mean, DELAY_SD)).astype(int) + k * rng.integers(0, 4, S)
    planned_delivery = required_day[sh_po] + delay
    transit = rng.integers(2, 9, S)
    created_day = order_day[sh_po] + 1
    dispatch_day = np.maximum(planned_delivery - transit, created_day + k)
    delivery_day = np.maximum(planned_delivery, dispatch_day + 1)
    dispatched = dispatch_day <= EXTRACT_DAY
    delivered = delivery_day <= EXTRACT_DAY
    delayed_day = np.maximum(required_day[sh_po] + 1, dispatch_day)
    has_delay_event = dispatched & (delivery_day > required_day[sh_po]) & (delayed_day <= EXTRACT_DAY)
    status = np.select([delivered, has_delay_event, dispatched], ["DELIVERED", "DELAYED", "IN_TRANSIT"], "CREATED")
    shipment_ids = np.array(ids.sequence("SHP-2026", 1, S, 6))
    carrier = np.array([f"CARRIER-{i:02d}" for i in rng.integers(1, 7, S)])
    warehouse = np.array([f"WH-{i:02d}" for i in rng.integers(1, 6, S)])

    # ---------------- ShipmentLine (bridge Shipment N:M POLine) ----------------
    rep = ship_cnt[line_po]
    bl_line = np.repeat(np.arange(L), rep)
    bl_off = np.arange(len(bl_line)) - _group_starts(rep)[bl_line]
    bl_ship = ship_start[line_po[bl_line]] + bl_off
    K = rep[bl_line]
    if (ordered_qty < rep).any():
        raise ValueError("Ordered quantity smaller than shipment split count.")
    wts = rng.uniform(0.5, 1.5, len(bl_line))
    wsum = np.bincount(bl_line, wts)[bl_line]
    part = 1 + np.floor((ordered_qty[bl_line] - K) * wts / wsum).astype(int)
    remainder = ordered_qty - np.bincount(bl_line, part, minlength=L).astype(int)
    last = bl_off == K - 1
    part[last] += remainder[bl_line[last]]
    bl_order = np.lexsort((bl_line, bl_ship))
    bl_line, bl_ship, part = bl_line[bl_order], bl_ship[bl_order], part[bl_order]
    shipment_lines = pd.DataFrame({
        "shipment_line_id": ids.sequence("SHL-2026", 1, len(bl_line), 7),
        "shipment_id": shipment_ids[bl_ship],
        "po_line_id": po_line_ids[bl_line],
        "shipped_qty": part,
    })
    shipped_total = np.bincount(bl_ship, part, minlength=S).astype(int)
    expected_amount = np.bincount(bl_ship, part * unit_price[bl_line], minlength=S).round(2)

    shipments = pd.DataFrame({
        "shipment_id": shipment_ids,
        "po_id": po_ids[sh_po],
        "supplier_id": sup_ids[po_sup[sh_po]],
        "site_id": site_ids[po_site[sh_po]],
        "carrier_id": carrier,
        "transport_mode": rng.choice(["ROAD", "SEA", "AIR"], S, p=[0.6, 0.25, 0.15]),
        "receiving_warehouse_id": warehouse,
        "created_date": to_date(created_day),
        "dispatch_date": to_date(np.where(dispatched, dispatch_day, np.nan)),
        "delivery_date": to_date(np.where(delivered, delivery_day, np.nan)),
        "shipped_qty_total": shipped_total,
        "status": status,
    })

    # ---------------- DeliveryEvent (append-only lifecycle) ----------------
    ev_parts = []
    def _events(mask, etype, seq, d, hour, recorded_by, location):
        idx = np.flatnonzero(mask)
        minutes = hour * 60 + rng.integers(0, 30, len(idx))
        ev_parts.append(pd.DataFrame({
            "_ship": idx, "event_seq": seq if np.isscalar(seq) else seq[idx], "event_type": etype,
            "event_timestamp": to_ts(d[idx], minutes),
            "recorded_by": recorded_by, "location": location[idx],
        }))
    all_ship = np.ones(S, dtype=bool)
    sup_site_loc = site_ids[po_site[sh_po]]
    _events(all_ship, "CREATED", 1, created_day, 8, "SUPPLIER", sup_site_loc)
    _events(dispatched, "DISPATCHED", 2, dispatch_day, 10, "CARRIER", carrier)
    _events(dispatched, "IN_TRANSIT", 3, dispatch_day, 18, "CARRIER", carrier)
    delayed_hour = np.where(delayed_day == dispatch_day, 20, 9)
    idx = np.flatnonzero(has_delay_event)
    ev_parts.append(pd.DataFrame({
        "_ship": idx, "event_seq": 4, "event_type": "DELAYED",
        "event_timestamp": to_ts(delayed_day[idx], delayed_hour[idx] * 60 + rng.integers(0, 30, len(idx))),
        "recorded_by": "CARRIER", "location": carrier[idx],
    }))
    _events(delivered, "DELIVERED", np.where(has_delay_event, 5, 4), delivery_day, 14, "WAREHOUSE", warehouse)
    events = pd.concat(ev_parts, ignore_index=True).sort_values(["_ship", "event_seq"], kind="mergesort")
    events.insert(0, "delivery_event_id", ids.sequence("DEV-2026", 1, len(events), 7))
    events.insert(1, "shipment_id", shipment_ids[events["_ship"].to_numpy()])
    delivery_events = events.drop(columns="_ship").reset_index(drop=True)

    # ---------------- Inspection ----------------
    eligible = np.flatnonzero(delivered)
    el_sparse = sh_prof[eligible] == "SPARSE"
    el_w = np.where(el_sparse, SPARSE_INSPECTION_WEIGHT, 1.0)
    n_ins = scale.inspections
    base = eligible[~el_sparse]
    if n_ins >= len(base):
        extra = rng.choice(eligible, n_ins - len(base), p=el_w / el_w.sum())
        ins_ship = np.concatenate([base, extra])
    else:
        ins_ship = rng.choice(eligible, n_ins, replace=False, p=el_w / el_w.sum())
    ins_day = np.minimum(delivery_day[ins_ship] + rng.integers(0, 4, n_ins), EXTRACT_DAY)
    ins_sup = po_sup[sh_po[ins_ship]]
    # Hidden quality-incident windows (one or two per QUALITY_INCIDENT supplier).
    w1s = rng.integers(day("2026-04-01"), day("2026-08-15") + 1, n)
    w1l = rng.integers(35, 61, n)
    has_w2 = rng.random(n) < 0.5
    w2s = w1s + w1l + rng.integers(30, 91, n)
    w2l = rng.integers(35, 61, n)
    is_qi = profile[ins_sup] == "QUALITY_INCIDENT"
    in_w1 = (ins_day >= w1s[ins_sup]) & (ins_day < w1s[ins_sup] + w1l[ins_sup])
    in_w2 = has_w2[ins_sup] & (ins_day >= w2s[ins_sup]) & (ins_day < w2s[ins_sup] + w2l[ins_sup])
    reject_p = np.where(is_qi & (in_w1 | in_w2), INCIDENT_REJECT_P, _map(profile[ins_sup], REJECT_P))
    inspected = np.minimum(shipped_total[ins_ship], rng.integers(20, 121, n_ins))
    rejected = rng.binomial(inspected, reject_p)
    reject_rate = rejected / inspected
    ins_order = np.lexsort((ins_ship, ins_day))
    ins_ship, ins_day, inspected, rejected, reject_rate = (a[ins_order] for a in (ins_ship, ins_day, inspected, rejected, reject_rate))
    defect = np.where(rejected == 0, "NONE", rng.choice(DEFECT_CODES[1:], n_ins))
    inspections = pd.DataFrame({
        "inspection_id": ids.sequence("INS-2026", 1, n_ins, 7),
        "shipment_id": shipment_ids[ins_ship],
        "lot_number": pd.Series(ins_ship).groupby(ins_ship).cumcount().to_numpy() + 1,
        "supplier_id": sup_ids[po_sup[sh_po[ins_ship]]],
        "inspector_id":[f"QA-{i:02d}" for i in rng.integers(1, 16, n_ins)],
        "inspection_date": to_date(ins_day),
        "inspected_qty": inspected,
        "accepted_qty": inspected - rejected,
        "rejected_qty": rejected,
        "defect_code": defect,
        "result": np.select([rejected == 0, reject_rate <= C.INSPECTION_DEVIATION_LIMIT],
                            ["ACCEPTED", "ACCEPTED_WITH_DEVIATION"], "REJECTED"),
    })

    # ---------------- Invoice (0..1 per delivered shipment) ----------------
    inv_day_all = delivery_day + rng.integers(0, 11, S)
    drop = (sh_prof == "SPARSE") & (rng.random(S) < SPARSE_INVOICE_DROP_P)
    has_inv = delivered & (inv_day_all <= EXTRACT_DAY) & ~drop
    has_var = rng.random(S) < _map(sh_prof, INVOICE_VARIANCE_P)
    var_frac = np.where(has_var, rng.uniform(0.005, 0.08, S) * rng.choice([-1, 1], S), 0.0)
    inv_ship = np.flatnonzero(has_inv)
    inv_ship = inv_ship[np.lexsort((inv_ship, inv_day_all[inv_ship]))]
    exp_amt = expected_amount[inv_ship]
    invoiced = (exp_amt * (1 + var_frac[inv_ship])).round(2)
    variance = (invoiced - exp_amt).round(2)
    variance_pct = (variance / exp_amt * 100).round(3)
    inv_days = inv_day_all[inv_ship]
    terms = payment_terms[po_site[sh_po[inv_ship]]]
    invoices = pd.DataFrame({
        "invoice_id": ids.sequence("INV-2026", 1, len(inv_ship), 6),
        "shipment_id": shipment_ids[inv_ship],
        "po_id": po_ids[sh_po[inv_ship]],
        "supplier_id": sup_ids[po_sup[sh_po[inv_ship]]],
        "invoice_date": to_date(inv_days),
        "due_date": to_date(inv_days + terms),
        "currency": "USD",
        "expected_amount": exp_amt,
        "invoiced_amount": invoiced,
        "variance_amount": variance,
        "variance_pct": variance_pct,
        "match_status": np.where(np.abs(variance_pct) <= C.INVOICE_TOLERANCE_PCT, "MATCHED", "PRICE_VARIANCE"),
    })

    # ---------------- POStatusEvent (append-only) + derived PO status ----------------
    agg = pd.DataFrame({
        "po": sh_po, "delivered": delivered,
        "del_day": np.where(delivered, delivery_day, np.nan),
        "inv_day": np.where(has_inv, inv_day_all, np.nan), "has_inv": has_inv,
    }).groupby("po").agg(n=("delivered", "size"), n_del=("delivered", "sum"),
                         first_del=("del_day", "min"), last_del=("del_day", "max"),
                         n_inv=("has_inv", "sum"), last_inv=("inv_day", "max"))
    agg = agg.reindex(np.arange(n_pos))
    agg["n"] = agg["n"].fillna(0)
    shipped_po = agg["n"] > 0
    full = shipped_po & (agg["n_del"] == agg["n"])
    partial = shipped_po & (agg["n"] >= 2) & (agg["n_del"] >= 1) & ((agg["n_del"] < agg["n"]) | (agg["first_del"] < agg["last_del"]))
    closed = full & (agg["n_inv"] == agg["n"])
    po_ev = [pd.DataFrame({"_po": np.arange(n_pos), "event_seq": 1, "status": "OPEN",
                           "event_timestamp": to_ts(order_day, np.full(n_pos, 9 * 60)), "trigger": "PO_RELEASED"})]
    for mask, seq, st, col, minute, trig in [
        (partial, 2, "PARTIALLY_RECEIVED", "first_del", 15 * 60, "FIRST_RECEIPT"),
        (full, 3, "RECEIVED", "last_del", 15 * 60 + 30, "FULL_RECEIPT"),
        (closed, 4, "CLOSED", "last_inv", 17 * 60, "ALL_INVOICED"),
    ]:
        idx = np.flatnonzero(mask.to_numpy())
        po_ev.append(pd.DataFrame({"_po": idx, "event_seq": seq, "status": st,
                                   "event_timestamp": to_ts(agg[col].to_numpy()[idx], np.full(len(idx), minute)),
                                   "trigger": trig}))
    po_events = pd.concat(po_ev, ignore_index=True).sort_values(["_po", "event_seq"], kind="mergesort")
    po_events["event_seq"] = po_events.groupby("_po").cumcount() + 1
    po_events.insert(0, "po_event_id", ids.sequence("POE-2026", 1, len(po_events), 7))
    po_events.insert(1, "po_id", po_ids[po_events["_po"].to_numpy()])
    po_status = po_events.groupby("_po")["status"].last().reindex(np.arange(n_pos)).to_numpy()
    po_status_events = po_events.drop(columns="_po").reset_index(drop=True)

    pos = pd.DataFrame({
        "po_id": po_ids,
        "supplier_id": sup_ids[po_sup],
        "site_id": site_ids[po_site],
        "contract_id": contracts["contract_id"].to_numpy()[po_site],
        "planner_id": [f"PLN-{i:03d}" for i in planner],
        "order_date": to_date(order_day),
        "required_date": to_date(required_day),
        "currency": "USD",
        "total_amount": np.bincount(line_po, line_total, minlength=n_pos).round(2),
        "line_count": n_lines,
        "status": po_status,
    })

    # ---------------- RiskSignal ----------------
    rs_cnt = np.minimum(rng.poisson(_map(profile, RISK_RATE)), 12)
    rs_sup = np.repeat(np.arange(n), rs_cnt)
    R = len(rs_sup)
    rs_sec = rng.integers(0, (EXTRACT_DAY + 1) * 86400, R)
    rs_order = np.lexsort((rs_sec, rs_sup))
    rs_sup, rs_sec = rs_sup[rs_order], rs_sec[rs_order]
    rs_seq = np.arange(R) - _group_starts(rs_cnt)[rs_sup] + 1
    stable_rs = profile[rs_sup] == "STABLE"
    sev_stable = rng.choice(["LOW", "MEDIUM", "HIGH"], R, p=[0.65, 0.30, 0.05])
    sev_other = rng.choice(["LOW", "MEDIUM", "HIGH"], R, p=[0.45, 0.38, 0.17])
    signal_ts = pd.Series(BASE + pd.to_timedelta(rs_sec, unit="s"))
    resolve_sec = rs_sec + rng.integers(2 * 86400, 61 * 86400, R)
    resolved_ok = resolve_sec < (EXTRACT_DAY + 1) * 86400
    risk_signals = pd.DataFrame({
        "risk_signal_id": [ids.risk_signal_id(sup_ids[s], q) for s, q in zip(rs_sup, rs_seq)],
        "supplier_id": sup_ids[rs_sup],
        "risk_type": rng.choice(RISK_TYPES, R, p=[0.18, 0.2, 0.12, 0.12, 0.12, 0.14, 0.12]),
        "severity": np.where(stable_rs, sev_stable, sev_other),
        "signal_timestamp": signal_ts,
        "source": rng.choice(RISK_SOURCES, R),
        "resolved_timestamp": pd.Series(BASE + pd.to_timedelta(np.where(resolved_ok, resolve_sec, np.nan), unit="s")),
        "status": np.where(resolved_ok, "RESOLVED", "OPEN"),
    })

    tables = {
        "Supplier": suppliers, "Site": sites, "Contract": contracts,
        "PurchaseOrder": pos, "POLine": polines, "POStatusEvent": po_status_events,
        "Shipment": shipments, "ShipmentLine": shipment_lines, "DeliveryEvent": delivery_events,
        "Inspection": inspections, "Invoice": invoices, "RiskSignal": risk_signals,
    }

    # ---------------- Hidden profile labels (ground truth only) ----------------
    def pattern(i: int) -> str:
        p = profile[i]
        if p == "STABLE":
            return "Delivery delay ~N(-3,2.5) days vs required date; reject p=0.8%; 8% invoices with variance."
        if p == "DETERIORATING":
            return f"Mean delivery delay grows {DETERIORATION_DAYS_PER_MONTH} days per order month from -3 days in January."
        if p == "QUALITY_INCIDENT":
            win = f"{to_date([w1s[i]])[0].date()}..{to_date([w1s[i] + w1l[i] - 1])[0].date()}"
            if has_w2[i]:
                win += f"; {to_date([w2s[i]])[0].date()}..{to_date([w2s[i] + w2l[i] - 1])[0].date()}"
            return f"Reject p={INCIDENT_REJECT_P:.0%} inside incident window(s) {win}; else 2%."
        if p == "SPARSE":
            return "~12% of normal PO volume; inspections 10x less likely; 65% of invoices and 60% of audits not recorded."
        caps = capacity[site_start[i]: site_start[i] + n_sites[i]]
        return f"Contracted capacity {int(caps.sum())} units/month across sites vs 2.5x normal order volume."

    risk_class = np.select([profile == "STABLE", profile == "SPARSE"], ["LOW_RISK", "UNKNOWN"], "AT_RISK")
    labels = pd.DataFrame({
        "supplier_id": sup_ids,
        "profile": profile,
        "risk_class": risk_class,
        "expected_pattern": [pattern(i) for i in range(n)],
        "expected_final_decision": np.select([profile == "STABLE", profile == "SPARSE"],
                                             ["APPROVED_OR_MONITOR", "REVIEW_DATA"], "MONITOR_OR_ESCALATE"),
    })
    return tables, labels
