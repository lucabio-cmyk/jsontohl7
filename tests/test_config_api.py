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


def _post(url: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _post_json(url: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
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


def test_vpn_up_down_endpoints():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(str(Path(tmpdir) / "test.db"))
        config_path = str(Path(tmpdir) / "config.json")
        app = hl7mw_api.init_api(store, config_path, DEFAULTS)

        port = _free_port()
        server, thread = _start_server(app, "127.0.0.1", port)
        base = f"http://127.0.0.1:{port}"
        try:
            # 1) vpn_enabled=false (default): entrambi rifiutati con 400
            status, data = _post(f"{base}/api/vpn/up")
            assert status == 400 and "vpn_enabled" in data["detail"]
            print("[9] POST /api/vpn/up con VPN disabilitata -> 400  OK")

            # 2) vpn_enabled=true ma manage_lifecycle=false (default): 400 esplicito
            _put(f"{base}/api/config", {"vpn_enabled": True})
            status, data = _post(f"{base}/api/vpn/up")
            assert status == 400 and "vpn_manage_lifecycle" in data["detail"]
            print("[10] POST /api/vpn/up con manage_lifecycle=false -> 400  OK")

            # 3) manage_lifecycle=true, provider=external: nessun comando da eseguire -> successo (no-op)
            _put(f"{base}/api/config", {"vpn_manage_lifecycle": True, "vpn_provider": "external"})
            status, data = _post(f"{base}/api/vpn/up")
            assert status == 200 and data["status"] == "ok"
            status, data = _post(f"{base}/api/vpn/down")
            assert status == 200 and data["status"] == "ok"
            print("[11] POST /api/vpn/up e /api/vpn/down con provider=external -> 200 (no-op)  OK")

            audit = store.get_audit_log(limit=10)
            assert any(a["event_type"] == "vpn_up_triggered" for a in audit)
            assert any(a["event_type"] == "vpn_down_triggered" for a in audit)
            print("[12] audit_log traccia vpn_up_triggered/vpn_down_triggered  OK")
        finally:
            _stop_server(server, thread)


def test_vpn_down_raises_on_failure():
    """VpnManager.down() ora e' simmetrico a up(): solleva VpnError se il
    comando fallisce, non lo inghiotte piu' silenziosamente (prima di questo
    fix un chiamante on-demand come /api/vpn/down non poteva mai sapere se
    l'arresto del tunnel fosse davvero riuscito)."""
    from hl7mw import vpn as vpnmod

    mgr = vpnmod.VpnManager(provider="external", manage_lifecycle=True,
                            down_command="/bin/eseguibile-che-non-esiste-xyz")
    try:
        mgr.down()
        assert False, "down() doveva sollevare VpnError"
    except vpnmod.VpnError:
        pass
    print("[13] VpnManager.down(): solleva VpnError se il comando fallisce  OK")


def test_hemoscreen_endpoints():
    """Endpoint di gestione HemoScreen POCT1-A2 (/api/hemoscreen/...): 404 se
    nessun device e' connesso a questo processo, 400 su payload incompleto,
    accodamento verso la conversazione attiva quando il device e' connesso
    (simulato registrando una conversazione fittizia nel registro in-process,
    senza aprire una vera connessione TCP — il flusso di conversazione reale
    e' gia' coperto da tests/test_hemoscreen.py)."""
    from hl7mw.adapters import hemoscreen_poct1a2 as poct1a2

    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(str(Path(tmpdir) / "test.db"))
        app = hl7mw_api.init_api(store, str(Path(tmpdir) / "config.json"), DEFAULTS)

        port = _free_port()
        server, thread = _start_server(app, "127.0.0.1", port)
        base = f"http://127.0.0.1:{port}"
        try:
            status, data = _get(f"{base}/api/hemoscreen/devices")
            assert status == 200 and data["devices"] == []
            print("[14] GET /api/hemoscreen/devices senza device connessi -> lista vuota  OK")

            status, data = _post(f"{base}/api/hemoscreen/UNKNOWN/lock")
            assert status == 404
            print("[15] POST /api/hemoscreen/{id}/lock su device non connesso -> 404  OK")

            status, data = _post_json(f"{base}/api/hemoscreen/UNKNOWN/operator-list", {})
            assert status == 400 and "operators" in data["detail"]
            print("[16] POST /api/hemoscreen/{id}/operator-list senza 'operators' -> 400  OK")

            class _FakeConv:
                def __init__(self):
                    self.calls = []

                def enqueue_directive(self, builder, topic_cd_after_ack, label):
                    self.calls.append((builder("1"), topic_cd_after_ack, label))

            fake = _FakeConv()
            with poct1a2._REGISTRY_LOCK:
                poct1a2._REGISTRY["FAKE-001"] = fake
            try:
                status, data = _post(f"{base}/api/hemoscreen/FAKE-001/lock")
                assert status == 200 and data["status"] == "queued"
                assert fake.calls and "LOCK" in fake.calls[-1][0]

                status, data = _post_json(
                    f"{base}/api/hemoscreen/FAKE-001/operator-list",
                    {"operators": [{"operator_id": "OP1", "permission_level_cd": "1"}]},
                )
                assert status == 200
                assert fake.calls[-1][1] == "OP_LST", "OPL.R01 deve richiedere un EOT(OP_LST) dopo l'ACK"

                status, data = _post_json(f"{base}/api/hemoscreen/FAKE-001/qc-lot", {
                    "lot_number": "PIX201205", "expiration_date": "2020-12-05", "revision": "01",
                    "levels": {"N": [{"observation_id": "6690-2", "lo": "4", "hi": "11.5"}]},
                })
                assert status == 200
                assert "DTV.PIX.QC" in fake.calls[-1][0]
            finally:
                with poct1a2._REGISTRY_LOCK:
                    poct1a2._REGISTRY.pop("FAKE-001", None)

            audit = store.get_audit_log(limit=20)
            assert any(a["event_type"] == "poct1a2_lock_queued" for a in audit)
            print("[17] Endpoint direttive HemoScreen: accodamento verso conversazione attiva + audit_log  OK")
        finally:
            _stop_server(server, thread)


if __name__ == "__main__":
    if not FASTAPI_AVAILABLE:
        print("fastapi/uvicorn non installati: test saltato (pip install -e \".[api]\").")
        sys.exit(0)
    test_config_roundtrip_and_validation()
    test_vpn_check_endpoint()
    test_vpn_up_down_endpoints()
    test_vpn_down_raises_on_failure()
    test_hemoscreen_endpoints()
    print("\nTUTTI I TEST CONFIG API OK")
