"""Deterministic, zero-padded identifier conventions shared by every module."""
from __future__ import annotations

from typing import Iterable


def supplier_id(i: int) -> str:
    return f"SUP-{i:06d}"


def site_id(supplier: str, seq: int) -> str:
    return f"SITE-{supplier}-{seq:02d}"


def risk_signal_id(supplier: str, seq: int) -> str:
    return f"RSK-{supplier}-2026-{seq:02d}"


def sequence(prefix: str, start: int, count: int, width: int) -> list[str]:
    """`sequence("PO-2026", 1, 3, 6)` -> PO-2026-000001 .. PO-2026-000003."""
    return [f"{prefix}-{i:0{width}d}" for i in range(start, start + count)]


def supplier_of(entity_id: str) -> str:
    """Extract the embedded supplier ID from SITE-/RSK- identifiers."""
    parts = entity_id.split("-")
    return f"{parts[1]}-{parts[2]}"


def max_counter(ids: Iterable[str]) -> int:
    values = [int(str(x).rsplit("-", 1)[1]) for x in ids]
    return max(values) if values else 0


# (prefix, width) for sequential identifiers.
COUNTERS = {
    "Contract": ("CON-2026", 6),
    "PurchaseOrder": ("PO-2026", 6),
    "POLine": ("POL-2026", 7),
    "POStatusEvent": ("POE-2026", 7),
    "Shipment": ("SHP-2026", 6),
    "ShipmentLine": ("SHL-2026", 7),
    "DeliveryEvent": ("DEV-2026", 7),
    "Inspection": ("INS-2026", 7),
    "Invoice": ("INV-2026", 6),
    "SupplierScore": ("SCORE-2026", 6),
    "SupplierScoreComponent": ("SCC-2026", 7),
    "ExceptionLog": ("EXC-2026", 6),
    "GroundTruth": ("GT-2026", 5),
}

PATTERNS = {
    "Supplier": r"^SUP-\d{6}$",
    "Site": r"^SITE-SUP-\d{6}-\d{2}$",
    "RiskSignal": r"^RSK-SUP-\d{6}-2026-\d{2}$",
    **{name: rf"^{prefix}-\d{{{width}}}$" for name, (prefix, width) in COUNTERS.items()},
}
