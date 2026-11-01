# Data Dictionary

_Generated from `src/schema.py` by `python -m src.docs` - do not edit by hand._

Types: STRING, INTEGER, DECIMAL, DATE (`YYYY-MM-DD`), DATETIME (`YYYY-MM-DDTHH:MM:SS`, local plant time), JSON (string).
Req = required (non-null). Uniq = unique across the table.

## Relationships

| Parent | Child | Cardinality | Note |
|---|---|---|---|
| Supplier | Site | 1:N | Each site belongs to exactly one supplier; supplier has 1-3 sites. |
| Supplier | Contract | 1:N | Via site; one active contract per site. |
| Site | Contract | 1:N | Contract is scoped to one site. |
| Contract | PurchaseOrder | 1:N | PO raised under one contract; order_date within effective dates. |
| PurchaseOrder | POLine | 1:N | 1-4 lines per PO. |
| PurchaseOrder | POStatusEvent | 1:N | Append-only lifecycle history. |
| PurchaseOrder | Shipment | 1:N | Split deliveries allowed; Shipment.po_id required. |
| Shipment | POLine | N:M | Bridge ShipmentLine: a shipment carries several lines; a line can be split. |
| Shipment | DeliveryEvent | 1:N | Append-only tracking events. |
| Shipment | Inspection | 1:N (optional) | 0..n inspections; only after delivery. |
| Shipment | Invoice | 1:0..1 | At most one invoice per shipment; optional (may be missing). |
| Supplier | RiskSignal | 1:N (optional) | 0..n signals. |
| Supplier | SupplierScore | 1:N | One score per supplier per cutoff per run. |
| SupplierScore | SupplierScoreComponent | 1:6 | Exactly six components per score. |

## Supplier

Legal supplier entity (master data).

- **Kind:** source  
- **Primary key:** `supplier_id` (pattern `^SUP-\d{6}$`)

| Field | Type | Req | Uniq | Allowed values | Unit | FK | Validation rule | Description |
|---|---|---|---|---|---|---|---|---|
| `supplier_id` | STRING | Y | Y |  |  |  | Immutable; zero-padded counter. | Supplier identifier SUP-NNNNNN. |
| `supplier_name` | STRING | Y |  |  |  |  |  | Synthetic display name. |
| `category` | STRING | Y |  | ELECTRONICS, PACKAGING, RAW_MATERIALS, MECHANICAL, CHEMICALS |  |  |  | Primary commodity category. |
| `country` | STRING | Y |  | IN, US, DE, SG, VN, MX, CN, PL |  |  |  | ISO-3166 alpha-2 country of registration. |
| `supplier_tier` | STRING | Y |  | STRATEGIC, PREFERRED, APPROVED |  |  |  | Commercial segmentation tier. |
| `onboarding_date` | DATE | Y |  |  |  |  | <= first site effective_from. | Date the supplier was approved. |
| `status` | STRING | Y |  | ACTIVE, ON_HOLD |  |  |  | Master-data status at extract date. |

## Site

Physical supplier site; Supplier 1:N Site (1-3 per supplier).

- **Kind:** source  
- **Primary key:** `site_id` (pattern `^SITE-SUP-\d{6}-\d{2}$`)

| Field | Type | Req | Uniq | Allowed values | Unit | FK | Validation rule | Description |
|---|---|---|---|---|---|---|---|---|
| `site_id` | STRING | Y | Y |  |  |  | Embedded supplier ID must equal supplier_id. | SITE-<supplier_id>-NN. |
| `supplier_id` | STRING | Y |  |  |  | Supplier.supplier_id |  | Owning supplier. |
| `site_seq` | INTEGER | Y |  |  |  |  | >= 1 | Site sequence within supplier (1..3). |
| `site_name` | STRING | Y |  |  |  |  |  | Display name. |
| `site_type` | STRING | Y |  | MANUFACTURING, WAREHOUSE, DISTRIBUTION |  |  |  | Site function. |
| `country` | STRING | Y |  | IN, US, DE, SG, VN, MX, CN, PL |  |  |  | ISO-3166 alpha-2. |
| `address` | STRING | Y | Y |  |  |  | An address belongs to exactly one supplier (no conflation). | Synthetic street address. |
| `effective_from` | DATE | Y |  |  |  |  | >= Supplier.onboarding_date | Site active from. |
| `status` | STRING | Y |  | ACTIVE |  |  |  | Site status. |

