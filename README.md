# HL7 Middleware (LIS ⇄ strumenti)

Middleware **order-driven** tra LIS e strumenti di laboratorio (emocromo). Riceve gli
**ordini** dal LIS, riceve i **risultati** dagli strumenti, li **associa** all'ordine e
**inoltra** i risultati completi al LIS come ORU^R01 — gestendo gli ACK in entrambi i sensi.

Core a **zero dipendenze esterne** (solo stdlib Python ≥ 3.10; stato su SQLite).

```
   LIS ──ORM/OML──▶ [OrderReceiver] ─▶ store ◀─ [ResultReceiver] ◀──ORU── strumenti
                                          │
                                    [Forwarder] ──ORU──▶ LIS  (con ACK)
```

Dettaglio completo di flussi, ciclo di vita degli ordini e roadmap in **`ARCHITECTURE.md`**.

## Struttura

```
hl7mw/
  hl7.py         parsing/costruzione HL7v2 (ordini, risultati, ACK, ORU)
  mllp.py        trasporto MLLP: client + server
  store.py       persistenza SQLite + ciclo di vita + query per la UI
  pipeline.py    OrderReceiver, ResultReceiver, Forwarder, regola di associazione
  monitor.py     DeviceMonitor: heartbeat/status strumenti (ONLINE/OFFLINE), audit log
  webstatus.py   endpoint di stato di sola lettura, minimale (senza dipendenze)
  api.py         REST API + dashboard HTML (FastAPI, opzionale)
  cli.py         CLI operativa (ordini, retry, cancel, audit, stats)
  adapters/      adapter per strumenti non-HL7 standard (es. HemoScreen)
  run.py         runner del servizio (avvia tutti i componenti sopra)
tests/           test end-to-end e di unità (senza pytest, eseguibili singolarmente)
config.example.json
ARCHITECTURE.md  · CLAUDE.md (contesto per Claude Code)
```

## Avvio

```bash
cp config.example.json config.json     # adatta host/porte del LIS
pip install -e ".[api]"                # opzionale: abilita REST API + dashboard
python3 -m hl7mw.run -c config.json
# Order in:      :6661 (MLLP, dal LIS)
# Result in:     :6662 (MLLP, dagli strumenti)
# Stato UI:      http://127.0.0.1:8080  (sempre attivo, sola lettura, zero dipendenze)
# API/Dashboard: http://127.0.0.1:8000  (se "api_enabled": true e fastapi/uvicorn installati)
```

Il core (`hl7mw/`) resta a **zero dipendenze esterne**: se FastAPI/uvicorn non sono
installati, il servizio parte comunque — l'API viene semplicemente disabilitata con un
warning nei log, e restano attivi ordini/risultati/inoltro + Stato UI minimale.

## CLI operativa

```bash
python3 -m hl7mw.cli --db hl7mw.db orders [--status READY|ERROR|SENT]
python3 -m hl7mw.cli --db hl7mw.db order <sample_key>
python3 -m hl7mw.cli --db hl7mw.db retry <sample_key>      # ERROR -> READY
python3 -m hl7mw.cli --db hl7mw.db cancel <sample_key>
python3 -m hl7mw.cli --db hl7mw.db audit-log [--sample-key S] [--event-type X]
python3 -m hl7mw.cli --db hl7mw.db instruments
python3 -m hl7mw.cli --db hl7mw.db stats
python3 -m hl7mw.cli --db hl7mw.db unmatched
```

## Test

```bash
python3 tests/test_e2e.py               # loop completo ordine -> risultato -> inoltro
python3 tests/test_management_system.py # API/store v2: dashboard, retry, audit, instruments
python3 tests/test_ack_retry_backoff.py # retry/backoff su ACK del LIS
python3 tests/test_hemoscreen.py        # adapter strumento HemoScreen (HL7 e POCT1-A2)
```

## Stato attuale e prossimi passi

Funzionante e testato: ricezione ordini, ricezione/associazione risultati, inoltro al LIS
con ACK (inclusi retry automatici su errori transitori), gestione risultati orfani,
device monitoring con heartbeat/status, audit log clinico, REST API + dashboard web
(Chart.js), CLI operativa completa.

Da sviluppare (vedi `ARCHITECTURE.md` → Roadmap e `CLAUDE.md` → "Da fare"): adapter
**ASTM E1381/E1394** per strumenti non-HL7 generici, regola di completezza reale basata
sui test richiesti vs ricevuti, retry/backoff persistente con storico e DLQ, sicurezza
(TLS sul MLLP, autenticazione API, RBAC dashboard).
