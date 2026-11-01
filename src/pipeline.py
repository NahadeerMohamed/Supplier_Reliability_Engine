"""End-to-end pipeline: generate -> inject -> quarantine/score -> export -> validate -> evaluate.

    python -m src.pipeline --scale full            # 1,000 / 10,000 / 15,000 / 20,000
    python -m src.pipeline --scale starter         # same generator, small volume
    python -m src.pipeline --scale full --repro    # also regenerate twice and compare checksums
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from . import anomalies, config as C, docs, export, scoring
from .config import Scale
from .generator import generate_baseline

SOURCE_MANIFEST = {
    "external_data_used": False,
    "statement": "All records are synthetic, produced by src/generator.py from a fixed seed. "
                 "No external dataset was downloaded, transformed or redistributed, and no personal "
                 "or confidential information is included. Supplier names and addresses are fabricated.",
    "schema_inspiration_only": [
        {"reference": "SCOR Digital Standard (ASCM) - perfect order fulfilment / delivery performance concepts",
         "use": "metric naming only; no data used"},
        {"reference": "ISO 9001 quality-management certification concept", "use": "certification field semantics only"},
    ],
    "fields_discarded": [],
    "license": "Synthetic output may be freely redistributed with this repository.",
}


def build_command(scale_name: str, scale: Scale, seed: int) -> str:
    cmd = f"python -m src.pipeline --scale {scale_name} --seed {seed}"
    if scale != C.SCALES.get(scale_name):
        cmd += f" --suppliers {scale.suppliers} --pos {scale.pos} --shipments {scale.shipments} --inspections {scale.inspections}"
    return cmd


def generate(out: Path, scale: Scale, seed: int, scale_name: str = "custom") -> dict:
    """Produce all data files + manifest (no validation)."""
    tables, labels = generate_baseline(scale, seed)
    baseline_counts = {name: len(df) for name, df in tables.items()}
    k = C.anomalies_per_type(scale)
    tables, gt_rows = anomalies.inject_source_anomalies(tables, k, seed)
    scores, comps, exceptions = scoring.run_scoring(tables)
    baseline_counts["SupplierScore"], baseline_counts["SupplierScoreComponent"] = len(scores), len(comps)
    scores, comps, leak_rows = anomalies.inject_score_leakage(tables, scores, comps, k, seed)
    tables["SupplierScore"], tables["SupplierScoreComponent"] = scores, comps
    ground_truth = anomalies.ground_truth_frame(gt_rows + leak_rows)

    export.write_all(out, tables, exceptions, ground_truth, labels)
    export.write_json(out / "source_manifest.json", SOURCE_MANIFEST)
    export.write_json(out / "data_dictionary.json", docs.dictionary_json())
    meta = {
        "generator": "src.pipeline", "generator_command": build_command(scale_name, scale, seed),
        "seed": seed, "scale": scale_name, "parameters": scale.as_dict(), "model_version": C.MODEL_VERSION,
        "observation_start": str(C.OBSERVATION_START.date()), "extract_date": str(C.EXTRACT_DATE.date()),
        "score_cutoffs": [str(c.date()) for c in C.SCORE_CUTOFFS], "anomalies_per_type": k,
        "ground_truth_records": int(len(ground_truth)), "exceptions_logged": int(len(exceptions)),
    }
    return export.write_manifest(out, meta, tables, baseline_counts)


def parse_scale(args) -> Scale:
    base = C.SCALES[args.scale]
    return Scale(suppliers=args.suppliers or base.suppliers, pos=args.pos or base.pos,
                 shipments=args.shipments or base.shipments, inspections=args.inspections or base.inspections)


def add_scale_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--scale", choices=sorted(C.SCALES), default="full")
    parser.add_argument("--seed", type=int, default=C.SEED)
    parser.add_argument("--output", help="Output directory (default data/<scale>)")
    for name in ("suppliers", "pos", "shipments", "inspections"):
        parser.add_argument(f"--{name}", type=int, help=f"Override {name} count")


def main() -> None:
    from .evaluation import evaluate
    from .reproducibility import check_reproducibility
    from .validation import validate_data

    parser = argparse.ArgumentParser(description="Run the supplier reliability pipeline end to end.")
    add_scale_args(parser)
    parser.add_argument("--repro", action="store_true", help="Regenerate twice and compare checksums")
    args = parser.parse_args()
    scale = parse_scale(args)
    out = Path(args.output or f"data/{args.scale}")

    t0 = time.time()
    manifest = generate(out, scale, args.seed, args.scale)
    print(f"Generated {out} in {time.time() - t0:.1f}s (seed {args.seed})")
    for name, c in manifest["row_counts"].items():
        extra = f"  (+{c['injected']} injected)" if c["injected"] else ""
        print(f"  {name:<24} {c['total']:>7,}{extra}")
    report = validate_data(out)
    evaluate(out)
    if args.repro:
        check_reproducibility(out, scale, args.seed, args.scale)
    print(f"\nDone in {time.time() - t0:.1f}s. Outputs in {out.resolve()}")
    raise SystemExit(0 if report["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
