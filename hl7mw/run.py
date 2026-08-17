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
import sys
import time
from pathlib import Path

from . import hl7
from . import vpn as vpnmod
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
_STOP = False

DEFAULTS = {
    "db_path": "hl7mw.db",
    "order_listen_host": "0.0.0.0", "order_listen_port": 6661,
    "result_listen_host": "0.0.0.0", "result_listen_port": 6662,
    "lis_host": "127.0.0.1", "lis_port": 2575,
    "forward_interval_seconds": 10.0,
    "ack_retry_attempts": 2,
    "ack_retry_backoff_seconds": 0.5,
    "status_host": "127.0.0.1", "status_port": 8080, "status_enabled": True,
    "api_enabled": True, "api_host": "0.0.0.0", "api_port": 8000,
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
    if path:
        cfg.update(json.loads(Path(path).read_text(encoding="utf-8")))
    return cfg


def _sig(_s, _f):
    global _STOP
    _STOP = True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Middleware HL7v2 order-driven.")
    ap.add_argument("-c", "--config")
    ap.add_argument("--loglevel", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.loglevel.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)

    store = Store(cfg["db_path"])
    monitor = DeviceMonitor(store, cfg.get("device_offline_timeout_seconds", 300.0))

    vpn_manager = None
    if cfg.get("vpn_enabled"):
        # Se non specificato esplicitamente, l'health-check punta al LIS
        # (lis_host/lis_port): e' l'endpoint che deve essere raggiungibile
        # attraverso il tunnel per poter inoltrare gli ORU.
        if not cfg.get("vpn_health_check_host"):
            cfg["vpn_health_check_host"] = cfg.get("lis_host")
            cfg["vpn_health_check_port"] = cfg.get("lis_port")
        vpn_manager = vpnmod.from_config(cfg)
        vpn_manager.ensure_up()  # non bloccante: logga ed eventualmente ritenta nel loop

    order_rx = OrderReceiver(store, cfg["order_listen_host"], cfg["order_listen_port"],
                             cfg["sending_app"], cfg["sending_facility"], monitor).start()
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
    if cfg.get("api_enabled") and FASTAPI_AVAILABLE:
        import threading
        app = init_api(store)

        def run_api():
            uvicorn.run(
                app,
                host=cfg.get("api_host", "0.0.0.0"),
                port=cfg.get("api_port", 8000),
                log_level="info",
                access_log=False,
                # Espliciti (non "auto"): "auto" risolve l'implementazione via
                # importlib a runtime, invisibile all'analisi statica di PyInstaller
                # nell'eseguibile Windows (vedi packaging/win/). h11/asyncio sono
                # puro Python, portabili senza compilazione; niente websocket:
                # la dashboard usa solo HTTP/JSON.
                loop="asyncio", http="h11", ws="none", lifespan="on",
            )

        api_thread = threading.Thread(target=run_api, daemon=True)
        api_thread.start()
        LOG.info("FastAPI Dashboard su http://%s:%s", cfg.get("api_host", "0.0.0.0"), cfg.get("api_port", 8000))
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
        ).start()

    hs_poct = None
    if cfg.get("hemoscreen_poct1a2_enabled"):
        hs_poct = HemoscreenPoct1A2Receiver(
            store,
            cfg["hemoscreen_poct1a2_host"],
            cfg["hemoscreen_poct1a2_port"],
            continuous_mode=cfg["hemoscreen_poct1a2_continuous_mode"],
            timeout=cfg["hemoscreen_poct1a2_timeout"],
        ).start()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    LOG.info("Middleware avviato. Ctrl-C per fermare.")

    try:
        while not _STOP:
            try:
                forwarder.forward_ready()
            except Exception:
                LOG.exception("Errore nel loop di inoltro; continuo.")
            slept = 0.0
            while slept < cfg["forward_interval_seconds"] and not _STOP:
                time.sleep(0.5)
                slept += 0.5
    finally:
        order_rx.stop()
        result_rx.stop()
        if status:
            status.stop()
        if hs_hl7:
            hs_hl7.stop()
        if hs_poct:
            hs_poct.stop()
        if vpn_manager:
            vpn_manager.down()
        LOG.info("Middleware arrestato.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