## Contract

Supply agreement per supplier site; Site 1:N Contract.

- **Kind:** source  
- **Primary key:** `contract_id` (pattern `^CON-2026-\d{6}$`)

| Field | Type | Req | Uniq | Allowed values | Unit | FK | Validation rule | Description |
|---|---|---|---|---|---|---|---|---|
| `contract_id` | STRING | Y | Y |  |  |  |  | CON-2026-NNNNNN. |
| `supplier_id` | STRING | Y |  |  |  | Supplier.supplier_id |  | Contracting supplier. |
| `site_id` | STRING | Y |  |  |  | Site.site_id | Site must belong to supplier_id. | Supplying site. |
| `start_date` | DATE | Y |  |  |  |  | start_date < end_date | Effective start. |
| `end_date` | DATE | Y |  |  |  |  |  | Effective end. |
| `currency` | STRING | Y |  | USD |  |  |  | Contract currency. |
| `payment_terms_days` | INTEGER | Y |  |  | days |  |  | Net payment terms. |
| `price_tolerance_pct` | DECIMAL | Y |  |  | % |  |  | Allowed invoice/PO price variance. |
| `capacity_units_month` | INTEGER | Y |  |  | units/month |  | > 0 | Contracted monthly capacity. |
| `certification_type` | STRING | Y |  | ISO9001 |  |  |  | Quality certification held. |
| `certification_expiry_date` | DATE | Y |  |  |  |  |  | Certification expiry; lapsed if < cutoff. |
| `last_audit_date` | DATE | N |  |  |  |  |  | Most recent supplier audit (pre-observation). |
| `last_audit_score` | DECIMAL | N |  |  | points 0-100 |  | 0..100 | Audit result; null = no audit on file. |
| `status` | STRING | Y |  | ACTIVE |  |  |  | Contract status. |

## PurchaseOrder

Purchase order header; Contract 1:N PurchaseOrder.

- **Kind:** source  
- **Primary key:** `po_id` (pattern `^PO-2026-\d{6}$`)
- **Lifecycle:** OPEN -> PARTIALLY_RECEIVED -> RECEIVED -> CLOSED (OPEN -> RECEIVED allowed)

| Field | Type | Req | Uniq | Allowed values | Unit | FK | Validation rule | Description |
|---|---|---|---|---|---|---|---|---|
| `po_id` | STRING | Y | Y |  |  |  |  | PO-2026-NNNNNN (chronological). |
| `supplier_id` | STRING | Y |  |  |  | Supplier.supplier_id |  | Supplier. |
| `site_id` | STRING | Y |  |  |  | Site.site_id | Site belongs to supplier. | Supplying site. |
| `contract_id` | STRING | Y |  |  |  | Contract.contract_id | Same supplier and site; order_date within contract dates. | Governing contract. |
| `planner_id` | STRING | Y |  |  |  |  |  | Planner who raised the PO (PLN-NNN). |
| `order_date` | DATE | Y |  |  |  |  |  | Order date. |
| `required_date` | DATE | Y |  |  |  |  | > order_date | Date goods are required at the dock. |
| `currency` | STRING | Y |  | USD |  |  |  | Currency. |
| `total_amount` | DECIMAL | Y |  |  | USD |  |  | Derived: sum of POLine.line_total. |
| `line_count` | INTEGER | Y |  |  |  |  |  | Derived: number of POLines. |
| `status` | STRING | Y |  | OPEN, PARTIALLY_RECEIVED, RECEIVED, CLOSED |  |  |  | Derived: latest POStatusEvent.status at extract. |

## POLine

Purchase order line; PurchaseOrder 1:N POLine (1-4).

- **Kind:** source  
- **Primary key:** `po_line_id` (pattern `^POL-2026-\d{7}$`)

