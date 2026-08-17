# Integrazione — sostituzione di Citizen Care Connect (CCHS)

Guida operativa per far sì che questo middleware **sostituisca il servizio
cloud di Citizen Care Connect (CCHS)** verso il vero LIS del cliente, sulla
base di "Citizen Care Connect — HL7 Specifications v1.0" (Citizen Care Health
Solutions, 16-Sep-2025, `support@citizencarehealth.com`).

## 1. Ruolo di CCHS nel flusso (e perché il middleware lo sostituisce)

**CCHS non è il LIS, e non è nemmeno un sistema con cui il middleware deve
parlare**: CCHS è essa stessa un middleware ("EMR Bridge Module", §1.1 della
loro spec) che si mette tra il LIS del cliente e il resto. Il documento è la
specifica **verso cui il LIS del cliente è già configurato a parlare**:

- §5.1: *"The LIS... must be configured to establish secure communication
  with the **CCHS Cloud Ingest Server**"* — è il LIS a connettersi verso CCHS.
- §5.1: *"The LIS/EMR must be configured to forward ADT and ORM messages to
  the designated CCHS Cloud destination IP addresses and ports"* — il LIS ha
  una destinazione IP/porta configurata per ADT/ORM, oggi puntata a CCHS.
- §5.2: *"results are gathered and sent back to the LIS/EMR **from the CCHS
  integration service**"* — l'ORU parte da CCHS, non dal LIS.

Il LIS del cliente è quindi **già pronto** a parlare con qualcosa che si
comporta come CCHS. Se CCHS (il fornitore) è il problema — es. in un'emergenza
in cui il loro servizio non è disponibile — questo middleware può **prendere il
posto di CCHS** in quello scambio, senza che il LIS debba essere riconfigurato:
basta reindirizzare la connessione (IP/porta, eventualmente il tunnel VPN) che
il LIS usava per raggiungere CCHS verso questo middleware.

```
  vero LIS del cliente ──ADT^A04──▶ hl7mw   (al posto di CCHS: OrderReceiver, ACK, nessun ordine)
  vero LIS del cliente ──ORM^O01──▶ hl7mw   (al posto di CCHS: OrderReceiver, crea l'ordine RECEIVED)
                                    ... test eseguito da uno strumento locale (es. HemoScreen) ...
  hl7mw ──ORU^R01──▶ vero LIS del cliente   (al posto di CCHS: Forwarder, status SENT)
```

Flusso di validazione della spec CCHS, §5.2 — la colonna "CCHS Application" è
quella che il middleware assume:

```
LIS/EMR                Direction    CCHS Application  (= questo middleware)
ADT^A04    —————>                   Patient Created
ORM^O01    —————>                   Order Created
                       .....        Test ran on device
Test results processed <—————       ORU^R01
```

## 2. Cosa cambia nel codice

Nulla nella direzione dei flussi: `OrderReceiver`/`Forwarder` già esistenti
giocano esattamente il ruolo di "CCHS Application" — non serve nessun adapter
dedicato. L'unica estensione reale:

- `hl7mw/hl7.py` → `parse_adt()`: parser per `ADT^A0x` (patient demographics),
  che CCHS (e quindi il LIS configurato per CCHS) invia prima dell'ordine.
- `hl7mw/pipeline.py` → `OrderReceiver._handle()` smista per tipo messaggio:
  `ADT*` → `_handle_adt()` (ACK positivo, audit log, **nessun ordine creato** —
  i dati paziente arrivano di nuovo, completi, nel successivo `ORM^O01`, che
  segue il percorso esistente invariato: `hl7.parse_order` → `store.upsert_order`).
  Qualunque altro tipo non gestito resta rifiutato con `AR`, come prima.
- **Nessuna modifica** a `ResultReceiver`, `Forwarder`, `try_complete`, al ciclo
  di vita dell'ordine (`RECEIVED → READY → SENT`/`ERROR`) o agli adapter
  strumento (`hemoscreen_hl7.py`, `hemoscreen_poct1a2.py`).

## 3. Collegare HemoScreen (o un altro strumento)

Lo strumento che esegue fisicamente il test si collega al middleware come
sempre, indipendentemente da CCHS/LIS — vedi `README.md` → adapter HemoScreen.
In `config.json`:

```jsonc
// scegliere UNO dei due, secondo il protocollo del proprio HemoScreen
"hemoscreen_hl7_enabled": true, "hemoscreen_hl7_port": 6663,
// oppure
"hemoscreen_poct1a2_enabled": true, "hemoscreen_poct1a2_port": 6664
```

