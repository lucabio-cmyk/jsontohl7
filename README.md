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
  webstatus.py   endpoint di stato di sola lettura (aggancio UI)
  run.py         runner del servizio
tests/test_e2e.py  test end-to-end del loop completo (senza pytest)
config.example.json
ARCHITECTURE.md  · CLAUDE.md (contesto per Claude Code)
```

## Avvio

```bash
cp config.example.json config.json     # adatta host/porte del LIS
python3 -m hl7mw.run -c config.json
# Order in:   :6661 (MLLP, dal LIS)
# Result in:  :6662 (MLLP, dagli strumenti)
# Stato UI:   http://127.0.0.1:8080
```

## Test

```bash
python3 tests/test_e2e.py
# [1] ordine ricevuto  [2] risultato associato  [3] inoltro al LIS  [4] orfano -> unmatched
```

## Stato attuale e prossimi passi

Funzionante e testato: ricezione ordini, ricezione/associazione risultati, inoltro al LIS
con ACK, gestione risultati orfani, dashboard di stato di sola lettura.

Da sviluppare (vedi `ARCHITECTURE.md` → Roadmap): adapter **ASTM** per gli strumenti che
non parlano HL7, regola di completezza basata sui test richiesti, retry/backoff persistente
nell'inoltro, e la **UI completa** (coda ordini, dettaglio, riassociazione orfani, azioni
retry/forza-inoltro) sopra `store.py`.
