# CLAUDE.md

Contesto per Claude Code. Leggi anche `ARCHITECTURE.md` per il dettaglio.

## WHY
Middleware sanitario **order-driven** tra un LIS e gli strumenti di laboratorio
(emocromo/CBC). Deve essere affidabile e semplice: è il pezzo che resta in piedi quando
il resto non funziona. Per questo il **core non ha dipendenze esterne** (solo stdlib;
stato su SQLite). Mantieni questa proprietà salvo decisione esplicita.

## WHAT
Tre flussi (in `hl7mw/pipeline.py`):
1. `OrderReceiver` — server MLLP, riceve ORM/OML dal LIS, salva l'ordine, risponde ACK.
   Riceve anche ADT^A0x (registrazione paziente, es. quando si sostituisce un
   fornitore cloud come Citizen Care Connect): ACK positivo, nessun ordine
   creato (vedi `_handle_adt`, `hl7.parse_adt`).
2. `ResultReceiver` — server MLLP, riceve ORU dagli strumenti, **associa** all'ordine.
3. `Forwarder` — ordini `READY` → ORU^R01 → LIS, gestisce l'ACK.

Associazione per `sample_key` (specimen/barcode → filler → placer). Risultati senza
ordine → tabella `unmatched_results`. Ciclo di vita ordine: RECEIVED → READY → SENT
(o ERROR). Vedi `store.py`.

## HOW
- Eseguire i test: `python3 tests/test_e2e.py` (deve stampare "TUTTI I TEST OK").
- Avviare il servizio: `python3 -m hl7mw.run -c config.json`.
- HL7: separatore segmento `\r`, framing MLLP `0x0B … 0x1C 0x0D`. Non introdurre `\n`.
- Convenzioni: commenti/log in italiano; nomi e docstring chiari; niente librerie esterne
  nel package `hl7mw/` senza prima discuterne (UI e tooling di sviluppo possono usarle).
- Prima di modificare il parsing HL7, aggiungi un test in `tests/` che riproduce il caso.

## Nuove Features (v2 — Sistema di Gestione & Monitoring)

### Database Esteso
- **`instruments`**: tracciamento device collegati con heartbeat, status (ONLINE/OFFLINE/UNKNOWN), msg count
- **`audit_log`**: log immutabile per tracciabilità clinica (events, severity, timestamp)
- **`order_timing`**: timing di ogni ordine (received, first_result, ready, sent) → metriche dashboard
- **`orders.source_instrument`**: associazione ordine al device sorgente
- Colonne estese su `results` e `unmatched_results` per source_instrument