L'ordine a cui il risultato dello strumento viene associato (per
`sample_key`/placer/filler order number) è quello creato dall'`ORM^O01`
ricevuto dal LIS — nessuna configurazione aggiuntiva necessaria: la stessa
`Store` è condivisa da tutti i componenti.

## 4. Configurazione applicativa

In `config.json` (vedi `config.example.json`): `lis_host`/`lis_port` e
`order_listen_port` NON sono endpoint CCHS — sono gli endpoint del **vero
LIS del cliente** (quelli che oggi ha configurati per parlare con CCHS).

```jsonc
"order_listen_host": "0.0.0.0", "order_listen_port": 6661,  // il vero LIS si connette qui per ADT/ORM
"lis_host": "10.9.0.10", "lis_port": 2576,                  // dove il vero LIS riceve l'ORU (endpoint gia' noto al cliente)
"sending_app": "HL7MW", "sending_facility": "MIDDLEWARE",
"receiving_app": "LIS", "receiving_facility": "OSP"          // identita' del vero LIS, non di CCHS
```

**Alcuni LIS separano ADT e ORM su due connessioni distinte** invece di un
unico canale — es. Dedalus, che verso l'EMR Bridge CCHS apre due connessioni
MLLP separate (una etichettata "Dedalus ADT to EMR Bridge", una "Dedalus ORM
to EMR Bridge", tipicamente su porte diverse). In quel caso impostare anche:

```jsonc
"adt_listen_host": "", "adt_listen_port": 6000   // stessa porta ADT gia' usata dal LIS verso CCHS
```

così il LIS non deve essere riconfigurato: `order_listen_port` continua a
gestire l'ORM (e comunque anche l'ADT, se mai arrivasse lì).

`receiving_app`/`receiving_facility` vanno valorizzati con l'identità
(MSH-5/MSH-6) che il LIS del cliente si aspetta nell'ORU — da verificare con
chi gestisce il LIS, non necessariamente uguali a "CCHS"/"CITIZENCARE".

## 5. VPN site-to-site (spec CCHS §5.1)

Il LIS del cliente è (probabilmente) già configurato per raggiungere CCHS
attraverso un tunnel VPN site-to-site che **il LIS stesso origina**. Per
sostituire CCHS senza toccare il LIS, questo middleware deve trovarsi
sull'altro capo di quello stesso tunnel (o di uno equivalente riconfigurato
per puntare qui). Setup completo, template WireGuard/OpenVPN e unit systemd in
**`vpn/README.md`**. In sintesi:

```jsonc
"vpn_enabled": true,
"vpn_manage_lifecycle": false,   // il tunnel e' gestito da systemd (consigliato)
"vpn_health_check_host": "10.9.0.10",  // default se omesso: uguale a lis_host
"vpn_health_check_port": 2576
```

All'avvio, il middleware verifica che l'endpoint del LIS sia raggiungibile
attraverso il tunnel e logga un errore chiaro se non lo è — senza bloccare gli
altri flussi (lo strumento locale continua a funzionare comunque, e gli invii
verso il LIS restano `READY`, ritentati automaticamente).

Se invece il LIS in questo caso è raggiungibile in rete locale/senza VPN
(topologia diversa da quella prevista dalla spec CCHS, che presuppone un
servizio cloud remoto), impostare semplicemente `"vpn_enabled": false`.

## 6. Dati da raccogliere per la sostituzione

Dal cliente/da chi gestisce il LIS (non da CCHS, che stiamo sostituendo):

- **IP/porta a cui il LIS invia** `ADT^A04`/`ORM^O01` oggi (verso CCHS) → va
  reindirizzato verso `order_listen_host:order_listen_port` di questo
  middleware.
- **IP/porta su cui il LIS riceve** l'`ORU^R01` di ritorno → `lis_host`/`lis_port`.
- Identità applicativa attesa nell'MSH dei messaggi (`sending_app`/`facility`,
  `receiving_app`/`facility`) e dettagli del tunnel VPN esistente (se il LIS
  ne usa uno per raggiungere CCHS), da ripuntare verso questo middleware.

## 7. Validazione (flusso da spec CCHS §5.2)

1. Il LIS invia `ADT^A04`: verificare nei log `Paziente registrato dal LIS: id=...`.
2. Il LIS invia `ORM^O01`: verificare `Ordine ricevuto dal LIS: sample=...`.
3. Lo strumento (es. HemoScreen) esegue il test e invia il risultato al
   middleware: l'ordine passa a `READY`.
