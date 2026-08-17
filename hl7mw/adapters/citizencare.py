"""
hl7mw.adapters.citizencare — Adapter per Citizen Care Connect (CCHS), fornitore
esterno che riceve ordini via HL7 e restituisce i risultati.

Riferimento: "Citizen Care Connect — HL7 Specifications v1.0" (Citizen Care Health
Solutions, 16-Sep-2025). Il ruolo di CCHS nel flusso è quello di un laboratorio/
strumento esterno raggiunto via VPN site-to-site (vedi hl7mw.vpn):

    LIS ──ORM/OML──▶ hl7mw (OrderReceiver, invariato)
    hl7mw ──ADT^A04 + ORM^O01──▶ CCHS   (questo adapter: CitizenCareForwarder)
    hl7mw ◀──ORU^R01── CCHS             (questo adapter: CitizenCareResultReceiver)
    hl7mw ──ORU^R01──▶ LIS   (Forwarder esistente, invariato)

CCHS gioca quindi lo stesso ruolo di uno strumento: gli ordini RECEIVED vengono
inoltrati (ADT poi ORM), e l'ORU di ritorno rientra nella pipeline standard
(store.add_result + pipeline.try_complete), che porta l'ordine a READY per poi
essere inoltrato al LIS come per qualunque altro risultato.

Messaggi HL7 v2.5 (§2.3 della spec). Matching sample_key basato su placer/filler
order number che il middleware stesso assegna nell'ORM^O01 inviato a CCHS: CCHS è
tenuto a restituirli in ORC/OBR dell'ORU^R01 (prassi HL7 standard, vedi §5.2 —
flusso di validazione ADT→ORM→ORU).

Solo stdlib. Nessuna dipendenza esterna.
"""
from __future__ import annotations

import json
import logging
import time

from .. import hl7
from .. import mllp
from ..monitor import DeviceMonitor
from ..pipeline import try_complete
from ..store import Store

LOG = logging.getLogger("hl7mw")

CITIZENCARE_INSTRUMENT = "CITIZENCARE"


class CitizenCareConfig:
    """Identità HL7 usata verso Citizen Care Connect (MSH-3..6)."""

    def __init__(self, sending_app: str = "HL7MW", sending_facility: str = "MIDDLEWARE",
                 receiving_app: str = "CCHS", receiving_facility: str = "CITIZENCARE",
                 processing_id: str = "P", hl7_version: str = "2.5"):
        self.sending_app = sending_app
        self.sending_facility = sending_facility
        self.receiving_app = receiving_app
        self.receiving_facility = receiving_facility
        self.processing_id = processing_id
        self.hl7_version = hl7_version


# --------------------------------------------------------------------------- outbound (ADT/ORM verso CCHS)
def build_adt_a04(order: dict, cfg: CitizenCareConfig) -> tuple[str, str]:
    """ADT^A04 — registrazione paziente, da inviare a CCHS prima dell'ordine (spec §4.1)."""
    patient = order.get("patient") or {}
    cid = hl7.control_id(order.get("sample_key"))
    ts = hl7.now_ts()
    FLD, CMP, SEG = hl7.FLD, hl7.CMP, hl7.SEG

    msh = (f"MSH{FLD}{hl7.ENCODING_CHARS}{FLD}{cfg.sending_app}{FLD}{cfg.sending_facility}{FLD}"
           f"{cfg.receiving_app}{FLD}{cfg.receiving_facility}{FLD}{ts}{FLD}{FLD}"
           f"ADT{CMP}A04{FLD}{cid}{FLD}{cfg.processing_id}{FLD}{cfg.hl7_version}")
    evn = f"EVN{FLD}A04{FLD}{ts}"
    pid = (f"PID{FLD}1{FLD}{FLD}{hl7.esc_text(patient.get('id'))}{FLD}{FLD}"
           f"{hl7.esc_text(patient.get('last_name'))}{CMP}{hl7.esc_text(patient.get('first_name'))}"
           f"{FLD}{FLD}{hl7.to_ts(patient.get('birth_date'))}{FLD}{hl7.esc_text(patient.get('sex', ''))}")
    pv1 = f"PV1{FLD}1{FLD}O"
    return SEG.join([msh, evn, pid, pv1]) + SEG, cid


