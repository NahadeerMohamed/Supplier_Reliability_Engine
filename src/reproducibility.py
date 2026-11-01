"""Regenerate twice with the same seed and compare row counts and SHA-256 checksums."""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from . import config as C
from .config import Scale
from .export import write_json
from .pipeline import add_scale_args, generate, parse_scale


def check_reproducibility(out: Path, scale: Scale, seed: int, scale_name: str, verbose: bool = True) -> dict:
    runs = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in (1, 2):
            runs.append(generate(Path(tmp) / f"run{i}", scale, seed, scale_name))
    reference = runs[0]
    mismatches = []
    compared = {"run1_vs_run2": runs[1]}
    published = out / "manifest.json"
    if published.exists():
        import json
        compared["run1_vs_published"] = json.loads(published.read_text())
    for label, other in compared.items():
        for path, info in reference["files"].items():
            o = other["files"].get(path)
            if o is None or o["sha256"] != info["sha256"]:
                mismatches.append({"comparison": label, "file": path})
        if other["row_counts"] != reference["row_counts"]:
            mismatches.append({"comparison": label, "file": "row_counts"})
    report = {
        "seed": seed, "scale": scale_name, "parameters": scale.as_dict(),
        "regenerations": 2, "files_compared": len(reference["files"]),
        "identical": not mismatches, "mismatches": mismatches,
        "row_counts": {k: v["total"] for k, v in reference["row_counts"].items()},
        "checksums": {k: v["sha256"] for k, v in reference["files"].items()},
    }
    write_json(out / "reproducibility_report.json", report)
    if verbose:
        print(f"\nREPRODUCIBILITY: {len(reference['files'])} files x {len(compared)} comparisons -> "
              f"{'IDENTICAL' if not mismatches else f'{len(mismatches)} MISMATCHES'}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_scale_args(parser)
    args = parser.parse_args()
    out = Path(args.output or f"data/{args.scale}")
    out.mkdir(parents=True, exist_ok=True)
    report = check_reproducibility(out, parse_scale(args), args.seed, args.scale)
    raise SystemExit(0 if report["identical"] else 1)


if __name__ == "__main__":
    main()
