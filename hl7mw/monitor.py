"""
hl7mw.monitor — tracciamento heartbeat e health status degli strumenti collegati.

Monitora:
- Ultimo messaggio ricevuto da ogni device
- Status ONLINE/OFFLINE basato su timeout
- Metriche di messaggi ricevuti
- Notifiche di cambio status (optional)

Integrato nei Receiver per registrare ogni messaggio.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from .store import Store


class DeviceMonitor:
    """Traccia health degli strumenti collegati."""

    def __init__(self, store: Store, offline_timeout_seconds: float = 300.0):
        self.store = store
        self.offline_timeout = timedelta(seconds=offline_timeout_seconds)

    def register_instrument(self, name: str, host: str, port: int, type_: str = "POCT") -> None:
        """Registra uno strumento nel database."""
        self.store.upsert_instrument(name, host, port, type_)

    def record_message(self, instrument_name: str, host: str = "", port: int = 0,
                        type_: str = "POCT") -> None:
        """Registra la ricezione di un messaggio da uno strumento.

        Auto-registra lo strumento se non è ancora presente in tabella (altrimenti
        l'UPDATE di mark_instrument_message/set_instrument_heartbeat non ha effetto
        su una riga inesistente e lo strumento non compare mai nella dashboard).
        """
        if not instrument_name:
            return
        if not self.store.get_instrument(instrument_name):
            self.store.upsert_instrument(instrument_name, host, port, type_)
        self.store.mark_instrument_message(instrument_name)
        self.store.set_instrument_heartbeat(instrument_name, "ONLINE")

    def update_health_status(self) -> dict[str, str]:
        """Aggiorna status ONLINE/OFFLINE basato su timeout. Ritorna {name: status}."""
        instruments = self.store.get_instruments()
        now = datetime.fromisoformat(self._now_iso())
        changes = {}

        for instr in instruments:
            current_status = instr.get("status", "UNKNOWN")
            new_status = "ONLINE"

            if instr.get("last_heartbeat"):
                try:
                    last_hb = datetime.fromisoformat(instr["last_heartbeat"])
                    if now - last_hb > self.offline_timeout:
                        new_status = "OFFLINE"
                except (ValueError, TypeError):
                    new_status = "UNKNOWN"
            elif instr.get("created_at"):
                # Mai ricevuto messaggio: è UNKNOWN
                new_status = "UNKNOWN"

            if new_status != current_status:
                self.store.set_instrument_heartbeat(instr["name"], new_status)
                changes[instr["name"]] = (current_status, new_status)
                self.store.audit_log(
                    "instrument_status_change",
                    instrument=instr["name"],
                    details=f"{current_status} → {new_status}",
                    severity="INFO" if new_status == "ONLINE" else "WARNING",
                )

        return changes

    def get_status(self, name: str) -> str | None:
        """Ritorna lo status corrente di uno strumento."""
        instr = self.store.get_instrument(name)
        return instr.get("status") if instr else None

    def get_all_status(self) -> dict[str, dict]:
        """Ritorna status di tutti gli strumenti."""
        instruments = self.store.get_instruments()
        return {i["name"]: {
            "status": i.get("status", "UNKNOWN"),
            "host": i["host"],
            "port": i["port"],
            "last_heartbeat": i.get("last_heartbeat"),
            "messages_received": i.get("messages_received", 0),
        } for i in instruments}

    @staticmethod
    def _now_iso() -> str:
        """ISO timestamp corrente."""
        return datetime.now().isoformat(timespec="seconds")
