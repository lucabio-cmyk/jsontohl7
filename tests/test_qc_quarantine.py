"""Verifica blocco rilascio in quarantena se QC failed/scaduto."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hl7mw.pipeline import try_complete
from hl7mw.store import Store


def build_order(sample_key: str) -> dict:
    return {
        "sample_key": sample_key,
        "placer_order_number": "PLAC-1",
        "filler_order_number": "FILL-1",
        "specimen_id": sample_key,
        "patient": {"id": "PAT-1", "name": "Rossi^Mario"},
        "universal_service_id": {"identifier": "58410-2", "text": "Emocromo completo"},
    }


def main() -> int:
    db = "/tmp/hl7mw_qc_test.db"
    Path(db).unlink(missing_ok=True)
    store = Store(db)
    store.upsert_order(build_order("BC-QC-1"))
    store.upsert_order(build_order("BC-QC-2"))

    store.add_result("BC-QC-1", {
        "sample_key": "BC-QC-1",
        "qc_status": "FAILED",
        "results": [{"id": {"identifier": "718-7"}, "value": "14.8"}],
    })
    store.add_result("BC-QC-2", {
        "sample_key": "BC-QC-2",
        "results": [{"id": {"identifier": "718-7"}, "value": "14.8", "qc_status": "EXPIRED"}],
    })

    try_complete(store, "BC-QC-1")
    try_complete(store, "BC-QC-2")

    q1 = store.get_order("BC-QC-1")
    q2 = store.get_order("BC-QC-2")
    assert q1["status"] == "QUARANTINED", q1
    assert q2["status"] == "QUARANTINED", q2
    print("qc quarantine OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
