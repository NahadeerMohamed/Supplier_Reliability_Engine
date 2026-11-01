import re
import shutil
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from src import config as C
from src import ids, scoring
from src.evaluation import evaluate
from src.export import load_ground_truth, load_tables
from src.pipeline import generate
from src.validation import validate_data

SCALE = C.SCALES["starter"]
SEED = 123


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    out = tmp_path_factory.mktemp("starter")
    manifest = generate(out, SCALE, SEED, "starter")
    report = validate_data(out, verbose=False)
    return out, manifest, report


def test_baseline_volumes_match_parameters(run):
    _, manifest, _ = run
    rc = manifest["row_counts"]
    assert rc["Supplier"]["total"] == SCALE.suppliers
    assert rc["PurchaseOrder"]["total"] == SCALE.pos
    assert rc["Shipment"]["baseline"] == SCALE.shipments
    assert rc["Inspection"]["total"] == SCALE.inspections
    assert rc["SupplierScore"]["baseline"] == SCALE.suppliers * len(C.SCORE_CUTOFFS)


def test_validation_passes_and_detects_every_anomaly(run):
    _, _, report = run
    assert report["status"] == "PASS", report["unexpected_findings"]
    assert report["unexpected_total"] == 0
    assert set(report["anomaly_detection"]) == {"SHIPMENT_NO_PO", "DELIVERY_BEFORE_DISPATCH", "SCORE_USES_FUTURE_DATA",
                                                "RISK_NO_SOURCE_OR_TIME", "SITE_CONFLATION"}
    for issue, d in report["anomaly_detection"].items():
        assert d["detected"] == d["injected"] >= 3, issue


def test_engine_quarantines_source_anomalies(run):
    out, _, _ = run
    report = evaluate(out, write_report=False, verbose=False)
    q = report["quarantine_vs_ground_truth"]
    assert q["precision"] == 1.0 and q["recall"] == 1.0


def test_same_seed_is_byte_identical_and_other_seed_differs(tmp_path):
    a = generate(tmp_path / "a", SCALE, SEED, "starter")
    b = generate(tmp_path / "b", SCALE, SEED, "starter")
    c = generate(tmp_path / "c", SCALE, SEED + 1, "starter")
    assert a["files"] == b["files"]
    assert a["files"]["csv/Shipment.csv"] != c["files"]["csv/Shipment.csv"]


def test_id_conventions(run):
    out, _, _ = run
    t = load_tables(out)
    assert t["Supplier"]["supplier_id"].iloc[0] == "SUP-000001"
    assert t["PurchaseOrder"]["po_id"].iloc[0] == "PO-2026-000001"
    assert t["Shipment"]["shipment_id"].iloc[0] == "SHP-2026-000001"
    assert t["SupplierScore"]["score_id"].iloc[0] == "SCORE-2026-000001"
    site = t["Site"].iloc[0]
    assert site["site_id"] == f"SITE-{site['supplier_id']}-01"
    assert all(re.match(ids.PATTERNS["RiskSignal"], r) for r in t["RiskSignal"]["risk_signal_id"])


def test_scoring_never_reads_ground_truth(run, tmp_path):
    out, _, _ = run
    source = Path(scoring.__file__).read_text()
    assert "ground_truth" not in source.replace("never opens ground_truth/", "")
    assert "profile" not in source
    copy = tmp_path / "copy"
    shutil.copytree(out, copy)
    shutil.rmtree(copy / "ground_truth")
    t = load_tables(copy)
    rescored, _, _ = scoring.run_scoring(t)
    official = t["SupplierScore"][t["SupplierScore"]["score_run_id"].str.startswith("RUN-")].reset_index(drop=True)
    pd.testing.assert_series_equal(rescored["reliability_score"], official["reliability_score"], check_exact=False, atol=0.01)
    assert (rescored["decision"] == official["decision"]).all()


def test_components_reconcile_and_missing_is_data_driven(run):
    out, _, _ = run
    t = load_tables(out)
    sc, cp = t["SupplierScore"], t["SupplierScoreComponent"]
    total = cp.groupby("score_id")["weighted_contribution"].sum()
    ok = sc["reliability_score"].notna()
    assert ((sc.loc[ok, "score_id"].map(total) - sc.loc[ok, "reliability_score"]).abs() <= 0.011).all()
    missing = cp[cp["status"] == "MISSING"]
    assert (missing["effective_weight"] == 0).all() and missing["component_score"].isna().all()
    minimum = {"delivery": C.MIN_DELIVERED_SHIPMENTS, "capacity": C.MIN_CAPACITY_MONTHS, "compliance": 1}
    for comp, floor in minimum.items():
        den = missing.loc[missing["component"] == comp, "denominator"].fillna(0)
        assert (den < floor).all(), comp
    _, labels = load_ground_truth(out)
    final = sc[(sc["score_timestamp"] == sc["score_timestamp"].max()) & sc["score_run_id"].str.startswith("RUN-")]
    sparse = final.merge(labels, on="supplier_id").query("profile == 'SPARSE'")
    if len(sparse):
        assert (sparse["reliability_tier"] == "INSUFFICIENT_DATA").mean() >= 0.5


def test_unlabelled_corruption_fails_validation(run, tmp_path):
    out, _, _ = run
    copy = tmp_path / "corrupt"
    shutil.copytree(out, copy)
    path = copy / "csv" / "POLine.csv"
    lines = pd.read_csv(path, dtype=str)
    lines.loc[0, "line_total"] = "1.00"
    lines.to_csv(path, index=False, lineterminator="\n")
    report = validate_data(copy, write_report=False, verbose=False)
    assert report["status"] == "FAIL"
    assert any(f["check"] == "ARITHMETIC" for f in report["unexpected_findings"])


def test_sql_seed_loads_and_fk_violations_are_labelled(run):
    out, _, _ = run
    con = sqlite3.connect(":memory:")
    con.executescript((out / "seed.sql").read_text(encoding="utf-8"))
    assert con.execute('SELECT COUNT(*) FROM "Supplier"').fetchone()[0] == SCALE.suppliers
    violations = con.execute("PRAGMA foreign_key_check").fetchall()
    shipments = {r[0] for r in con.execute('SELECT shipment_id, rowid FROM "Shipment"').fetchall()}
    rowid_to_id = {rowid: sid for sid, rowid in con.execute('SELECT shipment_id, rowid FROM "Shipment"')}
    gt, _ = load_ground_truth(out)
    labelled = set(gt.loc[gt["issue_type"] == "SHIPMENT_NO_PO", "record_id"])
    assert violations and shipments
    assert all(v[0] == "Shipment" and rowid_to_id[v[1]] in labelled for v in violations)
