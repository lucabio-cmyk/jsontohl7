#!/usr/bin/env python3
"""
hl7mw.run — avvia il middleware completo:
  - OrderReceiver  (ordini dal LIS: ORM/OML, + ADT^A0x di registrazione paziente)
  - ResultReceiver (risultati dagli strumenti)
  - Forwarder      (loop periodico: ordini READY -> ORU -> LIS)
  - status web     (pagina/JSON di stato per la UI, opzionale)
  - vpn            (opzionale: verifica/avvio tunnel VPN verso il LIS, es. Citizen Care Connect)

Uso:
    python3 -m hl7mw.run -c config.json
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import socket
import sys
import threading
import time
from pathlib import Path

from . import hl7
from . import mllp
from . import vpn as vpnmod
from .logging_setup import configure_logging
from .store import Store
from .pipeline import OrderReceiver, ResultReceiver, Forwarder
from .monitor import DeviceMonitor
from .webstatus import StatusServer
from .adapters.hemoscreen_hl7 import HemoscreenHl7ResultReceiver
from .adapters.hemoscreen_poct1a2 import HemoscreenPoct1A2Receiver

try:
    from .api import init_api
    import uvicorn
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False

LOG = logging.getLogger("hl7mw")

DEFAULTS = {
    "db_path": "hl7mw.db",
    # Log applicativo (diverso dall'audit_log clinico su DB, vedi store.py):
    # traccia tecnica di tutto il servizio (MLLP, DB, VPN, API) su file
    # rotante + console, per diagnosticare problemi senza dover riprodurli.
    # log_file="" disabilita il file e logga solo su console.
    "log_level": "INFO",
    "log_file": "hl7mw.log",
    "log_max_bytes": 10 * 1024 * 1024,
    "log_backup_count": 5,
    "log_console": True,
    "order_listen_host": "0.0.0.0", "order_listen_port": 6661,
    # Canale ADT dedicato, opzionale: alcuni LIS (es. Dedalus) aprono due
    # connessioni MLLP separate verso l'EMR Bridge, una per ADT e una per
    # ORM, invece di un unico canale. Se adt_listen_port e' impostato (>0),
    # il middleware apre un secondo listener su questa porta che riusa la
    # stessa logica di OrderReceiver (ADT^A0x -> ACK, nessun ordine creato);
    # order_listen_port resta comunque in grado di gestire entrambi i tipi.
    "adt_listen_host": "", "adt_listen_port": 0,
    "result_listen_host": "0.0.0.0", "result_listen_port": 6662,
    "lis_host": "127.0.0.1", "lis_port": 2575,
    "forward_interval_seconds": 10.0,
    "ack_retry_attempts": 2,
    "ack_retry_backoff_seconds": 0.5,
    # --- Riscontro HL7 (capitolo 2.9 dello standard) ---------------------------
    # In ingresso: "auto" onora MSH-15/MSH-16 se il mittente li valorizza
    # (enhanced mode: commit ACK + ACK applicativo), altrimenti risponde con un
    # solo ACK come sempre. "original" ignora MSH-15/16, "enhanced" li impone.
    "hl7_ack_mode": "auto",
    # Aggiunge il segmento ERR (tabella HL7 0357) ai NACK: rende diagnosticabile
    # il rifiuto invece di lasciare solo testo libero in MSA-3.
    "hl7_ack_include_err": True,
    # Idempotenza: una ritrasmissione con lo stesso MSH-10 non viene rielaborata,
    # le si ripete l'ACK gia' dato (vedi store.processed_messages).
    "hl7_dedup_enabled": True,
    "hl7_dedup_retention_hours": 72.0,
    # Risposta agli ordini: "ack" (ACK^O01^ACK) oppure "order" (ORR^O02 per ORM,
    # ORL^O22 per OML) per i LIS che si aspettano la risposta applicativa d'ordine.
    "order_response_mode": "ack",
    # In uscita verso il LIS: "original" (un solo ACK) o "enhanced" (MSH-15/16=AL,
    # il LIS risponde commit ACK e poi ACK applicativo; SENT solo sul secondo).
    "lis_ack_mode": "original",
    "lis_application_ack_timeout": 0,   # 0 = usa mllp_read_timeout
    # Timeout MLLP: attesa del primo messaggio e inattivita' massima di una
    # connessione persistente (un LIS tiene aperta la connessione per ore).
    "mllp_read_timeout": 60.0,
    "mllp_idle_timeout": 300.0,
    # Tetto alle connessioni simultanee per listener: con le connessioni
    # persistenti ogni peer trattiene un thread fino all'idle timeout, quindi
    # senza limite un host raggiungibile potrebbe saturare il processo e
    # impedire al LIS/strumento vero di collegarsi. 0 = nessun limite.
    "mllp_max_connections": 64,
    "status_host": "127.0.0.1", "status_port": 8080, "status_enabled": True,
    # La dashboard non ha ancora autenticazione (vedi "Da fare" in CLAUDE.md):
    # il default e' l'ascolto sul solo loopback. Per raggiungerla da un altro PC
    # va messo esplicitamente "0.0.0.0" in configurazione, con la consapevolezza
    # che chiunque sulla rete la vedrebbe.
    "api_enabled": True, "api_host": "127.0.0.1", "api_port": 8000,
    "sending_app": "HL7MW", "sending_facility": "MIDDLEWARE",
    "receiving_app": "LIS", "receiving_facility": "OSP",
    "device_offline_timeout_seconds": 300.0,
    # Adapter HemoScreen HL7 v2.4
    "hemoscreen_hl7_enabled": False,
    "hemoscreen_hl7_host": "0.0.0.0",
    "hemoscreen_hl7_port": 6663,
    # Adapter HemoScreen POCT1-A2
    "hemoscreen_poct1a2_enabled": False,
    "hemoscreen_poct1a2_host": "0.0.0.0",
    "hemoscreen_poct1a2_port": 6664,
    "hemoscreen_poct1a2_continuous_mode": False,
    "hemoscreen_poct1a2_timeout": 65.0,
    # VPN site-to-site verso il LIS (es. Citizen Care Connect richiede un tunnel
    # site-to-site verso il loro Cloud Ingest Server) — vedi hl7mw/vpn.py e vpn/README.md
    "vpn_enabled": False,
    "vpn_provider": "external",       # wireguard | openvpn | external
    "vpn_interface": "",
    "vpn_config_path": "",
    "vpn_up_command": "", "vpn_down_command": "",
    "vpn_manage_lifecycle": False,    # False = tunnel gestito fuori dal middleware (systemd/appliance)
    "vpn_health_check_host": "", "vpn_health_check_port": 0,   # default: lis_host/lis_port se non specificato
    "vpn_health_check_timeout": 5.0,
    "vpn_wait_seconds": 20.0, "vpn_poll_interval": 1.0,
}


# Chiavi di configurazione che contengono un percorso su disco.
PATH_KEYS = ("db_path", "log_file")
# Valori che non sono percorsi anche se non sono assoluti (SQLite in memoria,
# URI sqlite): vanno lasciati intatti.
_NON_PATH_VALUES = (":memory:",)


def resolve_config_paths(cfg: dict, config_path: str | None) -> dict:
    """Rende assoluti (in-place) i percorsi relativi, rispetto al file di
    configurazione che li contiene.

    "Relativo al proprio file" e non "relativo alla directory corrente": la
    directory corrente di un processo avviato da un'icona e' imprevedibile, e
    componenti diversi (servizio, dashboard, CLI) la vedrebbero diversa,
    finendo per scrivere e leggere file differenti con la stessa configurazione.
    """
    base = Path(config_path).expanduser().resolve().parent if config_path else Path.cwd()
    for key in PATH_KEYS:
        value = cfg.get(key)
        if not value or value in _NON_PATH_VALUES or str(value).startswith("file:"):
            continue
        if not Path(value).is_absolute():
            cfg[key] = str(base / value)
    return cfg


def load_config(path: str | None) -> dict:
    cfg = dict(DEFAULTS)
    if not path and Path("config.json").exists():
        # Auto-discovery: se non passato -c, ma un config.json esiste nella
        # cwd (es. salvato dalla GUI Impostazioni al giro precedente), usalo.
        path = "config.json"
    if path:
        cfg.update(json.loads(Path(path).read_text(encoding="utf-8")))
    return resolve_config_paths(cfg, path)


def resolve_vpn_health_check(cfg: dict) -> None:
    """Applica (in-place) il fallback host/porta dell'health-check VPN sul LIS
    (lis_host/lis_port) quando non specificati esplicitamente in config —
    indipendentemente l'uno dall'altro, cosi' un operatore che ne configura
    solo uno (es. un health-check su un endpoint diverso da lis_host ma sulla
    stessa porta) non si vede scavalcare anche l'altro."""
    if not cfg.get("vpn_health_check_host"):
        cfg["vpn_health_check_host"] = cfg.get("lis_host")
    if not cfg.get("vpn_health_check_port"):
        cfg["vpn_health_check_port"] = cfg.get("lis_port")


