"""
Conformita' del riscontro e della comunicazione HL7 (capitolo 2 dello standard).

Eseguibile senza pytest: `python3 tests/test_hl7_ack.py`.

Copre i punti che un LIS/strumento reale esercita e che il middleware deve
garantire:
  1  ACK ben formato (MSH-9 ACK^<trigger>^ACK, MSH-11/12 echeggiati, MSA-2)
  2  NACK diagnosticabile con segmento ERR (tabella HL7 0357)
  3  forma di ERR per versioni <= 2.3.1
  4  connessione persistente: piu' messaggi sulla stessa connessione TCP
  5  messaggi accodati nello stesso segmento TCP (pipelining)
  6  batch protocol FHS/BHS
  7  idempotenza: ritrasmissione con lo stesso MSH-10
  8  riuso improprio di MSH-10 con contenuto diverso
  9  enhanced mode in ingresso: commit ACK + ACK applicativo
 10  MSH-15/16 = NE: nessuna risposta
 11  MSH-15 = ER con esito positivo: solo ACK applicativo
 12  risposta applicativa d'ordine ORR^O02 / ORL^O22
 13  delimitatori non standard dichiarati in MSH-1/MSH-2
 14  enhanced mode in uscita: SENT solo dopo l'ACK applicativo del LIS
 15  solo commit ACK dal LIS: l'ordine resta ritentabile
 16  payload senza MSH: rifiutato con ERR
 17  finestra di deduplica misurata sull'ultima attivita' (last_seen)
 18  tetto alle connessioni simultanee del server MLLP
 19  rifiuto di un ordine in modalita' "order": ORR/ORL con ORC-1 = UA
 20  copie identiche concorrenti: elaborazione una sola volta
 21  dashboard: nessun campo HL7 interpolato senza escape (XSS memorizzato)
"""
import datetime as _dt
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hl7mw import ack as ackmod, hl7, mllp
from hl7mw.pipeline import Forwarder, OrderReceiver, ResultReceiver
from hl7mw.store import Store

CR = "\r"
TS = "20260817090000"


# --------------------------------------------------------------------------- helper
def msh(mtype="ORM^O01", ctrl="MSG001", version="2.5", processing="P",
        accept="", application="", app="LIS", facility="OSP", fld="|"):
    enc = "^~\\&"
    fields = ["MSH", enc, app, facility, "HL7MW", "MIDDLEWARE", TS, "",
              mtype, ctrl, processing, version, "", "", accept, application]
    while fields and fields[-1] == "":
        fields.pop()
    return fld.join(["MSH" if i == 0 else f for i, f in enumerate(fields)]).replace(
        "MSH" + fld + enc, "MSH" + fld + enc, 1)


def orm(ctrl="MSG001", sample="BC-1", **kw) -> str:
    return CR.join([
        msh(mtype=kw.pop("mtype", "ORM^O01"), ctrl=ctrl, **kw),
        "PID|1||PAT-1^^^OSP^MR||Rossi^Mario||19800512|M",
        "ORC|NW|PLAC-1|FILL-1",
        "OBR|1|PLAC-1|FILL-1|58410-2^Emocromo^LN|||" + TS,
        f"SPM|1|{sample}||BLD",
    ]) + CR


def oru(ctrl="RES001", sample="BC-1", value="7.2", app="ANALYZER", **kw) -> str:
    return CR.join([
        msh(mtype="ORU^R01", ctrl=ctrl, app=app, facility="LAB", **kw),
        "PID|1||PAT-1^^^OSP^MR||Rossi^Mario||19800512|M",
        "OBR|1|PLAC-1|FILL-1|58410-2^Emocromo^LN",
        f"SPM|1|{sample}||BLD",
        f"OBX|1|NM|6690-2^Leucociti^LN||{value}|10*3/uL|4.0-10.0|N|||F",
    ]) + CR


