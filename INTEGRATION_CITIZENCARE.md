# Integrazione — Citizen Care Connect (CCHS)

Guida operativa per collegare il middleware al fornitore esterno **Citizen Care
Connect** (Citizen Care Health Solutions), sulla base di "Citizen Care Connect —
HL7 Specifications v1.0" (16-Sep-2025, `support@citizencarehealth.com`).

## 1. Ruolo di CCHS nel flusso

CCHS riceve ordini e restituisce risultati: nell'architettura del middleware
gioca esattamente il ruolo di uno **strumento esterno**, raggiunto via VPN
invece che via cavo/rete locale.

```
  LIS ──ORM/OML──▶ hl7mw (OrderReceiver, invariato)     status: RECEIVED
  hl7mw ──ADT^A04──▶ CCHS   (registrazione paziente)
  hl7mw ──ORM^O01──▶ CCHS   (ordine)                    status: SENT_TO_CCHS
                    ... test eseguito da CCHS ...
  hl7mw ◀──ORU^R01── CCHS   (risultato)                 status: READY
  hl7mw ──ORU^R01──▶ LIS    (Forwarder esistente, invariato)  status: SENT
```

Il codice è in `hl7mw/adapters/citizencare.py`:
- `CitizenCareForwarder` — periodico (stesso loop del `Forwarder` verso il LIS),
  prende gli ordini `RECEIVED` e invia `ADT^A04` poi `ORM^O01` a CCHS.
- `CitizenCareResultReceiver` — server MLLP che riceve l'`ORU^R01` di CCHS,
  lo associa all'ordine (per placer/filler order number, non per specimen
  barcode: CCHS non lo riceve né lo restituisce) e riusa `pipeline.try_complete`
  standard: da quel momento l'ordine è indistinguibile da uno con risultato
  arrivato da uno strumento locale, e il `Forwarder` esistente lo inoltra al LIS.

Errori di rete/VPN transitori nell'invio a CCHS lasciano l'ordine in `RECEIVED`
per il retry automatico al giro successivo (stesso pattern del `Forwarder`
verso il LIS). Un ACK negativo (`AE`/`AR`) di CCHS porta l'ordine in `ERROR`.

## 2. Configurazione applicativa

In `config.json` (vedi `config.example.json` per tutti i default):

```jsonc
"citizencare_enabled": true,
"citizencare_host": "10.9.0.10",          // IP CCHS lato tunnel (da onboarding)
"citizencare_port": 2576,                 // porta ADT/ORM CCHS (da onboarding)
"citizencare_result_listen_host": "0.0.0.0",
"citizencare_result_listen_port": 6665,   // porta su cui CCHS ci invia gli ORU

"citizencare_sending_app": "",            // vuoto = usa sending_app/facility globali
"citizencare_sending_facility": "",
"citizencare_receiving_app": "CCHS",
"citizencare_receiving_facility": "CITIZENCARE",

"citizencare_ack_retry_attempts": 2,
"citizencare_ack_retry_backoff_seconds": 0.5,
"citizencare_connect_timeout": 10.0,
"citizencare_read_timeout": 30.0
```

## 3. VPN site-to-site (spec CCHS §5.1)

CCHS richiede che il traffico passi attraverso un tunnel VPN site-to-site
avviato dall'host del LIS/EMR. Setup completo, template WireGuard/OpenVPN e
unit systemd in **`vpn/README.md`**. In sintesi, in `config.json`:

```jsonc
"vpn_enabled": true,
"vpn_manage_lifecycle": false,   // il tunnel è gestito da systemd (consigliato)
"vpn_health_check_host": "10.9.0.10",
"vpn_health_check_port": 2576
```

All'avvio, il middleware verifica che l'endpoint CCHS sia raggiungibile
attraverso il tunnel e logga un errore chiaro se non lo è — senza bloccare gli
altri flussi (LIS/strumenti locali continuano a funzionare comunque). Se si
preferisce che sia il middleware stesso a portare su/giù il tunnel (es.
ambienti senza systemd), impostare `"vpn_manage_lifecycle": true` e
`"vpn_provider": "wireguard"` (o `"openvpn"`): vedi `hl7mw/vpn.py`.

## 4. Dati da richiedere a CCHS in onboarding

- Endpoint pubblico del gateway VPN + materiale di accesso (chiave pubblica
  WireGuard o certificati client OpenVPN).
- IP/porta di destinazione per l'invio di `ADT^A04`/`ORM^O01` → `citizencare_host`/`citizencare_port`.
- IP/porta sorgente da cui CCHS invierà gli `ORU^R01` → da aprire sul firewall
  verso `citizencare_result_listen_port`.

## 5. Validazione (flusso da spec CCHS §5.2)

1. Un ordine reale (o di test) arriva dal LIS al middleware.
2. Verificare nei log: `CitizenCare: ordine inoltrato a CCHS sample=...`.
3. CCHS esegue il test e restituisce l'`ORU^R01`; verificare nei log:
   `CitizenCare: risultato associato all'ordine sample=... (N analiti)`.
4. Verificare che l'ordine raggiunga `SENT` verso il LIS:
   ```bash
   python3 -m hl7mw.cli --db hl7mw.db order <sample_key>
   ```
   oppure dalla dashboard REST (`GET /api/orders/{sample_key}`, se
   `api_enabled: true`), che mostra stato e timing (`received_at`,
   `first_result_at`, `ready_at`, `sent_at`) di ogni passaggio.
5. Lo strumento `CITIZENCARE` compare in `python3 -m hl7mw.cli --db hl7mw.db instruments`
   con lo stato `ONLINE` e il conteggio messaggi ricevuti da CCHS.

## 6. Limiti noti / da estendere

- Il matching dell'`ORU^R01` di ritorno usa placer/filler order number (assegnati
  dal LIS originale e riecheggiati da CCHS in ORC/OBR), non lo specimen barcode:
  CCHS non lo riceve nell'`ORM^O01` che inviamo (nessun segmento SPM nella loro
  spec). Se in futuro CCHS supporta SPM, si può aggiungerlo a
  `build_orm_o01()`.
- `ADT^A08` (aggiornamento paziente) è dichiarato come supportato da CCHS nella
  spec (§5.2) ma non ancora implementato in questo adapter: al momento inviamo
  solo `ADT^A04` per ordini nuovi.
- Regola di completezza: come per il resto del sistema, un ordine passa a
  `READY` non appena arriva **almeno un** `ORU^R01`; se CCHS invia risultati in
  più messaggi per lo stesso ordine, adattare `pipeline.try_complete()`.
