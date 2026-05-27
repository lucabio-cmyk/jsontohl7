# PRODUCT ARCHITECTURE VISION (vNext)

## 1) Struttura applicativa proposta
Per supportare scalabilità, auditing e futuri moduli UI/API, è utile passare da una struttura "pipeline-only" ad una struttura modulare:

- **Ingestion Layer**: adapter HL7/ASTM/POCT1 + validazioni sintattiche.
- **Normalization Layer**: mapping verso un modello canonico unico.
- **Orchestration Layer**: rule engine, gating QC, gestione stati, retry e quarantine.
- **Persistence Layer**: storage operativo + audit + analytics.
- **Exposure Layer**: API (REST/FHIR), eventi, GUI operativa.

Questa separazione riduce accoppiamento, migliora testabilità e abilita deployment indipendenti.

## 2) Database consigliati (multi-store)
Una strategia realistica è usare database diversi per responsabilità diverse:

1. **PostgreSQL (OLTP principale)**
   - ordini, risultati, pazienti, encounter, stati workflow.
   - forte integrità referenziale e transazioni.

2. **TimescaleDB/estensione time-series su PostgreSQL**
   - telemetria strumenti, KPI time-series, trend QC.
   - query temporali efficienti.

3. **Redis**
   - cache, lock distribuiti, dedup temporanea, code veloci.

4. **Object Storage (S3-compatible)**
   - payload raw HL7, allegati, report, artefatti audit/versioning.

5. **OpenSearch/Elasticsearch (opzionale ma consigliato)**
   - ricerca full-text su eventi, troubleshooting operativo.

## 3) GUI di gestione e settaggio parametri
### Moduli principali della GUI
- **Dashboard Operativa**: stato flussi, backlog, errori critici, SLA.
- **Quarantine & Review**: code di eccezione, reprocess, override tracciato.
- **Rule Management**: versionamento regole, simulazione impatto, publish controllato.
- **Device & Connector Config**: endpoint, mapping, timeout, retry policy.
- **Quality & Compliance**: CAPA, audit trail, firme, evidenze.
- **Admin & Access**: RBAC, tenancy/siti, policy sicurezza.

### Principi UX
- Navigazione per ruolo (operatore, QA, admin, IT).
- Ogni azione sensibile con motivazione obbligatoria e audit automatico.
- Drill-down da KPI a singolo messaggio/evento.

## 4) KPI prioritari da visualizzare
1. **Throughput**: messaggi/ora, test/ora, per strumento/sito.
2. **Turnaround Time (TAT)**: p50/p95/p99 end-to-end e per fase.
3. **Quality Gate Pass Rate**: % record che passano i gate senza intervento.
4. **Quarantine Rate**: % e volumi assoluti in quarantine.
5. **Reprocess Success Rate**: successo al primo/secondo retry.
6. **Rule Impact KPI**: variazione errori prima/dopo una release regole.
7. **Device Reliability**: timeout, errori di comunicazione, downtime.
8. **Data Completeness**: campi obbligatori mancanti per feed/sorgente.
9. **Audit Latency**: tempo medio tra evento e persistenza audit.
10. **SLA Compliance**: % campioni entro soglia TAT definita.

## 5) Sequenza consigliata di implementazione
- **Step A**: consolidare schema PostgreSQL + audit trail.
- **Step B**: introdurre dashboard minima (throughput, TAT, quarantine).
- **Step C**: modulo GUI di configurazione regole/connector con versioning.
- **Step D**: layer analytics avanzato (trend, forecast, comparazione multi-sito).

