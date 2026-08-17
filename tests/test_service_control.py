"""
Test della configurazione "tutto da GUI": apertura automatica del browser
all'avvio e riavvio del servizio richiesto dalla pagina Impostazioni (invece
di richiedere terminale/Task Manager per applicare le modifiche).

Copre:
  - hl7mw.run.ServiceControl: coordinamento stop/restart tra loop principale e API
  - hl7mw.run._relaunch_command: ricostruzione del comando di rilancio
  - hl7mw.run._maybe_open_browser: rispetta il flag, apre solo se l'API risponde
  - POST /api/restart: 501 senza ServiceControl, 200 + audit_log con ServiceControl
  - Integrazione end-to-end: processo reale avviato, riavvio richiesto via API,
    verifica che una nuova istanza prenda il posto della vecchia sugli stessi
    endpoint (non solo che il processo termini)

Eseguibile senza pytest: `python3 tests/test_service_control.py`
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hl7mw import run as hl7mw_run

try:
    import uvicorn  # noqa: F401
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_service_control_stop_and_restart():
    control = hl7mw_run.ServiceControl()
    assert not control.stop_event.is_set()
    assert control.restart_requested is False

    control.request_stop()
    assert control.stop_event.is_set()
    assert control.restart_requested is False, "request_stop non deve implicare un riavvio"

    control2 = hl7mw_run.ServiceControl()
    control2.request_restart()
    assert control2.stop_event.is_set(), "request_restart deve fermare il loop principale"
    assert control2.restart_requested is True

    print("[1] ServiceControl: request_stop/request_restart  OK")


def test_relaunch_command():
    with mock.patch.object(sys, "frozen", True, create=True):
        cmd = hl7mw_run._relaunch_command("config.json", "DEBUG")
        assert cmd == [sys.executable, "-c", "config.json", "--loglevel", "DEBUG"], cmd

    assert not hasattr(sys, "frozen") or sys.frozen is not True
    cmd = hl7mw_run._relaunch_command("/tmp/config.json", None)
    assert cmd == [sys.executable, "-m", "hl7mw.run", "-c", "/tmp/config.json"], cmd

    print("[2] _relaunch_command: eseguibile PyInstaller vs 'python -m hl7mw.run'  OK")


def test_maybe_open_browser_respects_flag():
    with mock.patch("webbrowser.open") as opened:
        hl7mw_run._maybe_open_browser({"open_browser_on_start": False, "api_enabled": True})
        time.sleep(0.2)
        opened.assert_not_called()
    print("[3] _maybe_open_browser: open_browser_on_start=false -> non tenta nulla  OK")


def test_maybe_open_browser_waits_for_health_then_opens():
    if not FASTAPI_AVAILABLE:
        print("[4] fastapi/uvicorn non installati: test apertura browser saltato  OK")
        return

    import threading
    from hl7mw import api as hl7mw_api
    from hl7mw.store import Store

    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(str(Path(tmpdir) / "test.db"))
        app = hl7mw_api.init_api(store, str(Path(tmpdir) / "config.json"), hl7mw_run.DEFAULTS)
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
            with mock.patch("webbrowser.open") as opened:
                hl7mw_run._maybe_open_browser({"open_browser_on_start": True, "api_enabled": True,
                                              "api_port": port})
                deadline = time.monotonic() + 5.0
                while not opened.called and time.monotonic() < deadline:
                    time.sleep(0.05)
                opened.assert_called_once_with(f"http://127.0.0.1:{port}/")
            print("[4] _maybe_open_browser: apre il browser non appena /health risponde  OK")
        finally:
            server.should_exit = True
            thread.join(timeout=5.0)


def test_restart_endpoint():
    if not FASTAPI_AVAILABLE:
        print("[5] fastapi/uvicorn non installati: test /api/restart saltato  OK")
        return

    import threading
    from hl7mw import api as hl7mw_api
    from hl7mw.store import Store

    def _post(url):
        req = urllib.request.Request(url, data=b"", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(str(Path(tmpdir) / "test.db"))
        config_path = str(Path(tmpdir) / "config.json")
        app = hl7mw_api.init_api(store, config_path, hl7mw_run.DEFAULTS)  # control=None
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
            status, data = _post(f"http://127.0.0.1:{port}/api/restart")
            assert status == 501, f"senza ServiceControl atteso 501, ottenuto {status}"
            print("[5] POST /api/restart senza ServiceControl -> 501 esplicito  OK")

            control = hl7mw_run.ServiceControl()
            hl7mw_api.init_api(store, config_path, hl7mw_run.DEFAULTS, control)
            status, data = _post(f"http://127.0.0.1:{port}/api/restart")
            assert status == 200 and data["status"] == "restarting"
            assert control.restart_requested is True
            assert control.stop_event.is_set()

            audit = store.get_audit_log(limit=10)
            assert any(a["event_type"] == "service_restart_requested" for a in audit)
            print("[6] POST /api/restart con ServiceControl -> 200, restart_requested, audit_log  OK")
        finally:
            hl7mw_api.init_api(store, config_path, hl7mw_run.DEFAULTS)  # reset globale per gli altri test
            server.should_exit = True
            thread.join(timeout=5.0)


# ---------------------------------------------------------------------------
# Integrazione end-to-end: processo reale, riavvio via API, nuova istanza
# ---------------------------------------------------------------------------

def _wait_health(port: float, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.0) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.2)
    return False


def test_real_process_restart_end_to_end():
    """Avvia hl7mw.run.main() come processo reale (non un thread nello stesso
    interprete): questa e' l'unica cosa che verifica davvero il rilancio
    (subprocess.Popen del comando ricostruito) e non solo che il loop
    principale smetta di girare."""
    if not FASTAPI_AVAILABLE:
        print("[7] fastapi/uvicorn non installati: test end-to-end saltato  OK")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        api_port = _free_port()
        cfg = {
            "db_path": str(Path(tmpdir) / "svc.db"),
            "order_listen_host": "127.0.0.1", "order_listen_port": _free_port(),
            "result_listen_host": "127.0.0.1", "result_listen_port": _free_port(),
            "lis_host": "127.0.0.1", "lis_port": _free_port(),
            "forward_interval_seconds": 1.0,
            "status_enabled": False,
            "api_enabled": True, "api_host": "127.0.0.1", "api_port": api_port,
            "open_browser_on_start": False,
            "vpn_enabled": False,
            "log_file": str(Path(tmpdir) / "svc.log"),
            "log_console": False,
        }
        config_path = Path(tmpdir) / "config.json"
        config_path.write_text(json.dumps(cfg), encoding="utf-8")

        proc = subprocess.Popen(
            [sys.executable, "-m", "hl7mw.run", "-c", str(config_path)],
            cwd=str(Path(__file__).resolve().parent.parent),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            assert _wait_health(api_port, 15.0), "il processo originale non ha risposto a /health in tempo"
            first_pid = proc.pid

            req = urllib.request.Request(f"http://127.0.0.1:{api_port}/api/restart", data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                assert r.status == 200

            # Il processo originale deve terminare da solo (shutdown pulito),
            # senza bisogno di ucciderlo noi.
            proc.wait(timeout=15.0)
            assert proc.returncode == 0, f"il processo originale doveva uscire con 0, ottenuto {proc.returncode}"

            # Una nuova istanza deve aver preso il posto della vecchia sulla
            # stessa porta (avviata da subprocess.Popen dentro run.main(),
            # non da questo test): il file di log conterra' le righe di
            # entrambi gli avvii se e solo se il rilancio e' avvenuto davvero.
            assert _wait_health(api_port, 15.0), "nessuna nuova istanza ha risposto a /health dopo il riavvio"

            log_content = Path(cfg["log_file"]).read_text(encoding="utf-8")
            assert log_content.count("Middleware avviato.") == 2, \
                f"attesi 2 avvii nel log (originale + rilancio), trovati: {log_content.count('Middleware avviato.')}"
            assert "Riavvio richiesto dalla GUI" in log_content

            print("[7] Riavvio end-to-end: processo reale sostituito da una nuova istanza sulla stessa porta  OK")
        finally:
            # Trova ed elimina qualunque processo hl7mw.run rimasto sulla porta
            # (il rilancio ha un PID diverso da 'proc', che a questo punto e'
            # gia' terminato): tentativo via pkill sul pattern del comando,
            # tollerante se non trova nulla.
            subprocess.run(["pkill", "-f", f"hl7mw.run -c {config_path}"], check=False)
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5.0)


if __name__ == "__main__":
    test_service_control_stop_and_restart()
    test_relaunch_command()
    test_maybe_open_browser_respects_flag()
    test_maybe_open_browser_waits_for_health_then_opens()
    test_restart_endpoint()
    test_real_process_restart_end_to_end()
    print("\nTUTTI I TEST SERVICE CONTROL OK")
