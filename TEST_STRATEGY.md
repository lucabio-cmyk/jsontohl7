# TEST STRATEGY

- Unit test: parser HL7, rule evaluation, state transitions.
- Integration test: MLLP adapter, store, forwarder ACK/NACK.
- Contract test: OpenAPI/FHIR payload validation.
- E2E test: flussi A-F con simulatori device/LIS/ADT.
- Security test: RBAC/ABAC, break-glass, audit immutability.
- Resilience test: downtime/retry/store-and-forward.

## Traceability matrix
Mantenere matrice `requirement -> risk -> test -> evidence -> status` in CI artifact.
