"""
hl7mw.pipeline — i tre componenti del middleware order-driven.

1) OrderReceiver   server MLLP: riceve ORM/OML dal LIS -> salva ordine -> ACK
2) ResultReceiver  server MLLP: riceve ORU dagli strumenti -> associa all'ordine
3) Forwarder       prende gli ordini completi -> ORU -> invia al LIS -> ACK -> SENT

L'associazione avviene per 'sample_key' (specimen/barcode, poi filler/placer).
La regola di completezza dell'ordine e' volutamente semplice e va adattata: qui un
ordine e' "completo" appena arriva almeno un risultato per la sua chiave. In produzione
si confronta l'elenco dei test richiesti (universal_service_id / OBR) con quelli ricevuti.
"""
from __future__ import annotations

import logging
import time

from . import hl7, mllp
from .store import Store
from .monitor import DeviceMonitor

LOG = logging.getLogger("hl7mw")


# --------------------------------------------------------------------------- 1) ordini dal LIS
class OrderReceiver:
    def __init__(self, store: Store, host: str, port: int,
                 sending_app="HL7MW", sending_facility="MIDDLEWARE",
                 monitor: DeviceMonitor | None = None):
        self.store = store
        self.host, self.port = host, port
        self.sending_app, self.sending_facility = sending_app, sending_facility
        self.monitor = monitor
        self._server: mllp.MllpServer | None = None

    def _handle(self, message: str) -> str:
        mtype = hl7.msh_field(hl7.split_segments(message), 9)
        if mtype.startswith("ADT"):
            return self._handle_adt(message)
        try:
            order = hl7.parse_order(message)
        except hl7.Hl7Error as e:
            LOG.warning("Ordine non valido rifiutato: %s", e)
            return hl7.build_ack(message, "AR", str(e), self.sending_app, self.sending_facility)
        if not order["sample_key"]:
            return hl7.build_ack(message, "AR", "Nessun identificativo campione/ordine",
                                 self.sending_app, self.sending_facility)
        self.store.upsert_order(order)
        self.store.record_timing(order["sample_key"], "received")
        self.store.audit_log("order_received", sample_key=order["sample_key"],
                            details=f"Test: {order['universal_service_id'].get('text')}")
        # se erano gia' arrivati risultati prima dell'ordine, riconcilia
        try_complete(self.store, order["sample_key"])
        LOG.info("Ordine ricevuto dal LIS: sample=%s test=%s",
                 order["sample_key"], order["universal_service_id"].get("text"))
        return hl7.build_ack(message, "AA", "", self.sending_app, self.sending_facility)

    def _handle_adt(self, message: str) -> str:
        """ADT^A0x (es. A04 registrazione paziente): alcuni LIS lo inviano prima
        dell'ordine vero e proprio (es. Citizen Care Connect, spec CCHS §4.1). Non
        crea un ordine: l'ORM^O01 che segue porta gia' i dati paziente necessari
        (vedi hl7.parse_order/_patient). Qui riscontriamo solo con ACK positivo,
        cosi' il mittente non considera fallita la registrazione."""
        try:
            adt = hl7.parse_adt(message)
        except hl7.Hl7Error as e:
            LOG.warning("ADT non valido rifiutato: %s", e)
            return hl7.build_ack(message, "AR", str(e), self.sending_app, self.sending_facility)
        patient_id = adt["patient"].get("id", "")
        self.store.audit_log("patient_registered", details=f"ADT {adt['event_type']} patient={patient_id}")
        LOG.info("Paziente registrato dal LIS: id=%s evento=%s", patient_id, adt["event_type"])
        return hl7.build_ack(message, "AA", "", self.sending_app, self.sending_facility)

    def start(self):
        self._server = mllp.MllpServer(self.host, self.port, self._handle).start()
        LOG.info("OrderReceiver in ascolto su %s:%s (ordini dal LIS)", self.host, self.port)
        return self

    def stop(self):
        if self._server:
            self._server.stop()


