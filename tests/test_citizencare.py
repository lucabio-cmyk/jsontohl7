"""
Test della sostituzione di Citizen Care Connect (CCHS), e del modulo VPN.

CCHS non è né il LIS né uno strumento: è essa stessa un middleware/bridge
verso cui il vero LIS del cliente è oggi configurato (vedi
INTEGRATION_CITIZENCARE.md). Questo middleware ne prende il posto: riceve
ADT^A04 (registrazione paziente) e ORM^O01 (ordine) dal vero LIS
(OrderReceiver, invariato salvo il supporto ADT^A04 aggiunto), e gli
restituisce l'ORU^R01 (Forwarder, invariato) — esattamente il ruolo di "CCHS
Application" nella tabella di validazione §5.2 della loro spec. Nessun
componente dedicato: questo test verifica solo l'estensione ADT^A04 e il
modulo VPN.

Simula:
  - il vero LIS che invia ADT^A04 (paziente) poi ORM^O01 (ordine) al middleware
    (nella spec CCHS, MSH-3/4 di questi messaggi identificano il LIS mittente,
    non CCHS: CCHS è sempre il destinatario nei loro esempi — qui MSH-5/6)
  - uno strumento (es. HemoScreen) che invia il risultato
  - il middleware che inoltra l'ORU^R01 completo al LIS (al posto di CCHS)

Eseguibile senza pytest: `python3 tests/test_citizencare.py`
"""
import socket
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hl7mw import hl7, mllp, vpn
from hl7mw.store import Store
from hl7mw.pipeline import OrderReceiver, ResultReceiver, Forwarder

CR = "\r"

# Struttura adattata dal sample ADT_A04 della spec CCHS §4.1: MSH-3/4 = identita'
# del vero LIS mittente, MSH-5/6 = identita' di questo middleware (al posto di CCHS)
ADT_A04 = CR.join([
    r"MSH|^~\&|LIS|OSP|HL7MW|MIDDLEWARE|20260817120000||ADT^A04|645511|P|2.5",
    "EVN|A04|20260817120000",
    "PID|1||PATCC01||VERDI^LUCA||19750303|M",
    "PV1|1|O",
]) + CR

# Struttura adattata dal sample ORM_O01 della spec CCHS §4.2 (stessa identita' MSH)
ORM = CR.join([
    r"MSH|^~\&|LIS|OSP|HL7MW|MIDDLEWARE|20260817120010||ORM^O01|645512|P|2.5",
    "PID|1||PATCC01||VERDI^LUCA||19750303|M",
    "ORC|NW|PLACCC01|FILLCC01||||||20260817120010",
    "OBR|1|PLACCC01|FILLCC01|CBC^Complete Blood Count^L",
]) + CR

ORU_INSTR = CR.join([
    r"MSH|^~\&|HEMOSCREEN|LAB|HL7MW|MIDDLEWARE|20260817120100||ORU^R01|R001|P|2.5",
    "OBR|1|PLACCC01|FILLCC01|CBC^Complete Blood Count^L",
    "OBX|1|NM|6690-2^Leucociti^LN||7.2|10*3/uL|4.0-10.0|N|||F",
    "OBX|2|NM|718-7^Emoglobina^LN||14.8|g/dL|13.0-17.0|N|||F",
]) + CR


def _fake_lis(received: list):
    """Finto LIS reale (quello che oggi parla con CCHS): registra l'ORU ricevuto e risponde ACK AA."""
    def handler(message):
        received.append(message)
        return hl7.build_ack(message, "AA", "", "LIS", "OSP")
    return mllp.MllpServer("127.0.0.1", 0, handler)


def test_parse_adt():
    adt = hl7.parse_adt(ADT_A04)
    assert adt["message_type"].startswith("ADT")
    assert adt["event_type"] == "A04"
    assert adt["patient"]["id"] == "PATCC01"
    assert adt["patient"]["last_name"] == "VERDI"

    try:
        hl7.parse_adt(ORM)
        assert False, "parse_adt doveva rifiutare un ORM"
    except hl7.Hl7Error:
        pass


