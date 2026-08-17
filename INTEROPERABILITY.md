# INTEROPERABILITY

Stato reale dell'implementazione (non una lista di intenti): ogni riga marcata
"sì" è coperta da un test in `tests/` — vedi `tests/test_hl7_ack.py` per il
capitolo 2 dello standard. Il profilo attivo su una specifica installazione è
esposto a runtime da `GET /api/interop` e dal pulsante "Profilo interop" della
dashboard, perché riflette la configurazione e non solo il codice.

## HL7 v2 — messaggi

| Messaggio | Direzione | Stato |
|---|---|---|
| ORM^O01, OML^O21 | LIS → middleware | sì (`hl7.parse_order`) |
| ADT^A0x (registrazione paziente) | LIS → middleware | sì, ACK positivo senza creare ordini (`_handle_adt`) |
| ORU^R01, OUL^R2x | strumento → middleware | sì (`hl7.parse_result`) |
| ORU^R01 | middleware → LIS | sì (`hl7.build_oru`) |
| ACK^\<trigger\>^ACK | entrambe | sì (`hl7.build_ack`) |
| ORR^O02 / ORL^O22 | middleware → LIS | sì, opzionale (`order_response_mode: "order"`) |
| QBP^Q11 / RSP^K11 (query worklist) | — | no |

## HL7 v2 — riscontro (capitolo 2.9)

| Funzione | Stato |
|---|---|
| Original mode (singolo ACK applicativo AA/AE/AR) | sì |
| Enhanced mode (MSH-15/MSH-16, commit ACK CA/CE/CR + ACK applicativo) | sì, in ingresso e in uscita |
| MSH-16 assente ⇒ regole original mode (ACK applicativo comunque inviato) | sì |
| MSH-15/16 = `NE` ⇒ nessuna risposta | sì |
| Segmento ERR con tabella HL7 0357 (ERR-3/ERR-4) | sì |
| Forma legacy ERR-1 (ELD) per versioni ≤ 2.3.1 | sì |
| MSH-9 dell'ACK = `ACK^<trigger>^ACK` | sì |
| MSH-11 / MSH-12 echeggiati dal messaggio in ingresso | sì |
| MSA-2 = MSH-10 del messaggio riscontrato | sì |
| Verifica del control id sull'ACK ricevuto | sì (`expected_control_id`) |
| Un `CA` non viene scambiato per esito applicativo | sì |
| Sequence number protocol (MSH-13 / MSA-4) | no |

## Trasporto

| Funzione | Stato |
|---|---|
| MLLP `0x0B … 0x1C 0x0D` | sì |
| Più messaggi sulla stessa connessione (connessione persistente) | sì |
| Messaggi accodati nello stesso segmento TCP (pipelining) | sì |
| Batch protocol FHS/BHS in ingresso (un ACK per messaggio) | sì |
| Risposta batch aggregata (BHS/BTS in uscita) | no |
| Delimitatori non standard letti da MSH-1/MSH-2 | sì |
| Retry con backoff sugli errori di trasporto | sì |
| TLS/mTLS sul canale MLLP | no — usare stunnel o VPN (`vpn/README.md`) |

## Affidabilità

| Funzione | Stato |
|---|---|
| Idempotenza sulle ritrasmissioni (MSH-3 + MSH-10 + impronta contenuto) | sì |
| Riuso improprio di MSH-10 con contenuto diverso: elaborato + audit | sì |
| Tracciamento di ogni scambio con esito (`message_log`, `/api/messages`) | sì |
| Retry/backoff persistente con DLQ nel Forwarder | no (backlog) |

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
- POCT1-A2/POCT01 tramite adapter dedicati (`hl7mw/adapters/hemoscreen_poct1a2.py`).
