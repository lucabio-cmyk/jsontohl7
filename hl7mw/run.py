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
_STOP = False

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
    if not path and Path("config.json").exists():
        # Auto-discovery: se non passato -c, ma un config.json esiste nella
        # cwd (es. salvato dalla GUI Impostazioni al giro precedente), usalo.
        path = "config.json"
    if path:
        cfg.update(json.loads(Path(path).read_text(encoding="utf-8")))
    return cfg


def _sig(_s, _f):
    global _STOP
    _STOP = True


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

    store = Store(cfg["db_path"])
    monitor = DeviceMonitor(store, cfg.get("device_offline_timeout_seconds", 300.0))

    vpn_manager = None
    if cfg.get("vpn_enabled"):
        resolve_vpn_health_check(cfg)
        vpn_manager = vpnmod.from_config(cfg)
        vpn_manager.ensure_up()  # non bloccante: logga ed eventualmente ritenta nel loop

    # Opzioni di riscontro comuni ai canali in ingresso (vedi hl7mw/ack.py).
    inbound_opts = dict(
        ack_mode=cfg.get("hl7_ack_mode", "auto"),
        include_err=cfg.get("hl7_ack_include_err", True),
        dedup=cfg.get("hl7_dedup_enabled", True),
        read_timeout=cfg.get("mllp_read_timeout", 60.0),
        idle_timeout=cfg.get("mllp_idle_timeout", 300.0),
    )

    order_rx = OrderReceiver(store, cfg["order_listen_host"], cfg["order_listen_port"],
                             cfg["sending_app"], cfg["sending_facility"], monitor,
                             order_response_mode=cfg.get("order_response_mode", "ack"),
                             **inbound_opts).start()

    adt_rx_server = None
    if cfg.get("adt_listen_port"):
        adt_rx_server = mllp.MllpServer(
            cfg.get("adt_listen_host") or cfg["order_listen_host"],
            cfg["adt_listen_port"],
            order_rx._handle,
            read_timeout=cfg.get("mllp_read_timeout", 60.0),
            idle_timeout=cfg.get("mllp_idle_timeout", 300.0),
        ).start()
        LOG.info("Canale ADT dedicato in ascolto su %s:%s (es. LIS con connessioni ADT/ORM separate)",
                cfg.get("adt_listen_host") or cfg["order_listen_host"], cfg["adt_listen_port"])

    result_rx = ResultReceiver(store, cfg["result_listen_host"], cfg["result_listen_port"],
                               cfg["sending_app"], cfg["sending_facility"], monitor,
                               **inbound_opts).start()
    oru_cfg = hl7.OruConfig(cfg["sending_app"], cfg["sending_facility"],
                            cfg["receiving_app"], cfg["receiving_facility"])
    forwarder = Forwarder(store, cfg["lis_host"], cfg["lis_port"], oru_cfg,
                          read_timeout=cfg.get("mllp_read_timeout", 30.0),
                          ack_retry_attempts=cfg.get("ack_retry_attempts", 2),
                          ack_retry_backoff_seconds=cfg.get("ack_retry_backoff_seconds", 0.5),
                          ack_mode=cfg.get("lis_ack_mode", "original"),
                          application_ack_timeout=cfg.get("lis_application_ack_timeout") or None)

    status = None
    if cfg.get("status_enabled"):
        status = StatusServer(store, cfg["status_host"], cfg["status_port"]).start()
        LOG.info("Status UI su http://%s:%s", cfg["status_host"], cfg["status_port"])

    api_thread = None
    if cfg.get("api_enabled") and FASTAPI_AVAILABLE:
        import threading
        app = init_api(store, config_path, DEFAULTS)

        def run_api():
            uvicorn.run(
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

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    LOG.info("Middleware avviato. Ctrl-C per fermare.")
    LOG.info("Riscontro HL7: ingresso=%s, risposta ordini=%s, verso LIS=%s, deduplica=%s.",
             cfg.get("hl7_ack_mode", "auto"), cfg.get("order_response_mode", "ack"),
             cfg.get("lis_ack_mode", "original"),
             "attiva" if cfg.get("hl7_dedup_enabled", True) else "disattiva")
    last_purge = time.monotonic()

    try:
        while not _STOP:
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
            try:
                # Sfoltisce la tabella di deduplica: oltre la finestra di
                # ritrasmissione plausibile i control id non servono piu' e la
                # tabella crescerebbe senza limite.
                now = time.monotonic()
                if cfg.get("hl7_dedup_enabled", True) and now - last_purge >= 3600:
                    last_purge = now
                    removed = store.purge_processed(cfg.get("hl7_dedup_retention_hours", 72.0))
                    if removed:
                        LOG.info("Deduplica: rimossi %d control id oltre le %.0f ore.",
                                 removed, cfg.get("hl7_dedup_retention_hours", 72.0))
            except Exception:
                LOG.exception("Errore nella pulizia della tabella di deduplica; continuo.")
            slept = 0.0
            while slept < cfg["forward_interval_seconds"] and not _STOP:
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
        if vpn_manager:
            try:
                vpn_manager.down()
            except vpnmod.VpnError as e:
                LOG.warning("VPN: arresto tunnel non riuscito: %s", e)
        LOG.info("Middleware arrestato.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
