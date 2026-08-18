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
   creato (vedi `_handle_adt`, `hl7.parse_adt`). Alcuni LIS (es. Dedalus) aprono
   due connessioni MLLP separate verso l'EMR Bridge, una per ADT e una per ORM,
   invece di un unico canale: supportato con `adt_listen_port` opzionale in
   `hl7mw/run.py` (secondo `mllp.MllpServer` che riusa `OrderReceiver._handle`).
2. `ResultReceiver` — server MLLP, riceve ORU dagli strumenti, **associa** all'ordine.
3. `Forwarder` — ordini `READY` → ORU^R01 → LIS, gestisce l'ACK.

Associazione per `sample_key` (specimen/barcode → filler → placer). Risultati senza
ordine → tabella `unmatched_results`. Ciclo di vita ordine: RECEIVED → READY → SENT
(o ERROR). Vedi `store.py`.

## Riscontro e comunicazione HL7 (cap. 2 dello standard)

Tutto cio' che riguarda l'ACK sta in `hl7mw/ack.py` (politica) e `hl7mw/hl7.py`
(costruzione dei messaggi); i due receiver condividono `pipeline.InboundChannel`,
che applica a ogni canale in ingresso le stesse regole:

- **Original / enhanced mode**: `hl7_ack_mode` = `auto` (default) onora MSH-15
  (accept ack) e MSH-16 (application ack) se il mittente li valorizza — commit
  ACK `CA/CE/CR` seguito dall'ACK applicativo `AA/AE/AR`, due risposte sulla
  stessa connessione — altrimenti risponde con un solo ACK come sempre.
  MSH-16 assente = regole original mode (ACK applicativo comunque inviato);
  si tace solo su un `NE` esplicito.
- **NACK diagnosticabili**: segmento `ERR` con codice della tabella HL7 0357
  (ERR-3/ERR-4 dalla 2.4, ERR-1 in forma ELD fino alla 2.3.1). Gli `Hl7Error`
  portano il proprio `error_code`, non solo un testo.
- **ACK conforme**: `ACK^<trigger>^ACK`, MSH-11/MSH-12 echeggiati dal messaggio
  in ingresso (un messaggio di test `T` non va riscontrato come `P`), MSA-2 =
  MSH-10 ricevuto, MSH-10 dell'ACK proprio.
- **Connessione persistente**: il server MLLP resta in lettura finche' il peer
  chiude o scade `mllp_idle_timeout`; `mllp.FrameReader` conserva il buffer, per
  cui piu' messaggi nello stesso segmento TCP non vengono persi. Il rovescio
  della medaglia e' che ogni peer trattiene un thread: `mllp_max_connections`
  (default 64) limita le connessioni simultanee per listener.
- **Batch / multi-messaggio**: `hl7.split_messages` scarta gli involucri
  FHS/BHS/BTS/FTS e risponde un ACK per ogni messaggio contenuto.