| Field | Type | Req | Uniq | Allowed values | Unit | FK | Validation rule | Description |
|---|---|---|---|---|---|---|---|---|
| `po_line_id` | STRING | Y | Y |  |  |  |  | POL-2026-NNNNNNN. |
| `po_id` | STRING | Y |  |  |  | PurchaseOrder.po_id |  | Parent PO. |
| `line_number` | INTEGER | Y |  |  |  |  | Unique per po_id | Line number within PO. |
| `item_id` | STRING | Y |  |  |  |  |  | Synthetic item code ITEM-NNNNN. |
| `ordered_qty` | INTEGER | Y |  |  | units |  | > 0 | Ordered quantity. |
| `unit_price` | DECIMAL | Y |  |  | USD/unit |  | > 0 | Contract unit price. |
| `line_total` | DECIMAL | Y |  |  | USD |  |  | Derived: round(ordered_qty * unit_price, 2). |

## POStatusEvent

Append-only PO lifecycle history (never overwritten).

- **Kind:** event  
- **Primary key:** `po_event_id` (pattern `^POE-2026-\d{7}$`)

| Field | Type | Req | Uniq | Allowed values | Unit | FK | Validation rule | Description |
|---|---|---|---|---|---|---|---|---|
| `po_event_id` | STRING | Y | Y |  |  |  |  | POE-2026-NNNNNNN. |
| `po_id` | STRING | Y |  |  |  | PurchaseOrder.po_id |  | PO. |
| `event_seq` | INTEGER | Y |  |  |  |  |  | Order within PO, from 1. |
| `status` | STRING | Y |  | OPEN, PARTIALLY_RECEIVED, RECEIVED, CLOSED |  |  | Must follow PO lifecycle transitions. | New status. |
| `event_timestamp` | DATETIME | Y |  |  |  |  | Non-decreasing by event_seq; <= extract date. | When the transition happened. |
| `trigger` | STRING | Y |  | PO_RELEASED, FIRST_RECEIPT, FULL_RECEIPT, ALL_INVOICED |  |  |  | What caused the transition. |

## Shipment

Physical consignment; PurchaseOrder 1:N Shipment.

- **Kind:** source  
- **Primary key:** `shipment_id` (pattern `^SHP-2026-\d{6}$`)
- **Lifecycle:** CREATED -> DISPATCHED -> IN_TRANSIT -> [DELAYED ->] DELIVERED

| Field | Type | Req | Uniq | Allowed values | Unit | FK | Validation rule | Description |
|---|---|---|---|---|---|---|---|---|
| `shipment_id` | STRING | Y | Y |  |  |  |  | SHP-2026-NNNNNN. |
| `po_id` | STRING | Y |  |  |  | PurchaseOrder.po_id | Required: a shipment without a PO is an exception. | Primary PO. |
| `supplier_id` | STRING | Y |  |  |  | Supplier.supplier_id | Equals PO supplier. | Supplier. |
| `site_id` | STRING | Y |  |  |  | Site.site_id | Equals PO site. | Ship-from site. |
| `carrier_id` | STRING | Y |  |  |  |  |  | Carrier (CARRIER-NN). |
| `transport_mode` | STRING | Y |  | ROAD, SEA, AIR |  |  |  | Mode. |
| `receiving_warehouse_id` | STRING | Y |  |  |  |  |  | Receiving warehouse (WH-NN). |
| `created_date` | DATE | Y |  |  |  |  | >= PO order_date | Shipment created (ASN). |
| `dispatch_date` | DATE | N |  |  |  |  | >= created_date | Derived from DISPATCHED event; null if not yet dispatched. |
| `delivery_date` | DATE | N |  |  |  |  | >= dispatch_date | Derived from DELIVERED event; null if not delivered. |
| `shipped_qty_total` | INTEGER | Y |  |  | units |  |  | Derived: sum of ShipmentLine.shipped_qty. |
| `status` | STRING | Y |  | CREATED, IN_TRANSIT, DELAYED, DELIVERED |  |  |  | Derived from latest DeliveryEvent. |

## ShipmentLine

Bridge table Shipment N:M POLine (a line may be split across shipments).

