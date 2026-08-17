# VPN site-to-site — sostituzione di Citizen Care Connect (CCHS)

CCHS non è né il LIS né uno strumento: è essa stessa un middleware/bridge
("EMR Bridge Module") verso cui il vero LIS del cliente è oggi configurato.
Questo middleware corrisponde alla coppia già esistente `OrderReceiver`
(riceve `ADT^A04`+`ORM^O01`, al posto di CCHS) / `Forwarder` (invia l'`ORU^R01`,
al posto di CCHS) — vedi `INTEGRATION_CITIZENCARE.md` per il dettaglio del flusso.

La spec CCHS (§5.1 "Integration — Communication") richiede che questo
traffico passi attraverso un **tunnel VPN site-to-site**, avviato dal LIS/EMR
verso CCHS. Per sostituire CCHS senza toccare la configurazione del LIS,
questo middleware deve trovarsi sull'altro capo di quello stesso tunnel — o di
uno riconfigurato per puntare qui invece che al vero servizio CCHS. Il
middleware **non reimplementa** IPsec/WireGuard/OpenVPN: usa quello che è già
installato sull'host (vedi `hl7mw/vpn.py`), e nella modalità consigliata si
limita a verificare che il tunnel sia su, lasciando la gestione del tunnel a
systemd o a un'appliance di rete.

> **Tunnel già esistente/attivo?** Saltare le sezioni 2-3 (configurazione del
> tunnel): serve solo la sezione 4, impostando `vpn_provider: "external"` e
> `vpn_manage_lifecycle: false` (già i default) — il middleware si limita a
> verificare che l'endpoint del LIS sia raggiungibile attraverso il tunnel che
> già hai, senza toccarlo.

## 1. Dati da raccogliere per la sostituzione

Non da CCHS (che stiamo sostituendo), ma da chi gestisce il LIS/la rete del
cliente — tipicamente la configurazione VPN e di destinazione che il LIS ha
**già** verso CCHS, da reindirizzare verso questo middleware:

- Materiale VPN esistente lato LIS (chiave pubblica WireGuard del LIS,
  oppure certificati/CA usati per il tunnel OpenVPN verso CCHS) — o, se si
  crea un tunnel nuovo, generare una nuova coppia di chiavi per questo lato.
- IP/subnet da assegnare a questo lato del tunnel.
- **IP e porta a cui il LIS invia** `ADT^A04`/`ORM^O01` oggi (verso CCHS): va
  aperta in ingresso su questo host (`order_listen_host`/`order_listen_port`
  di `config.json`, raggiungibile dentro il tunnel).
- **IP e porta su cui il LIS riceve** l'`ORU^R01` di risposta → vanno in
  `lis_host`/`lis_port` di `config.json`.

## 2. Configurare il tunnel

Scegliere un provider e compilare il template corrispondente (i nomi file/
unit fanno riferimento a "cchs" per comodità, dato che il tunnel sostituisce
quello verso CCHS — rinominare a piacere):

- **WireGuard** (consigliato, più semplice): copiare
  [`wg-cchs.conf.example`](./wg-cchs.conf.example) in `/etc/wireguard/wg-cchs.conf`
  (permessi `600`), compilare i placeholder con i dati raccolti al punto 1, poi:
  ```bash
  sudo systemctl enable --now wg-quick@wg-cchs
  sudo wg show wg-cchs   # verifica handshake
  ```
- **OpenVPN**: copiare [`openvpn-cchs.conf.example`](./openvpn-cchs.conf.example)
  in `/etc/openvpn/client/cchs.conf` (o lato server, a seconda di chi origina
  il tunnel), poi:
  ```bash
  sudo systemctl enable --now openvpn-client@cchs
  sudo systemctl status openvpn-client@cchs
  ```

Il tunnel deve limitare `AllowedIPs`/le route al solo range concordato con il
LIS (non va usato come default gateway): serve esclusivamente a far transitare
il traffico HL7 che prima andava a CCHS.

## 3. Firewall

Bidirezionale, ristretto agli IP/porte concordati, attraverso il tunnel:

- **Inbound**: dal LIS verso `order_listen_host:order_listen_port` di questo
  host (`ADT^A04` + `ORM^O01`, MLLP/TCP) — il LIS apre la connessione verso di
  noi (come faceva verso CCHS).
- **Outbound**: da questo host verso `lis_host:lis_port` (`ORU^R01`, MLLP/TCP)
  — siamo noi ad aprire la connessione verso il LIS (come faceva CCHS).

## 4. Collegare il middleware

In `config.json` (vedi `config.example.json`): `lis_host`/`lis_port` sono gli
endpoint del **vero LIS**, non di CCHS.

```jsonc
"lis_host": "10.9.0.10",   // IP del LIS lato tunnel
"lis_port": 2576,          // porta su cui il LIS riceve l'ORU^R01

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

Se in questo caso il LIS è raggiungibile in rete locale (topologia diversa da
quella prevista dalla spec CCHS, che presuppone un servizio cloud remoto),
impostare semplicemente `"vpn_enabled": false` e saltare questa guida.

## 5. Validazione (flusso da spec CCHS §5.2)

1. Il LIS invia `ADT^A04` (registrazione paziente) al middleware → ACK positivo,
   nessun ordine creato (vedi `pipeline.OrderReceiver._handle_adt`).
2. Il LIS invia `ORM^O01` (ordine) → status `RECEIVED`.
3. Lo strumento (es. HemoScreen) esegue il test e invia il risultato al
   middleware → l'ordine passa a `READY`.
4. Il `Forwarder` inoltra l'`ORU^R01` completo al LIS (`lis_host:lis_port`) →
   status `SENT`.

Verificabile end-to-end con `python3 -m hl7mw.cli --db hl7mw.db order <sample_key>`
o dalla dashboard REST (`/api/orders/{sample_key}`), che mostrano stato e timing
di ogni passaggio.
