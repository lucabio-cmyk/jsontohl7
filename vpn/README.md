# VPN site-to-site verso Citizen Care Connect (CCHS)

CCHS è il **LIS**: nel middleware corrisponde esattamente alla coppia già
esistente `OrderReceiver` (riceve `ADT^A04`+`ORM^O01` da CCHS) / `Forwarder`
(invia l'`ORU^R01` a CCHS). Non serve nessun componente dedicato — vedi
`INTEGRATION_CITIZENCARE.md` per il dettaglio del flusso.

La loro spec (§5.1 "Integration — Communication") richiede però che questo
traffico passi attraverso un **tunnel VPN site-to-site**, avviato dall'host del
LIS/EMR — qui il nostro middleware. Il middleware **non reimplementa**
IPsec/WireGuard/OpenVPN: usa quello che è già installato sull'host (vedi
`hl7mw/vpn.py`), e nella modalità consigliata si limita a verificare che il
tunnel sia su, lasciando la gestione del tunnel a systemd o a un'appliance di rete.

## 1. Dati da richiedere a CCHS in onboarding

Contattare `support@citizencarehealth.com` (§7 della loro spec) e farsi fornire:

- Endpoint pubblico del gateway VPN (host:porta) e materiale della VPN
  (chiave pubblica WireGuard, oppure certificati client OpenVPN).
- IP/subnet assegnata a questo lato del tunnel.
- **IP e porta a cui CCHS invierà** `ADT^A04`/`ORM^O01`, per aprirla in ingresso
  (`order_listen_host`/`order_listen_port` di `config.json`, di norma
  raggiungibile dentro il tunnel).
- **IP e porta di destinazione** su cui CCHS riceve l'`ORU^R01` di risposta →
  vanno in `lis_host`/`lis_port` di `config.json`.

## 2. Configurare il tunnel

Scegliere un provider e compilare il template corrispondente:

- **WireGuard** (consigliato, più semplice): copiare
  [`wg-cchs.conf.example`](./wg-cchs.conf.example) in `/etc/wireguard/wg-cchs.conf`
  (permessi `600`), compilare i placeholder, poi:
  ```bash
  sudo systemctl enable --now wg-quick@wg-cchs
  sudo wg show wg-cchs   # verifica handshake
  ```
- **OpenVPN**: copiare [`openvpn-cchs.conf.example`](./openvpn-cchs.conf.example)
  in `/etc/openvpn/client/cchs.conf`, ottenere `ca.crt`/`client.crt`/`client.key`
  da CCHS (NON versionarli), poi:
  ```bash
  sudo systemctl enable --now openvpn-client@cchs
  sudo systemctl status openvpn-client@cchs
  ```

Il tunnel deve limitare `AllowedIPs`/le route al solo range CCHS concordato (non
va usato come default gateway): serve esclusivamente a raggiungere il loro Cloud
Ingest Server, come richiesto anche dalla loro sezione "Firewall and Routing".

## 3. Firewall

Bidirezionale, ristretto agli IP/porte concordati con CCHS, attraverso il tunnel:

- **Inbound**: da CCHS verso `order_listen_host:order_listen_port` di questo
  host (`ADT^A04` + `ORM^O01`, MLLP/TCP) — CCHS apre la connessione verso di noi.
- **Outbound**: da questo host verso `lis_host:lis_port` (`ORU^R01`, MLLP/TCP)
  — siamo noi ad aprire la connessione verso CCHS.

## 4. Collegare il middleware

In `config.json` (vedi `config.example.json`):

```jsonc
"lis_host": "10.9.0.10",   // IP CCHS lato tunnel, da onboarding
"lis_port": 2576,          // porta su cui CCHS riceve l'ORU^R01

"vpn_enabled": true,
"vpn_manage_lifecycle": false,   // il tunnel è gestito da systemd (step 2), non dal middleware
"vpn_health_check_host": "10.9.0.10",   // default se omesso: uguale a lis_host
"vpn_health_check_port": 2576
```

Con `vpn_manage_lifecycle: false` (default e modalità consigliata in produzione)
il middleware **non** avvia/ferma il tunnel: si limita, all'avvio, a verificare
che `vpn_health_check_host:port` sia raggiungibile e a loggare un errore chiaro
se non lo è — senza bloccare l'avvio degli altri flussi (strumenti locali come
HemoScreen continuano a funzionare comunque). Se invece si preferisce che sia il
middleware stesso a eseguire `wg-quick up`/`down` (es. ambienti senza systemd),
impostare `"vpn_manage_lifecycle": true`: vedi `hl7mw/vpn.py` per i comandi di
default per provider, o `vpn_up_command`/`vpn_down_command` per un comando custom.

## 5. Validazione (flusso da spec CCHS §5.2)

1. CCHS invia `ADT^A04` (registrazione paziente) al middleware → ACK positivo,
   nessun ordine creato (vedi `pipeline.OrderReceiver._handle_adt`).
2. CCHS invia `ORM^O01` (ordine) → status `RECEIVED`.
3. Lo strumento (es. HemoScreen) esegue il test e invia il risultato al
   middleware → l'ordine passa a `READY`.
4. Il `Forwarder` inoltra l'`ORU^R01` completo a CCHS (`lis_host:lis_port`) →
   status `SENT`.

Verificabile end-to-end con `python3 -m hl7mw.cli --db hl7mw.db order <sample_key>`
o dalla dashboard REST (`/api/orders/{sample_key}`), che mostrano stato e timing
di ogni passaggio.
