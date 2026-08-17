"""
Test della pagina Impostazioni (GET/PUT /api/config, GET /api/vpn/check).

Richiede fastapi/uvicorn (extra "api"): se non installati, il test si salta
con successo — coerente con l'API REST opzionale del resto del progetto
(vedi hl7mw/run.py: FASTAPI_AVAILABLE).

Eseguibile senza pytest: `python3 tests/test_config_api.py`
"""
import json
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import uvicorn  # noqa: F401
    from hl7mw import api as hl7mw_api
    from hl7mw.run import DEFAULTS
    from hl7mw.store import Store
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_server(app, host, port):
    config = uvicorn.Config(app, host=host, port=port, log_level="warning",
                            loop="asyncio", http="h11", ws="none", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "server di test non avviato in tempo"
    return server, thread


def _stop_server(server, thread):
    server.should_exit = True
    thread.join(timeout=5.0)


def _get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _put(url: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="PUT", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_config_roundtrip_and_validation():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(str(Path(tmpdir) / "test.db"))
        config_path = str(Path(tmpdir) / "config.json")
        app = hl7mw_api.init_api(store, config_path, DEFAULTS)

        port = _free_port()
        server, thread = _start_server(app, "127.0.0.1", port)
        base = f"http://127.0.0.1:{port}"
        try:
            # 1) senza file su disco: GET ritorna i default
            status, data = _get(f"{base}/api/config")
            assert status == 200
            assert data["file_exists"] is False
            assert data["config"]["lis_port"] == DEFAULTS["lis_port"]
            print("[1] GET /api/config senza file su disco -> default  OK")

            # 2) chiave sconosciuta rifiutata
            status, data = _put(f"{base}/api/config", {"chiave_inventata": 1})
            assert status == 400, f"atteso 400, ottenuto {status}"
            print("[2] PUT /api/config con chiave sconosciuta -> 400  OK")

            # 3) tipo sbagliato rifiutato
            status, data = _put(f"{base}/api/config", {"lis_port": "non-un-numero"})
            assert status == 400, f"atteso 400, ottenuto {status}"
            print("[3] PUT /api/config con tipo non valido -> 400  OK")

            # 4) aggiornamento valido: persiste su file, preserva le altre chiavi
            status, data = _put(f"{base}/api/config", {
                "lis_host": "10.9.0.99", "lis_port": 2599, "vpn_enabled": True,
            })
            assert status == 200 and data["restart_required"] is True
            assert Path(config_path).exists()
            on_disk = json.loads(Path(config_path).read_text())
            assert on_disk["lis_host"] == "10.9.0.99"
            assert on_disk["lis_port"] == 2599
            assert on_disk["vpn_enabled"] is True
            assert on_disk["sending_app"] == DEFAULTS["sending_app"], "chiavi non toccate devono restare quelle di default"
            print("[4] PUT /api/config valido -> persistito su file, altre chiavi intatte  OK")

            # 5) GET successivo riflette il file appena scritto
            status, data = _get(f"{base}/api/config")
            assert data["file_exists"] is True
            assert data["config"]["lis_host"] == "10.9.0.99"
            print("[5] GET /api/config dopo il salvataggio -> riflette il file  OK")

            # 6) audit log registrato
            audit = store.get_audit_log(limit=10)
            assert any(a["event_type"] == "config_updated" for a in audit)
            print("[6] audit_log traccia l'evento config_updated  OK")
        finally:
            _stop_server(server, thread)


def test_vpn_check_endpoint():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(str(Path(tmpdir) / "test.db"))
        app = hl7mw_api.init_api(store, str(Path(tmpdir) / "config.json"), DEFAULTS)

        port = _free_port()
        server, thread = _start_server(app, "127.0.0.1", port)
        base = f"http://127.0.0.1:{port}"
        try:
            # host irraggiungibile
            closed = _free_port()
            status, data = _get(f"{base}/api/vpn/check?host=127.0.0.1&port={closed}")
            assert status == 200 and data["reachable"] is False
            print("[7] GET /api/vpn/check su porta chiusa -> reachable=False  OK")

            # host raggiungibile: il server di test stesso
            status, data = _get(f"{base}/api/vpn/check?host=127.0.0.1&port={port}")
            assert status == 200 and data["reachable"] is True
            print("[8] GET /api/vpn/check su porta aperta -> reachable=True  OK")
        finally:
            _stop_server(server, thread)


if __name__ == "__main__":
    if not FASTAPI_AVAILABLE:
        print("fastapi/uvicorn non installati: test saltato (pip install -e \".[api]\").")
        sys.exit(0)
    test_config_roundtrip_and_validation()
    test_vpn_check_endpoint()
    print("\nTUTTI I TEST CONFIG API OK")