class MiddlewareService:
    """I componenti del middleware come un oggetto avviabile e fermabile.

    Esiste perche' il servizio ha due modi di essere eseguito e devono
    condividere esattamente la stessa inizializzazione:
      - `run.main()`, che avvia e poi resta nel loop periodico;
      - `hl7mw.desktop`, che avvia in un thread e tiene il thread principale
        per la finestra dell'interfaccia (i toolkit grafici lo richiedono).
    """

    def __init__(self, cfg: dict, config_path: str = "config.json"):
        self.cfg = cfg
        self.config_path = config_path
        self.store: Store | None = None
        self.monitor: DeviceMonitor | None = None
        self.forwarder: Forwarder | None = None
        self.vpn_manager = None
        self._servers: list = []          # oggetti con .stop()
        self._stop = threading.Event()
        self._started = False
        self._last_purge = time.monotonic()

    # ----- indirizzi -----
    @property
    def api_enabled(self) -> bool:
        return bool(self.cfg.get("api_enabled")) and FASTAPI_AVAILABLE

    @property
    def ui_endpoint(self) -> tuple[str, int] | None:
        """(host, porta) dell'interfaccia effettivamente avviata, None se non ce
        n'e' nessuna (API non disponibile e pagina di stato disabilitata)."""
        if self.api_enabled:
            host, port = self.cfg.get("api_host", "127.0.0.1"), self.cfg.get("api_port", 8000)
        elif self.cfg.get("status_enabled"):
            host, port = self.cfg.get("status_host", "127.0.0.1"), self.cfg.get("status_port", 8080)
        else:
            return None
        # 0.0.0.0 e' un indirizzo di ascolto, non di connessione.
        return ("127.0.0.1" if host in ("0.0.0.0", "::", "") else host), int(port)

    @property
    def ui_url(self) -> str:
        """URL dell'interfaccia da aprire, stringa vuota se non ce n'e' una."""
        endpoint = self.ui_endpoint
        return f"http://{endpoint[0]}:{endpoint[1]}" if endpoint else ""

    # ----- avvio -----
    def start(self) -> "MiddlewareService":
        """Avvia tutti i componenti. Se uno fallisce, quelli gia' avviati
        vengono fermati prima di propagare l'errore: altrimenti resterebbero
        listener aperti (e un tunnel VPN gestito da noi acceso) senza che il
        chiamante abbia un riferimento su cui invocare stop()."""
        try:
            return self._start()
        except Exception:
            LOG.exception("Avvio fallito: fermo i componenti gia' avviati.")
            self.stop()
            raise

    def _start(self) -> "MiddlewareService":
        cfg = self.cfg
        self.store = Store(cfg["db_path"])
        self.monitor = DeviceMonitor(self.store, cfg.get("device_offline_timeout_seconds", 300.0))

        if cfg.get("vpn_enabled"):
            resolve_vpn_health_check(cfg)
            self.vpn_manager = vpnmod.from_config(cfg)
            self.vpn_manager.ensure_up()  # non bloccante: logga ed eventualmente ritenta nel loop

        # Opzioni di riscontro comuni ai canali in ingresso (vedi hl7mw/ack.py).
        inbound_opts = dict(
            ack_mode=cfg.get("hl7_ack_mode", "auto"),
            include_err=cfg.get("hl7_ack_include_err", True),
            dedup=cfg.get("hl7_dedup_enabled", True),
            read_timeout=cfg.get("mllp_read_timeout", 60.0),
            idle_timeout=cfg.get("mllp_idle_timeout", 300.0),
            max_connections=cfg.get("mllp_max_connections", 64),
        )

        order_rx = OrderReceiver(self.store, cfg["order_listen_host"], cfg["order_listen_port"],
                                 cfg["sending_app"], cfg["sending_facility"], self.monitor,
                                 order_response_mode=cfg.get("order_response_mode", "ack"),
                                 **inbound_opts).start()
        self._servers.append(order_rx)

        if cfg.get("adt_listen_port"):
            adt_host = cfg.get("adt_listen_host") or cfg["order_listen_host"]
            self._servers.append(mllp.MllpServer(
                adt_host, cfg["adt_listen_port"], order_rx._handle,
                read_timeout=cfg.get("mllp_read_timeout", 60.0),
                idle_timeout=cfg.get("mllp_idle_timeout", 300.0),
                max_connections=cfg.get("mllp_max_connections", 64),
            ).start())
            LOG.info("Canale ADT dedicato in ascolto su %s:%s (es. LIS con connessioni ADT/ORM separate)",
                     adt_host, cfg["adt_listen_port"])

        self._servers.append(
            ResultReceiver(self.store, cfg["result_listen_host"], cfg["result_listen_port"],
                           cfg["sending_app"], cfg["sending_facility"], self.monitor,
                           **inbound_opts).start())

        oru_cfg = hl7.OruConfig(cfg["sending_app"], cfg["sending_facility"],
                                cfg["receiving_app"], cfg["receiving_facility"])
        self.forwarder = Forwarder(
            self.store, cfg["lis_host"], cfg["lis_port"], oru_cfg,
            read_timeout=cfg.get("mllp_read_timeout", 30.0),
            ack_retry_attempts=cfg.get("ack_retry_attempts", 2),
            ack_retry_backoff_seconds=cfg.get("ack_retry_backoff_seconds", 0.5),
            ack_mode=cfg.get("lis_ack_mode", "original"),
            application_ack_timeout=cfg.get("lis_application_ack_timeout") or None)

        if cfg.get("status_enabled"):
            self._servers.append(StatusServer(self.store, cfg["status_host"], cfg["status_port"]).start())
            LOG.info("Status UI su http://%s:%s", cfg["status_host"], cfg["status_port"])

        if self.api_enabled:
            self._start_api()
        elif cfg.get("api_enabled"):
            LOG.warning("API abilitato ma FastAPI non installato (pip install fastapi uvicorn)")

        if cfg.get("hemoscreen_hl7_enabled"):
            self._servers.append(HemoscreenHl7ResultReceiver(
                self.store, cfg["hemoscreen_hl7_host"], cfg["hemoscreen_hl7_port"],
                cfg["sending_app"], cfg["sending_facility"], self.monitor).start())

        if cfg.get("hemoscreen_poct1a2_enabled"):
            self._servers.append(HemoscreenPoct1A2Receiver(
                self.store, cfg["hemoscreen_poct1a2_host"], cfg["hemoscreen_poct1a2_port"],
                continuous_mode=cfg["hemoscreen_poct1a2_continuous_mode"],
                timeout=cfg["hemoscreen_poct1a2_timeout"], monitor=self.monitor).start())

        self._started = True
        LOG.info("Riscontro HL7: ingresso=%s, risposta ordini=%s, verso LIS=%s, deduplica=%s.",
                 cfg.get("hl7_ack_mode", "auto"), cfg.get("order_response_mode", "ack"),
                 cfg.get("lis_ack_mode", "original"),
                 "attiva" if cfg.get("hl7_dedup_enabled", True) else "disattiva")
        return self

    def _start_api(self) -> None:
        app = init_api(self.store, self.config_path, DEFAULTS)
        host, port = self.cfg.get("api_host", "127.0.0.1"), self.cfg.get("api_port", 8000)

        def run_api():
            uvicorn.run(
                app, host=host, port=port, log_level="info", access_log=True,
                # log_config=None: non applicare la configurazione di logging
                # separata di uvicorn (che per default non propaga al logger
                # radice) - cosi' anche i log di uvicorn/FastAPI (incluso
                # l'access log di ogni richiesta) finiscono nello stesso
                # file/console configurati da configure_logging(), invece di
                # un flusso separato invisibile a chi legge hl7mw.log.
                log_config=None,
                # Espliciti (non "auto"): "auto" risolve l'implementazione via
                # importlib a runtime, invisibile all'analisi statica di PyInstaller
                # nell'eseguibile Windows (vedi packaging/win/). h11/asyncio sono
                # puro Python, portabili senza compilazione; niente websocket:
                # la dashboard usa solo HTTP/JSON.
                loop="asyncio", http="h11", ws="none", lifespan="on",
            )

        threading.Thread(target=run_api, daemon=True, name="api").start()
        LOG.info("Dashboard su http://%s:%s", host, port)

    def wait_until_ready(self, timeout: float = 15.0) -> bool:
        """Attende che l'interfaccia risponda sulla sua porta.

        L'app desktop deve aprire la finestra solo quando c'e' qualcosa da
        mostrare: puntarla su un server non ancora in ascolto darebbe una
        pagina di errore che non si aggiorna da sola.
        """
        endpoint = self.ui_endpoint
        if endpoint is None:
            LOG.info("Nessuna interfaccia configurata (api_enabled/status_enabled disattivi): "
                     "il servizio lavora comunque, non c'e' nulla da aprire.")
            return False
        host, port = endpoint
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=0.5):
                    return True
            except OSError:
                time.sleep(0.2)
        LOG.warning("Interfaccia non raggiungibile su %s:%s entro %.0fs.", host, port, timeout)
        return False

    # ----- loop periodico -----
    def tick(self) -> None:
        """Un giro di manutenzione: inoltro, health strumenti, pulizia deduplica."""
        try:
            self.forwarder.forward_ready()
        except Exception:
            LOG.exception("Errore nel loop di inoltro; continuo.")
        try:
            # Rileva strumenti andati OFFLINE (nessun messaggio da oltre
            # device_offline_timeout_seconds): senza questa chiamata periodica
            # lo status resta ONLINE per sempre dopo il primo messaggio, e la
            # dashboard non segnalerebbe mai uno strumento spento/scollegato.
            self.monitor.update_health_status()
        except Exception:
            LOG.exception("Errore nel controllo health strumenti; continuo.")
        try:
            # Sfoltisce la tabella di deduplica: oltre la finestra di
            # ritrasmissione plausibile i control id non servono piu' e la
            # tabella crescerebbe senza limite.
            now = time.monotonic()
            if self.cfg.get("hl7_dedup_enabled", True) and now - self._last_purge >= 3600:
                self._last_purge = now
                removed = self.store.purge_processed(self.cfg.get("hl7_dedup_retention_hours", 72.0))
                if removed:
                    LOG.info("Deduplica: rimossi %d control id oltre le %.0f ore.",
                             removed, self.cfg.get("hl7_dedup_retention_hours", 72.0))
        except Exception:
            LOG.exception("Errore nella pulizia della tabella di deduplica; continuo.")

    def run_forever(self) -> None:
        """Loop periodico fino a stop(). Blocca il chiamante."""
        self._last_purge = time.monotonic()
        interval = self.cfg.get("forward_interval_seconds", 10.0)
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(interval)

    def run_in_background(self) -> threading.Thread:
        """Come run_forever() ma in un thread: usato dall'app desktop, che deve
        lasciare libero il thread principale per la finestra."""
        t = threading.Thread(target=self.run_forever, daemon=True, name="middleware-loop")
        t.start()
        return t

    # ----- arresto -----
    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        self._stop.set()

    def stop(self) -> None:
        self.request_stop()
        for server in self._servers:
            try:
                server.stop()
            except Exception:
                LOG.exception("Errore nell'arresto di %s; continuo.", type(server).__name__)
        self._servers.clear()
        if self.vpn_manager:
            try:
                self.vpn_manager.down()
            except vpnmod.VpnError as e:
                LOG.warning("VPN: arresto tunnel non riuscito: %s", e)
        if self._started:
            LOG.info("Middleware arrestato.")
        self._started = False


def setup(argv=None, description: str = "Middleware HL7v2 order-driven.") -> tuple[dict, str, argparse.Namespace]:
    """Argomenti + configurazione + logging: la parte comune a CLI e app desktop."""
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("-c", "--config")
    ap.add_argument("--loglevel", default=None,
                    help="Sovrascrive log_level della configurazione (DEBUG/INFO/WARNING/ERROR)")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    config_path = args.config or "config.json"
    configure_logging(
        level=args.loglevel or cfg.get("log_level", "INFO"),
        log_file=cfg.get("log_file", ""),
        max_bytes=cfg.get("log_max_bytes", 10 * 1024 * 1024),
        backup_count=cfg.get("log_backup_count", 5),
        console=cfg.get("log_console", True),
    )
    return cfg, config_path, args


def main(argv=None) -> int:
    cfg, config_path, _args = setup(argv)
    service = MiddlewareService(cfg, config_path).start()

    def _handle_signal(_s, _f):
        service.request_stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    LOG.info("Middleware avviato. Ctrl-C per fermare.")
    try:
        service.run_forever()
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
