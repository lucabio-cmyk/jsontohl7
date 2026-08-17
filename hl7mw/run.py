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
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
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


class ServiceControl:
    """Punto di coordinamento tra il loop principale e l'API REST per fermare
    o riavviare il servizio su richiesta della GUI (pagina Impostazioni) —
    senza questo, l'unico modo per applicare una modifica di configurazione
    era chiudere e riavviare manualmente il processo da terminale/Task
    Manager, in contrasto con l'obiettivo di configurazione interamente da
    GUI (specialmente per l'eseguibile Windows lanciato con un doppio click,
    dove l'operatore potrebbe non avere familiarita' con la riga di comando)."""

    def __init__(self):
        self.stop_event = threading.Event()
        self.restart_requested = False

    def request_stop(self) -> None:
        self.stop_event.set()

    def request_restart(self) -> None:
        self.restart_requested = True
        self.stop_event.set()

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
    "status_host": "127.0.0.1", "status_port": 8080, "status_enabled": True,
    "api_enabled": True, "api_host": "0.0.0.0", "api_port": 8000,
    # Apre automaticamente il browser sulla dashboard all'avvio (se l'API e'
    # attiva): l'obiettivo e' che l'intera configurazione sia raggiungibile
    # da GUI senza dover conoscere URL/porta a memoria, specialmente per
    # l'eseguibile Windows lanciato con un doppio click. Disabilitare per
    # deployment headless (server/systemd) dove non c'e' un browser locale.
    "open_browser_on_start": True,
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


def load_config(path: str | None) -> dict:
    cfg = dict(DEFAULTS)
    if not path and Path("config.json").exists():
        # Auto-discovery: se non passato -c, ma un config.json esiste nella
        # cwd (es. salvato dalla GUI Impostazioni al giro precedente), usalo.
        path = "config.json"
    if path:
        cfg.update(json.loads(Path(path).read_text(encoding="utf-8")))
    return cfg