### REST API (FastAPI)
- Endpoint `/api/dashboard` — statistiche globali (throughput, timing medio, status instrument)
- Endpoint `/api/orders` — lista ordini con filtri e dettaglio completo
- Endpoint `/api/orders/{sample_key}/retry` — riporta ERROR → READY
- Endpoint `/api/orders/{sample_key}/cancel` — annulla ordine
- Endpoint `/api/instruments` — status tutti device
- Endpoint `/api/unmatched` — risultati orfani + `/match` per riconciliazione manuale
- Endpoint `/api/audit-log` — log tracciabilità (filtri per sample/event)
- Endpoint `/api/config` (GET/PUT) — configurazione completa (LIS/strumenti/VPN/servizio) per
  la pagina Impostazioni della dashboard; scrive su `config.json`, **non applicata a runtime**
  (componenti inizializzati all'avvio, serve riavvio) — validazione chiavi/tipi contro
  `run.DEFAULTS` in `api._validate_config_update`
- Endpoint `/api/vpn/check` — health-check on-demand host:porta (bottone "Verifica tunnel")
- **Dashboard HTML moderna**: Chart.js (doughnut chart status, metriche KPI, tabelle ordini, azioni, device status)

Abilitazione: `"api_enabled": true` in config, oppure `pip install fastapi uvicorn`

### CLI Tool (`python3 -m hl7mw.cli`)
```bash
# Elenco ordini
python3 -m hl7mw.cli --db hl7mw.db orders [--status READY|ERROR|SENT]
# Dettaglio ordine con risultati e timing
python3 -m hl7mw.cli --db hl7mw.db order ABC123
# Operazioni
python3 -m hl7mw.cli --db hl7mw.db retry ABC123    # ripeti inoltro (ERROR→READY)
python3 -m hl7mw.cli --db hl7mw.db cancel ABC123   # annulla ordine
# Audit & stats
python3 -m hl7mw.cli --db hl7mw.db audit-log [--sample-key S] [--event-type X] [--limit 100]
python3 -m hl7mw.cli --db hl7mw.db instruments     # elenco device
python3 -m hl7mw.cli --db hl7mw.db stats           # statistiche globali
python3 -m hl7mw.cli --db hl7mw.db unmatched       # risultati orfani
```

### Device Monitoring (`monitor.py`)
- **`DeviceMonitor`** integrato nei Receiver: registra heartbeat ad ogni messaggio
  (auto-registra lo strumento se nuovo — vedi `record_message`)
- Aggiorna status ONLINE/OFFLINE basato su timeout (config: `device_offline_timeout_seconds`)
- Traccia msg count per device
- Audit log per cambio status (INFO online, WARNING offline)

## Sostituzione Citizen Care Connect (CCHS) + VPN

CCHS **non è né il LIS né uno strumento**: è essa stessa un middleware/bridge
("EMR Bridge Module") verso cui il LIS del cliente è oggi configurato — vedi
`INTEGRATION_CITIZENCARE.md`. Questo middleware **sostituisce CCHS** in quello
scambio (es. il fornitore CCHS non è affidabile/disponibile): il LIS non va
riconfigurato, gli si reindirizza solo la connessione (e il tunnel VPN, se
presente) che oggi usa per parlare con CCHS. Nessun adapter dedicato: usa
`OrderReceiver`/`Forwarder` esistenti, con `lis_host`/`lis_port`/
`order_listen_port` puntati agli endpoint del **vero LIS** (non di CCHS).
L'unica estensione è il supporto ADT^A0x in `OrderReceiver` (sopra), perché
CCHS (e quindi il LIS così configurato) lo invia prima dell'ordine. Lo
strumento fisico (es. HemoScreen) si collega come sempre tramite
`adapters/hemoscreen_*.py`, indipendentemente da CCHS/LIS.

`hl7mw/vpn.py` — `VpnManager`: health-check sempre (default su `lis_host:lis_port`);
avvio/arresto del tunnel (wg-quick/openvpn/comando custom) solo se
`vpn_manage_lifecycle: true`, altrimenti gestito esternamente (systemd —
consigliato in produzione). Il LIS raggiunge oggi CCHS via un tunnel site-to-site
che il LIS stesso origina (spec CCHS §5.1): per sostituire CCHS senza toccare
il LIS, questo middleware deve trovarsi sull'altro capo di quel tunnel (o di uno
riconfigurato per puntare qui). Solo stdlib (subprocess/socket): niente
crypto/tunneling reimplementato in Python. Template VPN (WireGuard/OpenVPN) e
guida setup in `vpn/README.md`.

## Da fare (priorità)
1. Adapter **ASTM E1381/E1394** per strumenti non-HL7 → produrre lo stesso dict di
   `hl7.parse_result`.
2. Regola di **completezza** reale in `pipeline.try_complete` (test richiesti vs ricevuti).
3. **Retry/backoff persistente** nel `Forwarder` (storico retry, DLQ).
4. **Sicurezza**: TLS sul MLLP, autenticazione API, RBAC dashboard.
5. Supporto esplicito `ADT^A08` (update paziente, dichiarato da CCHS ma non
   ancora distinto da A04 in `_handle_adt`, che oggi tratta tutti gli ADT allo
   stesso modo: ACK positivo, nessuna persistenza).

## Attenzione (dominio sanitario)
Dati clinici reali: niente dati paziente nei log/commit, attenzione a sicurezza del
trasporto (TLS/stunnel sul MLLP) e tracciabilità (audit). In dubbio, chiedi.