def test_full_flow_replacing_cchs():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(str(Path(tmpdir) / "test.db"))

        order_rx = OrderReceiver(store, "127.0.0.1", 0)
        order_rx._server = mllp.MllpServer("127.0.0.1", 0, order_rx._handle).start()
        op = order_rx._server._srv.server_address[1]

        result_rx = ResultReceiver(store, "127.0.0.1", 0)
        result_rx._server = mllp.MllpServer("127.0.0.1", 0, result_rx._handle).start()
        rp = result_rx._server._srv.server_address[1]

        received = []
        lis = _fake_lis(received).start()
        lp = lis._srv.server_address[1]

        try:
            # 1) il LIS registra il paziente (ADT^A04): ACK positivo, nessun ordine creato
            code = mllp.send_message("127.0.0.1", op, ADT_A04)
            assert code == "AA", "ADT^A04 non ACKato"
            assert store.get_order("FILLCC01") is None, "l'ADT non deve creare un ordine"

            audit = store.get_audit_log(limit=10)
            assert any(a["event_type"] == "patient_registered" for a in audit)
            print("[1] OrderReceiver: ADT^A04 dal LIS accettato, ACK AA, nessun ordine creato  OK")

            # 2) il LIS crea l'ordine (ORM^O01)
            code = mllp.send_message("127.0.0.1", op, ORM)
            assert code == "AA", "ORM^O01 non ACKato"
            order = store.get_order("FILLCC01")
            assert order and order["status"] == "RECEIVED"
            print("[2] OrderReceiver: ORM^O01 dal LIS accettato -> status=RECEIVED  OK")

            # 3) lo strumento (es. HemoScreen) invia il risultato
            code = mllp.send_message("127.0.0.1", rp, ORU_INSTR)
            assert code == "AA", "risultato strumento non ACKato"
            order = store.get_order("FILLCC01")
            assert order["status"] == "READY", f"atteso READY, ottenuto {order['status']}"
            print("[3] ResultReceiver: risultato strumento associato -> status=READY  OK")

            # 4) il middleware inoltra l'ORU completo al LIS, al posto di CCHS
            fwd = Forwarder(store, "127.0.0.1", lp)
            counts = fwd.forward_ready()
            assert counts["sent"] == 1, f"inoltro al LIS fallito: {counts}"
            assert store.get_order("FILLCC01")["status"] == "SENT"
            assert len(received) == 1
            parsed = hl7.parse_result(received[0])
            assert len(parsed["results"]) == 2
            print("[4] Forwarder: ORU^R01 inoltrato al LIS (al posto di CCHS) -> status=SENT  OK")
        finally:
            order_rx._server.stop()
            result_rx._server.stop()
            lis.stop()


def test_order_receiver_still_rejects_unknown_types():
    """Il dispatch ADT/ORM aggiunto non deve indebolire il rifiuto dei tipi non gestiti."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(str(Path(tmpdir) / "test.db"))
        order_rx = OrderReceiver(store, "127.0.0.1", 0)
        order_rx._server = mllp.MllpServer("127.0.0.1", 0, order_rx._handle).start()
        op = order_rx._server._srv.server_address[1]
        try:
            bogus = ADT_A04.replace("ADT^A04", "SIU^S12")
            reply = mllp.exchange("127.0.0.1", op, bogus)
            code, _, _ = mllp.parse_ack_code(reply)
            assert code == "AR", f"atteso AR per tipo non gestito, ottenuto {code}"
        finally:
            order_rx._server.stop()

        print("[5] OrderReceiver: tipi non gestiti (es. SIU) restano rifiutati con AR  OK")


def test_dedicated_adt_channel():
    """Alcuni LIS (es. Dedalus) aprono due connessioni MLLP separate verso
    l'EMR Bridge, una per ADT e una per ORM, invece di un unico canale.
    hl7mw.run supporta questo con adt_listen_port: un secondo MllpServer che
    riusa la stessa OrderReceiver._handle. Qui si replica lo stesso schema
    manualmente (senza avviare l'intero processo) per verificarlo."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = Store(str(Path(tmpdir) / "test.db"))
        order_rx = OrderReceiver(store, "127.0.0.1", 0)
        order_rx._server = mllp.MllpServer("127.0.0.1", 0, order_rx._handle).start()
        order_port = order_rx._server._srv.server_address[1]

        # secondo listener dedicato ad ADT, stessa logica di gestione
        adt_server = mllp.MllpServer("127.0.0.1", 0, order_rx._handle).start()
        adt_port = adt_server._srv.server_address[1]

        try:
            # ADT arriva sul canale dedicato
            code = mllp.send_message("127.0.0.1", adt_port, ADT_A04)
            assert code == "AA", "ADT^A04 sul canale dedicato non ACKato"
            assert store.get_order("FILLCC01") is None

            # ORM arriva sul canale "ordinario"
            code = mllp.send_message("127.0.0.1", order_port, ORM)
            assert code == "AA", "ORM^O01 sul canale ordinario non ACKato"
            order = store.get_order("FILLCC01")
            assert order and order["status"] == "RECEIVED"
        finally:
            order_rx._server.stop()
            adt_server.stop()

        print("[6] Canale ADT dedicato + canale ORM ordinario (schema Dedalus) -> entrambi funzionano  OK")


def test_vpn_health_check():
    """VpnManager.is_reachable/wait_until_reachable senza avviare alcun tunnel reale
    (provider='external', manage_lifecycle=False): solo verifica di raggiungibilità TCP."""
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

    mgr3 = vpn.VpnManager(provider="external", manage_lifecycle=False)
    assert mgr3.is_reachable() is True
    assert mgr3.ensure_up() is True

    print("[7] VpnManager: health-check raggiungibilità (down/up/non configurato)  OK")


def test_vpn_from_config_disabled():
    assert vpn.from_config({"vpn_enabled": False}) is None
    mgr = vpn.from_config({"vpn_enabled": True, "vpn_provider": "wireguard",
                           "vpn_interface": "wg-cchs"})
    assert mgr is not None and mgr.provider == "wireguard" and mgr.interface == "wg-cchs"
    print("[8] vpn.from_config: rispetta vpn_enabled e legge i parametri  OK")


if __name__ == "__main__":
    test_parse_adt()
    test_full_flow_replacing_cchs()
    test_order_receiver_still_rejects_unknown_types()
    test_dedicated_adt_channel()
    test_vpn_health_check()
    test_vpn_from_config_disabled()
    print("\nTUTTI I TEST CITIZENCARE OK")
