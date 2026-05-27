"""
Test end-to-end del core middleware (eseguibile senza pytest: `python3 tests/test_e2e.py`).

Simula:
  - il LIS che invia un ordine ORM al middleware (OrderReceiver)
  - uno strumento che invia un risultato ORU al middleware (ResultReceiver)
  - il middleware che inoltra l'ORU completo a un finto LIS e riceve l'ACK (Forwarder)
"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hl7mw import hl7, mllp
from hl7mw.store import Store
from hl7mw.pipeline import OrderReceiver, ResultReceiver, Forwarder

CR = "\r"

ORM = CR.join([
    r"MSH|^~\&|LIS|OSP|HL7MW|MIDDLEWARE|20260527080000||ORM^O01|MSG001|P|2.5",
    "PID|1||PAT-100^^^OSP^MR||Rossi^Mario||19800512|M",
    "ORC|NW|PLAC-777|FILL-777",
    "OBR|1|PLAC-777|FILL-777|58410-2^Emocromo completo^LN|||20260527080000||||||||1^Bianchi^Giulia",
    "SPM|1|BC-ABC123||BLD",
]) + CR

ORU_INSTR = CR.join([
    r"MSH|^~\&|ANALYZER|LAB|HL7MW|MIDDLEWARE|20260527091500||ORU^R01|R001|P|2.5",
    "PID|1||PAT-100^^^OSP^MR||Rossi^Mario||19800512|M",
    "OBR|1|PLAC-777|FILL-777|58410-2^Emocromo completo^LN",
    "SPM|1|BC-ABC123||BLD",
    "OBX|1|NM|6690-2^Leucociti^LN||7.2|10*3/uL|4.0-10.0|N|||F",
    "OBX|2|NM|718-7^Emoglobina^LN||14.8|g/dL|13.0-17.0|N|||F",
    "OBX|3|NM|777-3^Piastrine^LN||350|10*3/uL|150-400|N|||F",
]) + CR


def fake_lis_collector(received):
    """Finto LIS che riceve l'ORU inoltrato e risponde AA."""
    def handler(message):
        received.append(message)
        return hl7.build_ack(message, "AA")
    return mllp.MllpServer("127.0.0.1", 0, handler)


def main():
    ok = True
    store = Store(":memory:") if False else Store("/tmp/hl7mw_test.db")
    Path("/tmp/hl7mw_test.db").unlink(missing_ok=True)
    store = Store("/tmp/hl7mw_test.db")

    order_rx = OrderReceiver(store, "127.0.0.1", 0)
    result_rx = ResultReceiver(store, "127.0.0.1", 0)
    # avvio server e recupero le porte effettive
    order_rx._server = mllp.MllpServer("127.0.0.1", 0, order_rx._handle).start()
    result_rx._server = mllp.MllpServer("127.0.0.1", 0, result_rx._handle).start()
    op = order_rx._server._srv.server_address[1]
    rp = result_rx._server._srv.server_address[1]

    received = []
    lis = fake_lis_collector(received).start()
    lp = lis._srv.server_address[1]

    try:
        # 1) il LIS invia l'ordine al middleware
        code = mllp.send_message("127.0.0.1", op, ORM)
        assert code == "AA", "ordine non ACKato"
        o = store.get_order("BC-ABC123")
        assert o and o["status"] == "RECEIVED", f"ordine non salvato: {o}"
        print("[1] ordine ricevuto dal LIS e salvato  -> sample=BC-ABC123  status=RECEIVED  OK")

        # 2) lo strumento invia il risultato al middleware
        code = mllp.send_message("127.0.0.1", rp, ORU_INSTR)
        assert code == "AA", "risultato non ACKato"
        o = store.get_order("BC-ABC123")
        res = store.results_for("BC-ABC123")
        assert o["status"] == "READY", f"atteso READY, ottenuto {o['status']}"
        assert len(res) == 1 and len(res[0]["results"]) == 3, "risultati non associati"
        print(f"[2] risultato dello strumento associato -> {len(res[0]['results'])} analiti  status=READY  OK")

        # 3) il middleware inoltra l'ORU completo al LIS
        fwd = Forwarder(store, "127.0.0.1", lp)
        counts = fwd.forward_ready()
        assert counts["sent"] == 1, f"inoltro fallito: {counts}"
        assert store.get_order("BC-ABC123")["status"] == "SENT"
        assert received, "il LIS non ha ricevuto l'ORU"
        parsed = hl7.parse_result(received[0])
        assert len(parsed["results"]) == 3, "ORU inoltrato incompleto"
        print(f"[3] inoltro al LIS completato            -> status=SENT  analiti inoltrati={len(parsed['results'])}  OK")

        # 4) risultato orfano (nessun ordine) -> unmatched
        orphan = ORU_INSTR.replace("BC-ABC123", "BC-NOORDER")
        mllp.send_message("127.0.0.1", rp, orphan)
        assert len(store.unmatched()) == 1, "risultato orfano non registrato"
        print("[4] risultato senza ordine               -> registrato in unmatched  OK")

        print("\nDashboard:", store.dashboard_counts())
        print("\nTUTTI I TEST OK")
    except AssertionError as e:
        ok = False
        print("FALLITO:", e)
    finally:
        order_rx._server.stop()
        result_rx._server.stop()
        lis.stop()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
