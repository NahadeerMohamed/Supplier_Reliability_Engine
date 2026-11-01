# Supplier Reliability Scoring Engine

Scores supplier reliability using delivery, quality, cost, capacity, compliance and disruption signals, built on synthetic data that is generated, checked and reproducible.

## Tools used
| Tool | Used for |
|---|---|
| Python 3.13 | Main language |
| pandas, numpy | Data generation, scoring, validation |
| pytest | Automated tests |
| SQLite | Loading the exported `seed.sql` |

## What we built
- **Data generator:** creates 1,000 suppliers, 10,000 purchase orders, 15,000 shipments and 20,000 inspections. It also creates sites, contracts, PO lines, delivery events, invoices and risk signals. A fixed seed (`20261022`) means it always produces the same data.
- **Edge cases:** about 20 labelled bad records of each type: shipment with no PO, delivery before dispatch, score using future data, risk signal with no source or time, and supplier sites mixed up. They are listed separately in `ground_truth/`.
- **Scoring engine:** gives each supplier a 0–100 score from six weighted parts (delivery 30%, quality 25%, cost 15%, capacity 10%, compliance 10%, disruption 10%) and a decision: APPROVED, MONITOR, ESCALATE or REVIEW_DATA.
- **Validator:** checks IDs, links between tables, dates, status flows, totals and scores.
- **Outputs:** CSV, JSON and SQL files, plus reports.

## Process we followed
1. Read the problem statement and reviewed the existing draft code against it.
2. Wrote a plan and a proposal ([docs/PROPOSAL.md](docs/PROPOSAL.md)).
3. Defined all tables and fields in one place ([src/schema.py](src/schema.py)), then built the generator, edge cases, scoring, validation and reports.
4. Ran it, fixed the issues it found, and wrote 9 automated tests.
5. Tested from a fresh install: all tests pass, all 100 planted bad records are caught, and two runs with the same seed give identical files.

## How it works
```
generate data → add labelled bad records → set bad records aside → score suppliers → export files → validate → evaluate
```
1. Master data (suppliers, sites, contracts) is created first, then orders, shipments, inspections, invoices and risk signals that use valid IDs.
2. Bad records are added, and their labels are stored separately in `ground_truth/`. The scoring never reads that folder.
3. Before scoring, the engine moves bad records to an exception log, so they can't affect any score.
4. Each supplier is scored at 4 dates (end of Mar, Jun, Sep, Oct 2026) using only data available on that date. Each score shows how every part was calculated. If there isn't enough data, the decision is REVIEW_DATA instead of a guessed score.
5. The validator compares what it finds with the labelled list. The run passes only if every bad record is caught and nothing else is flagged.

## How to run
```bash
python -m venv .venv
.venv\Scripts\activate          # Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt

python -m src.pipeline --scale full --repro   # generate, score, validate (~25 s)
pytest -q                                     # 9 tests
```
Results are written to `data/full/`. Use `--scale starter` for a small, quick run.

More detail: [docs/SCORING.md](docs/SCORING.md) · [docs/ERD.md](docs/ERD.md) · [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md)