- **Kind:** bridge  
- **Primary key:** `shipment_line_id` (pattern `^SHL-2026-\d{7}$`)

| Field | Type | Req | Uniq | Allowed values | Unit | FK | Validation rule | Description |
|---|---|---|---|---|---|---|---|---|
| `shipment_line_id` | STRING | Y | Y |  |  |  |  | SHL-2026-NNNNNNN. |
| `shipment_id` | STRING | Y |  |  |  | Shipment.shipment_id |  | Shipment. |
| `po_line_id` | STRING | Y |  |  |  | POLine.po_line_id | PO line belongs to the shipment's PO; (shipment_id, po_line_id) unique. | PO line. |
| `shipped_qty` | INTEGER | Y |  |  | units |  | > 0; sum per line <= ordered_qty | Units of the line on this shipment. |

## DeliveryEvent

Append-only shipment tracking events from carriers/warehouses.

- **Kind:** event  
- **Primary key:** `delivery_event_id` (pattern `^DEV-2026-\d{7}$`)

| Field | Type | Req | Uniq | Allowed values | Unit | FK | Validation rule | Description |
|---|---|---|---|---|---|---|---|---|
| `delivery_event_id` | STRING | Y | Y |  |  |  |  | DEV-2026-NNNNNNN. |
| `shipment_id` | STRING | Y |  |  |  | Shipment.shipment_id |  | Shipment. |
| `event_seq` | INTEGER | Y |  |  |  |  |  | Order within shipment, from 1. |
| `event_type` | STRING | Y |  | CREATED, DISPATCHED, IN_TRANSIT, DELAYED, DELIVERED |  |  | Must follow shipment lifecycle transitions. | Lifecycle event. |
| `event_timestamp` | DATETIME | Y |  |  |  |  | Non-decreasing by event_seq; <= extract date. | Event time. |
| `recorded_by` | STRING | Y |  | SUPPLIER, CARRIER, WAREHOUSE |  |  |  | Actor that recorded the event. |
| `location` | STRING | Y |  |  |  |  |  | Site, carrier or warehouse code. |

## Inspection

Incoming quality inspection of a delivered shipment; Shipment 1:N Inspection.

- **Kind:** source  
- **Primary key:** `inspection_id` (pattern `^INS-2026-\d{7}$`)

| Field | Type | Req | Uniq | Allowed values | Unit | FK | Validation rule | Description |
|---|---|---|---|---|---|---|---|---|
| `inspection_id` | STRING | Y | Y |  |  |  |  | INS-2026-NNNNNNN. |
| `shipment_id` | STRING | Y |  |  |  | Shipment.shipment_id |  | Inspected shipment. |
| `lot_number` | INTEGER | Y |  |  |  |  | (shipment_id, lot_number) unique | Inspection lot within the shipment, from 1. |
| `supplier_id` | STRING | Y |  |  |  | Supplier.supplier_id | Equals shipment supplier. | Supplier. |
| `inspector_id` | STRING | Y |  |  |  |  |  | Inspector (QA-NN). |
| `inspection_date` | DATE | Y |  |  |  |  | >= shipment delivery_date | Inspection date. |
| `inspected_qty` | INTEGER | Y |  |  | units |  | > 0; <= shipment shipped_qty_total | Sample size. |
| `accepted_qty` | INTEGER | Y |  |  | units |  |  | Accepted units. |
| `rejected_qty` | INTEGER | Y |  |  | units |  | accepted + rejected = inspected | Rejected units. |
| `defect_code` | STRING | Y |  | NONE, DIMENSIONAL, COSMETIC, FUNCTIONAL, PACKAGING, DOCUMENTATION |  |  | NONE iff rejected_qty = 0 | Dominant defect. |
| `result` | STRING | Y |  | ACCEPTED, ACCEPTED_WITH_DEVIATION, REJECTED |  |  |  | Derived from reject rate (0 / <=5% / >5%). |

## Invoice

Supplier invoice; Shipment 1:0..1 Invoice.

- **Kind:** source  
- **Primary key:** `invoice_id` (pattern `^INV-2026-\d{6}$`)