def build_orm_o01(order: dict, cfg: CitizenCareConfig) -> tuple[str, str]:
    """ORM^O01 — ordine, da inviare a CCHS dopo l'ADT^A04 (spec §4.2).

    Placer/filler order number sono quelli assegnati dal LIS originale: CCHS deve
    restiturli nell'ORU^R01 di risposta, che e' cosi' riassociabile alla sample_key.
    """
    patient = order.get("patient") or {}
    usi = order.get("universal_service_id") or {}
    cid = hl7.control_id(order.get("filler_order_number") or order.get("sample_key"))
    ts = hl7.now_ts()
    FLD, CMP, SEG = hl7.FLD, hl7.CMP, hl7.SEG
    placer = hl7.esc_text(order.get("placer_order_number", ""))
    filler = hl7.esc_text(order.get("filler_order_number", ""))

    msh = (f"MSH{FLD}{hl7.ENCODING_CHARS}{FLD}{cfg.sending_app}{FLD}{cfg.sending_facility}{FLD}"
           f"{cfg.receiving_app}{FLD}{cfg.receiving_facility}{FLD}{ts}{FLD}{FLD}"
           f"ORM{CMP}O01{FLD}{cid}{FLD}{cfg.processing_id}{FLD}{cfg.hl7_version}")
    pid = (f"PID{FLD}1{FLD}{FLD}{hl7.esc_text(patient.get('id'))}{FLD}{FLD}"
           f"{hl7.esc_text(patient.get('last_name'))}{CMP}{hl7.esc_text(patient.get('first_name'))}"
           f"{FLD}{FLD}{hl7.to_ts(patient.get('birth_date'))}{FLD}{hl7.esc_text(patient.get('sex', ''))}")
    orc = f"ORC{FLD}NW{FLD}{placer}{FLD}{filler}{FLD}{FLD}{FLD}{FLD}{FLD}{FLD}{ts}"
    usi_field = (f"{hl7.esc_text(usi.get('code', ''))}{CMP}{hl7.esc_text(usi.get('text', ''))}"
                 f"{CMP}{hl7.esc_text(usi.get('system', '') or 'L')}")
    obr = (f"OBR{FLD}1{FLD}{placer}{FLD}{filler}{FLD}{usi_field}{FLD}{FLD}"
           f"{hl7.to_ts(order.get('requested_datetime')) or ts}")
    return SEG.join([msh, pid, orc, obr]) + SEG, cid


class CitizenCareForwarder:
    """Inoltra a Citizen Care Connect gli ordini RECEIVED come ADT^A04 + ORM^O01.

    CCHS gioca il ruolo dello strumento: dopo l'invio l'ordine passa a
    SENT_TO_CCHS (in attesa dell'ORU^R01 di risposta, gestito da
    CitizenCareResultReceiver). Errori di rete/VPN sono transitori: l'ordine resta
    RECEIVED e viene ritentato al giro successivo.
    """

    def __init__(self, store: Store, host: str, port: int,
                 cfg: CitizenCareConfig | None = None,
                 connect_timeout: float = 10.0, read_timeout: float = 30.0,
                 ack_retry_attempts: int = 0, ack_retry_backoff_seconds: float = 0.0):
        self.store = store
        self.host, self.port = host, port
        self.cfg = cfg or CitizenCareConfig()
        self.connect_timeout, self.read_timeout = connect_timeout, read_timeout
        self.ack_retry_attempts = max(0, int(ack_retry_attempts))
        self.ack_retry_backoff_seconds = max(0.0, float(ack_retry_backoff_seconds))

    def _send_with_retry(self, message: str, cid: str) -> str:
        for attempt in range(self.ack_retry_attempts + 1):
            try:
                return mllp.send_message(self.host, self.port, message, expected_control_id=cid,
                                         connect_timeout=self.connect_timeout,
                                         read_timeout=self.read_timeout)
            except mllp.MllpError:
                if attempt >= self.ack_retry_attempts:
                    raise
                delay = self.ack_retry_backoff_seconds * (2 ** attempt)
                LOG.warning("CitizenCare: invio fallito (tentativo %d/%d), ritento tra %.2fs.",
                            attempt + 1, self.ack_retry_attempts + 1, delay)
                if delay > 0:
                    time.sleep(delay)

    def forward_new_orders(self) -> dict:
        counts = {"sent": 0, "error": 0, "skipped": 0}
        for order in self.store.orders_by_status("RECEIVED"):
            key = order["sample_key"]
            order_doc = json.loads(order["order_json"])
            adt_msg, adt_cid = build_adt_a04(order_doc, self.cfg)
            orm_msg, orm_cid = build_orm_o01(order_doc, self.cfg)
            try:
                self._send_with_retry(adt_msg, adt_cid)
                self._send_with_retry(orm_msg, orm_cid)
            except mllp.MllpError as e:
                counts["skipped"] += 1
                LOG.warning("CitizenCare: invio ADT/ORM fallito per sample=%s (ritentabile, "
                            "verificare tunnel VPN): %s", key, e)
                self.store.audit_log("citizencare_send_retry", sample_key=key,
                                    details=str(e), severity="WARNING")
                continue
            except mllp.AckError as e:
                self.store.set_status(key, "ERROR", f"CCHS nack {e.code}: {e.text}")
                self.store.audit_log("citizencare_rejected", sample_key=key,
                                    details=f"{e.code}: {e.text}", severity="ERROR")
                counts["error"] += 1
                LOG.error("CitizenCare: ordine sample=%s rifiutato: %s", key, e)
                continue
            self.store.set_status(key, "SENT_TO_CCHS")
            self.store.audit_log("citizencare_order_sent", sample_key=key,
                                details="ADT^A04 + ORM^O01 inviati a Citizen Care Connect")
            counts["sent"] += 1
            LOG.info("CitizenCare: ordine inoltrato a CCHS sample=%s", key)
        return counts


