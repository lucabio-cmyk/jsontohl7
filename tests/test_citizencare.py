"""
Test dell'adapter Citizen Care Connect (CCHS) e del modulo VPN.

Simula:
  - un ordine RECEIVED nel middleware
  - CitizenCareForwarder che invia ADT^A04 + ORM^O01 a un finto server CCHS
  - CCHS che risponde con ORU^R01 (via CitizenCareResultReceiver)
  - l'ordine READY che rientra nel Forwarder standard verso un finto LIS

Eseguibile senza pytest: `python3 tests/test_citizencare.py`
"""
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hl7mw import hl7, mllp, vpn
from hl7mw.store import Store
from hl7mw.monitor import DeviceMonitor
from hl7mw.pipeline import Forwarder
from hl7mw.adapters.citizencare import (
    CitizenCareConfig,
    CitizenCareForwarder,
    CitizenCareResultReceiver,
    build_adt_a04,
    build_orm_o01,
)

CR = "\r"

ORDER = {
    "message_control_id": "MSG001",
    "message_type": "ORM^O01",
    "patient": {"id": "PAT-100", "id_authority": "", "last_name": "Rossi",
                "first_name": "Mario", "birth_date": "19800512", "sex": "M"},
    "placer_order_number": "PLAC-CCHS-1",
    "filler_order_number": "FILL-CCHS-1",
    "specimen_id": "BC-CCHS-001",
    "sample_key": "BC-CCHS-001",
    "universal_service_id": {"code": "58410-2", "text": "Emocromo completo", "system": "LN"},
    "ordering_provider": "", "requested_datetime": "",
    "raw": "",
}


def _fake_ack_server(received: list, expected_type_prefix: str):
    """Finto server CCHS: registra il messaggio e risponde ACK AA."""
    def handler(message):
        received.append(message)
        return hl7.build_ack(message, "AA", "", "CCHS", "CITIZENCARE")
    return mllp.MllpServer("127.0.0.1", 0, handler)


def test_build_messages():
    cfg = CitizenCareConfig()
    adt, adt_cid = build_adt_a04(ORDER, cfg)
    assert adt.startswith("MSH|"), "ADT malformato"
    assert "ADT^A04" in adt.split(CR)[0] or "ADT" in adt, "message type ADT^A04 mancante"
    assert "Rossi" in adt and "Mario" in adt, "dati paziente mancanti nell'ADT"
    assert adt_cid

    orm, orm_cid = build_orm_o01(ORDER, cfg)
    assert "ORM" in orm.split(CR)[0], "message type ORM^O01 mancante"
    assert "PLAC-CCHS-1" in orm and "FILL-CCHS-1" in orm, "placer/filler mancanti nell'ORM"
    assert orm_cid


def test_forward_and_receive_roundtrip():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(str(Path(tmpdir) / "test.db"))
        monitor = DeviceMonitor(store, offline_timeout_seconds=300.0)

        store.upsert_order(ORDER)
        assert store.get_order("BC-CCHS-001")["status"] == "RECEIVED"

        # --- finto server CCHS: ack ADT + ORM ---
        received = []
        cchs = _fake_ack_server(received, "").start()
        cchs_port = cchs._srv.server_address[1]

        try:
            fwd = CitizenCareForwarder(store, "127.0.0.1", cchs_port,
                                       CitizenCareConfig(), ack_retry_attempts=1,
                                       ack_retry_backoff_seconds=0.05)
            counts = fwd.forward_new_orders()
            assert counts["sent"] == 1, f"inoltro a CCHS fallito: {counts}"
            assert len(received) == 2, "attesi 2 messaggi (ADT poi ORM)"
            assert "ADT" in received[0].split(CR)[0]
            assert "ORM" in received[1].split(CR)[0]
            order = store.get_order("BC-CCHS-001")
            assert order["status"] == "SENT_TO_CCHS", f"status inatteso: {order['status']}"
        finally:
            cchs.stop()

        print("[1] CitizenCareForwarder: ADT^A04 + ORM^O01 inviati, status=SENT_TO_CCHS  OK")

        # --- CCHS ci restituisce l'ORU^R01 (stesso placer/filler) ---
        cc_receiver = CitizenCareResultReceiver(store, "127.0.0.1", 0,
                                                monitor=monitor).start()
        cc_port = cc_receiver._server._srv.server_address[1]
        try:
            oru = CR.join([
                r"MSH|^~\&|CCHS|CITIZENCARE|HL7MW|MIDDLEWARE|20260817120000||ORU^R01|R900|P|2.5",
                "PID|1||PAT-100||Rossi^Mario||19800512|M",
                "OBR|1|PLAC-CCHS-1|FILL-CCHS-1|58410-2^Emocromo completo^LN",
                "OBX|1|NM|6690-2^Leucociti^LN||7.2|10*3/uL|4.0-10.0|N|||F",
                "OBX|2|NM|718-7^Emoglobina^LN||14.8|g/dL|13.0-17.0|N|||F",
            ]) + CR
            code = mllp.send_message("127.0.0.1", cc_port, oru)
            assert code == "AA", "ORU da CCHS non ACKato"

            order = store.get_order("BC-CCHS-001")
            assert order["status"] == "READY", f"atteso READY, ottenuto {order['status']}"
            res = store.results_for("BC-CCHS-001")
            assert len(res) == 1 and len(res[0]["results"]) == 2

            instr = store.get_instrument("CITIZENCARE")
            assert instr is not None, "strumento CITIZENCARE non auto-registrato"
            assert instr["messages_received"] == 1
            assert instr["status"] == "ONLINE"

            with store._conn() as c:
                src = c.execute(
                    "SELECT source_instrument FROM results WHERE sample_key=?", ("BC-CCHS-001",)
                ).fetchone()["source_instrument"]
            assert src == "CITIZENCARE", "source_instrument non salvato sul risultato"
        finally:
            cc_receiver.stop()

        print("[2] CitizenCareResultReceiver: ORU^R01 associato, status=READY, "
              "strumento CITIZENCARE registrato  OK")

        # --- il Forwarder standard inoltra l'ORU completo al vero LIS ---
        lis_received = []
        lis = _fake_ack_server(lis_received, "").start()
        lis_port = lis._srv.server_address[1]
        try:
            std_fwd = Forwarder(store, "127.0.0.1", lis_port)
            counts = std_fwd.forward_ready()
            assert counts["sent"] == 1, f"inoltro al LIS fallito: {counts}"
            assert store.get_order("BC-CCHS-001")["status"] == "SENT"
            assert len(lis_received) == 1
            parsed = hl7.parse_result(lis_received[0])
            assert len(parsed["results"]) == 2
        finally:
            lis.stop()

        print("[3] Forwarder standard: ORU inoltrato al LIS, status=SENT  OK")


