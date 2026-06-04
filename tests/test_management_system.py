"""
Test per il sistema di gestione: database esteso, timing, audit log, device monitoring.
Eseguibile: python3 tests/test_management_system.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hl7mw.store import Store
from hl7mw.monitor import DeviceMonitor


def test_extended_schema():
    """Verifica che le nuove tabelle siano create correttamente."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Store(Path(tmpdir) / "test.db")

        # Verifica tabelle
        with db._conn() as c:
            tables = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = [t["name"] for t in tables]

        assert "orders" in table_names
        assert "results" in table_names
        assert "unmatched_results" in table_names
        assert "instruments" in table_names
        assert "audit_log" in table_names
        assert "order_timing" in table_names


def test_instrument_tracking():
    """Verifica il tracking degli strumenti."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Store(Path(tmpdir) / "test.db")
        monitor = DeviceMonitor(db, offline_timeout_seconds=10.0)

        # Registra strumento
        monitor.register_instrument("HEMO1", "192.168.1.100", 6662, "POCT")

        instr = db.get_instrument("HEMO1")
        assert instr is not None
        assert instr["host"] == "192.168.1.100"
        assert instr["port"] == 6662
        assert instr["status"] == "UNKNOWN"

        # Registra messaggio
        monitor.record_message("HEMO1")
        instr = db.get_instrument("HEMO1")
        assert instr["status"] == "ONLINE"
        assert instr["messages_received"] == 1


def test_audit_log():
    """Verifica il log di audit."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Store(Path(tmpdir) / "test.db")

        # Aggiungi entry audit
        db.audit_log(
            "test_event",
            sample_key="ABC123",
            instrument="HEMO1",
            details="Test message",
            severity="INFO",
        )

        logs = db.get_audit_log(limit=10)
        assert len(logs) == 1
        assert logs[0]["event_type"] == "test_event"
        assert logs[0]["sample_key"] == "ABC123"
        assert logs[0]["severity"] == "INFO"


def test_order_timing():
    """Verifica il tracciamento dei timing dell'ordine."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Store(Path(tmpdir) / "test.db")

        # Registra ordine
        order = {
            "sample_key": "ABC123",
            "placer_order_number": "P001",
            "filler_order_number": "F001",
            "specimen_id": "SP001",
            "universal_service_id": {"text": "CBC"},
        }
        db.upsert_order(order)

        # Registra timing
        db.record_timing("ABC123", "received")
        db.record_timing("ABC123", "first_result")
        db.record_timing("ABC123", "ready")
        db.record_timing("ABC123", "sent")

        timing = db.get_timing("ABC123")
        assert timing is not None
        assert timing["received_at"] is not None
        assert timing["first_result_at"] is not None
        assert timing["ready_at"] is not None
        assert timing["sent_at"] is not None


def test_dashboard_stats():
    """Verifica il calcolo delle statistiche per la dashboard."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Store(Path(tmpdir) / "test.db")

        # Crea alcuni ordini
        for i in range(3):
            order = {
                "sample_key": f"KEY{i}",
                "placer_order_number": f"P{i}",
                "filler_order_number": f"F{i}",
                "specimen_id": f"S{i}",
                "universal_service_id": {"text": "CBC"},
            }
            db.upsert_order(order)

        db.set_status("KEY0", "RECEIVED")
        db.set_status("KEY1", "READY")
        db.set_status("KEY2", "SENT")

        # Aggiungi risultati orfani
        db.add_unmatched({
            "sample_key": "ORPHAN",
            "results": [{"test": "value"}]
        })

        stats = db.get_dashboard_stats()
        assert stats["total_orders"] == 3
        assert stats["status_counts"]["RECEIVED"] == 1
        assert stats["status_counts"]["READY"] == 1
        assert stats["status_counts"]["SENT"] == 1
        assert stats["unmatched_results"] == 1


def test_instrument_health_check():
    """Verifica l'aggiornamento dello stato health dei device."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Store(Path(tmpdir) / "test.db")
        monitor = DeviceMonitor(db, offline_timeout_seconds=1.0)

        # Registra strumento ma senza messaggio
        monitor.register_instrument("HEMO1", "192.168.1.100", 6662)
        instr = db.get_instrument("HEMO1")
        assert instr["status"] == "UNKNOWN"

        # Registra messaggio -> diventa ONLINE
        monitor.record_message("HEMO1")
        instr = db.get_instrument("HEMO1")
        assert instr["status"] == "ONLINE"


def test_match_unmatched_atomic():
    """Verifica il matching atomico dei risultati orfani."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Store(Path(tmpdir) / "test.db")

        # Crea ordine
        order = {
            "sample_key": "ABC123",
            "placer_order_number": "P001",
            "filler_order_number": "F001",
            "specimen_id": "SP001",
            "universal_service_id": {"text": "CBC"},
        }
        db.upsert_order(order)

        # Aggiungi risultato orfano con source_instrument
        from hl7mw.store import _now
        orphan_result = {
            "sample_key": "ABC123",
            "results": [{"test": "value"}]
        }
        with db._conn() as c:
            cursor = c.execute(
                "INSERT INTO unmatched_results(sample_key, result_json, received_at, source_instrument) VALUES(?,?,?,?)",
                ("ABC123", json.dumps(orphan_result, ensure_ascii=False), _now(), "HEMO1"),
            )
            result_id = cursor.lastrowid

        # Verifica che sia orfano
        unmatched_before = db.unmatched()
        assert len(unmatched_before) == 1

        # Effettua matching atomico
        success = db.match_unmatched(result_id, "ABC123")
        assert success is True

        # Verifica che non sia più orfano
        unmatched_after = db.unmatched()
        assert len(unmatched_after) == 0

        # Verifica che sia nei risultati matched con source_instrument preservato
        results = db.results_for("ABC123")
        assert len(results) == 1
        assert results[0]["results"][0]["test"] == "value"


if __name__ == "__main__":
    test_extended_schema()
    test_instrument_tracking()
    test_audit_log()
    test_order_timing()
    test_dashboard_stats()
    test_instrument_health_check()
    test_match_unmatched_atomic()
    print("TUTTI I TEST OK")
