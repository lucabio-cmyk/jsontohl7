# INTEROPERABILITY

## HL7 v2 (current + MVP)
- ADT^A01/A04/A08/A40 (patient/encounter/merge)
- ORM/OML (orders)
- ORU/OUL (results)
- ACK/NACK with retry/idempotency

## FHIR REST (target)
- Patient, Encounter, ServiceRequest, Specimen
- Observation, DiagnosticReport
- Device, Practitioner, AuditEvent, Provenance, Task

## Canonical mapping principles
- Adapter-specific Z-segments permessi solo nel connector.
- Core senza hardcoding vendor.
- LOINC per test code, UCUM per unità.

## IHE/CLSI
- IHE LAW per flussi work order/risultati quando disponibile.
- POCT1-A2/POCT01 tramite adapter dedicati.
