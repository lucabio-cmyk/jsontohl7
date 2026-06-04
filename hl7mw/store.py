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

from . import auth

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
    updated_at TEXT NOT NULL,
    source_instrument TEXT,
    forwarding_attempts INTEGER DEFAULT 0,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_key TEXT NOT NULL,
    result_json TEXT NOT NULL,
    received_at TEXT NOT NULL,
    source_instrument TEXT
);
CREATE TABLE IF NOT EXISTS unmatched_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_key TEXT,
    result_json TEXT NOT NULL,
    received_at TEXT NOT NULL,
    source_instrument TEXT
);
CREATE TABLE IF NOT EXISTS instruments (
    name TEXT PRIMARY KEY,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    type TEXT DEFAULT 'POCT',
    status TEXT DEFAULT 'UNKNOWN',
    last_heartbeat TEXT,
    last_message_at TEXT,
    messages_received INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    sample_key TEXT,
    instrument TEXT,
    details TEXT,
    severity TEXT DEFAULT 'INFO'
);
CREATE TABLE IF NOT EXISTS order_timing (
    sample_key TEXT PRIMARY KEY,
    received_at TEXT,
    first_result_at TEXT,
    ready_at TEXT,
    sent_at TEXT,
    FOREIGN KEY(sample_key) REFERENCES orders(sample_key)
);
CREATE TABLE IF NOT EXISTS operators (
    operator_id TEXT PRIMARY KEY,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'OPERATOR',
    password_hash TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    poct_permission TEXT DEFAULT 'OPERATOR',
    certifications TEXT DEFAULT '[]',
    valid_from TEXT,
    valid_until TEXT,
    locked INTEGER NOT NULL DEFAULT 0,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    last_login TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operator_sessions (
    token TEXT PRIMARY KEY,
    operator_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY(operator_id) REFERENCES operators(operator_id)
);
CREATE TABLE IF NOT EXISTS device_config (
    instrument TEXT NOT NULL,
    param_key TEXT NOT NULL,
    param_value TEXT,
    updated_at TEXT NOT NULL,
    updated_by TEXT,
    PRIMARY KEY(instrument, param_key)
);
CREATE TABLE IF NOT EXISTS device_config_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument TEXT NOT NULL,
    param_key TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    changed_at TEXT NOT NULL,
    changed_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_results_sample ON results(sample_key);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_sample ON audit_log(sample_key);
CREATE INDEX IF NOT EXISTS idx_instruments_status ON instruments(status);
CREATE INDEX IF NOT EXISTS idx_sessions_operator ON operator_sessions(operator_id);
CREATE INDEX IF NOT EXISTS idx_config_history_instr ON device_config_history(instrument);
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

    def match_unmatched(self, result_id: int, sample_key: str) -> bool:
        """Associa atomicamente un risultato orfano a un ordine preservando lo strumento sorgente."""
        with self._conn() as c:
            row = c.execute(
                "SELECT result_json, source_instrument FROM unmatched_results WHERE id=?", (result_id,)
            ).fetchone()
            if not row:
                return False
            c.execute(
                "INSERT INTO results(sample_key, result_json, received_at, source_instrument) VALUES(?,?,?,?)",
                (sample_key, row["result_json"], _now(), row["source_instrument"]),
            )
            c.execute("DELETE FROM unmatched_results WHERE id=?", (result_id,))
            return True

    # ----- instruments -----
    def upsert_instrument(self, name: str, host: str, port: int, type_: str = "POCT") -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO instruments(name, host, port, type, created_at, updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET
                       host=excluded.host,
                       port=excluded.port,
                       type=excluded.type,
                       updated_at=excluded.updated_at""",
                (name, host, port, type_, _now(), _now()),
            )

    def set_instrument_heartbeat(self, name: str, status: str = "ONLINE") -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE instruments SET status=?, last_heartbeat=?, updated_at=? WHERE name=?",
                (status, _now(), _now(), name),
            )

    def mark_instrument_message(self, name: str) -> None:
        with self._conn() as c:
            c.execute(
                """UPDATE instruments SET last_message_at=?, messages_received=messages_received+1, updated_at=?
                   WHERE name=?""",
                (_now(), _now(), name),
            )

    def get_instruments(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM instruments ORDER BY name")]

    def get_instrument(self, name: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM instruments WHERE name=?", (name,)).fetchone()
            return dict(row) if row else None

    # ----- audit log -----
    def audit_log(self, event_type: str, sample_key: str | None = None,
                  instrument: str | None = None, details: str | None = None,
                  severity: str = "INFO") -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO audit_log(timestamp, event_type, sample_key, instrument, details, severity)
                   VALUES(?,?,?,?,?,?)""",
                (_now(), event_type, sample_key, instrument, details, severity),
            )

    def get_audit_log(self, limit: int = 500) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,))]

    # ----- timing & stats -----
    def record_timing(self, sample_key: str, event: str) -> None:
        """Record order timing events: 'received', 'first_result', 'ready', 'sent'."""
        with self._conn() as c:
            col = {"received": "received_at", "first_result": "first_result_at",
                   "ready": "ready_at", "sent": "sent_at"}.get(event)
            if not col:
                return
            c.execute(
                f"INSERT INTO order_timing(sample_key, {col}) VALUES(?,?) "
                f"ON CONFLICT(sample_key) DO UPDATE SET {col}=excluded.{col}",
                (sample_key, _now()),
            )

    def get_timing(self, sample_key: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM order_timing WHERE sample_key=?", (sample_key,)).fetchone()
            return dict(row) if row else None

    def get_dashboard_stats(self) -> dict:
        """Compute comprehensive dashboard statistics."""
        with self._conn() as c:
            status_counts = {r["status"]: r["n"]
                            for r in c.execute("SELECT status, COUNT(*) n FROM orders GROUP BY status").fetchall()}
            total_orders = sum(status_counts.values())

            unmatched_count = c.execute("SELECT COUNT(*) n FROM unmatched_results").fetchone()["n"]
            instrument_count = c.execute("SELECT COUNT(*) n FROM instruments").fetchone()["n"]
            online_instruments = c.execute(
                "SELECT COUNT(*) n FROM instruments WHERE status='ONLINE'").fetchone()["n"]

            total_results = c.execute("SELECT COUNT(*) n FROM results").fetchone()["n"]

            timings = c.execute(
                "SELECT received_at, ready_at, sent_at FROM order_timing WHERE sent_at IS NOT NULL"
            ).fetchall()

            avg_time_to_ready = None
            avg_time_ready_to_sent = None
            if timings:
                import datetime
                diffs_to_ready = []
                diffs_ready_to_sent = []
                for t in timings:
                    try:
                        if t["received_at"] and t["ready_at"]:
                            r = _dt.datetime.fromisoformat(t["received_at"])
                            rdy = _dt.datetime.fromisoformat(t["ready_at"])
                            diffs_to_ready.append((rdy - r).total_seconds())
                        if t["ready_at"] and t["sent_at"]:
                            rdy = _dt.datetime.fromisoformat(t["ready_at"])
                            s = _dt.datetime.fromisoformat(t["sent_at"])
                            diffs_ready_to_sent.append((s - rdy).total_seconds())
                    except (ValueError, TypeError):
                        pass
                if diffs_to_ready:
                    avg_time_to_ready = sum(diffs_to_ready) / len(diffs_to_ready)
                if diffs_ready_to_sent:
                    avg_time_ready_to_sent = sum(diffs_ready_to_sent) / len(diffs_ready_to_sent)

        return {
            "total_orders": total_orders,
            "status_counts": status_counts,
            "unmatched_results": unmatched_count,
            "instruments": {"total": instrument_count, "online": online_instruments},
            "total_results": total_results,
            "avg_time_to_ready_seconds": avg_time_to_ready,
            "avg_time_ready_to_sent_seconds": avg_time_ready_to_sent,
        }

    # ----- operatori (RBAC) -----
    @staticmethod
    def _operator_public(row: sqlite3.Row | dict) -> dict:
        """Vista pubblica di un operatore: niente hash password, certifications come lista."""
        d = dict(row)
        d.pop("password_hash", None)
        try:
            d["certifications"] = json.loads(d.get("certifications") or "[]")
        except (ValueError, TypeError):
            d["certifications"] = []
        d["active"] = bool(d.get("active"))
        d["locked"] = bool(d.get("locked"))
        return d

    def upsert_operator(self, operator_id: str, full_name: str, role: str = "OPERATOR",
                        password: str | None = None, poct_permission: str = "OPERATOR",
                        certifications: list[str] | None = None,
                        valid_from: str | None = None, valid_until: str | None = None,
                        active: bool = True) -> None:
        """Crea o aggiorna un operatore. La password (se data) viene cifrata con PBKDF2.

        Se `password` è None su un operatore esistente, l'hash corrente è preservato.
        """
        if not operator_id:
            raise ValueError("operator_id vuoto non ammesso.")
        if not auth.is_valid_role(role):
            raise ValueError(f"Ruolo non valido: {role!r}")
        pwd_hash = auth.hash_password(password) if password else None
        certs_json = json.dumps(certifications or [], ensure_ascii=False)
        with self._conn() as c:
            existing = c.execute(
                "SELECT password_hash FROM operators WHERE operator_id=?", (operator_id,)
            ).fetchone()
            if existing and pwd_hash is None:
                pwd_hash = existing["password_hash"]
            c.execute(
                """INSERT INTO operators(operator_id, full_name, role, password_hash,
                       active, poct_permission, certifications, valid_from, valid_until,
                       created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(operator_id) DO UPDATE SET
                       full_name=excluded.full_name,
                       role=excluded.role,
                       password_hash=excluded.password_hash,
                       active=excluded.active,
                       poct_permission=excluded.poct_permission,
                       certifications=excluded.certifications,
                       valid_from=excluded.valid_from,
                       valid_until=excluded.valid_until,
                       updated_at=excluded.updated_at""",
                (operator_id, full_name, role, pwd_hash, 1 if active else 0,
                 poct_permission, certs_json, valid_from, valid_until, _now(), _now()),
            )

    def set_operator_password(self, operator_id: str, password: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE operators SET password_hash=?, updated_at=? WHERE operator_id=?",
                (auth.hash_password(password), _now(), operator_id),
            )
            return cur.rowcount > 0

    def set_operator_active(self, operator_id: str, active: bool) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE operators SET active=?, updated_at=? WHERE operator_id=?",
                (1 if active else 0, _now(), operator_id),
            )
            return cur.rowcount > 0

    def set_operator_locked(self, operator_id: str, locked: bool) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE operators SET locked=?, failed_attempts=?, updated_at=? WHERE operator_id=?",
                (1 if locked else 0, 0 if not locked else None, _now(), operator_id),
            )
            return cur.rowcount > 0

    def delete_operator(self, operator_id: str) -> bool:
        with self._conn() as c:
            c.execute("DELETE FROM operator_sessions WHERE operator_id=?", (operator_id,))
            cur = c.execute("DELETE FROM operators WHERE operator_id=?", (operator_id,))
            return cur.rowcount > 0

    def get_operator(self, operator_id: str) -> dict | None:
        """Vista pubblica dell'operatore (senza hash password)."""
        with self._conn() as c:
            row = c.execute("SELECT * FROM operators WHERE operator_id=?", (operator_id,)).fetchone()
            return self._operator_public(row) if row else None

    def list_operators(self, active_only: bool = False) -> list[dict]:
        with self._conn() as c:
            q = "SELECT * FROM operators"
            if active_only:
                q += " WHERE active=1"
            q += " ORDER BY operator_id"
            return [self._operator_public(r) for r in c.execute(q)]

    def authenticate_operator(self, operator_id: str, password: str) -> dict | None:
        """Verifica credenziali. Ritorna la vista pubblica se OK, altrimenti None.

        Gestisce lockout dopo 5 tentativi falliti e aggiorna last_login al successo.
        Operatori inattivi/bloccati non possono autenticarsi.
        """
        with self._conn() as c:
            row = c.execute("SELECT * FROM operators WHERE operator_id=?", (operator_id,)).fetchone()
            if not row:
                return None
            if not row["active"] or row["locked"]:
                return None
            if auth.verify_password(password, row["password_hash"]):
                c.execute(
                    "UPDATE operators SET failed_attempts=0, last_login=?, updated_at=? WHERE operator_id=?",
                    (_now(), _now(), operator_id),
                )
                return self._operator_public(row)
            # Password errata: incrementa i tentativi ed eventualmente blocca.
            attempts = (row["failed_attempts"] or 0) + 1
            locked = 1 if attempts >= 5 else 0
            c.execute(
                "UPDATE operators SET failed_attempts=?, locked=?, updated_at=? WHERE operator_id=?",
                (attempts, locked, _now(), operator_id),
            )
            return None

    # ----- sessioni / token -----
    def create_session(self, operator_id: str, ttl_seconds: int = 8 * 3600) -> str:
        token = auth.generate_token()
        expires = (_dt.datetime.now() + _dt.timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
        with self._conn() as c:
            c.execute(
                "INSERT INTO operator_sessions(token, operator_id, created_at, expires_at) VALUES(?,?,?,?)",
                (token, operator_id, _now(), expires),
            )
        return token

    def get_session_operator(self, token: str) -> dict | None:
        """Operatore associato a un token valido e non scaduto, altrimenti None."""
        if not token:
            return None
        with self._conn() as c:
            sess = c.execute("SELECT * FROM operator_sessions WHERE token=?", (token,)).fetchone()
            if not sess:
                return None
            try:
                if _dt.datetime.fromisoformat(sess["expires_at"]) < _dt.datetime.now():
                    c.execute("DELETE FROM operator_sessions WHERE token=?", (token,))
                    return None
            except (ValueError, TypeError):
                return None
            row = c.execute(
                "SELECT * FROM operators WHERE operator_id=?", (sess["operator_id"],)
            ).fetchone()
            if not row or not row["active"] or row["locked"]:
                return None
            return self._operator_public(row)

    def delete_session(self, token: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM operator_sessions WHERE token=?", (token,))

    def purge_expired_sessions(self) -> int:
        with self._conn() as c:
            cur = c.execute("DELETE FROM operator_sessions WHERE expires_at < ?", (_now(),))
            return cur.rowcount

    def count_operators(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) n FROM operators").fetchone()["n"]

    # ----- configurazione remota strumenti -----
    def set_device_config(self, instrument: str, params: dict[str, str],
                          updated_by: str | None = None) -> None:
        """Applica una mappa di parametri di configurazione a uno strumento.

        Registra ogni variazione in device_config_history (tracciabilità).
        """
        with self._conn() as c:
            for key, value in params.items():
                value_s = "" if value is None else str(value)
                old = c.execute(
                    "SELECT param_value FROM device_config WHERE instrument=? AND param_key=?",
                    (instrument, key),
                ).fetchone()
                old_value = old["param_value"] if old else None
                if old_value == value_s:
                    continue
                c.execute(
                    """INSERT INTO device_config(instrument, param_key, param_value, updated_at, updated_by)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(instrument, param_key) DO UPDATE SET
                           param_value=excluded.param_value,
                           updated_at=excluded.updated_at,
                           updated_by=excluded.updated_by""",
                    (instrument, key, value_s, _now(), updated_by),
                )
                c.execute(
                    """INSERT INTO device_config_history(instrument, param_key, old_value, new_value, changed_at, changed_by)
                       VALUES(?,?,?,?,?,?)""",
                    (instrument, key, old_value, value_s, _now(), updated_by),
                )

    def get_device_config(self, instrument: str) -> dict[str, str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT param_key, param_value FROM device_config WHERE instrument=? ORDER BY param_key",
                (instrument,),
            ).fetchall()
            return {r["param_key"]: r["param_value"] for r in rows}

    def get_device_config_history(self, instrument: str, limit: int = 200) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM device_config_history WHERE instrument=? ORDER BY changed_at DESC LIMIT ?",
                (instrument, limit),
            )]
