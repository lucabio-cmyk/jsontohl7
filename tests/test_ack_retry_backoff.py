"""Verifica retry ACK con backoff esponenziale nel Forwarder."""
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hl7mw.pipeline import Forwarder
from hl7mw.store import Store


def build_order_and_result(sample_key: str) -> tuple[dict, dict]:
    order = {
        "sample_key": sample_key,
        "placer_order_number": "PLAC-1",
        "filler_order_number": "FILL-1",
        "specimen_id": sample_key,
        "patient": {"id": "PAT-1", "name": "Rossi^Mario"},
        "universal_service_id": {"identifier": "58410-2", "text": "Emocromo completo"},
    }
    result = {
        "sample_key": sample_key,
        "results": [
            {"set_id": "1", "id": {"identifier": "718-7", "text": "Emoglobina"}, "value": "14.8", "units": "g/dL"}
        ],
    }
    return order, result


def main() -> int:
    db = "/tmp/hl7mw_retry_test.db"
    Path(db).unlink(missing_ok=True)
    store = Store(db)
    order, result = build_order_and_result("BC-RETRY")
    store.upsert_order(order)
    store.add_result("BC-RETRY", result)
    store.set_status("BC-RETRY", "READY")

    # porta aperta ma senza listener -> connessione rifiutata immediata
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    fwd = Forwarder(
        store,
        "127.0.0.1",
        port,
        ack_retry_attempts=2,
        ack_retry_backoff_seconds=0.2,
        connect_timeout=0.2,
        read_timeout=0.2,
    )

    t0 = time.monotonic()
    counts = fwd.forward_ready()
    elapsed = time.monotonic() - t0

    assert counts["skipped"] == 1, counts
    assert store.get_order("BC-RETRY")["status"] == "READY"
    # 2 retry => sleep attesi: 0.2 + 0.4 = 0.6s (+ overhead)
    assert elapsed >= 0.55, f"backoff non applicato, elapsed={elapsed:.3f}s"

    print("retry backoff OK", {"elapsed_s": round(elapsed, 3), "counts": counts})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
