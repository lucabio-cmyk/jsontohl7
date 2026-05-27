# RISK ANALYSIS (estratto)

| Rischio | Preventivo | Rilevazione | Mitigazione | Test |
|---|---|---|---|---|
| Paziente errato | matching multiplo + barcode | quarantine mismatch | riconciliazione autorizzata | workflow mismatch |
| Unità errata | UCUM + mapping versionato | validation rule | blocco release | parser+rule test |
| Duplicato | idempotency key | duplicate detector | reject/quarantine | e2e duplicate |
| ACK mancante | retry policy | ack timeout metric | retry + escalation | integration retry |
| QC fallito | QC gate | qc status checker | device/test lockout | workflow QC fail |
| Operatore non valido | competency check | auth event | quarantine + notify | workflow operator |
