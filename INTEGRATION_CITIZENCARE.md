# Integrazione — Citizen Care Connect (CCHS) come LIS

Guida operativa per collegare il middleware al **LIS Citizen Care Connect**
(Citizen Care Health Solutions), sulla base di "Citizen Care Connect — HL7
Specifications v1.0" (16-Sep-2025, `support@citizencarehealth.com`).

## 1. Ruolo di CCHS nel flusso

**CCHS è il LIS**, non uno strumento: il documento è la specifica che CCHS
fornisce a chi sviluppa il connettore lato LIS/EMR. Nel middleware corrisponde
esattamente ai componenti già esistenti — non serve nessun adapter dedicato:

```
  CCHS ──ADT^A04──▶ hl7mw   (OrderReceiver: registrazione paziente, ACK, nessun ordine)
  CCHS ──ORM^O01──▶ hl7mw   (OrderReceiver: crea l'ordine, status RECEIVED)
                    ... test eseguito da uno strumento locale (es. HemoScreen) ...
  hl7mw ──ORU^R01──▶ CCHS   (Forwarder: come per qualunque LIS, status SENT)
```

Questo è il flusso di validazione descritto nella spec CCHS, §5.2:

```
LIS/EMR                Direction    CCHS Application
ADT^A04    —————>                   Patient Created
ORM^O01    —————>                   Order Created
                       .....        Test ran on device
Test results processed <—————       ORU^R01
```

Da notare: nella tabella "LIS/EMR" è la colonna di CCHS stesso (chi ha scritto
il documento generico); noi, che riceviamo ADT/ORM da loro e gli rispondiamo
con l'ORU, siamo dal loro punto di vista "CCHS Application" nel ruolo di
sistema periferico — ma nell'architettura del middleware la parte che riceve
ordini e restituisce risultati è sempre "il LIS": qui è CCHS.

## 2. Cosa cambia nel codice

- `hl7mw/hl7.py` → `parse_adt()`: parser per `ADT^A0x` (patient demographics).
- `hl7mw/pipeline.py` → `OrderReceiver._handle()` ora smista per tipo messaggio:
  `ADT*` → `_handle_adt()` (ACK positivo, audit log, **nessun ordine creato** —
  i dati paziente arrivano di nuovo, completi, nel successivo `ORM^O01`, che
  segue il percorso esistente invariato: `hl7.parse_order` → `store.upsert_order`).
  Qualunque altro tipo non gestito resta rifiutato con `AR`, come prima.
- **Nessuna modifica** a `ResultReceiver`, `Forwarder`, `try_complete`, al ciclo
  di vita dell'ordine (`RECEIVED → READY → SENT`/`ERROR`) o agli adapter
  strumento (`hemoscreen_hl7.py`, `hemoscreen_poct1a2.py`): CCHS usa
  esattamente lo stesso percorso di un LIS "tradizionale".

## 3. Collegare HemoScreen (o un altro strumento)

Lo strumento che esegue fisicamente il test si collega al middleware come
sempre, indipendentemente da CCHS — vedi `README.md` → adapter HemoScreen.
In `config.json`:

```jsonc
// scegliere UNO dei due, secondo il protocollo del proprio HemoScreen
"hemoscreen_hl7_enabled": true, "hemoscreen_hl7_port": 6663,
// oppure
"hemoscreen_poct1a2_enabled": true, "hemoscreen_poct1a2_port": 6664
```

L'ordine a cui il risultato dello strumento viene associato (per
`sample_key`/placer/filler order number) è quello creato dall'`ORM^O01`
ricevuto da CCHS — nessuna configurazione aggiuntiva necessaria: la stessa
`Store` è condivisa da tutti i componenti.

## 4. Configurazione applicativa

In `config.json` (vedi `config.example.json`):

```jsonc
"order_listen_host": "0.0.0.0", "order_listen_port": 6661,  // CCHS si connette qui per ADT/ORM
"lis_host": "10.9.0.10", "lis_port": 2576,                  // dove CCHS riceve l'ORU (da onboarding)
"sending_app": "HL7MW", "sending_facility": "MIDDLEWARE",
"receiving_app": "CCHS", "receiving_facility": "CITIZENCARE"
```

## 5. VPN site-to-site (spec CCHS §5.1)

CCHS richiede che il traffico passi attraverso un tunnel VPN site-to-site
avviato dall'host del LIS/EMR. Setup completo, template WireGuard/OpenVPN e
unit systemd in **`vpn/README.md`**. In sintesi:

```jsonc
"vpn_enabled": true,
"vpn_manage_lifecycle": false,   // il tunnel è gestito da systemd (consigliato)
"vpn_health_check_host": "10.9.0.10",  // default se omesso: uguale a lis_host
"vpn_health_check_port": 2576
```

All'avvio, il middleware verifica che l'endpoint CCHS sia raggiungibile
attraverso il tunnel e logga un errore chiaro se non lo è — senza bloccare gli
altri flussi (lo strumento locale continua a funzionare comunque, e gli invii
verso CCHS restano `RECEIVED`/`READY`, ritentati automaticamente).

## 6. Dati da richiedere a CCHS in onboarding

- Endpoint pubblico del gateway VPN + materiale di accesso (chiave pubblica
  WireGuard o certificati client OpenVPN).
- **IP/porta a cui CCHS invierà** `ADT^A04`/`ORM^O01` → da aprire in ingresso
  su `order_listen_port`.
- **IP/porta di destinazione** su cui CCHS riceve l'`ORU^R01` → `lis_host`/`lis_port`.

## 7. Validazione (flusso da spec CCHS §5.2)

1. CCHS invia `ADT^A04`: verificare nei log `Paziente registrato dal LIS: id=...`.
2. CCHS invia `ORM^O01`: verificare `Ordine ricevuto dal LIS: sample=...`.
3. Lo strumento (es. HemoScreen) esegue il test e invia il risultato al
   middleware: l'ordine passa a `READY`.
4. Verificare che l'ordine raggiunga `SENT` verso CCHS:
   ```bash
   python3 -m hl7mw.cli --db hl7mw.db order <sample_key>
   ```
   oppure dalla dashboard REST (`GET /api/orders/{sample_key}`, se
   `api_enabled: true`), che mostra stato e timing (`received_at`,
   `first_result_at`, `ready_at`, `sent_at`) di ogni passaggio.

## 8. Limiti noti

- `ADT^A08` (aggiornamento paziente) è dichiarato come supportato da CCHS nella
  spec (§5.2) ma non ancora gestito: al momento solo `ADT^A04` viene
  riconosciuto esplicitamente (`_handle_adt` in realtà accetta qualunque
  sottotipo `ADT*` con lo stesso comportamento — ACK positivo, nessuna
  persistenza — ma non fa nulla di specifico per un A08).
- Regola di completezza: come per il resto del sistema, un ordine passa a
  `READY` non appena arriva **almeno un** risultato dallo strumento; se
  servono più test/risultati per lo stesso ordine, adattare
  `pipeline.try_complete()`.