# --------------------------------------------------------------------------- inbound (ORU da CCHS)
class CitizenCareResultReceiver:
    """Riceve ORU^R01 da Citizen Care Connect (risultati dei test inoltrati a CCHS).

    Riusa il parser HL7 generico (hl7.parse_result): CCHS dichiara HL7 v2.5
    standard (spec §2.3). Il matching avviene per sample_key (placer/filler order
    number, gli stessi assegnati nell'ORM^O01 inviato da CitizenCareForwarder).
    """

    def __init__(self, store: Store, host: str, port: int,
                 sending_app: str = "HL7MW", sending_facility: str = "MIDDLEWARE",
                 monitor: DeviceMonitor | None = None,
                 instrument_name: str = CITIZENCARE_INSTRUMENT):
        self.store = store
        self.host, self.port = host, port
        self.sending_app, self.sending_facility = sending_app, sending_facility
        self.monitor = monitor
        self.instrument_name = instrument_name
        self._server: mllp.MllpServer | None = None

    def _handle(self, message: str) -> str:
        try:
            result = hl7.parse_result(message)
        except hl7.Hl7Error as e:
            LOG.warning("CitizenCare: risultato non valido rifiutato: %s", e)
            return hl7.build_ack(message, "AR", str(e), self.sending_app, self.sending_facility)

        if self.monitor:
            self.monitor.record_message(self.instrument_name, self.host, self.port, "CCHS")

        # Il matching riparte da sample_key, ma CCHS potrebbe echeggiare solo
        # placer/filler order number (non necessariamente uno specimen barcode):
        # find_order() fa fallback sulle colonne filler/placer se serve.
        key = result["sample_key"]
        order = self.store.find_order(key, result.get("filler_order_number", ""),
                                      result.get("placer_order_number", ""))
        if order:
            key = order["sample_key"]

        if not order:
            self.store.add_unmatched(result, source_instrument=self.instrument_name)
            self.store.audit_log("citizencare_result_unmatched", sample_key=key,
                                instrument=self.instrument_name,
                                details="Nessun ordine corrispondente", severity="WARNING")
            LOG.warning("CitizenCare: risultato senza ordine corrispondente: sample=%s -> unmatched", key)
            return hl7.build_ack(message, "AA", "", self.sending_app, self.sending_facility)

        self.store.add_result(key, result, source_instrument=self.instrument_name)
        timing = self.store.get_timing(key)
        if not timing or not timing.get("first_result_at"):
            self.store.record_timing(key, "first_result")
        self.store.audit_log("citizencare_result_received", sample_key=key,
                            instrument=self.instrument_name,
                            details=f"{len(result['results'])} analiti")
        try_complete(self.store, key)
        LOG.info("CitizenCare: risultato associato all'ordine sample=%s (%d analiti)",
                 key, len(result["results"]))
        return hl7.build_ack(message, "AA", "", self.sending_app, self.sending_facility)

    def start(self) -> "CitizenCareResultReceiver":
        self._server = mllp.MllpServer(self.host, self.port, self._handle).start()
        LOG.info("CitizenCareResultReceiver in ascolto su %s:%s (ORU^R01 da Citizen Care Connect)",
                 self.host, self.port)
        return self

    def stop(self) -> None:
        if self._server:
            self._server.stop()