4. Verificare che l'ordine raggiunga `SENT` verso il LIS:
   ```bash
   python3 -m hl7mw.cli --db hl7mw.db order <sample_key>
   ```
   oppure dalla dashboard REST (`GET /api/orders/{sample_key}`, se
   `api_enabled: true`), che mostra stato e timing (`received_at`,
   `first_result_at`, `ready_at`, `sent_at`) di ogni passaggio.

## 8. Esempio concreto — LIS Dedalus (schema a 3 porte)

Ricostruito da evidenza raccolta in un ambiente reale (`netstat` sull'host che
oggi esegue il fornitore CCHS + log applicativo del processo "EMR Bridge"):

```
$ netstat -putna | grep -E '6000|6001|10100'
tcp  ESTABLISHED  <host>:6000  172.25.13.1:*        (PID del processo EMR Bridge)
tcp  ESTABLISHED  <host>:6001  172.25.13.1:*
tcp  LISTEN       0.0.0.0:10100
```
```
[INFO] Dedalus ADT to EMR Bridge: New connection from ('10.250.227.20', 44182)
[INFO] Dedalus ORM to EMR Bridge: New connection from ('10.250.227.20', 43488)
```

I nomi dei due log ("Dedalus ADT to EMR Bridge" / "Dedalus ORM to EMR Bridge")
confermano lo schema Dedalus descritto al §4: **ADT e ORM viaggiano su due
connessioni MLLP separate**, non su un canale unico — coerente con la nota
dello sviluppatore CCHS: *"tre porte diverse: una per i pazienti (ADT), una
per gli ordini (ORM) e una per i risultati (ORU). ADT e ORM sono gestiti dal
cliente [il LIS li invia], mentre ORU è gestito dal fornitore [il LIS lo
riceve], e i server ricevono ADT e ORM"* — esattamente il ruolo di
`adt_listen_port`/`order_listen_port` (in ascolto, riceviamo) e `lis_host`/
`lis_port` (in uscita, inviamo l'ORU) di questo middleware.

Config pronta all'uso in **`config.dedalus-cchs.example.json`** (anche
caricabile/adattabile dalla GUI Impostazioni → sezione LIS). Valori:

| Chiave | Valore | Stato |
|---|---|---|
| `adt_listen_port` | `6000` | Plausibile dal netstat; **DA_VERIFICARE** quale delle due porte è davvero ADT (il log conferma che i due canali esistono, non l'associazione porta↔tipo) |
| `order_listen_port` | `6001` | Idem, per ORM |
| `order_listen_host` / `adt_listen_host` | `0.0.0.0` | Endpoint di questo middleware — non richiede verifica |
| `lis_host` / `lis_port` | — | **DA_VERIFICARE**: sono l'endpoint del *vero* LIS dove inviamo l'ORU di ritorno, un dato diverso da quelli sopra (che sono dove *noi* ascoltiamo) e non deducibile dal netstat raccolto finora |
| ruolo della porta `10100` (LISTEN) | — | **DA_VERIFICARE**: non è detto sia `lis_port` — potrebbe essere un canale interno del fornitore CCHS (status/keep-alive) da non replicare |
| `receiving_app` / `receiving_facility` | — | **DA_VERIFICARE** con chi gestisce il LIS (MSH-5/6 attesi nell'ORU) |

Prima di andare live su questo scenario: confermare con il fornitore/gestore
del LIS Dedalus i tre campi **DA_VERIFICARE** sopra (bastano un test ADT/ORM
verso ciascuna delle due porte osservate e la porta di ascolto ORU sul LIS),
poi salvare la config finale dalla GUI Impostazioni (richiede riavvio del
servizio, vedi §4 sopra).

## 9. Limiti noti

- `ADT^A08` (aggiornamento paziente) è dichiarato come supportato da CCHS nella
  spec (§5.2) ma non ancora distinto: `_handle_adt` accetta qualunque
  sottotipo `ADT*` con lo stesso comportamento (ACK positivo, nessuna
  persistenza) senza fare nulla di specifico per un A08.
- Regola di completezza: come per il resto del sistema, un ordine passa a
  `READY` non appena arriva **almeno un** risultato dallo strumento; se
  servono più test/risultati per lo stesso ordine, adattare
  `pipeline.try_complete()`.
- Il documento CCHS descrive i campi minimi/di esempio (§4.1-4.3): se il LIS
  reale invia varianti (segmenti aggiuntivi, codifica diversa dei test), va
  verificato messaggio alla mano e, se serve, esteso `hl7.parse_order`/`parse_adt`.
