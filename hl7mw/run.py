#!/usr/bin/env python3
"""
hl7mw.run — avvia il middleware completo:
  - OrderReceiver  (ordini dal LIS)
  - ResultReceiver (risultati dagli strumenti)
  - Forwarder      (loop periodico: ordini READY -> ORU -> LIS)
  - status web     (pagina/JSON di stato per la UI, opzionale)

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
from .store import Store
from .pipeline import OrderReceiver, ResultReceiver, Forwarder
from .webstatus import StatusServer
from .adapters.hemoscreen_hl7 import HemoscreenHl7ResultReceiver
from .adapters.hemoscreen_poct1a2 import HemoscreenPoct1A2Receiver

LOG = logging.getLogger("hl7mw")
_STOP = False

DEFAULTS = {
    "db_path": "hl7mw.db",
    "order_listen_host": "0.0.0.0", "order_listen_port": 6661,
    "result_listen_host": "0.0.0.0", "result_listen_port": 6662,
    "lis_host": "127.0.0.1", "lis_port": 2575,
    "forward_interval_seconds": 10.0,
    "status_host": "127.0.0.1", "status_port": 8080, "status_enabled": True,
    "sending_app": "HL7MW", "sending_facility": "MIDDLEWARE",
    "receiving_app": "LIS", "receiving_facility": "OSP",
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
    order_rx = OrderReceiver(store, cfg["order_listen_host"], cfg["order_listen_port"],
                             cfg["sending_app"], cfg["sending_facility"]).start()
    result_rx = ResultReceiver(store, cfg["result_listen_host"], cfg["result_listen_port"],
                               cfg["sending_app"], cfg["sending_facility"]).start()
    oru_cfg = hl7.OruConfig(cfg["sending_app"], cfg["sending_facility"],
                            cfg["receiving_app"], cfg["receiving_facility"])
    forwarder = Forwarder(store, cfg["lis_host"], cfg["lis_port"], oru_cfg)

    status = None
    if cfg.get("status_enabled"):
        status = StatusServer(store, cfg["status_host"], cfg["status_port"]).start()
        LOG.info("Status UI su http://%s:%s", cfg["status_host"], cfg["status_port"])

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
        LOG.info("Middleware arrestato.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
