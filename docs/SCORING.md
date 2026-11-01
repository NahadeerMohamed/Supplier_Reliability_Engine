# Scoring Method (model `SRS-1.0`)

All constants live in `src/config.py`. The implementation is in `src/scoring.py`.

## 1. Point-in-time rule
A score at cutoff **T** (end of day) uses only rows dated on or before T.
- Delivery, quality, cost and capacity use a trailing **180-day** window (not earlier than 2026-01-01).
- Disruption and regulatory signals use a 180-day look-back.
- Snapshots: 2026-03-31, 2026-06-30, 2026-09-30 and 2026-10-31 (the extract date), giving 4 × suppliers scores.
- Each score stores `max_source_date`. Validation fails any score where `max_source_date > score_timestamp`, and independently re-scores the source data to confirm the stored value (`SCORE_REPRODUCIBILITY`).

## 2. Exception path (quarantine before scoring)
The engine applies its own rules. It never reads ground truth.

| Rule | Condition | Action | Owner |
|---|---|---|---|
| `SHP_NO_PO` | `Shipment.po_id` null or unknown | excluded, logged | Logistics analyst |
| `SHP_DATE_ORDER` | delivery before dispatch (row or events) | excluded, logged | Logistics analyst |
| `RSK_PROVENANCE` | risk signal without `source` or `signal_timestamp` | excluded, logged | Logistics analyst |
| `SITE_CONFLATION` | site ID/address attributed to more than one supplier; PO/shipment site not owned by its supplier | excluded, logged | Operations manager / planner |

Children of an excluded shipment (inspections, invoices) are excluded too. Every exception lands in `outputs/ExceptionLog.csv` with status `OPEN`.

## 3. Components (numerator / denominator always stored)

| Component | Weight | Numerator | Denominator | Score | Missing when |
|---|---:|---|---|---|---|
| Delivery | 30% | shipments delivered on/before PO `required_date` | shipments delivered in window | on-time % | < 3 delivered |
| Quality | 25% | rejected units | inspected units | `max(0, 100 − 5 × reject%)` | < 2 inspections or < 30 units |
| Cost | 15% | Σ \|invoice variance\| | Σ expected invoice amount | `max(0, 100 − 10 × variance%)` | < 2 invoices |
| Capacity | 10% | months with dispatched units ≤ Σ contracted monthly capacity | months with dispatches | within-capacity % | < 2 active months |
| Compliance | 10% | Σ audit scores | audited active contracts | `mean − 20 (cert lapsed) − 10 × REGULATORY signals`, clipped 0–100 | no audit on file |
| Disruption | 10% | Σ severity points (LOW 5 / MED 15 / HIGH 30, × 0.5 if resolved by T) | signals in look-back | `max(0, 100 − points)` | never: zero signals is evidence under continuous monitoring |

## 4. Missing data (explicit, never imputed)
- A component below its minimum evidence is `MISSING`, with `component_score = null` and `effective_weight = 0`. It is listed in `components_missing`.
- The remaining weights are renormalised: `effective_weight = base_weight / Σ base_weight(OK)`.
- `data_coverage_pct = Σ base_weight(OK) × 100`.
- If coverage is below **60%** or fewer than **3** components are OK, the tier is `INSUFFICIENT_DATA` and the decision is `REVIEW_DATA`, even if a partial score exists.

## 5. Aggregation, tier and decision
`reliability_score = Σ effective_weight × component_score`, which is exactly Σ `weighted_contribution` in `SupplierScoreComponent`.

| Tier | Rule | Decision |
|---|---|---|
| INSUFFICIENT_DATA | coverage < 60% or < 3 components | `REVIEW_DATA` |
| HIGH | score ≥ 85 | `APPROVED` (unless an open HIGH risk → `ESCALATE`) |
| MEDIUM | 70 ≤ score < 85 | `MONITOR` (unless an open HIGH risk → `ESCALATE`) |
| LOW | score < 70 | `ESCALATE` |

`top_drivers` names the two components that lose the most weighted points (`effective_weight × (100 − score)`), giving a planner the "why" at a glance.

## 6. Worked example (`SCORE-2026-000001`, SUP-000001 at 2026-03-31)
| component | numerator / denominator | metric | score | eff. weight | contribution |
|---|---|---|---:|---:|---:|
| delivery | 4 / 4 shipments | 100% on time | 100.00 | 0.30 | 30.00 |
| quality | 2 / 399 units | 0.50% rejected | 97.49 | 0.25 | 24.37 |
| cost | 0 / 98,689.88 USD | 0% variance | 100.00 | 0.15 | 15.00 |
| capacity | 3 / 3 months | 100% | 100.00 | 0.10 | 10.00 |
| compliance | audit 90.3 | no penalty | 90.30 | 0.10 | 9.03 |
| disruption | 0 points / 0 signals | — | 100.00 | 0.10 | 10.00 |
| **total** | | | | | **98.40 → HIGH → APPROVED** |

## 7. Hidden profiles used only for generation and evaluation
| Profile | Share | Generation rule |
|---|---:|---|
| STABLE | 45% | delay ~ N(−3, 2.5) days vs required date; reject p = 0.8%; 8% of invoices vary |
| DETERIORATING | 20% | mean delay grows 1.3 days per order month |
| QUALITY_INCIDENT | 20% | reject p = 22% inside 1–2 hidden 35–60-day incident windows, else 2% |
| SPARSE | 10% | ~12% PO volume; inspections 10× rarer; 65% invoices and 60% audits missing |
| CAPACITY_STRAINED | 5% | contracted capacity 120–300 units/site/month vs 2.5× order volume |

These live in `ground_truth/supplier_profiles.csv` and are never scoring inputs. A test deletes `ground_truth/` and confirms the scores are unchanged.
