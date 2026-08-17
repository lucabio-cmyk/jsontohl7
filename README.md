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
  vpn.py         gestione opzionale del tunnel VPN verso il LIS (es. sostituzione Citizen Care Connect)
  run.py         runner del servizio (avvia tutti i componenti sopra)
tests/           test end-to-end e di unità (senza pytest, eseguibili singolarmente)
vpn/             template di configurazione VPN (WireGuard/OpenVPN) + guida setup
config.example.json
ARCHITECTURE.md  · CLAUDE.md (contesto per Claude Code) · INTEGRATION_CITIZENCARE.md
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

## Configurazione da GUI

La dashboard (`http://host:8000/`) ha un pulsante **⚙ Impostazioni** che apre un
form per l'intera configurazione — LIS, strumenti (HemoScreen), VPN, rete/servizio
— con validazione (chiavi/tipi), un pulsante **Verifica tunnel** che testa la
raggiungibilità VPN in tempo reale (`GET /api/vpn/check`) senza dover salvare
prima, e — se `vpn_manage_lifecycle: true` — **Avvia tunnel**/**Ferma tunnel**
per avviarlo/fermarlo on-demand (`POST /api/vpn/up`/`/down`, agiscono sulla
configurazione già salvata, non sul form). Il salvataggio scrive su
`config.json` (`GET`/`PUT /api/config`): **non è applicato a runtime** — LIS,
VPN e adapter strumenti sono inizializzati una sola volta all'avvio, quindi
serve riavviare il servizio perché le modifiche abbiano effetto (la dashboard
lo segnala esplicitamente dopo il salvataggio).

## Eseguibile Windows (.exe)

Per far girare il middleware su una macchina Windows senza Python installato:
build automatica via GitHub Actions (workflow
[`build-windows-exe.yml`](.github/workflows/build-windows-exe.yml)), che
compila un `.exe` standalone (core + dashboard REST) con PyInstaller su un
runner Windows reale.

1. Dalla scheda **Actions** del repo → *Build Windows exe* → **Run workflow**.
2. A fine build (qualche minuto), scaricare l'artifact `hl7mw-middleware-windows`
   (contiene `hl7mw-middleware.exe` + `config.example.json`).
3. Sulla macchina Windows: rinominare `config.example.json` in `config.json`,
   adattare host/porte del LIS, poi:
   ```powershell
   .\hl7mw-middleware.exe -c config.json
   ```
   (avviabile anche senza `-c`: usa i default in `hl7mw/run.py` → `DEFAULTS`).

Per una build locale (es. su una macchina Windows con Python già installato):
```powershell
pip install -e ".[build,api]"
pyinstaller --onefile --name hl7mw-middleware packaging/win/hl7mw_entry.py
```

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

## Sostituzione di Citizen Care Connect (CCHS) + VPN

**CCHS non è né il LIS né uno strumento: è essa stessa un middleware/bridge
("EMR Bridge Module") che il vero LIS del cliente raggiunge oggi.** Questo
middleware può **prendere il posto di CCHS** in quello scambio — il LIS non
richiede nessuna modifica, basta reindirizzargli la connessione (e il tunnel
VPN, se presente) che oggi usa per parlare con CCHS verso qui. Usa esattamente
`OrderReceiver` (ora con supporto `ADT^A04` di registrazione paziente, oltre a
`ORM^O01`) e `Forwarder`, invariati — nessun adapter dedicato necessario:

```bash
# config.json — lis_host/lis_port/receiving_app sono il VERO LIS, non CCHS
"order_listen_port": 6661,                       # il vero LIS si connette qui (oggi punta a CCHS)
"lis_host": "10.9.0.10", "lis_port": 2576,        # dove il vero LIS riceve l'ORU (endpoint gia' noto)
"receiving_app": "LIS", "receiving_facility": "OSP",

"vpn_enabled": true, "vpn_manage_lifecycle": false            # tunnel gestito da systemd
```

Lo strumento fisico (es. HemoScreen) si collega come sempre tramite gli
adapter esistenti (`hemoscreen_hl7_enabled`/`hemoscreen_poct1a2_enabled`),
indipendentemente da CCHS/LIS.

Guida completa (ruoli, dati da raccogliere per la sostituzione, config
WireGuard/OpenVPN, systemd, firewall, validazione) in
**`INTEGRATION_CITIZENCARE.md`** e **`vpn/README.md`**.

## Test

```bash
python3 tests/test_e2e.py               # loop completo ordine -> risultato -> inoltro
python3 tests/test_management_system.py # API/store v2: dashboard, retry, audit, instruments
python3 tests/test_ack_retry_backoff.py # retry/backoff su ACK del LIS
python3 tests/test_hemoscreen.py        # adapter strumento HemoScreen (HL7 e POCT1-A2)
python3 tests/test_citizencare.py       # sostituzione CCHS: ADT^A04 + ORM^O01 -> ORU^R01 verso il vero LIS + modulo VPN
python3 tests/test_config_api.py        # pagina Impostazioni: GET/PUT /api/config, avvio/arresto/verifica VPN
```

## Stato attuale e prossimi passi

Funzionante e testato: ricezione ordini (ORM/OML + ADT^A0x di registrazione
paziente), ricezione/associazione risultati, inoltro al LIS con ACK (inclusi
retry automatici su errori transitori), gestione risultati orfani, device
monitoring con heartbeat/status, audit log clinico, REST API + dashboard web
(Chart.js) con pagina Impostazioni per l'intera configurazione (LIS, strumenti,
VPN) e verifica tunnel in tempo reale, CLI operativa completa, VPN configurabile
per sostituire fornitori cloud come Citizen Care Connect (CCHS) senza toccare
il LIS del cliente.

Da sviluppare (vedi `ARCHITECTURE.md` → Roadmap e `CLAUDE.md` → "Da fare"): adapter
**ASTM E1381/E1394** per strumenti non-HL7 generici, regola di completezza reale basata
sui test richiesti vs ricevuti, retry/backoff persistente con storico e DLQ, sicurezza
(TLS sul MLLP, autenticazione API, RBAC dashboard).
