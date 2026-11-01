"""CSV / JSON / SQL export, manifest checksums, and the matching loader."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .schema import BY_NAME, ENTITIES, Entity, foreign_keys

SQL_TYPES = {"STRING": "TEXT", "INTEGER": "INTEGER", "DECIMAL": "REAL",
             "DATE": "TEXT", "DATETIME": "TEXT", "JSON": "TEXT"}
DATA_DIRS = ("csv", "json", "ground_truth", "outputs")


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _format(df: pd.DataFrame, entity: Entity) -> pd.DataFrame:
    """Order columns per schema and render dates as ISO strings."""
    out = df[entity.columns].copy()
    for f in entity.fields:
        if f.dtype == "DATE":
            out[f.name] = pd.to_datetime(out[f.name]).dt.strftime("%Y-%m-%d")
        elif f.dtype == "DATETIME":
            out[f.name] = pd.to_datetime(out[f.name]).dt.strftime("%Y-%m-%dT%H:%M:%S")
        elif f.dtype == "INTEGER":
            out[f.name] = pd.to_numeric(out[f.name]).astype("Int64")
        out[f.name] = out[f.name].astype(object).where(out[f.name].notna(), None)
    return out


def _write_table(df: pd.DataFrame, entity: Entity, csv_dir: Path, json_dir: Path) -> pd.DataFrame:
    out = _format(df, entity)
    out.to_csv(csv_dir / f"{entity.name}.csv", index=False, lineterminator="\n")
    records = [{k: (v.item() if isinstance(v, np.generic) else v) for k, v in r.items()}
               for r in out.to_dict("records")]
    (json_dir / f"{entity.name}.json").write_text(
        json.dumps(records, separators=(",", ":"), ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def _sql_value(v: Any) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "NULL"
    if isinstance(v, (bool, np.bool_)):
        return "1" if v else "0"
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    if isinstance(v, (float, np.floating)):
        return repr(float(v))
    return "'" + str(v).replace("'", "''") + "'"


def write_sql(formatted: dict[str, pd.DataFrame], path: Path, batch: int = 500) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write("-- Supplier Reliability Engine seed data (SQLite dialect).\n"
                "-- Foreign keys are declared but enforcement is off during load because the\n"
                "-- dataset deliberately contains labelled anomalies (see ground_truth/).\n"
                "-- After loading run: PRAGMA foreign_key_check;  -> only SHIPMENT_NO_PO rows appear.\n"
                "PRAGMA foreign_keys = OFF;\nBEGIN TRANSACTION;\n")
        for entity in ENTITIES:
            cols = [f'  "{fd.name}" {SQL_TYPES[fd.dtype]}{" NOT NULL" if fd.name == entity.primary_key else ""}'
                    for fd in entity.fields]
            cols.append(f'  PRIMARY KEY ("{entity.primary_key}")')
            for col, parent, pcol in foreign_keys(entity):
                cols.append(f'  FOREIGN KEY ("{col}") REFERENCES "{parent}" ("{pcol}")')
            f.write(f'DROP TABLE IF EXISTS "{entity.name}";\nCREATE TABLE "{entity.name}" (\n' + ",\n".join(cols) + "\n);\n")
        for entity in ENTITIES:
            df = formatted[entity.name]
            col_sql = ", ".join(f'"{c}"' for c in df.columns)
            rows = list(df.itertuples(index=False, name=None))
            for i in range(0, len(rows), batch):
                values = ",\n".join("(" + ", ".join(_sql_value(v) for v in r) + ")" for r in rows[i:i + batch])
                f.write(f'INSERT INTO "{entity.name}" ({col_sql}) VALUES\n{values};\n')
        f.write("COMMIT;\n")


def write_all(out: Path, tables: dict[str, pd.DataFrame], exceptions: pd.DataFrame,
              ground_truth: pd.DataFrame, labels: pd.DataFrame) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for d in DATA_DIRS:                       # remove stale files from a previous run
        shutil.rmtree(out / d, ignore_errors=True)
        (out / d).mkdir()
    formatted = {e.name: _write_table(tables[e.name], e, out / "csv", out / "json") for e in ENTITIES}
    _write_table(exceptions, BY_NAME["ExceptionLog"], out / "outputs", out / "outputs")
    gt_dir = out / "ground_truth"
    for df, name, file in [(ground_truth, "GroundTruth", "ground_truth"), (labels, "SupplierProfileLabel", "supplier_profiles")]:
        e = BY_NAME[name]
        _write_table(df, e, gt_dir, gt_dir)
        (gt_dir / f"{e.name}.csv").rename(gt_dir / f"{file}.csv")
        (gt_dir / f"{e.name}.json").rename(gt_dir / f"{file}.json")
    write_sql(formatted, out / "seed.sql")


def data_files(out: Path) -> list[Path]:
    files = [p for d in DATA_DIRS for p in sorted((out / d).glob("*")) if p.is_file()]
    return files + [out / "seed.sql"]


def write_manifest(out: Path, meta: dict, tables: dict[str, pd.DataFrame], baseline_counts: dict[str, int]) -> dict:
    manifest = {
        **meta,
        "row_counts": {name: {"baseline": int(baseline_counts.get(name, 0)), "total": int(len(tables[name])),
                              "injected": int(len(tables[name]) - baseline_counts.get(name, 0))}
                       for name in [e.name for e in ENTITIES]},
        "files": {p.relative_to(out).as_posix(): {"bytes": p.stat().st_size, "sha256": sha256(p)} for p in data_files(out)},
    }
    write_json(out / "manifest.json", manifest)
    return manifest


def _read(path: Path, entity: Entity) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
    for f in entity.fields:
        if f.name not in df:
            continue
        if f.dtype in ("DATE", "DATETIME"):
            df[f.name] = pd.to_datetime(df[f.name], errors="coerce", format="ISO8601")
        elif f.dtype == "INTEGER":
            df[f.name] = pd.to_numeric(df[f.name], errors="coerce").astype("Int64")
        elif f.dtype == "DECIMAL":
            df[f.name] = pd.to_numeric(df[f.name], errors="coerce")
        else:
            df[f.name] = df[f.name].astype(object).where(df[f.name].notna(), None)
    return df


def load_tables(out: str | Path) -> dict[str, pd.DataFrame]:
    out = Path(out)
    tables = {}
    for e in ENTITIES:
        path = out / "csv" / f"{e.name}.csv"
        if not path.exists():
            raise FileNotFoundError(f"Missing required data file: {path}")
        tables[e.name] = _read(path, e)
    return tables


def load_ground_truth(out: str | Path) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    gt_dir = Path(out) / "ground_truth"
    gt = gt_dir / "ground_truth.csv"
    lb = gt_dir / "supplier_profiles.csv"
    return (_read(gt, BY_NAME["GroundTruth"]) if gt.exists() else None,
            _read(lb, BY_NAME["SupplierProfileLabel"]) if lb.exists() else None)


def load_exceptions(out: str | Path) -> pd.DataFrame:
    return _read(Path(out) / "outputs" / "ExceptionLog.csv", BY_NAME["ExceptionLog"])
