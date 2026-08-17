"""
Test del sistema di logging applicativo.

Copre:
  - hl7mw.logging_setup.configure_logging: handler file+console, livello, rotazione
  - MllpServer: un'eccezione nel gestore del messaggio finisce nel log (con
    stack trace) invece di essere ingoiata silenziosamente (bug corretto)
  - DeviceMonitor.update_health_status: transizione ONLINE->OFFLINE loggata
    (oltre che su audit_log) — prima non era mai richiamata dal loop principale
  - GET /api/logs: tail del file di log dalla dashboard

Eseguibile senza pytest: `python3 tests/test_logging.py`
"""
from __future__ import annotations

import logging
import logging.handlers
import socket
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hl7mw import mllp
from hl7mw.logging_setup import configure_logging
from hl7mw.monitor import DeviceMonitor
from hl7mw.store import Store


class _CaptureHandler(logging.Handler):
    """Handler minimale per catturare i LogRecord emessi durante un test,
    senza dipendere da caplog (progetto senza pytest)."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _capture(logger_name: str = "hl7mw"):
    handler = _CaptureHandler()
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return handler, logger


def test_configure_logging_writes_rotating_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        log_file = str(Path(tmpdir) / "hl7mw.log")
        configure_logging(level="DEBUG", log_file=log_file, max_bytes=1_000_000,
                          backup_count=3, console=False)

        logging.getLogger("hl7mw").info("riga di prova")
        logging.getLogger("hl7mw.sotto.modulo").warning("riga da logger figlio")

        content = Path(log_file).read_text(encoding="utf-8")
        assert "riga di prova" in content
        assert "riga da logger figlio" in content, "i logger figli devono propagare al root configurato"

        root = logging.getLogger()
        file_handlers = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(file_handlers) == 1
        assert file_handlers[0].maxBytes == 1_000_000
        assert file_handlers[0].backupCount == 3

        # richiamabile più volte senza accumulare handler duplicati (log ripetuti)
        configure_logging(level="INFO", log_file=log_file, console=False)
        root = logging.getLogger()
        assert len(root.handlers) == 1, "una seconda chiamata non deve accumulare handler"

        print("[1] configure_logging: file rotante scritto, propagazione logger figli, no handler duplicati  OK")


def test_mllp_server_logs_handler_exception():
    """Bug corretto: un'eccezione nel gestore del messaggio finiva solo in un
    ACK generico al mittente, senza nessuna traccia in log. Ora LOG.exception
    (stack trace incluso) prima di rispondere."""
    handler, logger = _capture()
    try:
        def _boom(message: str) -> str:
            raise RuntimeError("errore simulato nel gestore")

        srv = mllp.MllpServer("127.0.0.1", 0, _boom).start()
        port = srv._srv.server_address[1]
        try:
            raw = mllp.exchange("127.0.0.1", port, "MSH|^~\\&|X|Y|Z|W|20260101000000||ORU^R01|1|P|2.5\r")
            code, ctrl, text = mllp.parse_ack_code(raw)
            assert code == "AE", f"atteso AE, ottenuto {code}"
            assert "errore simulato" not in text, \
                "il dettaglio dell'eccezione non deve essere esposto al mittente (solo nel log)"
        finally:
            srv.stop()

        time.sleep(0.1)
        exc_records = [r for r in handler.records if r.exc_info]
        assert exc_records, "l'eccezione nel gestore deve produrre un LogRecord con stack trace (LOG.exception)"
        assert any("errore nel gestore del messaggio" in r.getMessage() for r in exc_records)
        print("[2] MllpServer: eccezione nel gestore -> loggata con stack trace, non esposta nell'ACK  OK")
    finally:
        logger.removeHandler(handler)


def test_device_monitor_offline_logged():
    """update_health_status(): la transizione ONLINE->OFFLINE deve comparire
    sia su audit_log (DB) sia sul log tecnico applicativo."""
    import datetime as _dt

    handler, logger = _capture()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(str(Path(tmpdir) / "test.db"))
            monitor = DeviceMonitor(store, offline_timeout_seconds=1.0)

            monitor.record_message("HEMO-LOG-TEST", "127.0.0.1", 6664)
            assert store.get_instrument("HEMO-LOG-TEST")["status"] == "ONLINE"

            # Retrodata l'heartbeat invece di un time.sleep(): last_heartbeat ha
            # precisione al secondo (vedi store._now), un'attesa reale sotto il
            # secondo sarebbe una fonte di flakiness inutile.
            past = (_dt.datetime.now() - _dt.timedelta(seconds=10)).isoformat(timespec="seconds")
            with store._conn() as c:
                c.execute("UPDATE instruments SET last_heartbeat=? WHERE name=?",
                         (past, "HEMO-LOG-TEST"))

            changes = monitor.update_health_status()
            assert changes.get("HEMO-LOG-TEST") == ("ONLINE", "OFFLINE")
            assert store.get_instrument("HEMO-LOG-TEST")["status"] == "OFFLINE"

            audit = store.get_audit_log(limit=20)
            assert any(a["event_type"] == "instrument_status_change" and a["instrument"] == "HEMO-LOG-TEST"
                      for a in audit)

            warn_records = [r for r in handler.records if r.levelno == logging.WARNING]
            assert any("HEMO-LOG-TEST" in r.getMessage() and "OFFLINE" in r.getMessage()
                      for r in warn_records), \
                "il passaggio a OFFLINE deve comparire nel log tecnico (non solo su audit_log)"

            print("[3] DeviceMonitor.update_health_status: OFFLINE tracciato su audit_log e log tecnico  OK")
    finally:
        logger.removeHandler(handler)


def test_api_logs_endpoint():
    try:
        import uvicorn  # noqa: F401
        from hl7mw import api as hl7mw_api
        from hl7mw.run import DEFAULTS
    except ImportError:
        print("[4] fastapi/uvicorn non installati: test /api/logs saltato  OK")
        return

    import json
    import threading
    import urllib.error
    import urllib.request

    def _free_port() -> int:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def _get(url: str):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(str(Path(tmpdir) / "test.db"))
        config_path = str(Path(tmpdir) / "config.json")
        log_path = Path(tmpdir) / "hl7mw.log"
        log_path.write_text("\n".join(f"riga {i}" for i in range(1, 11)) + "\n", encoding="utf-8")

        Path(config_path).write_text(json.dumps({"log_file": str(log_path)}), encoding="utf-8")
        app = hl7mw_api.init_api(store, config_path, DEFAULTS)

        port = _free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                loop="asyncio", http="h11", ws="none", lifespan="on")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5.0
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started

        try:
            status, body = _get(f"http://127.0.0.1:{port}/api/logs?lines=3")
            assert status == 200, f"atteso 200, ottenuto {status}: {body}"
            assert body.strip().splitlines() == ["riga 8", "riga 9", "riga 10"], \
                f"attese le ultime 3 righe, ottenuto: {body!r}"

            # config senza log_file -> 404 esplicito
            Path(config_path).write_text(json.dumps({"log_file": ""}), encoding="utf-8")
            status, body = _get(f"http://127.0.0.1:{port}/api/logs")
            assert status == 404 and "log_file" in body

            print("[5] GET /api/logs: tail del file di log configurato, 404 se non configurato  OK")
        finally:
            server.should_exit = True
            thread.join(timeout=5.0)


def test_cli_logs_command():
    """cmd_logs: legge le ultime N righe, auto-scopre log_file da config.json
    nella cwd se --log-file non è passato (stesso pattern di run.load_config),
    e non richiede il database (deve restare utilizzabile anche se il servizio
    non e' mai partito correttamente)."""
    import argparse
    import io
    import json
    import os
    import contextlib

    from hl7mw.cli import cmd_logs

    with tempfile.TemporaryDirectory() as tmpdir:
        cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            log_path = Path(tmpdir) / "custom.log"
            log_path.write_text("\n".join(f"riga {i}" for i in range(1, 6)) + "\n", encoding="utf-8")
            Path("config.json").write_text(json.dumps({"log_file": "custom.log"}), encoding="utf-8")

            args = argparse.Namespace(log_file=None, lines=2)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cmd_logs(args)
            assert rc == 0
            assert buf.getvalue().strip().splitlines() == ["riga 4", "riga 5"]

            # file esplicito e inesistente -> errore gestito, non un'eccezione
            args2 = argparse.Namespace(log_file="non-esiste.log", lines=10)
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                rc2 = cmd_logs(args2)
            assert rc2 == 1
            assert "non trovato" in buf2.getvalue()

            print("[6] hl7mw.cli logs: tail file, auto-discovery da config.json, gestione file mancante  OK")
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    test_configure_logging_writes_rotating_file()
    test_mllp_server_logs_handler_exception()
    test_device_monitor_offline_logged()
    test_api_logs_endpoint()
    test_cli_logs_command()
    print("\nTUTTI I TEST LOGGING OK")
