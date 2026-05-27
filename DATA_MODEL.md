# DATA MODEL

## Canonical entities (MVP+)
- Organization, Facility, Location, Ward
- Patient, Encounter
- Order, Specimen
- TestDefinition, TestPanel
- Result, ResultComponent, ResultVersion, ResultFlag, ResultComment
- Device, DeviceModel, DeviceAdapter, DeviceStatus, DeviceEvent
- Operator, OperatorCredential, TrainingRecord, CompetencyRecord
- Lot, Reagent, Consumable
- QCDefinition, QCEvent, QCResult
- Rule, RuleVersion, RuleExecution
- InboundMessage, OutboundMessage, MessageAcknowledgement
- AuditEvent, ProvenanceRecord

## Invariants
- Ogni risultato ha `result_id`, `status`, `created_at`, `updated_at`, `version`.
- Raw payload immutabile collegato a `InboundMessage`.
- Ogni trasformazione crea evento audit/provenance.

## Result state machine
`received -> parsed -> normalized -> matched -> quality_checked -> (auto_validated | pending_review | quarantined) -> released -> sent_to_lis -> acknowledged_by_lis`

Terminali: `corrected`, `cancelled`, `rejected`, `archived`.