# --------------------------------------------------------------------------- 2) risultati dagli strumenti
class ResultReceiver:
    def __init__(self, store: Store, host: str, port: int,
                 sending_app="HL7MW", sending_facility="MIDDLEWARE",
                 monitor: DeviceMonitor | None = None):
        self.store = store
        self.host, self.port = host, port
        self.sending_app, self.sending_facility = sending_app, sending_facility
        self.monitor = monitor
        self._server: mllp.MllpServer | None = None

    def _handle(self, message: str) -> str:
        try:
            result = hl7.parse_result(message)
        except hl7.Hl7Error as e:
            LOG.warning("Risultato non valido rifiutato: %s", e)
            return hl7.build_ack(message, "AR", str(e), self.sending_app, self.sending_facility)

        key = result["sample_key"]
        order = self.store.get_order(key) if key else None

        # Registra heartbeat dello strumento
        device_name = result.get("sending_application", "UNKNOWN")
        if self.monitor:
            self.monitor.record_message(device_name)

        if not order:
            self.store.add_unmatched(result)
            self.store.audit_log("result_unmatched", sample_key=key,
                                instrument=device_name,
                                details=f"No matching order")
            LOG.warning("Risultato senza ordine corrispondente: sample=%s -> unmatched", key)
            return hl7.build_ack(message, "AA", "", self.sending_app, self.sending_facility)

        self.store.add_result(key, result)
        timing = self.store.get_timing(key)
        if not timing or not timing.get("first_result_at"):
            self.store.record_timing(key, "first_result")

        self.store.audit_log("result_received", sample_key=key,
                            instrument=device_name,
                            details=f"{len(result['results'])} analytes")

        try_complete(self.store, key)
        LOG.info("Risultato associato all'ordine: sample=%s (%d analiti)",
                 key, len(result["results"]))
        return hl7.build_ack(message, "AA", "", self.sending_app, self.sending_facility)

    def start(self):
        self._server = mllp.MllpServer(self.host, self.port, self._handle).start()
        LOG.info("ResultReceiver in ascolto su %s:%s (risultati dagli strumenti)", self.host, self.port)
        return self

    def stop(self):
        if self._server:
            self._server.stop()


def try_complete(store: Store, sample_key: str) -> None:
    """Regola di completezza (adattare). Qui: ordine + >=1 risultato => READY."""
    order = store.get_order(sample_key)
    if not order:
        return
    if order["status"] in ("READY", "FORWARDING", "SENT"):
        return
    if store.results_for(sample_key):
        store.set_status(sample_key, "READY")
        store.record_timing(sample_key, "ready")
        store.audit_log("order_ready", sample_key=sample_key,
                       details="All required results received")
        LOG.info("Ordine pronto per l'inoltro: sample=%s", sample_key)
    # senza risultati l'ordine resta nello stato corrente (RECEIVED): in attesa.


# --------------------------------------------------------------------------- 3) inoltro al LIS
class Forwarder:
    """Prende gli ordini READY, costruisce l'ORU e lo invia al LIS gestendo l'ACK."""
    def __init__(self, store: Store, lis_host: str, lis_port: int,
                 oru_cfg: hl7.OruConfig | None = None,
                 connect_timeout: float = 10.0, read_timeout: float = 30.0,
                 ack_retry_attempts: int = 0,
                 ack_retry_backoff_seconds: float = 0.0):
        self.store = store
        self.lis_host, self.lis_port = lis_host, lis_port
        self.cfg = oru_cfg or hl7.OruConfig()
        self.connect_timeout, self.read_timeout = connect_timeout, read_timeout
        self.ack_retry_attempts = max(0, int(ack_retry_attempts))
        self.ack_retry_backoff_seconds = max(0.0, float(ack_retry_backoff_seconds))

    def forward_ready(self) -> dict:
        counts = {"sent": 0, "error": 0, "skipped": 0}
        for order in self.store.orders_by_status("READY"):
            key = order["sample_key"]
            results = self.store.results_for(key)
            analytes = [a for r in results for a in r.get("results", [])]
            import json as _json
            order_doc = _json.loads(order["order_json"])
            try:
                message, cid = hl7.build_oru(order_doc, analytes, self.cfg)
            except hl7.Hl7Error as e:
                self.store.set_status(key, "ERROR", f"build: {e}")
                counts["error"] += 1
                continue
            self.store.set_status(key, "FORWARDING")
            try:
                code = ""
                for attempt in range(self.ack_retry_attempts + 1):
                    try:
                        code = mllp.send_message(self.lis_host, self.lis_port, message,
                                                 expected_control_id=cid,
                                                 connect_timeout=self.connect_timeout,
                                                 read_timeout=self.read_timeout)
                        break
                    except mllp.MllpError:
                        if attempt >= self.ack_retry_attempts:
                            raise
                        delay = self.ack_retry_backoff_seconds * (2 ** attempt)
                        LOG.warning("Inoltro sample=%s fallito (tentativo %d/%d), ritento tra %.2fs.",
                                    key, attempt + 1, self.ack_retry_attempts + 1, delay)
                        if delay > 0:
                            time.sleep(delay)
            except mllp.MllpError as e:
                # transitorio: torna READY, ritentabile
                self.store.set_status(key, "READY", f"transient: {e}")
                counts["skipped"] += 1
                LOG.warning("Inoltro sample=%s fallito (ritentabile): %s", key, e)
                continue
            except mllp.AckError as e:
                self.store.set_status(key, "ERROR", f"nack {e.code}: {e.text}")
                counts["error"] += 1
                LOG.error("Inoltro sample=%s rifiutato dal LIS: %s", key, e)
                continue
            self.store.set_status(key, "SENT")
            self.store.record_timing(key, "sent")
            self.store.audit_log("order_sent", sample_key=key, details=f"ACK {code} from LIS")
            counts["sent"] += 1
            LOG.info("Inoltrato al LIS sample=%s (ACK %s)", key, code)
        return counts
