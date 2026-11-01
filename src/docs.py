"""Generate the data dictionary (JSON + Markdown) from src/schema.py.

    python -m src.docs      # rewrites docs/DATA_DICTIONARY.md
"""
from __future__ import annotations

from pathlib import Path

from .schema import CARDINALITY, ENTITIES, OUTPUT_ENTITIES, foreign_keys, id_pattern


def dictionary_json() -> dict:
    return {
        "entities": [{
            "name": e.name, "kind": e.kind, "description": e.description, "primary_key": e.primary_key,
            "id_pattern": id_pattern(e.name), "lifecycle": e.lifecycle,
            "foreign_keys": [{"column": c, "references": f"{p}.{pc}"} for c, p, pc in foreign_keys(e)],
            "fields": [{"name": f.name, "type": f.dtype, "required": f.required, "allowed_values": list(f.allowed) if f.allowed else None,
                        "unit": f.unit, "unique": f.unique or f.name == e.primary_key, "foreign_key": f.fk,
                        "validation_rule": f.rule, "description": f.description} for f in e.fields],
        } for e in ENTITIES + OUTPUT_ENTITIES],
        "relationships": [{"parent": p, "child": c, "cardinality": k, "note": n} for p, c, k, n in CARDINALITY],
    }


def _cell(v) -> str:
    return "" if v in (None, "") else str(v).replace("|", "\\|")


def dictionary_markdown() -> str:
    out = ["# Data Dictionary", "",
           "_Generated from `src/schema.py` by `python -m src.docs` - do not edit by hand._", "",
           "Types: STRING, INTEGER, DECIMAL, DATE (`YYYY-MM-DD`), DATETIME (`YYYY-MM-DDTHH:MM:SS`, local plant time), JSON (string).",
           "Req = required (non-null). Uniq = unique across the table.", "",
           "## Relationships", "", "| Parent | Child | Cardinality | Note |", "|---|---|---|---|"]
    out += [f"| {p} | {c} | {k} | {_cell(n)} |" for p, c, k, n in CARDINALITY]
    for e in ENTITIES + OUTPUT_ENTITIES:
        out += ["", f"## {e.name}", "", f"{e.description}", "",
                f"- **Kind:** {e.kind}  ", f"- **Primary key:** `{e.primary_key}`" + (f" (pattern `{id_pattern(e.name)}`)" if id_pattern(e.name) else "")]
        if e.lifecycle:
            out.append(f"- **Lifecycle:** {e.lifecycle}")
        out += ["", "| Field | Type | Req | Uniq | Allowed values | Unit | FK | Validation rule | Description |",
                "|---|---|---|---|---|---|---|---|---|"]
        for f in e.fields:
            out.append("| " + " | ".join([
                f"`{f.name}`", f.dtype, "Y" if f.required else "N", "Y" if (f.unique or f.name == e.primary_key) else "",
                _cell(", ".join(f.allowed) if f.allowed else ""), _cell(f.unit), _cell(f.fk), _cell(f.rule), _cell(f.description),
            ]) + " |")
    return "\n".join(out) + "\n"


def main() -> None:
    path = Path(__file__).resolve().parent.parent / "docs" / "DATA_DICTIONARY.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(dictionary_markdown(), encoding="utf-8")
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
