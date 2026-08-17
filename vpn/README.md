# VPN site-to-site verso Citizen Care Connect (CCHS)

La spec di CCHS (§5.1 "Integration — Communication") richiede che il traffico HL7
verso il loro Cloud Ingest Server passi attraverso un **tunnel VPN site-to-site**
avviato dall'host del LIS/EMR — qui il nostro middleware. Il middleware **non
reimplementa** IPsec/WireGuard/OpenVPN: usa quello che è già installato sull'host
(vedi `hl7mw/vpn.py`), e nella modalità consigliata si limita a verificare che il
tunnel sia su, lasciando la gestione del tunnel a systemd o a un'appliance di rete.

## 1. Dati da richiedere a CCHS in onboarding

Contattare `support@citizencarehealth.com` (§7 della loro spec) e farsi fornire:

- Endpoint pubblico del gateway VPN (host:porta) e materiale della VPN
  (chiave pubblica WireGuard, oppure certificati client OpenVPN).
- IP/subnet assegnata a questo lato del tunnel.
- **IP e porta di destinazione** per l'invio di ADT^A04 / ORM^O01 (verso CCHS) →
  vanno in `citizencare_host` / `citizencare_port` di `config.json`.
- **IP e porta sorgente** da cui CCHS invierà gli ORU^R01 di ritorno, per aprire
  la porta in ingresso (`citizencare_result_listen_port`, default 6665) sul
  firewall lato tunnel.

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

Bidirezionale, ristretto agli IP/porte concordati con CCHS:

- **Outbound**: da questo host verso `citizencare_host:citizencare_port`
  (ADT^A04 + ORM^O01, MLLP/TCP).
- **Inbound**: da CCHS verso `citizencare_result_listen_host:citizencare_result_listen_port`
  di questo host (ORU^R01, MLLP/TCP), attraverso il tunnel.

## 4. Collegare il middleware

In `config.json` (vedi `config.example.json`):

```jsonc
"citizencare_enabled": true,
"citizencare_host": "10.9.0.10",        // IP CCHS lato tunnel, da onboarding
"citizencare_port": 2576,               // porta ADT/ORM CCHS, da onboarding
"citizencare_result_listen_port": 6665, // porta su cui CCHS ci invia gli ORU

"vpn_enabled": true,
"vpn_manage_lifecycle": false,          // il tunnel è gestito da systemd (step 2), non dal middleware
"vpn_health_check_host": "10.9.0.10",   // stesso IP di citizencare_host
"vpn_health_check_port": 2576
```

Con `vpn_manage_lifecycle: false` (default e modalità consigliata in produzione)
il middleware **non** avvia/ferma il tunnel: si limita, all'avvio, a verificare
che `vpn_health_check_host:port` sia raggiungibile e a loggare un errore chiaro
se non lo è — senza bloccare l'avvio degli altri flussi (LIS/strumenti locali
continuano a funzionare). Se invece si preferisce che sia il middleware stesso a
eseguire `wg-quick up`/`down` (es. ambienti senza systemd), impostare
`"vpn_manage_lifecycle": true`: vedi `hl7mw/vpn.py` per i comandi di default per
provider, o `vpn_up_command`/`vpn_down_command` per un comando custom.

## 5. Validazione (flusso da spec CCHS §5.2)

1. Un ordine arriva dal LIS al middleware (`order_listen_port`) → status `RECEIVED`.
2. Il middleware invia `ADT^A04` poi `ORM^O01` a CCHS → status `SENT_TO_CCHS`.
3. CCHS esegue il test e restituisce `ORU^R01` sulla porta
   `citizencare_result_listen_port` → il middleware associa il risultato,
   l'ordine passa a `READY`.
4. Il `Forwarder` esistente inoltra l'`ORU^R01` completo al LIS → status `SENT`.

Verificabile end-to-end con `python3 -m hl7mw.cli --db hl7mw.db order <sample_key>`
o dalla dashboard REST (`/api/orders/{sample_key}`), che mostrano stato e timing
di ogni passaggio.
