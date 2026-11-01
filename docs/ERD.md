# Entity-Relationship Model

```mermaid
erDiagram
    Supplier ||--|{ Site : "owns (1:N)"
    Site ||--|{ Contract : "covered by (1:N)"
    Supplier ||--|{ Contract : "signs (1:N)"
    Contract ||--o{ PurchaseOrder : "governs (1:N)"
    PurchaseOrder ||--|{ POLine : "has (1:N)"
    PurchaseOrder ||--|{ POStatusEvent : "history (1:N)"
    PurchaseOrder ||--o{ Shipment : "fulfilled by (1:N)"
    Shipment ||--|{ ShipmentLine : "carries"
    POLine ||--|{ ShipmentLine : "split across"
    Shipment ||--|{ DeliveryEvent : "tracked by (1:N)"
    Shipment ||--o{ Inspection : "inspected (0..N)"
    Shipment ||--o| Invoice : "billed (0..1)"
    Supplier ||--o{ RiskSignal : "exposed to (0..N)"
    Supplier ||--|{ SupplierScore : "scored (1 per cutoff per run)"
    SupplierScore ||--|{ SupplierScoreComponent : "explained by (1:6)"

    Supplier { string supplier_id PK }
    Site { string site_id PK  string supplier_id FK }
    Contract { string contract_id PK  string supplier_id FK  string site_id FK  date start_date  date end_date }
    PurchaseOrder { string po_id PK  string contract_id FK  string site_id FK  date required_date  string status "derived" }
    POLine { string po_line_id PK  string po_id FK  int ordered_qty }
    POStatusEvent { string po_event_id PK  string po_id FK  string status  datetime event_timestamp }
    Shipment { string shipment_id PK  string po_id FK  date dispatch_date  date delivery_date  string status "derived" }
    ShipmentLine { string shipment_line_id PK  string shipment_id FK  string po_line_id FK  int shipped_qty }
    DeliveryEvent { string delivery_event_id PK  string shipment_id FK  string event_type  datetime event_timestamp }
    Inspection { string inspection_id PK  string shipment_id FK  int inspected_qty  int rejected_qty }
    Invoice { string invoice_id PK  string shipment_id FK  decimal variance_amount }
    RiskSignal { string risk_signal_id PK  string supplier_id FK  datetime signal_timestamp  string source }
    SupplierScore { string score_id PK  string supplier_id FK  date score_timestamp  decimal reliability_score }
    SupplierScoreComponent { string score_component_id PK  string score_id FK  string component }
```

## Cardinality, optionality and effective dates

| Relationship | Cardinality | Optional? | Notes |
|---|---|---|---|
| Supplier → Site | 1:N | no (1–3 sites) | `SITE-<supplier_id>-NN`; the embedded supplier ID must equal `supplier_id`. |
| Site → Contract | 1:N | no | One active contract per site with `start_date`/`end_date` effective dates. |
| Contract → PurchaseOrder | 1:N | yes | `order_date` must fall within the contract's effective dates. |
| PurchaseOrder → POLine | 1:N | no (1–4) | `total_amount` = Σ `line_total`. |
| PurchaseOrder → Shipment | 1:N | yes (late POs may be unshipped) | Split deliveries allowed. `Shipment.po_id` is required. |
| Shipment ↔ POLine | **N:M** | — | Bridge table **ShipmentLine**: a shipment carries several lines and a line can be split. |
| Shipment → DeliveryEvent | 1:N | no (≥ CREATED) | Append-only. |
| Shipment → Inspection | 1:0..N | yes | Only after delivery. `(shipment_id, lot_number)` is unique. |
| Shipment → Invoice | 1:0..1 | yes | Missing invoices are real missing data, not zero-variance. |
| Supplier → RiskSignal | 1:0..N | yes | `RSK-<supplier_id>-2026-NN`, a per-supplier sequence. |
| Supplier → SupplierScore | 1:N | — | One per cutoff per `score_run_id`. |
| SupplierScore → SupplierScoreComponent | 1:6 | — | Exactly six components, including MISSING ones. |

## Lifecycle states (history is append-only, never overwritten)

**Shipment** (`DeliveryEvent`): `CREATED → DISPATCHED → IN_TRANSIT → [DELAYED →] DELIVERED`.
`Shipment.status`, `dispatch_date` and `delivery_date` are *derived* from the latest and first matching events.
DELAYED is emitted the day after `required_date` if the shipment is still not delivered.

**PurchaseOrder** (`POStatusEvent`): `OPEN → [PARTIALLY_RECEIVED →] RECEIVED → CLOSED`.
`PARTIALLY_RECEIVED` fires on the first receipt of a split PO, `RECEIVED` when all shipments are delivered, and `CLOSED` when all are invoiced.
`PurchaseOrder.status` is the latest event.

**RiskSignal**: `OPEN → RESOLVED` (`resolved_timestamp`). Scoring evaluates "open at cutoff" point-in-time, so a signal resolved after the cutoff counts as open.

## Join validation
Every FK, every denormalised reference (shipment ↔ PO supplier/site, inspection/invoice ↔ shipment, PO ↔ contract ↔ site ↔ supplier) and every bridge row (PO line belongs to the shipment's PO) is checked by `src/validation.py`. See `validation_summary.json`.