def test_forward_retries_on_unreachable_cchs():
    """Se CCHS e' irraggiungibile l'ordine resta RECEIVED (ritentabile), non ERROR."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(str(Path(tmpdir) / "test.db"))
        store.upsert_order(ORDER)

        # porta chiusa: nessun server in ascolto
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
        s.close()

        fwd = CitizenCareForwarder(store, "127.0.0.1", closed_port,
                                   ack_retry_attempts=0, connect_timeout=1.0)
        counts = fwd.forward_new_orders()
        assert counts["skipped"] == 1
        order = store.get_order("BC-CCHS-001")
        assert order["status"] == "RECEIVED", f"atteso RECEIVED (ritentabile), ottenuto {order['status']}"

        print("[4] CitizenCareForwarder: CCHS irraggiungibile -> resta RECEIVED (ritentabile)  OK")


def test_vpn_health_check():
    """VpnManager.is_reachable/wait_until_reachable senza avviare alcun tunnel reale
    (provider='external', manage_lifecycle=False): solo verifica di raggiungibilità TCP."""
    # 1) endpoint down -> non raggiungibile
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    closed_port = s.getsockname()[1]
    s.close()

    mgr = vpn.VpnManager(provider="external", manage_lifecycle=False,
                         health_check_host="127.0.0.1", health_check_port=closed_port,
                         health_check_timeout=0.5, wait_seconds=1.0, poll_interval=0.2)
    assert mgr.is_reachable() is False
    assert mgr.wait_until_reachable() is False
    assert mgr.ensure_up() is False  # non solleva, ritorna False

    # 2) endpoint up -> raggiungibile
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    open_port = srv.getsockname()[1]

    def _accept_loop():
        try:
            while True:
                conn, _ = srv.accept()
                conn.close()
        except OSError:
            pass

    t = threading.Thread(target=_accept_loop, daemon=True)
    t.start()
    try:
        mgr2 = vpn.VpnManager(provider="external", manage_lifecycle=False,
                              health_check_host="127.0.0.1", health_check_port=open_port,
                              health_check_timeout=0.5, wait_seconds=1.0, poll_interval=0.2)
        assert mgr2.is_reachable() is True
        assert mgr2.ensure_up() is True
    finally:
        srv.close()

    # 3) nessun health-check configurato -> assume ok (comportamento "no-op" documentato)
    mgr3 = vpn.VpnManager(provider="external", manage_lifecycle=False)
    assert mgr3.is_reachable() is True
    assert mgr3.ensure_up() is True

    print("[5] VpnManager: health-check raggiungibilità (down/up/non configurato)  OK")


def test_vpn_from_config_disabled():
    assert vpn.from_config({"vpn_enabled": False}) is None
    mgr = vpn.from_config({"vpn_enabled": True, "vpn_provider": "wireguard",
                           "vpn_interface": "wg-cchs"})
    assert mgr is not None and mgr.provider == "wireguard" and mgr.interface == "wg-cchs"
    print("[6] vpn.from_config: rispetta vpn_enabled e legge i parametri  OK")


if __name__ == "__main__":
    test_build_messages()
    test_forward_and_receive_roundtrip()
    test_forward_retries_on_unreachable_cchs()
    test_vpn_health_check()
    test_vpn_from_config_disabled()
    print("\nTUTTI I TEST CITIZENCARE OK")
