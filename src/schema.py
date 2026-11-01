"""Single source of truth for entities: fields, types, enums, keys and rules.

Drives the data dictionary (docs + JSON), SQL DDL, export formatting and the
generic part of validation.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .ids import PATTERNS


@dataclass(frozen=True)
class Field:
    name: str
    dtype: str                      # STRING | INTEGER | DECIMAL | DATE | DATETIME | JSON
    required: bool
    description: str
    allowed: tuple[str, ...] | None = None
    unit: str | None = None
    unique: bool = False
    fk: str | None = None           # "Entity.column"
    rule: str | None = None


@dataclass(frozen=True)
class Entity:
    name: str
    description: str
    primary_key: str
    fields: list[Field]
    lifecycle: str | None = None
    kind: str = "source"            # source | event | bridge | derived | output | ground_truth

    @property
    def columns(self) -> list[str]:
        return [f.name for f in self.fields]

    def field(self, name: str) -> Field:
        return next(f for f in self.fields if f.name == name)


CATEGORIES = ("ELECTRONICS", "PACKAGING", "RAW_MATERIALS", "MECHANICAL", "CHEMICALS")
COUNTRIES = ("IN", "US", "DE", "SG", "VN", "MX", "CN", "PL")
SUPPLIER_TIERS = ("STRATEGIC", "PREFERRED", "APPROVED")
SUPPLIER_STATUS = ("ACTIVE", "ON_HOLD")
SITE_TYPES = ("MANUFACTURING", "WAREHOUSE", "DISTRIBUTION")
PO_STATUS = ("OPEN", "PARTIALLY_RECEIVED", "RECEIVED", "CLOSED")
SHIPMENT_STATUS = ("CREATED", "IN_TRANSIT", "DELAYED", "DELIVERED")
DELIVERY_EVENT_TYPES = ("CREATED", "DISPATCHED", "IN_TRANSIT", "DELAYED", "DELIVERED")
TRANSPORT_MODES = ("ROAD", "SEA", "AIR")
INSPECTION_RESULTS = ("ACCEPTED", "ACCEPTED_WITH_DEVIATION", "REJECTED")
DEFECT_CODES = ("NONE", "DIMENSIONAL", "COSMETIC", "FUNCTIONAL", "PACKAGING", "DOCUMENTATION")
MATCH_STATUS = ("MATCHED", "PRICE_VARIANCE")
RISK_TYPES = ("WEATHER", "PORT_CONGESTION", "LABOR_STRIKE", "FINANCIAL_DISTRESS",
              "REGULATORY", "GEOPOLITICAL", "CYBER")
SEVERITIES = ("LOW", "MEDIUM", "HIGH")
RISK_SOURCES = ("NEWS_FEED", "CARRIER_ALERT", "SUPPLIER_SELF_REPORT", "ANALYST_ENTRY", "REGULATORY_DB")
RISK_STATUS = ("OPEN", "RESOLVED")
TIERS = ("HIGH", "MEDIUM", "LOW", "INSUFFICIENT_DATA")
DECISIONS = ("APPROVED", "MONITOR", "ESCALATE", "REVIEW_DATA")
COMPONENTS = ("delivery", "quality", "cost", "capacity", "compliance", "disruption")
COMPONENT_STATUS = ("OK", "MISSING")
ISSUE_TYPES = ("SHIPMENT_NO_PO", "DELIVERY_BEFORE_DISPATCH", "SCORE_USES_FUTURE_DATA",
               "RISK_NO_SOURCE_OR_TIME", "SITE_CONFLATION")

# Legal lifecycle transitions (from -> to). None = first event.
SHIPMENT_TRANSITIONS = {
    None: {"CREATED"},
    "CREATED": {"DISPATCHED"},
    "DISPATCHED": {"IN_TRANSIT"},
    "IN_TRANSIT": {"DELAYED", "DELIVERED"},
    "DELAYED": {"DELIVERED"},
}
PO_TRANSITIONS = {
    None: {"OPEN"},
    "OPEN": {"PARTIALLY_RECEIVED", "RECEIVED"},
    "PARTIALLY_RECEIVED": {"RECEIVED"},
    "RECEIVED": {"CLOSED"},
}
EVENT_TO_SHIPMENT_STATUS = {
    "CREATED": "CREATED", "DISPATCHED": "IN_TRANSIT", "IN_TRANSIT": "IN_TRANSIT",
    "DELAYED": "DELAYED", "DELIVERED": "DELIVERED",
}

F = Field

ENTITIES: list[Entity] = [
    Entity("Supplier", "Legal supplier entity (master data).", "supplier_id", [
        F("supplier_id", "STRING", True, "Supplier identifier SUP-NNNNNN.", unique=True, rule="Immutable; zero-padded counter."),
        F("supplier_name", "STRING", True, "Synthetic display name."),
        F("category", "STRING", True, "Primary commodity category.", CATEGORIES),
        F("country", "STRING", True, "ISO-3166 alpha-2 country of registration.", COUNTRIES),
        F("supplier_tier", "STRING", True, "Commercial segmentation tier.", SUPPLIER_TIERS),
        F("onboarding_date", "DATE", True, "Date the supplier was approved.", rule="<= first site effective_from."),
        F("status", "STRING", True, "Master-data status at extract date.", SUPPLIER_STATUS),
    ]),
    Entity("Site", "Physical supplier site; Supplier 1:N Site (1-3 per supplier).", "site_id", [
        F("site_id", "STRING", True, "SITE-<supplier_id>-NN.", unique=True, rule="Embedded supplier ID must equal supplier_id."),
        F("supplier_id", "STRING", True, "Owning supplier.", fk="Supplier.supplier_id"),
        F("site_seq", "INTEGER", True, "Site sequence within supplier (1..3).", rule=">= 1"),
        F("site_name", "STRING", True, "Display name."),
        F("site_type", "STRING", True, "Site function.", SITE_TYPES),
        F("country", "STRING", True, "ISO-3166 alpha-2.", COUNTRIES),
        F("address", "STRING", True, "Synthetic street address.", unique=True, rule="An address belongs to exactly one supplier (no conflation)."),
        F("effective_from", "DATE", True, "Site active from.", rule=">= Supplier.onboarding_date"),
        F("status", "STRING", True, "Site status.", ("ACTIVE",)),
    ]),
    Entity("Contract", "Supply agreement per supplier site; Site 1:N Contract.", "contract_id", [
        F("contract_id", "STRING", True, "CON-2026-NNNNNN.", unique=True),
        F("supplier_id", "STRING", True, "Contracting supplier.", fk="Supplier.supplier_id"),
        F("site_id", "STRING", True, "Supplying site.", fk="Site.site_id", rule="Site must belong to supplier_id."),
        F("start_date", "DATE", True, "Effective start.", rule="start_date < end_date"),
        F("end_date", "DATE", True, "Effective end."),
        F("currency", "STRING", True, "Contract currency.", ("USD",)),
        F("payment_terms_days", "INTEGER", True, "Net payment terms.", unit="days"),
        F("price_tolerance_pct", "DECIMAL", True, "Allowed invoice/PO price variance.", unit="%"),
        F("capacity_units_month", "INTEGER", True, "Contracted monthly capacity.", unit="units/month", rule="> 0"),
        F("certification_type", "STRING", True, "Quality certification held.", ("ISO9001",)),
        F("certification_expiry_date", "DATE", True, "Certification expiry; lapsed if < cutoff."),
        F("last_audit_date", "DATE", False, "Most recent supplier audit (pre-observation)."),
        F("last_audit_score", "DECIMAL", False, "Audit result; null = no audit on file.", unit="points 0-100", rule="0..100"),
        F("status", "STRING", True, "Contract status.", ("ACTIVE",)),
    ]),
    Entity("PurchaseOrder", "Purchase order header; Contract 1:N PurchaseOrder.", "po_id", [
        F("po_id", "STRING", True, "PO-2026-NNNNNN (chronological).", unique=True),
        F("supplier_id", "STRING", True, "Supplier.", fk="Supplier.supplier_id"),
        F("site_id", "STRING", True, "Supplying site.", fk="Site.site_id", rule="Site belongs to supplier."),
        F("contract_id", "STRING", True, "Governing contract.", fk="Contract.contract_id", rule="Same supplier and site; order_date within contract dates."),
        F("planner_id", "STRING", True, "Planner who raised the PO (PLN-NNN)."),
        F("order_date", "DATE", True, "Order date."),
        F("required_date", "DATE", True, "Date goods are required at the dock.", rule="> order_date"),
        F("currency", "STRING", True, "Currency.", ("USD",)),
        F("total_amount", "DECIMAL", True, "Derived: sum of POLine.line_total.", unit="USD"),
        F("line_count", "INTEGER", True, "Derived: number of POLines."),
        F("status", "STRING", True, "Derived: latest POStatusEvent.status at extract.", PO_STATUS),
    ], lifecycle="OPEN -> PARTIALLY_RECEIVED -> RECEIVED -> CLOSED (OPEN -> RECEIVED allowed)"),
    Entity("POLine", "Purchase order line; PurchaseOrder 1:N POLine (1-4).", "po_line_id", [
        F("po_line_id", "STRING", True, "POL-2026-NNNNNNN.", unique=True),
        F("po_id", "STRING", True, "Parent PO.", fk="PurchaseOrder.po_id"),
        F("line_number", "INTEGER", True, "Line number within PO.", rule="Unique per po_id"),
        F("item_id", "STRING", True, "Synthetic item code ITEM-NNNNN."),
        F("ordered_qty", "INTEGER", True, "Ordered quantity.", unit="units", rule="> 0"),
        F("unit_price", "DECIMAL", True, "Contract unit price.", unit="USD/unit", rule="> 0"),
        F("line_total", "DECIMAL", True, "Derived: round(ordered_qty * unit_price, 2).", unit="USD"),
    ]),
    Entity("POStatusEvent", "Append-only PO lifecycle history (never overwritten).", "po_event_id", [
        F("po_event_id", "STRING", True, "POE-2026-NNNNNNN.", unique=True),
        F("po_id", "STRING", True, "PO.", fk="PurchaseOrder.po_id"),
        F("event_seq", "INTEGER", True, "Order within PO, from 1."),
        F("status", "STRING", True, "New status.", PO_STATUS, rule="Must follow PO lifecycle transitions."),
        F("event_timestamp", "DATETIME", True, "When the transition happened.", rule="Non-decreasing by event_seq; <= extract date."),
        F("trigger", "STRING", True, "What caused the transition.", ("PO_RELEASED", "FIRST_RECEIPT", "FULL_RECEIPT", "ALL_INVOICED")),
    ], kind="event"),
    Entity("Shipment", "Physical consignment; PurchaseOrder 1:N Shipment.", "shipment_id", [
        F("shipment_id", "STRING", True, "SHP-2026-NNNNNN.", unique=True),
        F("po_id", "STRING", True, "Primary PO.", fk="PurchaseOrder.po_id", rule="Required: a shipment without a PO is an exception."),
        F("supplier_id", "STRING", True, "Supplier.", fk="Supplier.supplier_id", rule="Equals PO supplier."),
        F("site_id", "STRING", True, "Ship-from site.", fk="Site.site_id", rule="Equals PO site."),
        F("carrier_id", "STRING", True, "Carrier (CARRIER-NN)."),
        F("transport_mode", "STRING", True, "Mode.", TRANSPORT_MODES),
        F("receiving_warehouse_id", "STRING", True, "Receiving warehouse (WH-NN)."),
        F("created_date", "DATE", True, "Shipment created (ASN).", rule=">= PO order_date"),
        F("dispatch_date", "DATE", False, "Derived from DISPATCHED event; null if not yet dispatched.", rule=">= created_date"),
        F("delivery_date", "DATE", False, "Derived from DELIVERED event; null if not delivered.", rule=">= dispatch_date"),
        F("shipped_qty_total", "INTEGER", True, "Derived: sum of ShipmentLine.shipped_qty.", unit="units"),
        F("status", "STRING", True, "Derived from latest DeliveryEvent.", SHIPMENT_STATUS),
    ], lifecycle="CREATED -> DISPATCHED -> IN_TRANSIT -> [DELAYED ->] DELIVERED"),
    Entity("ShipmentLine", "Bridge table Shipment N:M POLine (a line may be split across shipments).", "shipment_line_id", [
        F("shipment_line_id", "STRING", True, "SHL-2026-NNNNNNN.", unique=True),
        F("shipment_id", "STRING", True, "Shipment.", fk="Shipment.shipment_id"),
        F("po_line_id", "STRING", True, "PO line.", fk="POLine.po_line_id", rule="PO line belongs to the shipment's PO; (shipment_id, po_line_id) unique."),
        F("shipped_qty", "INTEGER", True, "Units of the line on this shipment.", unit="units", rule="> 0; sum per line <= ordered_qty"),
    ], kind="bridge"),
    Entity("DeliveryEvent", "Append-only shipment tracking events from carriers/warehouses.", "delivery_event_id", [
        F("delivery_event_id", "STRING", True, "DEV-2026-NNNNNNN.", unique=True),
        F("shipment_id", "STRING", True, "Shipment.", fk="Shipment.shipment_id"),
        F("event_seq", "INTEGER", True, "Order within shipment, from 1."),
        F("event_type", "STRING", True, "Lifecycle event.", DELIVERY_EVENT_TYPES, rule="Must follow shipment lifecycle transitions."),
        F("event_timestamp", "DATETIME", True, "Event time.", rule="Non-decreasing by event_seq; <= extract date."),
        F("recorded_by", "STRING", True, "Actor that recorded the event.", ("SUPPLIER", "CARRIER", "WAREHOUSE")),
        F("location", "STRING", True, "Site, carrier or warehouse code."),
    ], kind="event"),
    Entity("Inspection", "Incoming quality inspection of a delivered shipment; Shipment 1:N Inspection.", "inspection_id", [
        F("inspection_id", "STRING", True, "INS-2026-NNNNNNN.", unique=True),
        F("shipment_id", "STRING", True, "Inspected shipment.", fk="Shipment.shipment_id"),
        F("lot_number", "INTEGER", True, "Inspection lot within the shipment, from 1.", rule="(shipment_id, lot_number) unique"),
        F("supplier_id", "STRING", True, "Supplier.", fk="Supplier.supplier_id", rule="Equals shipment supplier."),
        F("inspector_id", "STRING", True, "Inspector (QA-NN)."),
        F("inspection_date", "DATE", True, "Inspection date.", rule=">= shipment delivery_date"),
        F("inspected_qty", "INTEGER", True, "Sample size.", unit="units", rule="> 0; <= shipment shipped_qty_total"),
        F("accepted_qty", "INTEGER", True, "Accepted units.", unit="units"),
        F("rejected_qty", "INTEGER", True, "Rejected units.", unit="units", rule="accepted + rejected = inspected"),
        F("defect_code", "STRING", True, "Dominant defect.", DEFECT_CODES, rule="NONE iff rejected_qty = 0"),
        F("result", "STRING", True, "Derived from reject rate (0 / <=5% / >5%).", INSPECTION_RESULTS),
    ]),
    Entity("Invoice", "Supplier invoice; Shipment 1:0..1 Invoice.", "invoice_id", [
        F("invoice_id", "STRING", True, "INV-2026-NNNNNN.", unique=True),
        F("shipment_id", "STRING", True, "Invoiced shipment.", fk="Shipment.shipment_id", unique=True),
        F("po_id", "STRING", True, "PO.", fk="PurchaseOrder.po_id", rule="Equals shipment PO."),
        F("supplier_id", "STRING", True, "Supplier.", fk="Supplier.supplier_id", rule="Equals shipment supplier."),
        F("invoice_date", "DATE", True, "Invoice date.", rule=">= shipment delivery_date"),
        F("due_date", "DATE", True, "Derived: invoice_date + contract payment terms."),
        F("currency", "STRING", True, "Currency.", ("USD",)),
        F("expected_amount", "DECIMAL", True, "Derived: sum(shipped_qty * unit_price) for the shipment.", unit="USD"),
        F("invoiced_amount", "DECIMAL", True, "Amount billed by supplier.", unit="USD"),
        F("variance_amount", "DECIMAL", True, "Derived: invoiced_amount - expected_amount.", unit="USD"),
        F("variance_pct", "DECIMAL", True, "Derived: variance_amount / expected_amount * 100.", unit="%"),
        F("match_status", "STRING", True, "Derived: |variance_pct| <= contract tolerance.", MATCH_STATUS),
    ]),
    Entity("RiskSignal", "Timestamped disruption / risk signal; Supplier 1:N RiskSignal.", "risk_signal_id", [
        F("risk_signal_id", "STRING", True, "RSK-<supplier_id>-2026-NN (per-supplier sequence).", unique=True, rule="Embedded supplier ID equals supplier_id."),
        F("supplier_id", "STRING", True, "Affected supplier.", fk="Supplier.supplier_id"),
        F("risk_type", "STRING", True, "Signal category.", RISK_TYPES),
        F("severity", "STRING", True, "Severity.", SEVERITIES),
        F("signal_timestamp", "DATETIME", True, "When the signal was observed.", rule="Required for provenance."),
        F("source", "STRING", True, "Signal provenance.", RISK_SOURCES, rule="Required for provenance."),
        F("resolved_timestamp", "DATETIME", False, "When resolved; null = still open.", rule=">= signal_timestamp"),
        F("status", "STRING", True, "Derived at extract date.", RISK_STATUS),
    ], kind="event"),
    Entity("SupplierScore", "Point-in-time reliability score; Supplier 1:N SupplierScore (one per cutoff per run).", "score_id", [
        F("score_id", "STRING", True, "SCORE-2026-NNNNNN.", unique=True),
        F("score_run_id", "STRING", True, "Scoring run (RUN-YYYY-MM-DD or ADHOC-...)."),
        F("supplier_id", "STRING", True, "Scored supplier.", fk="Supplier.supplier_id"),
        F("model_version", "STRING", True, "Scoring model version."),
        F("score_timestamp", "DATE", True, "Cutoff date (end of day); only data on/before it may be used."),
        F("observation_start", "DATE", True, "Start of the look-back window."),
        F("max_source_date", "DATE", False, "Latest source date actually used.", rule="<= score_timestamp (no future data)"),
        F("delivery_score", "DECIMAL", False, "On-time delivery %.", unit="points 0-100"),
        F("quality_score", "DECIMAL", False, "100 - 5 x reject %.", unit="points 0-100"),
        F("cost_score", "DECIMAL", False, "100 - 10 x absolute invoice variance %.", unit="points 0-100"),
        F("capacity_score", "DECIMAL", False, "% of active months within contracted capacity.", unit="points 0-100"),
        F("compliance_score", "DECIMAL", False, "Audit score less certification/regulatory penalties.", unit="points 0-100"),
        F("disruption_score", "DECIMAL", False, "100 - severity-weighted risk penalties.", unit="points 0-100"),
        F("data_coverage_pct", "DECIMAL", True, "Sum of weights of available components x 100.", unit="%"),
        F("components_missing", "STRING", False, "Semicolon list of MISSING components."),
        F("reliability_score", "DECIMAL", False, "Weighted mean of available components (weights renormalised).", unit="points 0-100", rule="= sum of component weighted_contribution"),
        F("reliability_tier", "STRING", True, "HIGH >=85, MEDIUM >=70, LOW; INSUFFICIENT_DATA if coverage < 60% or < 3 components.", TIERS),
        F("decision", "STRING", True, "Recommended action.", DECISIONS),
        F("top_drivers", "STRING", False, "Two components losing the most weighted points."),
        F("open_high_risks", "INTEGER", True, "HIGH-severity risk signals open at cutoff."),
        F("source_record_counts", "JSON", True, "Counts of source records used per entity."),
    ], kind="derived"),
    Entity("SupplierScoreComponent", "Explainability: one row per score x component with numerator/denominator.", "score_component_id", [
        F("score_component_id", "STRING", True, "SCC-2026-NNNNNNN.", unique=True),
        F("score_id", "STRING", True, "Parent score.", fk="SupplierScore.score_id"),
        F("component", "STRING", True, "Component name.", COMPONENTS),
        F("status", "STRING", True, "OK or MISSING (below minimum evidence).", COMPONENT_STATUS),
        F("numerator", "DECIMAL", False, "Metric numerator (see denominator_definition)."),
        F("denominator", "DECIMAL", False, "Metric denominator."),
        F("denominator_definition", "STRING", True, "What the denominator counts."),
        F("metric_value", "DECIMAL", False, "Raw metric (e.g. on-time %, reject %)."),
        F("adjustment", "DECIMAL", True, "Penalty points applied after the metric (compliance only).", unit="points"),
        F("formula", "STRING", True, "How component_score is derived from metric_value."),
        F("component_score", "DECIMAL", False, "Normalised 0-100 score.", unit="points 0-100"),
        F("base_weight", "DECIMAL", True, "Configured weight."),
        F("effective_weight", "DECIMAL", True, "Renormalised weight (0 if MISSING)."),
        F("weighted_contribution", "DECIMAL", True, "effective_weight x component_score.", unit="points"),
    ], kind="derived"),
]

OUTPUT_ENTITIES: list[Entity] = [
    Entity("ExceptionLog", "Records quarantined by the scoring engine's own rules (exception path).", "exception_id", [
        F("exception_id", "STRING", True, "EXC-2026-NNNNNN.", unique=True),
        F("rule_id", "STRING", True, "Detection rule.", ("SHP_NO_PO", "SHP_DATE_ORDER", "RSK_PROVENANCE", "SITE_CONFLATION")),
        F("entity", "STRING", True, "Entity of the quarantined record."),
        F("record_id", "STRING", True, "Quarantined record ID."),
        F("supplier_id", "STRING", False, "Supplier (as recorded on the row)."),
        F("reason", "STRING", True, "Human-readable reason."),
        F("action", "STRING", True, "Engine action.", ("EXCLUDED_FROM_SCORING",)),
        F("assigned_role", "STRING", True, "Actor that must resolve it.", ("LOGISTICS_ANALYST", "PLANNER", "OPERATIONS_MANAGER")),
        F("detected_at", "DATE", True, "Run date (extract date)."),
        F("status", "STRING", True, "Workflow status.", ("OPEN",)),
    ], kind="output"),
    Entity("GroundTruth", "Labelled injected anomalies (ground_truth/). Never an input to scoring.", "gt_id", [
        F("gt_id", "STRING", True, "GT-2026-NNNNN.", unique=True),
        F("issue_type", "STRING", True, "Injected edge case.", ISSUE_TYPES),
        F("entity", "STRING", True, "Entity holding the anomalous record."),
        F("record_id", "STRING", True, "Anomalous record ID."),
        F("supplier_id", "STRING", False, "Supplier involved."),
        F("injection_method", "STRING", True, "NEW_ROW or MUTATED_ROW.", ("NEW_ROW", "MUTATED_ROW")),
        F("field", "STRING", True, "Affected field(s)."),
        F("original_value", "STRING", False, "Value before mutation (MUTATED_ROW)."),
        F("injected_value", "STRING", False, "Anomalous value."),
        F("expected_detection", "STRING", True, "Validation rule expected to fire."),
        F("expected_decision", "STRING", True, "Expected engine/analyst decision."),
    ], kind="ground_truth"),
    Entity("SupplierProfileLabel", "Hidden behavioural profile per supplier (ground_truth/). Never an input to scoring.", "supplier_id", [
        F("supplier_id", "STRING", True, "Supplier.", unique=True, fk="Supplier.supplier_id"),
        F("profile", "STRING", True, "Generation profile.", tuple(["STABLE", "DETERIORATING", "QUALITY_INCIDENT", "SPARSE", "CAPACITY_STRAINED"])),
        F("risk_class", "STRING", True, "Rule: STABLE->LOW_RISK; SPARSE->UNKNOWN; others->AT_RISK.", ("LOW_RISK", "AT_RISK", "UNKNOWN")),
        F("expected_pattern", "STRING", True, "Documented generation rule."),
        F("expected_final_decision", "STRING", True, "Decision the documented rules should lead to at the final cutoff.", ("APPROVED_OR_MONITOR", "MONITOR_OR_ESCALATE", "REVIEW_DATA")),
    ], kind="ground_truth"),
]

BY_NAME = {e.name: e for e in ENTITIES + OUTPUT_ENTITIES}
TABLE_NAMES = [e.name for e in ENTITIES]

def id_pattern(entity_name: str) -> str | None:
    return PATTERNS.get(entity_name)


def foreign_keys(entity: Entity) -> list[tuple[str, str, str]]:
    """(column, parent_entity, parent_column) for every FK field."""
    out = []
    for f in entity.fields:
        if f.fk:
            parent, col = f.fk.split(".")
            out.append((f.name, parent, col))
    return out


CARDINALITY = [
    ("Supplier", "Site", "1:N", "Each site belongs to exactly one supplier; supplier has 1-3 sites."),
    ("Supplier", "Contract", "1:N", "Via site; one active contract per site."),
    ("Site", "Contract", "1:N", "Contract is scoped to one site."),
    ("Contract", "PurchaseOrder", "1:N", "PO raised under one contract; order_date within effective dates."),
    ("PurchaseOrder", "POLine", "1:N", "1-4 lines per PO."),
    ("PurchaseOrder", "POStatusEvent", "1:N", "Append-only lifecycle history."),
    ("PurchaseOrder", "Shipment", "1:N", "Split deliveries allowed; Shipment.po_id required."),
    ("Shipment", "POLine", "N:M", "Bridge ShipmentLine: a shipment carries several lines; a line can be split."),
    ("Shipment", "DeliveryEvent", "1:N", "Append-only tracking events."),
    ("Shipment", "Inspection", "1:N (optional)", "0..n inspections; only after delivery."),
    ("Shipment", "Invoice", "1:0..1", "At most one invoice per shipment; optional (may be missing)."),
    ("Supplier", "RiskSignal", "1:N (optional)", "0..n signals."),
    ("Supplier", "SupplierScore", "1:N", "One score per supplier per cutoff per run."),
    ("SupplierScore", "SupplierScoreComponent", "1:6", "Exactly six components per score."),
]
