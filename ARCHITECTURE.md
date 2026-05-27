# Architettura — HL7 Middleware (LIS ⇄ strumenti)

Specifica di riferimento del sistema. Serve sia come documentazione sia come
**contesto per un agente di coding** (Claude Code / Codex) che continui lo sviluppo.

## Ruolo del sistema

Il middleware sta **tra il LIS e gli strumenti** di laboratorio (analizzatori di
emocromo). Non genera ordini propri: li riceve dal LIS, li tiene in stato, riceve i
risultati dagli strumenti, li **associa** all'ordine corrispondente e li **inoltra**
al LIS come risultati strutturati.

```
            ORM / OML (ordini)                         ORU (risultati associati)
   LIS  ─────────────────────────▶  MIDDLEWARE  ─────────────────────────────▶  LIS
                                        ▲   │
                  ORU / ASTM (risultati)│   │ (eventuale download ordini: FUORI SCOPE per ora)
                                        │   ▼
                                    STRUMENTI
```

Tre flussi:
1. **LIS → middleware**: ordini in ingresso. Il LIS si connette e invia ORM^O01 /
   OML^O21; il middleware risponde con ACK e persiste l'ordine.
2. **Strumenti → middleware**: risultati in ingresso. Gli strumenti inviano i
   risultati; il middleware li associa all'ordine per chiave campione.
3. **Middleware → LIS**: i risultati associati e completi vengono inoltrati al LIS
   come ORU^R01, con gestione dell'ACK.

> Il download degli ordini *verso* gli strumenti (worklist) non è richiesto al momento
> ("non mandiamo necessariamente ordini agli strumenti"). Predisposto come estensione
> futura (vedi Roadmap), ma non implementato.

## Componenti (`hl7mw/`)

| modulo            | responsabilità                                                        |
|-------------------|-----------------------------------------------------------------------|
| `hl7.py`          | parsing/costruzione HL7v2: `parse_order`, `parse_result`, `build_ack`, `build_oru` |
| `mllp.py`         | trasporto MLLP: client (`send_message`/`exchange`) e server (`MllpServer`) |
| `store.py`        | persistenza SQLite: ordini, risultati, orfani, ciclo di vita, query UI |
| `pipeline.py`     | `OrderReceiver`, `ResultReceiver`, `Forwarder` + regola di associazione |
| `webstatus.py`    | endpoint di stato di sola lettura (aggancio per la UI)                 |
| `run.py`          | runner del servizio: avvia i receiver + loop del forwarder            |

## Associazione (matching)

Chiave primaria di matching: **`sample_key`** = primo identificativo non vuoto tra
specimen/barcode (`SPM-2`), filler order number (`OBR-3`/`ORC-3`), placer order number
(`OBR-2`/`ORC-2`), normalizzato (upper, trim).

- L'ordine viene salvato con la sua `sample_key`.
- Ogni risultato in ingresso calcola la stessa `sample_key` e cerca l'ordine.
- **Risultato prima dell'ordine**: gestito. Quando l'ordine arriva, `try_complete`
  riconcilia i risultati già presenti.
- **Risultato senza ordine**: salvato in `unmatched_results` (ACK comunque positivo
  allo strumento), così non si perde nulla e la UI lo può mostrare/risolvere.

> La **regola di completezza** attuale è minimale: ordine + almeno un risultato ⇒
> `READY`. In produzione va sostituita con il confronto tra test richiesti
> (`universal_service_id`/OBR multipli) e analiti ricevuti. Punto unico da estendere:
> `pipeline.try_complete()`.

## Ciclo di vita dell'ordine (`orders.status`)

```
RECEIVED ──(arriva ≥1 risultato)──▶ READY ──(forward OK, ACK AA)──▶ SENT
   │                                  │
   │                                  ├─(ACK negativo del LIS)──▶ ERROR
   │                                  └─(rete/timeout)──▶ READY (ritenta)
   └─(in attesa risultati)──▶ RECEIVED
```

Stati: `RECEIVED`, `READY`, `FORWARDING`, `SENT`, `ERROR` (più `RESULTS_PARTIAL`
riservato alla regola di completezza evoluta). Errori transitori (rete/VPN) riportano
l'ordine a `READY` per il retry; errori applicativi (ACK negativo, dati invalidi) vanno
in `ERROR` con `last_error`.

## Porte e rete

- Ordini dal LIS: server MLLP su `order_listen_port` (default 6661).
- Risultati dagli strumenti: server MLLP su `result_listen_port` (default 6662).
- Inoltro al LIS: client MLLP verso `lis_host:lis_port`.
- La VPN/connettività è a carico del SO; il middleware si limita a usare gli endpoint.

## Decisioni tecniche

- **Zero dipendenze esterne nel core** (solo stdlib): meno punti di rottura per un
  componente che deve restare in piedi. `sqlite3` (stdlib) per lo stato → transazionale
  e interrogabile dalla UI.
- **MLLP** come unico trasporto HL7. Framing standard `0x0B … 0x1C 0x0D`.
- **Idempotenza inoltro**: il control id dell'ORU deriva dal filler/sample, così un
  reinvio mantiene lo stesso ID e il LIS può deduplicare.

## Punti da validare con il sito reale

1. **Tipi messaggio ordini**: ORM^O01 vs OML^O21 (e relativi gruppi di segmenti).
   `parse_order` copre entrambi; verificare la posizione reale dell'ID campione
   (`SPM-2` vs `OBR-3` vs SAC).
2. **Protocollo strumenti**: molti analizzatori di emocromo parlano **ASTM
   E1381/E1394** (seriale/TCP), non HL7. Se è il vostro caso, serve un *adapter ASTM*
   che decodifichi i record e produca lo stesso dict di `parse_result`. Vedi Roadmap.
3. **Mappatura analiti → LOINC/UCUM** (`hl7.CBC_ANALYTES`): allineare al dizionario
   del LIS.
4. **Regola di completezza** dell'ordine (`try_complete`).

## Roadmap (sviluppo successivo, ideale in agente)

- [ ] **Adapter ASTM** per gli strumenti che non parlano HL7 → normalizza in `parse_result`.
- [ ] **Regola di completezza** basata sui test richiesti vs ricevuti.
- [ ] **Forwarder con retry/backoff persistente** (ora il retry è "torna READY"): portare
      la logica di backoff/quarantena già scritta nel prototipo a cartelle dentro lo store.
- [ ] **UI**: l'attuale `webstatus.py` è di sola lettura. La UI completa dovrebbe offrire:
      coda ordini con stato, dettaglio ordine+risultati, gestione `unmatched`
      (riassociazione manuale), azioni *retry*/*forza inoltro*, log/audit, ricerca per
      barcode/paziente. Stack consigliato per l'agente: API REST (FastAPI) sopra `store.py`
      + frontend (React/Svelte). `store.py` è già pensato per essere la sorgente dati.
- [ ] **Sicurezza**: TLS sul MLLP (o stunnel), autenticazione UI, audit immutabile.
- [ ] **Osservabilità**: metriche (ordini/min, latenza inoltro, tasso unmatched), healthcheck.
- [ ] **Test**: ampliare `tests/` (parsing edge case, riconciliazione fuori ordine,
      ACK negativi, concorrenza SQLite sotto carico).