| Field | Type | Req | Uniq | Allowed values | Unit | FK | Validation rule | Description |
|---|---|---|---|---|---|---|---|---|
| `invoice_id` | STRING | Y | Y |  |  |  |  | INV-2026-NNNNNN. |
| `shipment_id` | STRING | Y | Y |  |  | Shipment.shipment_id |  | Invoiced shipment. |
| `po_id` | STRING | Y |  |  |  | PurchaseOrder.po_id | Equals shipment PO. | PO. |
| `supplier_id` | STRING | Y |  |  |  | Supplier.supplier_id | Equals shipment supplier. | Supplier. |
| `invoice_date` | DATE | Y |  |  |  |  | >= shipment delivery_date | Invoice date. |
| `due_date` | DATE | Y |  |  |  |  |  | Derived: invoice_date + contract payment terms. |
| `currency` | STRING | Y |  | USD |  |  |  | Currency. |
| `expected_amount` | DECIMAL | Y |  |  | USD |  |  | Derived: sum(shipped_qty * unit_price) for the shipment. |
| `invoiced_amount` | DECIMAL | Y |  |  | USD |  |  | Amount billed by supplier. |
| `variance_amount` | DECIMAL | Y |  |  | USD |  |  | Derived: invoiced_amount - expected_amount. |
| `variance_pct` | DECIMAL | Y |  |  | % |  |  | Derived: variance_amount / expected_amount * 100. |
| `match_status` | STRING | Y |  | MATCHED, PRICE_VARIANCE |  |  |  | Derived: \|variance_pct\| <= contract tolerance. |

## RiskSignal

Timestamped disruption / risk signal; Supplier 1:N RiskSignal.

- **Kind:** event  
- **Primary key:** `risk_signal_id` (pattern `^RSK-SUP-\d{6}-2026-\d{2}$`)

| Field | Type | Req | Uniq | Allowed values | Unit | FK | Validation rule | Description |
|---|---|---|---|---|---|---|---|---|
| `risk_signal_id` | STRING | Y | Y |  |  |  | Embedded supplier ID equals supplier_id. | RSK-<supplier_id>-2026-NN (per-supplier sequence). |
| `supplier_id` | STRING | Y |  |  |  | Supplier.supplier_id |  | Affected supplier. |
| `risk_type` | STRING | Y |  | WEATHER, PORT_CONGESTION, LABOR_STRIKE, FINANCIAL_DISTRESS, REGULATORY, GEOPOLITICAL, CYBER |  |  |  | Signal category. |
| `severity` | STRING | Y |  | LOW, MEDIUM, HIGH |  |  |  | Severity. |
| `signal_timestamp` | DATETIME | Y |  |  |  |  | Required for provenance. | When the signal was observed. |
| `source` | STRING | Y |  | NEWS_FEED, CARRIER_ALERT, SUPPLIER_SELF_REPORT, ANALYST_ENTRY, REGULATORY_DB |  |  | Required for provenance. | Signal provenance. |
| `resolved_timestamp` | DATETIME | N |  |  |  |  | >= signal_timestamp | When resolved; null = still open. |
| `status` | STRING | Y |  | OPEN, RESOLVED |  |  |  | Derived at extract date. |

## SupplierScore

Point-in-time reliability score; Supplier 1:N SupplierScore (one per cutoff per run).

- **Kind:** derived  
- **Primary key:** `score_id` (pattern `^SCORE-2026-\d{6}$`)

