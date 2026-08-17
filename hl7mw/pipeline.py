"""
hl7mw.pipeline — i tre componenti del middleware order-driven.

1) OrderReceiver   server MLLP: riceve ORM/OML (+ADT) dal LIS -> salva ordine -> ACK
2) ResultReceiver  server MLLP: riceve ORU/OUL dagli strumenti -> associa all'ordine
3) Forwarder       prende gli ordini completi -> ORU -> invia al LIS -> ACK -> SENT

I due receiver condividono `InboundChannel`, che concentra tutto cio' che lo
standard richiede a un ricevente e che prima era duplicato (o mancante):

  - payload multi-messaggio e batch FHS/BHS  (hl7.split_messages)
  - modalita' di riscontro original/enhanced (hl7mw/ack.py)
  - NACK diagnosticabili con segmento ERR    (tabella HL7 0357)
  - idempotenza sulle ritrasmissioni         (store.processed_messages)
  - tracciamento del traffico                (store.message_log)

Le sottoclassi implementano solo `process()`: cosa fare del messaggio.

L'associazione avviene per 'sample_key' (specimen/barcode, poi filler/placer).
La regola di completezza dell'ordine e' volutamente semplice e va adattata: qui un
ordine e' "completo" appena arriva almeno un risultato per la sua chiave. In produzione
si confronta l'elenco dei test richiesti (universal_service_id / OBR) con quelli ricevuti.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from contextlib import contextmanager

from . import ack as ackmod
from . import hl7, mllp
from .store import Store
from .monitor import DeviceMonitor

LOG = logging.getLogger("hl7mw")

# Modalita' di risposta agli ordini (config `order_response_mode`).
RESPONSE_ACK = "ack"          # ACK^O01^ACK generico (default, comportamento storico)
RESPONSE_ORDER = "order"      # ORR^O02 (per ORM) / ORL^O22 (per OML)


class _KeyedLocks:
    """Lock per chiave, con conteggio dei detentori.

    Serve a rendere atomica la sequenza "controlla se e' un duplicato →
    elabora → registra": senza, due copie identiche che arrivano insieme su due
    connessioni diverse passerebbero entrambe dal controllo prima che l'una
    registri, e il risultato clinico verrebbe inserito due volte. I listener
    vivono tutti in questo processo, quindi un lock in memoria e' sufficiente.
    """

    def __init__(self):
        self._guard = threading.Lock()
        self._locks: dict[tuple, tuple[threading.Lock, int]] = {}

    @contextmanager
    def acquire(self, key: tuple):
        with self._guard:
            lock, holders = self._locks.get(key, (threading.Lock(), 0))
            self._locks[key] = (lock, holders + 1)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._guard:
                lock, holders = self._locks[key]
                if holders <= 1:
                    del self._locks[key]        # niente crescita illimitata
                else:
                    self._locks[key] = (lock, holders - 1)


# --------------------------------------------------------------------------- canale in ingresso
class InboundChannel:
    """Server MLLP con la logica di riscontro comune a tutti i canali in ingresso."""

    channel = "inbound"

    def __init__(self, store: Store, host: str, port: int,
                 sending_app: str = "HL7MW", sending_facility: str = "MIDDLEWARE",
                 monitor: DeviceMonitor | None = None, *,
                 ack_mode: str = ackmod.MODE_AUTO, include_err: bool = True,
                 dedup: bool = True, read_timeout: float = 60.0,
                 idle_timeout: float = 300.0, max_connections: int = 64):
        self.store = store
        self.host, self.port = host, port
        self.sending_app, self.sending_facility = sending_app, sending_facility
        self.monitor = monitor
        self.dedup = dedup
        self.read_timeout, self.idle_timeout = read_timeout, idle_timeout
        self.max_connections = max_connections
        self.policy = ackmod.AckPolicy(sending_app=sending_app,
                                       sending_facility=sending_facility,
                                       mode=ack_mode, include_err=include_err)
        self._locks = _KeyedLocks()
        self._server: mllp.MllpServer | None = None

    # ----- da implementare nelle sottoclassi -----
    def process(self, message: str, header: hl7.MessageHeader) -> ackmod.AckOutcome:
        raise NotImplementedError

    # ----- ciclo di vita -----
    def start(self):
        self._server = mllp.MllpServer(self.host, self.port, self._handle,
                                       read_timeout=self.read_timeout,
                                       idle_timeout=self.idle_timeout,
                                       max_connections=self.max_connections).start()
        LOG.info("%s in ascolto su %s:%s (canale %s)", type(self).__name__,
                 self.host, self._server.bound_port, self.channel)
        return self

    def stop(self):
        if self._server:
            self._server.stop()

    # ----- gestione messaggio -----
    def _handle(self, payload: str) -> list[str]:
        """Callback del server MLLP. Un blocco puo' contenere piu' messaggi
        (batch HL7 o MSH concatenati): a ognuno risponde il proprio ACK."""
        messages = hl7.split_messages(payload)
        if not messages:
            LOG.warning("%s: payload senza segmento MSH (%d byte) — rifiutato.",
                        type(self).__name__, len(payload))
            return self.policy.malformed(payload, "Payload privo di segmento MSH")
        if len(messages) > 1:
            LOG.info("%s: ricevuto blocco con %d messaggi (batch/concatenati).",
                     type(self).__name__, len(messages))
        replies: list[str] = []
        for m in messages:
            replies.extend(self._handle_one(m))
        return replies

    def _replay(self, message: str, header: hl7.MessageHeader,
                digest: str) -> ackmod.AckOutcome | None:
        """Riconosce una ritrasmissione e ne restituisce il riscontro gia' dato.

        Confronta anche l'impronta del contenuto: se lo stesso MSH-10 torna con un
        messaggio DIVERSO non e' una ritrasmissione ma un riuso dell'identificativo
        (difetto del mittente). In quel caso il messaggio viene elaborato — perdere
        un risultato clinico e' peggio che accettarne uno con id ripetuto — e
        l'anomalia finisce in audit.
        """
        prev = self.store.find_processed(header.sending_app, header.control_id)
        if not prev:
            return None
        if prev.get("payload_hash") and prev["payload_hash"] != digest:
            LOG.warning("%s: control id %s riusato da %s con contenuto diverso — "
                        "elaborato comunque (MSH-10 dovrebbe essere univoco).",
                        type(self).__name__, header.control_id, header.sending_app or "?")
            self.store.audit_log("message_control_id_reuse",
                                 details=f"{header.message_type} control_id={header.control_id}",
                                 severity="WARNING")
            return None
        self.store.bump_processed(header.sending_app, header.control_id)
        LOG.warning("%s: messaggio duplicato (control id %s da %s) — ACK ripetuto, "
                    "nessuna rielaborazione.", type(self).__name__,
                    header.control_id, header.sending_app or "?")
        self.store.audit_log("message_duplicate", sample_key=prev.get("sample_key") or None,
                             details=f"{header.message_type} control_id={header.control_id}",
                             severity="WARNING")
        return ackmod.AckOutcome(code=prev.get("ack_code") or "AA",
                                 body=prev.get("ack_raw") or None,
                                 sample_key=prev.get("sample_key") or "",
                                 duplicate=True)

    def _process_or_replay(self, message: str, header: hl7.MessageHeader,
                           digest: str) -> tuple[ackmod.AckOutcome, list[str]]:
        """Elabora il messaggio (o ne ripete il riscontro se e' un duplicato) e
        costruisce le risposte. Chiamata sotto lock per chiave di deduplica."""
        outcome = self._replay(message, header, digest) if (
            self.dedup and header.control_id) else None
        if outcome is None:
            try:
                outcome = self.process(message, header)
            except hl7.Hl7Error as e:
                LOG.warning("%s: messaggio rifiutato: %s", type(self).__name__, e)
                outcome = ackmod.AckOutcome.from_hl7_error(e)
                outcome.body = self.error_response(message, header, outcome)
        replies = self.policy.responses(message, header, outcome)
        if self.dedup and not outcome.duplicate and header.control_id:
            application = replies[-1] if replies else ""
            self.store.remember_processed(header.sending_app, header.control_id,
                                          header.message_type, outcome.sample_key or None,
                                          outcome.code, application, digest)
        return outcome, replies

    def error_response(self, message: str, header: hl7.MessageHeader,
                       outcome: ackmod.AckOutcome) -> str | None:
        """Risposta applicativa alternativa all'ACK in caso di rifiuto.
        None = ACK/NACK generico. Ridefinita da OrderReceiver."""
        return None

    def _handle_one(self, message: str) -> list[str]:
        started = time.monotonic()
        header = hl7.parse_header(message)
        peer = mllp.current_peer()
        digest = hashlib.sha256(message.encode("utf-8", errors="replace")).hexdigest()

        if not header.message_code:
            LOG.warning("%s: MSH illeggibile o incompleto — rifiutato.", type(self).__name__)
            outcome = ackmod.AckOutcome(code="AR", text="MSH incompleto o illeggibile",
                                        error_code="101")
            replies = self.policy.responses(message, header, outcome)
        elif self.dedup and header.control_id:
            # Sotto lock: due copie identiche arrivate insieme su connessioni
            # diverse non devono poter superare entrambe il controllo duplicati.
            with self._locks.acquire((header.sending_app, header.control_id)):
                outcome, replies = self._process_or_replay(message, header, digest)
        else:
            outcome, replies = self._process_or_replay(message, header, digest)
        plan = self.policy.plan(header, outcome.ok)
        self.store.log_message(
            "IN", channel=self.channel, peer=peer,
            sending_app=header.sending_app, sending_facility=header.sending_facility,
            receiving_app=header.receiving_app, message_type=header.message_type,
            control_id=header.control_id, version=header.version,
            processing_id=header.processing_id, sample_key=outcome.sample_key or None,
            ack_code=outcome.code, ack_text=outcome.text, error_code=outcome.error_code,
            ack_mode=ackmod.MODE_ENHANCED if plan.commit else ackmod.MODE_ORIGINAL,
            duplicate=outcome.duplicate,
            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
        )
        return replies


# --------------------------------------------------------------------------- 1) ordini dal LIS
class OrderReceiver(InboundChannel):
    channel = "orders"

    def __init__(self, store: Store, host: str, port: int,
                 sending_app="HL7MW", sending_facility="MIDDLEWARE",
                 monitor: DeviceMonitor | None = None, *,
                 order_response_mode: str = RESPONSE_ACK, **kwargs):
        super().__init__(store, host, port, sending_app, sending_facility, monitor, **kwargs)
        # "ack" (default) oppure "order": risposta applicativa ORR^O02 / ORL^O22.
        self.order_response_mode = (order_response_mode or RESPONSE_ACK).lower()

    def process(self, message: str, header: hl7.MessageHeader) -> ackmod.AckOutcome:
        if header.is_adt:
            return self._handle_adt(message, header)
        if not header.is_order:
            raise hl7.Hl7Error(
                f"Tipo messaggio non gestito su questo canale: {header.message_type!r}",
                error_code="200")

        order = hl7.parse_order(message)
        if not order["sample_key"]:
            raise hl7.Hl7Error("Nessun identificativo campione/ordine (SPM-2/ORC-2/ORC-3)",
                               error_code="101")

        self.store.upsert_order(order)
        self.store.record_timing(order["sample_key"], "received")
        self.store.audit_log("order_received", sample_key=order["sample_key"],
                             details=f"Test: {order['universal_service_id'].get('text')}")
        # se erano gia' arrivati risultati prima dell'ordine, riconcilia
        try_complete(self.store, order["sample_key"])
        LOG.info("Ordine ricevuto dal LIS: sample=%s test=%s",
                 order["sample_key"], order["universal_service_id"].get("text"))

        outcome = ackmod.AckOutcome(code="AA", sample_key=order["sample_key"])
        if self.order_response_mode == RESPONSE_ORDER:
            outcome.body = hl7.build_order_response(
                message, order, accepted=True, sending_app=self.sending_app,
                sending_facility=self.sending_facility, header=header)
        return outcome

    def error_response(self, message: str, header: hl7.MessageHeader,
                       outcome: ackmod.AckOutcome) -> str | None:
        """In modalita' "order" anche il rifiuto di un ordine deve arrivare come
        risposta d'ordine (ORC-1 = UA, "unable to accept"), non come ACK generico:
        altrimenti il LIS riceve un formato diverso da quello concordato proprio
        nel caso in cui deve capire cosa non ha funzionato."""
        if self.order_response_mode != RESPONSE_ORDER or not header.is_order:
            return None
        return hl7.build_order_response(
            message, None, accepted=False, text=outcome.text,
            sending_app=self.sending_app, sending_facility=self.sending_facility,
            error_code=outcome.error_code if self.policy.include_err else "",
            header=header, ack_code=outcome.code)

    def _handle_adt(self, message: str, header: hl7.MessageHeader) -> ackmod.AckOutcome:
        """ADT^A0x (es. A04 registrazione paziente): alcuni LIS lo inviano prima
        dell'ordine vero e proprio (es. Citizen Care Connect, spec CCHS §4.1). Non
        crea un ordine: l'ORM^O01 che segue porta gia' i dati paziente necessari
        (vedi hl7.parse_order/_patient). Qui riscontriamo solo con ACK positivo,
        cosi' il mittente non considera fallita la registrazione."""
        adt = hl7.parse_adt(message)
        # Niente identificativi paziente in log/audit (SECURITY_PRIVACY.md: "no PHI
        # nei technical logs") — solo il tipo di evento, coerente con l'audit degli
        # ordini (order_received) che logga sample_key ma mai i dati del paziente.
        self.store.audit_log("patient_registered", details=f"ADT {adt['event_type']}")
        LOG.info("Registrazione paziente ricevuta dal LIS: evento=%s", adt["event_type"])
        return ackmod.AckOutcome(code="AA")


# --------------------------------------------------------------------------- 2) risultati dagli strumenti
class ResultReceiver(InboundChannel):
    channel = "results"

    def process(self, message: str, header: hl7.MessageHeader) -> ackmod.AckOutcome:
        if not header.is_result:
            raise hl7.Hl7Error(
                f"Tipo messaggio non gestito su questo canale: {header.message_type!r}",
                error_code="200")
        result = hl7.parse_result(message)

        key = result["sample_key"]
        order = self.store.get_order(key) if key else None

        # Registra heartbeat dello strumento (MSH-3 del messaggio ORU)
        device_name = result.get("sending_application") or "UNKNOWN"
        if self.monitor:
            self.monitor.record_message(device_name)

        if not order:
            self.store.add_unmatched(result, source_instrument=device_name)
            self.store.audit_log("result_unmatched", sample_key=key,
                                 instrument=device_name,
                                 details="No matching order")
            LOG.warning("Risultato senza ordine corrispondente: sample=%s -> unmatched", key)
            return ackmod.AckOutcome(code="AA", sample_key=key,
                                     text="Risultato accettato, ordine non ancora presente")

        self.store.add_result(key, result, source_instrument=device_name)
        timing = self.store.get_timing(key)
        if not timing or not timing.get("first_result_at"):
            self.store.record_timing(key, "first_result")

        self.store.audit_log("result_received", sample_key=key,
                             instrument=device_name,
                             details=f"{len(result['results'])} analytes")

        try_complete(self.store, key)
        LOG.info("Risultato associato all'ordine: sample=%s (%d analiti)",
                 key, len(result["results"]))
        return ackmod.AckOutcome(code="AA", sample_key=key)


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
    """Prende gli ordini READY, costruisce l'ORU e lo invia al LIS gestendo l'ACK.

    Se `ack_mode` e' "enhanced" l'ORU esce con MSH-15/MSH-16 = AL: il LIS deve
    rispondere prima con un commit ACK (CA) e poi con l'ACK applicativo, e
    l'ordine passa a SENT solo sul secondo. In "original" (default) vale il
    comportamento storico a singolo ACK; anche in quel caso un CA ricevuto non
    viene piu' scambiato per un esito definitivo (vedi mllp.MllpClient.send).
    """

    def __init__(self, store: Store, lis_host: str, lis_port: int,
                 oru_cfg: hl7.OruConfig | None = None,
                 connect_timeout: float = 10.0, read_timeout: float = 30.0,
                 ack_retry_attempts: int = 0,
                 ack_retry_backoff_seconds: float = 0.0,
                 ack_mode: str = ackmod.MODE_ORIGINAL,
                 application_ack_timeout: float | None = None):
        self.store = store
        self.lis_host, self.lis_port = lis_host, lis_port
        self.cfg = oru_cfg or hl7.OruConfig()
        self.connect_timeout, self.read_timeout = connect_timeout, read_timeout
        self.ack_retry_attempts = max(0, int(ack_retry_attempts))
        self.ack_retry_backoff_seconds = max(0.0, float(ack_retry_backoff_seconds))
        self.ack_mode = (ack_mode or ackmod.MODE_ORIGINAL).lower()
        self.application_ack_timeout = application_ack_timeout
        if self.ack_mode == ackmod.MODE_ENHANCED:
            # MSH-15/16 dell'ORU in uscita: chiediamo esplicitamente entrambi i
            # riscontri (commit + applicativo).
            self.cfg.accept_ack_type = self.cfg.accept_ack_type or ackmod.ALWAYS
            self.cfg.application_ack_type = self.cfg.application_ack_type or ackmod.ALWAYS

    def forward_ready(self) -> dict:
        counts = {"sent": 0, "error": 0, "skipped": 0}
        for order in self.store.orders_by_status("READY"):
            key = order["sample_key"]
            results = self.store.results_for(key)
            analytes = [a for r in results for a in r.get("results", [])]
            order_doc = json.loads(order["order_json"])
            try:
                message, cid = hl7.build_oru(order_doc, analytes, self.cfg)
            except hl7.Hl7Error as e:
                self.store.set_status(key, "ERROR", f"build: {e}")
                counts["error"] += 1
                continue
            self.store.set_status(key, "FORWARDING")
            started = time.monotonic()
            try:
                result = self._send_with_retry(message, cid, key)
            except mllp.MllpError as e:
                # transitorio: torna READY, ritentabile
                self.store.set_status(key, "READY", f"transient: {e}")
                counts["skipped"] += 1
                LOG.warning("Inoltro sample=%s fallito (ritentabile): %s", key, e)
                self._log_out(key, cid, "", str(e), started, transport_error=True)
                continue
            except mllp.AckError as e:
                self.store.set_status(key, "ERROR", f"nack {e.code}: {e.text}")
                counts["error"] += 1
                LOG.error("Inoltro sample=%s rifiutato dal LIS: %s", key, e)
                self._log_out(key, cid, e.code, e.text, started)
                continue
            self.store.set_status(key, "SENT")
            self.store.record_timing(key, "sent")
            self.store.audit_log("order_sent", sample_key=key,
                                 details=f"ACK {result.code} from LIS")
            counts["sent"] += 1
            LOG.info("Inoltrato al LIS sample=%s (ACK %s%s)", key, result.code,
                     f", commit {result.commit_code}" if result.commit_code else "")
            self._log_out(key, cid, result.code, result.text, started,
                          commit_code=result.commit_code)
        return counts

    def _send_with_retry(self, message: str, cid: str, key: str) -> mllp.AckResult:
        """Invia ritentando solo gli errori di trasporto (un NACK applicativo e'
        una risposta definitiva: ritentarlo produrrebbe lo stesso rifiuto)."""
        for attempt in range(self.ack_retry_attempts + 1):
            try:
                return mllp.send_message_ex(
                    self.lis_host, self.lis_port, message, expected_control_id=cid,
                    connect_timeout=self.connect_timeout, read_timeout=self.read_timeout,
                    # Attendiamo il secondo riscontro solo se l'abbiamo chiesto
                    # (MSH-16=AL): in original mode un peer che risponde CA non
                    # deve costringerci ad aspettare un ACK che non arrivera'.
                    expect_application_ack=(self.cfg.application_ack_type == ackmod.ALWAYS),
                    application_ack_timeout=self.application_ack_timeout)
            except mllp.MllpError:
                if attempt >= self.ack_retry_attempts:
                    raise
                delay = self.ack_retry_backoff_seconds * (2 ** attempt)
                LOG.warning("Inoltro sample=%s fallito (tentativo %d/%d), ritento tra %.2fs.",
                            key, attempt + 1, self.ack_retry_attempts + 1, delay)
                if delay > 0:
                    time.sleep(delay)
        raise mllp.MllpError("Tentativi di inoltro esauriti.")  # pragma: no cover

    def _log_out(self, sample_key: str, cid: str, ack_code: str, ack_text: str,
                 started: float, commit_code: str = "", transport_error: bool = False) -> None:
        self.store.log_message(
            "OUT", channel="forward", peer=f"{self.lis_host}:{self.lis_port}",
            sending_app=self.cfg.sending_app, sending_facility=self.cfg.sending_facility,
            receiving_app=self.cfg.receiving_app, message_type="ORU^R01",
            control_id=cid, version=self.cfg.hl7_version,
            processing_id=self.cfg.processing_id, sample_key=sample_key,
            ack_code=ack_code, ack_text=ack_text[:200] if ack_text else "",
            error_code="TRANSPORT" if transport_error else "",
            ack_mode=ackmod.MODE_ENHANCED if commit_code else self.ack_mode,
            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
        )