- **Idempotenza**: la chiave e' MSH-3 + MSH-10 + impronta del contenuto
  (`store.processed_messages`). Ritrasmissione identica → si ripete lo stesso
  ACK senza rielaborare; stesso MSH-10 con contenuto diverso → si elabora
  comunque (perdere un risultato clinico e' peggio) e si registra l'anomalia in
  audit. La sequenza "controlla → elabora → registra" gira sotto lock per
  chiave (`pipeline._KeyedLocks`): due copie identiche in arrivo insieme su
  connessioni diverse non possono superare entrambe il controllo. Finestra
  `hl7_dedup_retention_hours` misurata sull'ultima attivita' (`last_seen`),
  ripulita dal loop di `run.main()`.
- **In uscita**: `lis_ack_mode: "enhanced"` mette MSH-15/16=AL nell'ORU e passa
  l'ordine a `SENT` solo dopo l'ACK applicativo; un `CA` isolato e' condizione
  transitoria (ordine ritentabile), non un successo.
- **Risposta d'ordine**: `order_response_mode: "order"` risponde `ORR^O02`
  (ORM) / `ORL^O22` (OML) invece dell'ACK generico, con ORC-1 `OK`/`UA` — anche
  sui rifiuti (`InboundChannel.error_response`), altrimenti il LIS riceverebbe
  un formato diverso proprio quando deve capire cosa non ha funzionato.
- **Tracciamento**: ogni scambio finisce in `store.message_log` (solo metadati
  di header e ACK, mai payload) → `GET /api/messages`, `python3 -m hl7mw.cli
  messages`, pannello "Traffico HL7 & riscontri" della dashboard. Il profilo
  attivo e' esposto da `GET /api/interop`.

Test: `python3 tests/test_hl7_ack.py` (deve stampare "TUTTI I TEST ACK/HL7 OK").

## Applicazione desktop (eseguibile Windows)

`hl7mw/desktop.py` e' il guscio dell'exe: NON contiene logica HL7, avvia
`run.MiddlewareService` e si occupa di tutto cio' che serve perche' un doppio
click funzioni senza spiegazioni.

- **Cartella dati**: `%LOCALAPPDATA%\hl7mw` (Windows) o `~/.local/share/hl7mw`.
  Database, log e `config.json` non finiscono piu' nella cartella di lancio, che
  puo' essere di sola lettura (Program Files) o essere la cartella Download.
  Alla prima esecuzione il `config.json` viene creato con i default.
- **Istanza singola**: un socket sul loopback (porta 47615). Il secondo avvio
  avvisa e indica l'interfaccia gia' attiva invece di morire con
  "address already in use".
- **Controllo porte prima dell'avvio**: `busy_ports()` dice *quale* porta e a
  cosa serviva, invece di fallire a meta' avvio con alcuni listener gia' aperti.
- **Interfaccia in tre livelli**: finestra embedded (pywebview/WebView2) →
  finestra applicazione di Edge/Chrome (`--app=`, profilo dedicato) → browser di
  sistema. L'utente non digita mai un indirizzo.
- **Errori visibili**: un avvio fallito produce una finestra di dialogo
  (`MessageBoxW`) oltre al log, perche' la build e' `--windowed` e non c'e'
  nessuna console dove leggere una traceback.
- Flag: `--headless` (solo servizio), `--browser`, `--selftest` (avvia,
  verifica che l'interfaccia risponda, esce: e' anche lo smoke test del
  workflow di build), `--data-dir`, `-c`.

`api_host` di default e' `127.0.0.1`: la dashboard non ha ancora autenticazione,
quindi non va esposta in rete senza una scelta esplicita.

Test: `python3 tests/test_desktop.py` (deve stampare "TUTTI I TEST DESKTOP OK").

## HOW
- Eseguire i test: `python3 tests/test_e2e.py` (deve stampare "TUTTI I TEST OK");
  gli altri file in `tests/` si eseguono allo stesso modo, uno per volta.
- Avviare il servizio: `python3 -m hl7mw.run -c config.json`.
- HL7: separatore segmento `\r`, framing MLLP `0x0B … 0x1C 0x0D`. Non introdurre `\n`.
- Convenzioni: commenti/log in italiano; nomi e docstring chiari; niente librerie esterne
  nel package `hl7mw/` senza prima discuterne (UI e tooling di sviluppo possono usarle).
- Prima di modificare il parsing HL7, aggiungi un test in `tests/` che riproduce il caso.
- Logging: ogni modulo usa `LOG = logging.getLogger("hl7mw")` (o logger figli, es.
  `"hl7mw.xxx"` — propagano allo stesso handler). La configurazione (file rotante + console,
  livello) è centralizzata in `hl7mw/logging_setup.py:configure_logging()`, chiamata una sola
  volta in `run.main()`: non richiamare `logging.basicConfig()` altrove. Un'eccezione nel
  gestore di un messaggio MLLP o in un endpoint API deve sempre finire in log
  (`LOG.exception`/handler globale in `api.py`) prima di essere trasformata in una risposta di
  errore al chiamante — mai ingoiata silenziosamente (era un bug reale in `mllp.py`).

## Nuove Features (v2 — Sistema di Gestione & Monitoring)

### Database Esteso
- **`instruments`**: tracciamento device collegati con heartbeat, status (ONLINE/OFFLINE/UNKNOWN), msg count
- **`audit_log`**: log immutabile per tracciabilità clinica (events, severity, timestamp)
- **`order_timing`**: timing di ogni ordine (received, first_result, ready, sent) → metriche dashboard
- **`message_log`**: un record per messaggio HL7 scambiato (header + esito ACK, senza payload)
- **`processed_messages`**: control id già elaborati per l'idempotenza (vedi sezione riscontro)
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
- Endpoint `/api/messages` — traffico HL7 con esito del riscontro (filtri direzione/canale/
  sample/control id/solo NACK), `/api/messages/stats` (riepilogo) e `/api/messages/duplicates`
  (ritrasmissioni rilevate); solo metadati, nessun payload → nessun dato paziente
- Endpoint `/api/interop` — profilo di interoperabilità attivo (messaggi accettati, modalità di
  riscontro, deduplica, cosa NON è supportato): la dichiarazione da consegnare a chi integra
- Endpoint `/api/config` (GET/PUT) — configurazione completa (LIS/strumenti/VPN/servizio) per
  la pagina Impostazioni della dashboard; scrive su `config.json`, **non applicata a runtime**
  (componenti inizializzati all'avvio, serve riavvio) — validazione chiavi/tipi contro
  `run.DEFAULTS` in `api._validate_config_update`
- Endpoint `/api/vpn/check` — health-check on-demand host:porta (bottone "Verifica tunnel")
- Endpoint `/api/vpn/up` / `/api/vpn/down` (POST) — avvia/ferma il tunnel on-demand (wg-quick/openvpn/
  comando custom, vedi `hl7mw/vpn.py`), solo se `vpn_enabled`+`vpn_manage_lifecycle` nella
  configurazione **salvata** (non nel form non ancora salvato); 400 esplicito altrimenti
- Endpoint `/api/logs` — tail (default 200 righe) del log tecnico applicativo su file
  (`hl7mw/logging_setup.py`), diverso dall'audit_log clinico — 404 se `log_file` non configurato
- **Dashboard HTML moderna**: Chart.js (doughnut chart status, metriche KPI, tabelle ordini, azioni,
  device status), pannello "Traffico HL7 & riscontri" (KPI messaggi/NACK/duplicati, ultimi scambi,
  filtro solo-NACK, modale "Profilo interop"), sezione HL7 nelle Impostazioni, cronologia degli
  scambi nel dettaglio ordine, pannello "Log Applicativo" (tail del file di log, refresh manuale)

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
# Traffico HL7 e riscontri
python3 -m hl7mw.cli --db hl7mw.db messages [--direction IN|OUT] [--channel orders] [--errors]
python3 -m hl7mw.cli --db hl7mw.db message-stats   # totali per direzione/ACK, NACK, duplicati
python3 -m hl7mw.cli --db hl7mw.db duplicates      # control id ripetuti (ritrasmissioni/riuso)
python3 -m hl7mw.cli logs [--lines 100]            # log tecnico (non richiede --db)
```

### Device Monitoring (`monitor.py`)
- **`DeviceMonitor`** integrato nei Receiver: registra heartbeat ad ogni messaggio
  (auto-registra lo strumento se nuovo — vedi `record_message`)
- Aggiorna status ONLINE/OFFLINE basato su timeout (config: `device_offline_timeout_seconds`) —
  `update_health_status()` richiamato dal loop principale in `run.main()` ad ogni giro
  (insieme a `forwarder.forward_ready()`), non solo on-demand
- Traccia msg count per device
- Audit log **e** log tecnico per cambio status (INFO online, WARNING offline)

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
6. HL7 ancora scoperto (vedi tabella in `INTEROPERABILITY.md`): query/response
   `QBP^Q11`–`RSP^K11` per la worklist su richiesta dello strumento, risposta
   batch aggregata (BHS/BTS in uscita), sequence number protocol (MSH-13/MSA-4).

## Attenzione (dominio sanitario)
La dashboard mostra valori che arrivano da messaggi HL7 di terzi (sample key,
control id, nome strumento): vanno sempre inseriti con `esc()` nel markup, mai
interpolati grezzi in `innerHTML` o dentro un gestore inline — un MSH-10 con
`<script>` verrebbe altrimenti eseguito nel browser dell'operatore.

Dati clinici reali: niente dati paziente nei log/commit, attenzione a sicurezza del
trasporto (TLS/stunnel sul MLLP) e tracciabilità (audit). In dubbio, chiedi.