def _relaunch_command(config_path: str, loglevel: str | None) -> list[str]:
    """Ricostruisce il comando per rilanciare il servizio dopo un riavvio
    richiesto da GUI. Quando 'congelato' da PyInstaller, sys.executable punta
    gia' all'eseguibile stesso (non serve '-m hl7mw.run')."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable]
    else:
        cmd = [sys.executable, "-m", "hl7mw.run"]
    cmd += ["-c", config_path]
    if loglevel:
        cmd += ["--loglevel", loglevel]
    return cmd


def _maybe_open_browser(cfg: dict) -> None:
    """Se abilitato e l'API e' attiva, apre il browser di sistema sulla
    dashboard non appena risponde (poll su /health, max 10s) — in un thread
    separato per non bloccare l'avvio degli altri componenti. Fallisce in
    modo silenzioso (solo log) su ambienti headless senza browser."""
    if not cfg.get("open_browser_on_start", True):
        return
    if not (cfg.get("api_enabled") and FASTAPI_AVAILABLE):
        return
    port = cfg.get("api_port", 8000)
    url = f"http://127.0.0.1:{port}/"

    def _wait_and_open():
        deadline = time.monotonic() + 10.0
        reachable = False
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.0) as r:
                    if r.status == 200:
                        reachable = True
                        break
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.3)
        if not reachable:
            LOG.debug("Dashboard non raggiungibile entro 10s: apertura automatica del browser saltata.")
            return
        try:
            webbrowser.open(url)
            LOG.info("Dashboard aperta automaticamente nel browser (%s).", url)
        except Exception as e:
            LOG.debug("Apertura automatica del browser non riuscita (%s): apri manualmente %s", e, url)

    threading.Thread(target=_wait_and_open, daemon=True, name="open-browser").start()


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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Middleware HL7v2 order-driven.")
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

    control = ServiceControl()

    store = Store(cfg["db_path"])
    monitor = DeviceMonitor(store, cfg.get("device_offline_timeout_seconds", 300.0))

    vpn_manager = None
    if cfg.get("vpn_enabled"):
        resolve_vpn_health_check(cfg)
        vpn_manager = vpnmod.from_config(cfg)
        vpn_manager.ensure_up()  # non bloccante: logga ed eventualmente ritenta nel loop

    order_rx = OrderReceiver(store, cfg["order_listen_host"], cfg["order_listen_port"],
                             cfg["sending_app"], cfg["sending_facility"], monitor).start()

    adt_rx_server = None
    if cfg.get("adt_listen_port"):
        adt_rx_server = mllp.MllpServer(
            cfg.get("adt_listen_host") or cfg["order_listen_host"],
            cfg["adt_listen_port"],
            order_rx._handle,
        ).start()
        LOG.info("Canale ADT dedicato in ascolto su %s:%s (es. LIS con connessioni ADT/ORM separate)",
                cfg.get("adt_listen_host") or cfg["order_listen_host"], cfg["adt_listen_port"])

    result_rx = ResultReceiver(store, cfg["result_listen_host"], cfg["result_listen_port"],
                               cfg["sending_app"], cfg["sending_facility"], monitor).start()
    oru_cfg = hl7.OruConfig(cfg["sending_app"], cfg["sending_facility"],
                            cfg["receiving_app"], cfg["receiving_facility"])
    forwarder = Forwarder(store, cfg["lis_host"], cfg["lis_port"], oru_cfg,
                          ack_retry_attempts=cfg.get("ack_retry_attempts", 2),
                          ack_retry_backoff_seconds=cfg.get("ack_retry_backoff_seconds", 0.5))

    status = None
    if cfg.get("status_enabled"):
        status = StatusServer(store, cfg["status_host"], cfg["status_port"]).start()
        LOG.info("Status UI su http://%s:%s", cfg["status_host"], cfg["status_port"])

    api_thread = None
    uvicorn_server = None
    if cfg.get("api_enabled") and FASTAPI_AVAILABLE:
        app = init_api(store, config_path, DEFAULTS, control)

        uvicorn_config = uvicorn.Config(
            app,
            host=cfg.get("api_host", "0.0.0.0"),
            port=cfg.get("api_port", 8000),
            log_level="info",
            access_log=True,
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
        # Server esplicito (non uvicorn.run) per poter chiedere uno shutdown
        # pulito (should_exit) dal loop principale: necessario perche' un
        # riavvio richiesto da GUI deve rilasciare davvero le porte prima di
        # avviare la nuova istanza, non solo terminare il processo di colpo.
        uvicorn_server = uvicorn.Server(uvicorn_config)
        api_thread = threading.Thread(target=uvicorn_server.run, daemon=True)
        api_thread.start()
        LOG.info("FastAPI Dashboard su http://%s:%s", cfg.get("api_host", "0.0.0.0"), cfg.get("api_port", 8000))
        _maybe_open_browser(cfg)
    elif cfg.get("api_enabled") and not FASTAPI_AVAILABLE:
        LOG.warning("API abilitato ma FastAPI non installato (pip install fastapi uvicorn)")

    hs_hl7 = None
    if cfg.get("hemoscreen_hl7_enabled"):
        hs_hl7 = HemoscreenHl7ResultReceiver(
            store,
            cfg["hemoscreen_hl7_host"],
            cfg["hemoscreen_hl7_port"],
            cfg["sending_app"],
            cfg["sending_facility"],
            monitor,
        ).start()

    hs_poct = None
    if cfg.get("hemoscreen_poct1a2_enabled"):
        hs_poct = HemoscreenPoct1A2Receiver(
            store,
            cfg["hemoscreen_poct1a2_host"],
            cfg["hemoscreen_poct1a2_port"],
            continuous_mode=cfg["hemoscreen_poct1a2_continuous_mode"],
            timeout=cfg["hemoscreen_poct1a2_timeout"],
            monitor=monitor,
        ).start()

    def _sig(_s, _f):
        control.request_stop()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    LOG.info("Middleware avviato. Ctrl-C per fermare.")

    try:
        while not control.stop_event.is_set():
            try:
                forwarder.forward_ready()
            except Exception:
                LOG.exception("Errore nel loop di inoltro; continuo.")
            try:
                # Rileva strumenti andati OFFLINE (nessun messaggio da oltre
                # device_offline_timeout_seconds): senza questa chiamata
                # periodica lo status resta ONLINE per sempre dopo il primo
                # messaggio, e la dashboard non segnalerebbe mai uno strumento
                # spento/scollegato.
                monitor.update_health_status()
            except Exception:
                LOG.exception("Errore nel controllo health strumenti; continuo.")
            slept = 0.0
            while slept < cfg["forward_interval_seconds"] and not control.stop_event.is_set():
                time.sleep(0.5)
                slept += 0.5
    finally:
        order_rx.stop()
        if adt_rx_server:
            adt_rx_server.stop()
        result_rx.stop()
        if status:
            status.stop()
        if hs_hl7:
            hs_hl7.stop()
        if hs_poct:
            hs_poct.stop()
        if uvicorn_server:
            uvicorn_server.should_exit = True
            if api_thread:
                api_thread.join(timeout=10.0)
        if vpn_manager:
            try:
                vpn_manager.down()
            except vpnmod.VpnError as e:
                LOG.warning("VPN: arresto tunnel non riuscito: %s", e)
        LOG.info("Middleware arrestato.")

    if control.restart_requested:
        try:
            cmd = _relaunch_command(config_path, args.loglevel)
            LOG.info("Riavvio richiesto dalla GUI: avvio nuova istanza (%s)", " ".join(cmd))
            subprocess.Popen(cmd, close_fds=True)
        except Exception:
            LOG.exception("Riavvio richiesto ma impossibile avviare la nuova istanza")
    return 0


if __name__ == "__main__":
    sys.exit(main())
