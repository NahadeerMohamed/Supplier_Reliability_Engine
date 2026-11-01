"""Central configuration: seed, calendar, scale presets, scoring weights and thresholds."""
from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

SEED = 20261022
MODEL_VERSION = "SRS-1.0"

# Synthetic world calendar. EXTRACT_DATE is the "today" of the data extract:
# no source record carries a timestamp after the end of this day.
OBSERVATION_START = pd.Timestamp("2026-01-01")
EXTRACT_DATE = pd.Timestamp("2026-10-31")
LAST_ORDER_DATE = pd.Timestamp("2026-10-15")

# Point-in-time scoring snapshots (end-of-day cutoffs).
SCORE_CUTOFFS = [
    pd.Timestamp("2026-03-31"),
    pd.Timestamp("2026-06-30"),
    pd.Timestamp("2026-09-30"),
    pd.Timestamp("2026-10-31"),
]
SCORING_WINDOW_DAYS = 180   # delivery / quality / cost / capacity look-back
RISK_WINDOW_DAYS = 180      # disruption / regulatory look-back


@dataclass(frozen=True)
class Scale:
    suppliers: int
    pos: int
    shipments: int
    inspections: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


SCALES = {
    "starter": Scale(suppliers=50, pos=500, shipments=750, inspections=1000),
    "full": Scale(suppliers=1000, pos=10000, shipments=15000, inspections=20000),
}

# Hidden behavioural profiles. They drive generation only and are written to
# ground_truth/; the scoring engine never reads them.
PROFILES = ["STABLE", "DETERIORATING", "QUALITY_INCIDENT", "SPARSE", "CAPACITY_STRAINED"]
PROFILE_PROBS = [0.45, 0.20, 0.20, 0.10, 0.05]
AT_RISK_PROFILES = {"DETERIORATING", "QUALITY_INCIDENT", "CAPACITY_STRAINED"}

WEIGHTS = {
    "delivery": 0.30,
    "quality": 0.25,
    "cost": 0.15,
    "capacity": 0.10,
    "compliance": 0.10,
    "disruption": 0.10,
}

# Minimum evidence (denominator) before a component is reported; below it the
# component is MISSING and its weight is redistributed. Nothing is imputed.
MIN_DELIVERED_SHIPMENTS = 3
MIN_INSPECTIONS = 2
MIN_INSPECTED_UNITS = 30
MIN_INVOICES = 2
MIN_CAPACITY_MONTHS = 2
MIN_COVERAGE_PCT = 60.0
MIN_COMPONENTS = 3

QUALITY_POINTS_PER_REJECT_PCT = 5.0     # 1% rejected units costs 5 points
COST_POINTS_PER_VARIANCE_PCT = 10.0     # 1% absolute invoice variance costs 10 points
CERT_LAPSED_PENALTY = 20.0
REGULATORY_SIGNAL_PENALTY = 10.0
SEVERITY_POINTS = {"LOW": 5.0, "MEDIUM": 15.0, "HIGH": 30.0}
RESOLVED_SIGNAL_FACTOR = 0.5

TIER_HIGH = 85.0
TIER_MEDIUM = 70.0

INVOICE_TOLERANCE_PCT = 2.0
INSPECTION_DEVIATION_LIMIT = 0.05


def anomalies_per_type(scale: Scale) -> int:
    """About 20 injected cases per edge-case type at full scale; at least 3."""
    return max(3, scale.shipments // 750)