| Field | Type | Req | Uniq | Allowed values | Unit | FK | Validation rule | Description |
|---|---|---|---|---|---|---|---|---|
| `score_id` | STRING | Y | Y |  |  |  |  | SCORE-2026-NNNNNN. |
| `score_run_id` | STRING | Y |  |  |  |  |  | Scoring run (RUN-YYYY-MM-DD or ADHOC-...). |
| `supplier_id` | STRING | Y |  |  |  | Supplier.supplier_id |  | Scored supplier. |
| `model_version` | STRING | Y |  |  |  |  |  | Scoring model version. |
| `score_timestamp` | DATE | Y |  |  |  |  |  | Cutoff date (end of day); only data on/before it may be used. |
| `observation_start` | DATE | Y |  |  |  |  |  | Start of the look-back window. |
| `max_source_date` | DATE | N |  |  |  |  | <= score_timestamp (no future data) | Latest source date actually used. |
| `delivery_score` | DECIMAL | N |  |  | points 0-100 |  |  | On-time delivery %. |
| `quality_score` | DECIMAL | N |  |  | points 0-100 |  |  | 100 - 5 x reject %. |
| `cost_score` | DECIMAL | N |  |  | points 0-100 |  |  | 100 - 10 x absolute invoice variance %. |
| `capacity_score` | DECIMAL | N |  |  | points 0-100 |  |  | % of active months within contracted capacity. |
| `compliance_score` | DECIMAL | N |  |  | points 0-100 |  |  | Audit score less certification/regulatory penalties. |
| `disruption_score` | DECIMAL | N |  |  | points 0-100 |  |  | 100 - severity-weighted risk penalties. |
| `data_coverage_pct` | DECIMAL | Y |  |  | % |  |  | Sum of weights of available components x 100. |
| `components_missing` | STRING | N |  |  |  |  |  | Semicolon list of MISSING components. |
| `reliability_score` | DECIMAL | N |  |  | points 0-100 |  | = sum of component weighted_contribution | Weighted mean of available components (weights renormalised). |
| `reliability_tier` | STRING | Y |  | HIGH, MEDIUM, LOW, INSUFFICIENT_DATA |  |  |  | HIGH >=85, MEDIUM >=70, LOW; INSUFFICIENT_DATA if coverage < 60% or < 3 components. |
| `decision` | STRING | Y |  | APPROVED, MONITOR, ESCALATE, REVIEW_DATA |  |  |  | Recommended action. |
| `top_drivers` | STRING | N |  |  |  |  |  | Two components losing the most weighted points. |
| `open_high_risks` | INTEGER | Y |  |  |  |  |  | HIGH-severity risk signals open at cutoff. |
| `source_record_counts` | JSON | Y |  |  |  |  |  | Counts of source records used per entity. |

## SupplierScoreComponent

Explainability: one row per score x component with numerator/denominator.

- **Kind:** derived  
- **Primary key:** `score_component_id` (pattern `^SCC-2026-\d{7}$`)

| Field | Type | Req | Uniq | Allowed values | Unit | FK | Validation rule | Description |
|---|---|---|---|---|---|---|---|---|
| `score_component_id` | STRING | Y | Y |  |  |  |  | SCC-2026-NNNNNNN. |
| `score_id` | STRING | Y |  |  |  | SupplierScore.score_id |  | Parent score. |
| `component` | STRING | Y |  | delivery, quality, cost, capacity, compliance, disruption |  |  |  | Component name. |
| `status` | STRING | Y |  | OK, MISSING |  |  |  | OK or MISSING (below minimum evidence). |
| `numerator` | DECIMAL | N |  |  |  |  |  | Metric numerator (see denominator_definition). |
| `denominator` | DECIMAL | N |  |  |  |  |  | Metric denominator. |
| `denominator_definition` | STRING | Y |  |  |  |  |  | What the denominator counts. |
| `metric_value` | DECIMAL | N |  |  |  |  |  | Raw metric (e.g. on-time %, reject %). |
| `adjustment` | DECIMAL | Y |  |  | points |  |  | Penalty points applied after the metric (compliance only). |
| `formula` | STRING | Y |  |  |  |  |  | How component_score is derived from metric_value. |
| `component_score` | DECIMAL | N |  |  | points 0-100 |  |  | Normalised 0-100 score. |
| `base_weight` | DECIMAL | Y |  |  |  |  |  | Configured weight. |
| `effective_weight` | DECIMAL | Y |  |  |  |  |  | Renormalised weight (0 if MISSING). |
| `weighted_contribution` | DECIMAL | Y |  |  | points |  |  | effective_weight x component_score. |

## ExceptionLog

Records quarantined by the scoring engine's own rules (exception path).

- **Kind:** output  
- **Primary key:** `exception_id` (pattern `^EXC-2026-\d{6}$`)