def exchange_raw(port: int, payloads, expect: int = 1, timeout: float = 3.0) -> list[str]:
    """Invia N payload sulla STESSA connessione e legge `expect` risposte."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        reader = mllp.FrameReader(s)
        for p in payloads:
            s.sendall(mllp.frame(p))
        out = []
        for _ in range(expect):
            data = reader.read(timeout)
            if data is None:
                break
            out.append(data.decode("utf-8"))
        return out


def expect_no_reply(port: int, payload: str, timeout: float = 0.6) -> bool:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(mllp.frame(payload))
        try:
            data = mllp.FrameReader(s).read(timeout)
        except mllp.MllpTimeout:
            return True
        return data is None


def field(message: str, seg_name: str, idx: int) -> str:
    m = hl7.Message(message)
    seg = m.seg(seg_name)
    if seg_name == "MSH":
        return seg[idx - 1] if seg and idx - 1 < len(seg) else ""
    return hl7.get(seg or [], idx)


def fresh_store(name: str) -> Store:
    db = f"/tmp/hl7mw_ack_{name}.db"
    Path(db).unlink(missing_ok=True)
    return Store(db)


class Check:
    def __init__(self):
        self.failed = []

    def __call__(self, label: str, condition: bool, detail: str = ""):
        if condition:
            print(f"[OK]     {label}")
        else:
            self.failed.append(label)
            print(f"[FALLITO] {label} {detail}")


ck = Check()


# --------------------------------------------------------------------------- 1-3 ACK / ERR
def test_ack_wellformed(store):
    rx = OrderReceiver(store, "127.0.0.1", 0).start()
    port = rx._server.bound_port
    try:
        reply = exchange_raw(port, [orm(ctrl="CTRL-A", processing="T")])[0]
        ck("1 ACK: MSH-9 = ACK^O01^ACK", field(reply, "MSH", 9) == "ACK^O01^ACK",
           field(reply, "MSH", 9))
        ck("1 ACK: MSH-11 echeggia il processing id del mittente (T)",
           field(reply, "MSH", 11) == "T", field(reply, "MSH", 11))
        ck("1 ACK: MSH-12 echeggia la versione (2.5)",
           field(reply, "MSH", 12) == "2.5", field(reply, "MSH", 12))
        ck("1 ACK: MSH-10 dell'ACK e' un identificativo proprio, non l'eco di MSA-2",
           field(reply, "MSH", 10) not in ("", "CTRL-A"), field(reply, "MSH", 10))
        ck("1 ACK: MSA-1 = AA, MSA-2 = MSH-10 in ingresso",
           field(reply, "MSA", 1) == "AA" and field(reply, "MSA", 2) == "CTRL-A",
           f"{field(reply, 'MSA', 1)}/{field(reply, 'MSA', 2)}")
        ck("1 ACK: mittente/destinatario invertiti",
           field(reply, "MSH", 5) == "LIS" and field(reply, "MSH", 3) == "HL7MW")

        # 2 — tipo non gestito su questo canale: NACK con ERR
        reply = exchange_raw(port, [oru(ctrl="CTRL-B")])[0]
        err = hl7.Message(reply).seg("ERR")
        ck("2 NACK: codice AR su tipo messaggio non gestito",
           field(reply, "MSA", 1) == "AR", field(reply, "MSA", 1))
        ck("2 NACK: segmento ERR presente con codice 200 (Unsupported message type)",
           bool(err) and hl7.comp(hl7.get(err, 3), 0) == "200",
           hl7.get(err or [], 3))
        ck("2 NACK: ERR-3 riporta la tabella HL70357 e ERR-4 la severity",
           bool(err) and hl7.comp(hl7.get(err, 3), 2) == "HL70357"
           and hl7.get(err, 4) == "E", hl7.get(err or [], 4))

        # ordine senza identificativo campione -> 101 (Required field missing)
        bad = CR.join([msh(ctrl="CTRL-C"), "ORC|NW||", "OBR|1|||58410-2^Emocromo^LN"]) + CR
        reply = exchange_raw(port, [bad])[0]
        err = hl7.Message(reply).seg("ERR")
        ck("2 NACK: ordine senza identificativo campione -> ERR 101",
           bool(err) and hl7.comp(hl7.get(err, 3), 0) == "101", hl7.get(err or [], 3))
    finally:
        rx.stop()


def test_err_legacy_version():
    old = hl7.build_ack(CR.join([msh(version="2.3.1", ctrl="X1")]) + CR, "AR",
                        "tipo non gestito", error_code="200")
    err = hl7.Message(old).seg("ERR")
    ck("3 ERR: su HL7 2.3.1 l'errore va in ERR-1 (ELD), non in ERR-3",
       bool(err) and hl7.get(err, 1).startswith("^^^200")
       and len(err) == 2, hl7.get(err or [], 1))
    ck("3 ERR: la versione dell'ACK segue quella del mittente (2.3.1)",
       field(old, "MSH", 12) == "2.3.1", field(old, "MSH", 12))


# --------------------------------------------------------------------------- 4-6 trasporto
def test_persistent_connection(store):
    rx = OrderReceiver(store, "127.0.0.1", 0).start()
    port = rx._server.bound_port
    try:
        replies = exchange_raw(port, [orm(ctrl="P1", sample="BC-P1"),
                                      orm(ctrl="P2", sample="BC-P2")], expect=2)
        ck("4 Connessione persistente: due messaggi, due ACK sulla stessa connessione",
           len(replies) == 2 and all(field(r, "MSA", 1) == "AA" for r in replies),
           str(len(replies)))
        ck("4 Connessione persistente: entrambi gli ordini salvati",
           bool(store.get_order("BC-P1")) and bool(store.get_order("BC-P2")))
        ck("4 Connessione persistente: MSA-2 corrisponde al messaggio giusto",
           field(replies[0], "MSA", 2) == "P1" and field(replies[1], "MSA", 2) == "P2")

        # 5 — due messaggi in un unico segmento TCP (pipelining)
        with socket.create_connection(("127.0.0.1", port), timeout=3.0) as s:
            s.sendall(mllp.frame(orm(ctrl="Q1", sample="BC-Q1"))
                      + mllp.frame(orm(ctrl="Q2", sample="BC-Q2")))
            reader = mllp.FrameReader(s)
            got = [reader.read(3.0).decode(), reader.read(3.0).decode()]
        ck("5 Pipelining: due messaggi in un solo segmento TCP ricevono due ACK",
           [field(g, "MSA", 2) for g in got] == ["Q1", "Q2"],
           str([field(g, "MSA", 2) for g in got]))
        ck("5 Pipelining: nessun ordine perso",
           bool(store.get_order("BC-Q1")) and bool(store.get_order("BC-Q2")))
    finally:
        rx.stop()


def test_batch(store):
    rx = ResultReceiver(store, "127.0.0.1", 0).start()
    port = rx._server.bound_port
    try:
        store.upsert_order({"sample_key": "BC-B1", "patient": {}, "universal_service_id": {}})
        store.upsert_order({"sample_key": "BC-B2", "patient": {}, "universal_service_id": {}})
        batch = ("FHS|^~\\&|LIS|OSP|HL7MW|MIDDLEWARE|" + TS + CR
                 + "BHS|^~\\&|LIS|OSP|HL7MW|MIDDLEWARE|" + TS + CR
                 + oru(ctrl="B1", sample="BC-B1")
                 + oru(ctrl="B2", sample="BC-B2")
                 + "BTS|2" + CR + "FTS|1" + CR)
        replies = exchange_raw(port, [batch], expect=2)
        ck("6 Batch FHS/BHS: un ACK per ogni messaggio contenuto",
           [field(r, "MSA", 2) for r in replies] == ["B1", "B2"],
           str([field(r, "MSA", 2) for r in replies]))
        ck("6 Batch FHS/BHS: entrambi i risultati associati agli ordini",
           len(store.results_for("BC-B1")) == 1 and len(store.results_for("BC-B2")) == 1)
        ck("6 Batch: split_messages scarta gli involucri FHS/BHS/BTS/FTS",
           len(hl7.split_messages(batch)) == 2)
    finally:
        rx.stop()


# --------------------------------------------------------------------------- 7-8 idempotenza
def test_deduplication(store):
    rx = ResultReceiver(store, "127.0.0.1", 0).start()
    port = rx._server.bound_port
    try:
        store.upsert_order({"sample_key": "BC-D1", "patient": {}, "universal_service_id": {}})
        message = oru(ctrl="DUP-1", sample="BC-D1")
        first = exchange_raw(port, [message])[0]
        second = exchange_raw(port, [message])[0]
        ck("7 Idempotenza: la ritrasmissione non duplica il risultato",
           len(store.results_for("BC-D1")) == 1, str(len(store.results_for("BC-D1"))))
        ck("7 Idempotenza: alla ritrasmissione si ripete lo stesso ACK",
           field(second, "MSA", 1) == "AA" and field(second, "MSA", 2) == "DUP-1"
           and hl7.Message(second).seg("MSA") == hl7.Message(first).seg("MSA"))
        prev = store.find_processed("ANALYZER", "DUP-1")
        ck("7 Idempotenza: il control id e' contato come ritrasmesso (hits=2)",
           bool(prev) and prev["hits"] == 2, str(prev and prev["hits"]))
        dup_rows = [m for m in store.get_messages(limit=10) if m["duplicate"]]
        ck("7 Idempotenza: il duplicato e' tracciato nel message log", len(dup_rows) == 1)
        ck("7 Idempotenza: l'evento finisce in audit log",
           any(a["event_type"] == "message_duplicate" for a in store.get_audit_log(20)))

        # 8 — stesso control id, contenuto diverso: NON e' una ritrasmissione
        exchange_raw(port, [oru(ctrl="DUP-1", sample="BC-D1", value="9.9")])
        ck("8 Riuso di MSH-10 con contenuto diverso: il messaggio viene elaborato",
           len(store.results_for("BC-D1")) == 2, str(len(store.results_for("BC-D1"))))
        ck("8 Riuso di MSH-10: anomalia registrata in audit log",
           any(a["event_type"] == "message_control_id_reuse" for a in store.get_audit_log(20)))
    finally:
        rx.stop()


# --------------------------------------------------------------------------- 9-11 enhanced mode
def test_enhanced_mode(store):
    rx = OrderReceiver(store, "127.0.0.1", 0).start()
    port = rx._server.bound_port
    try:
        replies = exchange_raw(port, [orm(ctrl="E1", sample="BC-E1",
                                          accept="AL", application="AL")], expect=2)
        ck("9 Enhanced mode: MSH-15=AL e MSH-16=AL producono due risposte",
           len(replies) == 2, str(len(replies)))
        ck("9 Enhanced mode: prima il commit ACK (CA), poi l'ACK applicativo (AA)",
           len(replies) == 2 and field(replies[0], "MSA", 1) == "CA"
           and field(replies[1], "MSA", 1) == "AA",
           str([field(r, "MSA", 1) for r in replies]))
        ck("9 Enhanced mode: entrambe le risposte riportano lo stesso MSA-2",
           all(field(r, "MSA", 2) == "E1" for r in replies))
        ck("9 Enhanced mode: il message log annota la modalita' enhanced",
           store.get_messages(limit=1)[0]["ack_mode"] == "enhanced")

        # 10 — il mittente non vuole riscontri
        ck("10 MSH-15=NE e MSH-16=NE: nessuna risposta inviata",
           expect_no_reply(port, orm(ctrl="E2", sample="BC-E2",
                                     accept="NE", application="NE")))
        ck("10 MSH-15/16=NE: il messaggio e' comunque stato elaborato",
           bool(store.get_order("BC-E2")))

        # 11 — commit solo in caso di errore, esito positivo
        replies = exchange_raw(port, [orm(ctrl="E3", sample="BC-E3", accept="ER")], expect=1)
        ck("11 MSH-15=ER con esito positivo: solo l'ACK applicativo",
           len(replies) == 1 and field(replies[0], "MSA", 1) == "AA",
           str([field(r, "MSA", 1) for r in replies]))

        # ...e in caso di errore il commit negativo arriva
        replies = exchange_raw(port, [oru(ctrl="E4", accept="ER")], expect=2)
        ck("11 MSH-15=ER con errore: commit negativo (CR) + NACK applicativo (AR)",
           len(replies) == 2 and field(replies[0], "MSA", 1) == "CR"
           and field(replies[1], "MSA", 1) == "AR",
           str([field(r, "MSA", 1) for r in replies]))
    finally:
        rx.stop()


# --------------------------------------------------------------------------- 12-13 risposte e delimitatori
def test_order_response(store):
    rx = OrderReceiver(store, "127.0.0.1", 0, order_response_mode="order").start()
    port = rx._server.bound_port
    try:
        reply = exchange_raw(port, [orm(ctrl="R1", sample="BC-R1")])[0]
        ck("12 ORR^O02: risposta applicativa d'ordine per ORM^O01",
           field(reply, "MSH", 9) == "ORR^O02^ORR_O02", field(reply, "MSH", 9))
        ck("12 ORR^O02: MSA positivo e ORC-1 = OK",
           field(reply, "MSA", 1) == "AA" and field(reply, "ORC", 1) == "OK",
           f"{field(reply, 'MSA', 1)}/{field(reply, 'ORC', 1)}")
        ck("12 ORR^O02: riporta placer e filler dell'ordine",
           field(reply, "ORC", 2) == "PLAC-1" and field(reply, "ORC", 3) == "FILL-1")

        reply = exchange_raw(port, [orm(ctrl="R2", sample="BC-R2", mtype="OML^O21")])[0]
        ck("12 ORL^O22: risposta applicativa per OML^O21",
           field(reply, "MSH", 9) == "ORL^O22^ORL_O22", field(reply, "MSH", 9))
    finally:
        rx.stop()


def test_custom_delimiters(store):
    rx = OrderReceiver(store, "127.0.0.1", 0).start()
    port = rx._server.bound_port
    try:
        custom = CR.join([
            msh(ctrl="D1", fld="!"),
            "PID!1!!PAT-9^^^OSP^MR!!Rossi^Mario!!19800512!M",
            "ORC!NW!PLAC-9!FILL-9",
            "OBR!1!PLAC-9!FILL-9!58410-2^Emocromo^LN",
            "SPM!1!BC-CUSTOM!!BLD",
        ]) + CR
        reply = exchange_raw(port, [custom])[0]
        ck("13 Delimitatori non standard: il separatore di campo e' letto da MSH-1",
           bool(store.get_order("BC-CUSTOM")))
        ck("13 Delimitatori non standard: ACK con MSA-2 corretto",
           field(reply, "MSA", 2) == "D1", field(reply, "MSA", 2))
    finally:
        rx.stop()


# --------------------------------------------------------------------------- 14-15 lato uscita
def _ready_order(store, key: str):
    store.upsert_order({
        "sample_key": key, "placer_order_number": "PLAC-1", "filler_order_number": key,
        "patient": {"id": "PAT-1", "last_name": "Rossi", "first_name": "Mario"},
        "universal_service_id": {"code": "58410-2", "text": "Emocromo"},
    })
    store.add_result(key, {"sample_key": key,
                           "results": [{"code": "WBC", "value": "7.2", "status": "F"}]})
    store.set_status(key, "READY")


def test_forward_enhanced(store):
    seen = []

    def handler(message):
        seen.append(message)
        header = hl7.parse_header(message)
        policy = ackmod.AckPolicy(sending_app="LIS", sending_facility="OSP")
        return policy.responses(message, header, ackmod.AckOutcome(code="AA"))

    lis = mllp.MllpServer("127.0.0.1", 0, handler).start()
    try:
        _ready_order(store, "BC-F1")
        fwd = Forwarder(store, "127.0.0.1", lis.bound_port,
                        ack_mode=ackmod.MODE_ENHANCED, read_timeout=3.0)
        counts = fwd.forward_ready()
        header = hl7.parse_header(seen[0]) if seen else hl7.MessageHeader()
        ck("14 Enhanced in uscita: l'ORU chiede commit e ACK applicativo (MSH-15/16=AL)",
           header.accept_ack_type == "AL" and header.application_ack_type == "AL",
           f"{header.accept_ack_type}/{header.application_ack_type}")
        ck("14 Enhanced in uscita: SENT dopo il secondo riscontro",
           counts["sent"] == 1 and store.get_order("BC-F1")["status"] == "SENT", str(counts))
        out = [m for m in store.get_messages(limit=5, direction="OUT")]
        ck("14 Enhanced in uscita: l'inoltro e' tracciato con l'ACK finale",
           bool(out) and out[0]["ack_code"] == "AA" and out[0]["sample_key"] == "BC-F1")
    finally:
        lis.stop()

    # 15 — il LIS manda solo il commit ACK: non e' un esito, l'ordine resta ritentabile
    def commit_only(message):
        return hl7.build_ack(message, "CA")

    lis2 = mllp.MllpServer("127.0.0.1", 0, commit_only).start()
    try:
        _ready_order(store, "BC-F2")
        fwd = Forwarder(store, "127.0.0.1", lis2.bound_port, ack_mode=ackmod.MODE_ENHANCED,
                        read_timeout=1.0, application_ack_timeout=0.5)
        counts = fwd.forward_ready()
        ck("15 Solo commit ACK: l'ordine non passa a SENT ed e' ritentabile",
           counts["skipped"] == 1 and store.get_order("BC-F2")["status"] == "READY",
           f"{counts} status={store.get_order('BC-F2')['status']}")
    finally:
        lis2.stop()

    # ...mentre in original mode il comportamento storico resta invariato
    lis3 = mllp.MllpServer("127.0.0.1", 0, lambda m: hl7.build_ack(m, "AA")).start()
    store3 = fresh_store("forward_original")   # store pulito: nessun ordine residuo
    try:
        _ready_order(store3, "BC-F3")
        fwd = Forwarder(store3, "127.0.0.1", lis3.bound_port, read_timeout=3.0)
        counts = fwd.forward_ready()
        ck("15 Original mode: inoltro invariato con singolo ACK applicativo",
           counts["sent"] == 1 and store3.get_order("BC-F3")["status"] == "SENT", str(counts))
    finally:
        lis3.stop()


# --------------------------------------------------------------------------- 16 payload illeggibile
def test_malformed(store):
    rx = ResultReceiver(store, "127.0.0.1", 0).start()
    port = rx._server.bound_port
    try:
        reply = exchange_raw(port, ["QUESTO NON E' UN MESSAGGIO HL7" + CR])[0]
        err = hl7.Message(reply).seg("ERR")
        ck("16 Payload senza MSH: rifiutato con AR",
           field(reply, "MSA", 1) == "AR", field(reply, "MSA", 1))
        ck("16 Payload senza MSH: ERR con codice 100 (Segment sequence error)",
           bool(err) and hl7.comp(hl7.get(err, 3), 0) == "100", hl7.get(err or [], 3))
    finally:
        rx.stop()


# --------------------------------------------------------------------------- 17-21 rilievi di review
def test_dedup_purge_uses_last_seen(store):
    """La finestra di deduplica si misura sull'ultima attivita': un messaggio
    ancora in ritrasmissione non deve scadere e rientrare come nuovo."""
    store.remember_processed("LIS", "OLD-1", "ORM^O01", "BC-OLD", "AA", "ack")
    store.remember_processed("LIS", "ACTIVE-1", "ORM^O01", "BC-ACT", "AA", "ack")
    with store._conn() as c:
        # Entrambi visti la prima volta 100 ore fa; solo il secondo e' ancora attivo.
        old = (_dt.datetime.now() - _dt.timedelta(hours=100)).isoformat(timespec="seconds")
        c.execute("UPDATE processed_messages SET first_seen=?, last_seen=?", (old, old))
        c.execute("UPDATE processed_messages SET last_seen=? WHERE control_id='ACTIVE-1'",
                  (_dt.datetime.now().isoformat(timespec="seconds"),))
    removed = store.purge_processed(older_than_hours=72.0)
    ck("17 Deduplica: la pulizia rimuove solo i control id inattivi", removed == 1, str(removed))
    ck("17 Deduplica: il control id ancora ritrasmesso sopravvive alla pulizia",
       store.find_processed("LIS", "ACTIVE-1") is not None
       and store.find_processed("LIS", "OLD-1") is None)


def test_connection_limit(store):
    """Con le connessioni persistenti un peer potrebbe tenere occupati thread e
    socket: il tetto configurato deve rifiutare le connessioni in eccesso."""
    rx = ResultReceiver(store, "127.0.0.1", 0, max_connections=2, idle_timeout=5.0).start()
    port = rx._server.bound_port
    held = []
    try:
        for _ in range(2):
            s = socket.create_connection(("127.0.0.1", port), timeout=3.0)
            s.sendall(mllp.frame(oru(ctrl=f"C{len(held)}", sample="BC-LIMIT")))
            mllp.FrameReader(s).read(3.0)          # connessione attiva e in attesa
            held.append(s)
        extra = socket.create_connection(("127.0.0.1", port), timeout=3.0)
        held.append(extra)
        extra.settimeout(3.0)
        closed = extra.recv(16) == b""             # il server chiude subito l'eccedenza
        ck("18 Limite connessioni: la connessione oltre il tetto viene chiusa subito", closed)
        ck("18 Limite connessioni: le connessioni entro il tetto restano attive",
           rx._server.active_connections == 2, str(rx._server.active_connections))
        held.pop().close()
        for s in list(held):                       # liberato uno slot, si rientra
            s.close()
            held.remove(s)
        deadline = time.monotonic() + 3.0
        while rx._server.active_connections and time.monotonic() < deadline:
            time.sleep(0.05)
        s = socket.create_connection(("127.0.0.1", port), timeout=3.0)
        held.append(s)
        s.sendall(mllp.frame(oru(ctrl="C-AFTER", sample="BC-LIMIT")))
        reply = mllp.FrameReader(s).read(3.0)
        ck("18 Limite connessioni: chiusa una connessione lo slot torna disponibile",
           reply is not None and field(reply.decode(), "MSA", 1) == "AA")
    finally:
        for s in held:
            s.close()
        rx.stop()


def test_order_response_on_reject(store):
    """In modalita' "order" anche il rifiuto deve arrivare come ORR/ORL con
    ORC-1 = UA, non come ACK generico."""
    rx = OrderReceiver(store, "127.0.0.1", 0, order_response_mode="order").start()
    port = rx._server.bound_port
    try:
        bad = CR.join([msh(ctrl="RJ1"), "ORC|NW||", "OBR|1|||58410-2^Emocromo^LN"]) + CR
        reply = exchange_raw(port, [bad])[0]
        ck("19 Rifiuto in modalita' order: risposta ORR^O02, non ACK",
           field(reply, "MSH", 9) == "ORR^O02^ORR_O02", field(reply, "MSH", 9))
        ck("19 Rifiuto in modalita' order: MSA negativo e ORC-1 = UA",
           field(reply, "MSA", 1) == "AR" and field(reply, "ORC", 1) == "UA",
           f"{field(reply, 'MSA', 1)}/{field(reply, 'ORC', 1)}")
        err = hl7.Message(reply).seg("ERR")
        ck("19 Rifiuto in modalita' order: il segmento ERR resta presente",
           bool(err) and hl7.comp(hl7.get(err, 3), 0) == "101", hl7.get(err or [], 3))

        oml = CR.join([msh(ctrl="RJ2", mtype="OML^O21"), "ORC|NW||"]) + CR
        reply = exchange_raw(port, [oml])[0]
        ck("19 Rifiuto in modalita' order: OML riceve ORL^O22",
           field(reply, "MSH", 9) == "ORL^O22^ORL_O22", field(reply, "MSH", 9))
    finally:
        rx.stop()


def test_concurrent_duplicates(store):
    """Due copie identiche in arrivo insieme su connessioni diverse: una sola
    deve essere elaborata (la deduplica non deve avere una finestra di corsa)."""
    rx = ResultReceiver(store, "127.0.0.1", 0).start()
    port = rx._server.bound_port
    try:
        store.upsert_order({"sample_key": "BC-RACE", "patient": {}, "universal_service_id": {}})
        message = oru(ctrl="RACE-1", sample="BC-RACE")
        replies: list[str] = []
        errors: list[Exception] = []
        start = threading.Barrier(4)

        def worker():
            try:
                start.wait(timeout=5)
                replies.extend(exchange_raw(port, [message]))
            except Exception as e:      # pragma: no cover - diagnostica del test
                errors.append(e)

        # La barriera sincronizza i soli 4 worker: partono insieme, cosi' le
        # copie identiche arrivano davvero in contemporanea.
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        ck("20 Corsa sulla deduplica: nessun errore nelle 4 richieste concorrenti",
           not errors and len(replies) == 4, f"{errors} {len(replies)}")
        ck("20 Corsa sulla deduplica: il risultato viene inserito una sola volta",
           len(store.results_for("BC-RACE")) == 1, str(len(store.results_for("BC-RACE"))))
        ck("20 Corsa sulla deduplica: tutte le copie ricevono un ACK positivo",
           all(field(r, "MSA", 1) == "AA" for r in replies))
        prev = store.find_processed("ANALYZER", "RACE-1")
        ck("20 Corsa sulla deduplica: le ritrasmissioni sono contate",
           bool(prev) and prev["hits"] == 4, str(prev and prev["hits"]))
    finally:
        rx.stop()


def test_dashboard_escaping():
    """I valori che arrivano da messaggi HL7 finiscono nella dashboard: devono
    essere inseriti come testo, mai come markup (XSS memorizzato)."""
    try:
        from hl7mw.api import get_dashboard_html
    except ImportError:
        print("[OK]     21 Dashboard: test escaping saltato (fastapi non installato)")
        return
    html = get_dashboard_html()
    ck("21 Dashboard: esiste la funzione di escaping", "function esc(value)" in html)
    risky = ["${r.control_id", "${r.sample_key", "${r.message_type", "${o.sample_key}",
             "${i.name}", "${m.control_id", "${data.order.status}"]
    found = [r for r in risky if r in html]
    ck("21 Dashboard: nessun campo HL7 interpolato senza escape", not found, str(found))
    ck("21 Dashboard: la sample key non finisce dentro un gestore inline",
       "onclick=\"viewOrder(this.dataset.key)\"" in html
       and "viewOrder('${" not in html)


def test_parse_ack_helpers():
    raw = hl7.build_ack(CR.join([msh(ctrl="Z1")]) + CR, "AE", "campo mancante",
                        error_code="101")
    parsed = mllp.parse_ack(raw)
    ck("16 parse_ack: legge codice, control id, testo e codice errore",
       (parsed.code, parsed.control_id, parsed.error_code) == ("AE", "Z1", "101"),
       str(parsed))
    ck("16 parse_ack: il codice AE non e' considerato positivo", not parsed.positive)
    ck("16 is_commit_code distingue CA/CE/CR dai codici applicativi",
       mllp.is_commit_code("CA") and not mllp.is_commit_code("AA"))


def main() -> int:
    test_ack_wellformed(fresh_store("ack"))
    test_err_legacy_version()
    test_persistent_connection(fresh_store("persist"))
    test_batch(fresh_store("batch"))
    test_deduplication(fresh_store("dedup"))
    test_enhanced_mode(fresh_store("enhanced"))
    test_order_response(fresh_store("orr"))
    test_custom_delimiters(fresh_store("delims"))
    test_forward_enhanced(fresh_store("forward"))
    test_malformed(fresh_store("malformed"))
    test_dedup_purge_uses_last_seen(fresh_store("purge"))
    test_connection_limit(fresh_store("connlimit"))
    test_order_response_on_reject(fresh_store("reject"))
    test_concurrent_duplicates(fresh_store("race"))
    test_dashboard_escaping()
    test_parse_ack_helpers()

    if ck.failed:
        print(f"\n{len(ck.failed)} TEST FALLITI:")
        for f in ck.failed:
            print(f"  - {f}")
        return 1
    print("\nTUTTI I TEST ACK/HL7 OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
