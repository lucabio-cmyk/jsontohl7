"""
hl7mw.store — persistenza su SQLite (stdlib, zero dipendenze).

Tiene lo stato del middleware: ordini ricevuti dal LIS, risultati ricevuti dagli
strumenti, loro associazione e ciclo di vita. Query pronte per alimentare la UI.

Ciclo di vita di un ordine (colonna orders.status):
  RECEIVED         ordine ricevuto dal LIS e ACKato
  RESULTS_PARTIAL  arrivati alcuni risultati ma l'ordine non e' completo
  READY            risultati completi, pronto per l'inoltro al LIS
  FORWARDING       inoltro in corso
  SENT             inoltrato e confermato (ACK del LIS)
  ERROR            errore permanente (vedi last_error)

I risultati senza ordine corrispondente finiscono in 'unmatched_results'.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    sample_key TEXT PRIMARY KEY,
    placer_order_number TEXT,
    filler_order_number TEXT,
    specimen_id TEXT,
    patient_json TEXT,
    order_json TEXT,
    status TEXT NOT NULL DEFAULT 'RECEIVED',
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_key TEXT NOT NULL,
    result_json TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unmatched_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_key TEXT,
    result_json TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_results_sample ON results(sample_key);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
"""


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str = "hl7mw.db"):
        self.path = path
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ----- ordini -----
    def upsert_order(self, order: dict) -> None:
        key = order["sample_key"]
        if not key:
            raise ValueError("Ordine senza chiave di matching (sample_key vuoto).")
        with self._conn() as c:
            c.execute(
                """INSERT INTO orders(sample_key, placer_order_number, filler_order_number,
                       specimen_id, patient_json, order_json, status, created_at, updated_at)
                   VALUES(?,?,?,?,?,?, 'RECEIVED', ?, ?)
                   ON CONFLICT(sample_key) DO UPDATE SET
                       placer_order_number=excluded.placer_order_number,
                       filler_order_number=excluded.filler_order_number,
                       specimen_id=excluded.specimen_id,
                       patient_json=excluded.patient_json,
                       order_json=excluded.order_json,
                       updated_at=excluded.updated_at""",
                (key, order.get("placer_order_number"), order.get("filler_order_number"),
                 order.get("specimen_id"), json.dumps(order.get("patient", {}), ensure_ascii=False),
                 json.dumps(order, ensure_ascii=False), _now(), _now()),
            )

    def get_order(self, sample_key: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM orders WHERE sample_key=?", (sample_key,)).fetchone()
            return dict(row) if row else None

    def set_status(self, sample_key: str, status: str, error: str | None = None) -> None:
        with self._conn() as c:
            c.execute("UPDATE orders SET status=?, last_error=?, updated_at=? WHERE sample_key=?",
                      (status, error, _now(), sample_key))

    def orders_by_status(self, status: str) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM orders WHERE status=? ORDER BY updated_at", (status,))]

    def dashboard_counts(self) -> dict[str, int]:
        with self._conn() as c:
            rows = c.execute("SELECT status, COUNT(*) n FROM orders GROUP BY status").fetchall()
            d = {r["status"]: r["n"] for r in rows}
            d["unmatched"] = c.execute("SELECT COUNT(*) n FROM unmatched_results").fetchone()["n"]
            return d

    # ----- risultati -----
    def add_result(self, sample_key: str, result: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO results(sample_key, result_json, received_at) VALUES(?,?,?)",
                      (sample_key, json.dumps(result, ensure_ascii=False), _now()))

    def results_for(self, sample_key: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT result_json FROM results WHERE sample_key=?", (sample_key,))
            return [json.loads(r["result_json"]) for r in rows]

    def add_unmatched(self, result: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO unmatched_results(sample_key, result_json, received_at) VALUES(?,?,?)",
                      (result.get("sample_key"), json.dumps(result, ensure_ascii=False), _now()))

    def unmatched(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM unmatched_results ORDER BY received_at")]