| Field | Type | Req | Uniq | Allowed values | Unit | FK | Validation rule | Description |
|---|---|---|---|---|---|---|---|---|
| `exception_id` | STRING | Y | Y |  |  |  |  | EXC-2026-NNNNNN. |
| `rule_id` | STRING | Y |  | SHP_NO_PO, SHP_DATE_ORDER, RSK_PROVENANCE, SITE_CONFLATION |  |  |  | Detection rule. |
| `entity` | STRING | Y |  |  |  |  |  | Entity of the quarantined record. |
| `record_id` | STRING | Y |  |  |  |  |  | Quarantined record ID. |
| `supplier_id` | STRING | N |  |  |  |  |  | Supplier (as recorded on the row). |
| `reason` | STRING | Y |  |  |  |  |  | Human-readable reason. |
| `action` | STRING | Y |  | EXCLUDED_FROM_SCORING |  |  |  | Engine action. |
| `assigned_role` | STRING | Y |  | LOGISTICS_ANALYST, PLANNER, OPERATIONS_MANAGER |  |  |  | Actor that must resolve it. |
| `detected_at` | DATE | Y |  |  |  |  |  | Run date (extract date). |
| `status` | STRING | Y |  | OPEN |  |  |  | Workflow status. |

## GroundTruth

Labelled injected anomalies (ground_truth/). Never an input to scoring.

- **Kind:** ground_truth  
- **Primary key:** `gt_id` (pattern `^GT-2026-\d{5}$`)

| Field | Type | Req | Uniq | Allowed values | Unit | FK | Validation rule | Description |
|---|---|---|---|---|---|---|---|---|
| `gt_id` | STRING | Y | Y |  |  |  |  | GT-2026-NNNNN. |
| `issue_type` | STRING | Y |  | SHIPMENT_NO_PO, DELIVERY_BEFORE_DISPATCH, SCORE_USES_FUTURE_DATA, RISK_NO_SOURCE_OR_TIME, SITE_CONFLATION |  |  |  | Injected edge case. |
| `entity` | STRING | Y |  |  |  |  |  | Entity holding the anomalous record. |
| `record_id` | STRING | Y |  |  |  |  |  | Anomalous record ID. |
| `supplier_id` | STRING | N |  |  |  |  |  | Supplier involved. |
| `injection_method` | STRING | Y |  | NEW_ROW, MUTATED_ROW |  |  |  | NEW_ROW or MUTATED_ROW. |
| `field` | STRING | Y |  |  |  |  |  | Affected field(s). |
| `original_value` | STRING | N |  |  |  |  |  | Value before mutation (MUTATED_ROW). |
| `injected_value` | STRING | N |  |  |  |  |  | Anomalous value. |
| `expected_detection` | STRING | Y |  |  |  |  |  | Validation rule expected to fire. |
| `expected_decision` | STRING | Y |  |  |  |  |  | Expected engine/analyst decision. |

## SupplierProfileLabel

Hidden behavioural profile per supplier (ground_truth/). Never an input to scoring.

- **Kind:** ground_truth  
- **Primary key:** `supplier_id`

| Field | Type | Req | Uniq | Allowed values | Unit | FK | Validation rule | Description |
|---|---|---|---|---|---|---|---|---|
| `supplier_id` | STRING | Y | Y |  |  | Supplier.supplier_id |  | Supplier. |
| `profile` | STRING | Y |  | STABLE, DETERIORATING, QUALITY_INCIDENT, SPARSE, CAPACITY_STRAINED |  |  |  | Generation profile. |
| `risk_class` | STRING | Y |  | LOW_RISK, AT_RISK, UNKNOWN |  |  |  | Rule: STABLE->LOW_RISK; SPARSE->UNKNOWN; others->AT_RISK. |
| `expected_pattern` | STRING | Y |  |  |  |  |  | Documented generation rule. |
| `expected_final_decision` | STRING | Y |  | APPROVED_OR_MONITOR, MONITOR_OR_ESCALATE, REVIEW_DATA |  |  |  | Decision the documented rules should lead to at the final cutoff. |
