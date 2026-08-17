"""
hl7mw.hl7 — utilita' HL7v2 per il middleware.

Contiene:
  - parsing generico di messaggi/segmenti (Delimiters, Message, MessageHeader)
  - split_messages():  payload MLLP/batch (FHS/BHS) -> lista di messaggi singoli
  - parse_order():  ORM^O01 / OML^O21  -> dict ordine (dal LIS)
  - parse_result(): ORU^R01 / OUL^R2x  -> dict risultato (dagli strumenti)
  - build_ack():    ACK^<trigger>^ACK da rispondere al mittente (con ERR opzionale)
  - build_orr()/build_orl(): risposte applicative d'ordine (ORR^O02 / ORL^O22)
  - build_oru():    dict ordine+risultati -> ORU^R01 (inoltro verso il LIS)

Riferimento: HL7 v2.5 capitolo 2 (Control), in particolare 2.9 (acknowledgement),
2.10 (batch protocol) e 2.15.8 (segmento ERR).

Solo stdlib.
"""
from __future__ import annotations

import datetime as _dt
import random
import re
import string
from dataclasses import dataclass
from typing import Any

SEG, FLD, CMP, REP, ESC, SUB = "\r", "|", "^", "~", "\\", "&"
ENCODING_CHARS = CMP + REP + ESC + SUB

# Tabella analiti emocromo -> (LOINC, descrizione, unita' UCUM). Adattare al proprio dominio.
CBC_ANALYTES: dict[str, tuple[str, str, str]] = {
    "WBC": ("6690-2", "Leucociti", "10*3/uL"), "RBC": ("789-8", "Eritrociti", "10*6/uL"),
    "HGB": ("718-7", "Emoglobina", "g/dL"), "HCT": ("4544-3", "Ematocrito", "%"),
    "MCV": ("787-2", "MCV", "fL"), "MCH": ("785-6", "MCH", "pg"), "MCHC": ("786-4", "MCHC", "g/dL"),
    "RDW": ("788-0", "RDW", "%"), "PLT": ("777-3", "Piastrine", "10*3/uL"), "MPV": ("32623-1", "MPV", "fL"),
    "NEUT%": ("770-8", "Neutrofili %", "%"), "LYMPH%": ("736-9", "Linfociti %", "%"),
    "MONO%": ("5905-5", "Monociti %", "%"), "EOS%": ("713-8", "Eosinofili %", "%"),
    "BASO%": ("706-2", "Basofili %", "%"),
}
CODE_SYSTEM = "LN"

# Tipi di messaggio riconosciuti dai due canali in ingresso (MSH-9.1).
ORDER_MESSAGE_CODES = ("ORM", "OML")
RESULT_MESSAGE_CODES = ("ORU", "OUL")


class Hl7Error(ValueError):
    """Errore di contenuto: il messaggio e' arrivato ma non e' processabile.

    Porta con se' il codice errore HL7 (tabella 0357) da riportare in ERR-3,
    cosi' il NACK verso il mittente e' diagnosticabile e non solo testo libero.
    """

    def __init__(self, message: str, error_code: str = "207", ack_code: str = "AR"):
        super().__init__(message)
        self.error_code = error_code
        self.ack_code = ack_code


# --------------------------------------------------------------------------- delimitatori
@dataclass(frozen=True)
class Delimiters:
    """Delimitatori del messaggio, letti da MSH-1 (separatore campo) e MSH-2.

    Lo standard non impone `|^~\\&`: un mittente puo' dichiarare altri caratteri
    e ogni parser conforme deve leggerli dal messaggio invece di assumerli.
    """
    field: str = FLD
    comp: str = CMP
    rep: str = REP
    esc: str = ESC
    sub: str = SUB

    @property
    def encoding_chars(self) -> str:
        return self.comp + self.rep + self.esc + self.sub

    @classmethod
    def from_message(cls, message: str) -> "Delimiters":
        idx = message.find("MSH")
        if idx < 0 or len(message) < idx + 8:
            return cls()
        fld = message[idx + 3]
        enc = message[idx + 4: idx + 8]
        if not fld.strip() or len(set(enc)) != 4 or fld in enc:
            # Header malformato: meglio i default che delimitatori incoerenti.
            return cls()
        return cls(field=fld, comp=enc[0], rep=enc[1], esc=enc[2], sub=enc[3])


DEFAULT_DELIMS = Delimiters()


# --------------------------------------------------------------------------- parsing
def split_segments(message: str, delims: Delimiters | None = None) -> list[list[str]]:
    """Spezza un messaggio HL7 in lista di segmenti, ognuno lista di campi."""
    d = delims or Delimiters.from_message(message)
    segs = []
    for raw in re.split(r"[\r\n]+", message):
        if raw.strip():
            segs.append(raw.split(d.field))
    return segs


def get(seg: list[str], idx: int) -> str:
    return seg[idx] if 0 <= idx < len(seg) else ""


def comp(value: str, idx: int, delims: Delimiters | None = None) -> str:
    parts = value.split((delims or DEFAULT_DELIMS).comp)
    return parts[idx] if 0 <= idx < len(parts) else ""


def find(segs: list[list[str]], name: str) -> list[str] | None:
    for s in segs:
        if s and s[0] == name:
            return s
    return None


def find_all(segs: list[list[str]], name: str) -> list[list[str]]:
    return [s for s in segs if s and s[0] == name]


def msh_field(segs: list[list[str]], idx: int) -> str:
    """idx secondo numerazione HL7 (MSH-9 -> idx 9). MSH-1 e' '|' implicito."""
    msh = find(segs, "MSH")
    if not msh:
        return ""
    # In split, MSH[0]='MSH', MSH[1]=encoding chars (=MSH-2). MSH-9 -> indice 8.
    return msh[idx - 1] if idx - 1 < len(msh) else ""


def esc_text(value: Any, delims: Delimiters | None = None) -> str:
    if value is None:
        return ""
    d = delims or DEFAULT_DELIMS
    s = str(value)
    for a, b in ((d.esc, "\\E\\"), (d.field, "\\F\\"), (d.comp, "\\S\\"),
                 (d.rep, "\\R\\"), (d.sub, "\\T\\")):
        s = s.replace(a, b)
    return s.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def now_ts() -> str:
    return _dt.datetime.now().strftime("%Y%m%d%H%M%S")


def to_ts(value: Any) -> str:
    if not value:
        return ""
    s = str(value).strip()
    if re.fullmatch(r"\d{8}(\d{6})?", s):
        return s
    try:
        if "T" in s or (" " in s and ":" in s):
            return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).strftime("%Y%m%d%H%M%S")
        return _dt.date.fromisoformat(s).strftime("%Y%m%d")
    except ValueError:
        return s


def control_id(seed: str | None = None) -> str:
    if seed:
        return re.sub(r"[^A-Za-z0-9._-]", "", str(seed))[:199]
    rnd = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return now_ts() + rnd


def sample_key(*candidates: str) -> str:
    """Chiave di matching: primo identificativo non vuoto, normalizzato."""
    for c in candidates:
        if c and c.strip():
            return c.strip().upper()
    return ""


def version_tuple(version: str) -> tuple[int, ...]:
    """'2.5.1' -> (2, 5, 1). Serve a scegliere la forma dei segmenti che
    cambiano fra versioni (es. ERR-1 fino alla 2.3.1, ERR-3 dalla 2.4)."""
    parts = []
    for p in re.split(r"[.\-]", (version or "").strip()):
        if p.isdigit():
            parts.append(int(p))
        else:
            break
    return tuple(parts) or (2, 5)


# --------------------------------------------------------------------------- header
@dataclass
class MessageHeader:
    """MSH normalizzato: tutto cio' che serve per rispondere e tracciare.

    Include MSH-15/MSH-16 (accept/application acknowledgement type), che
    determinano la modalita' di riscontro richiesta dal mittente — vedi
    hl7mw/ack.py.
    """
    sending_app: str = ""
    sending_facility: str = ""
    receiving_app: str = ""
    receiving_facility: str = ""
    timestamp: str = ""
    message_code: str = ""          # MSH-9.1  (ORM, ORU, ADT, ACK, ...)
    trigger_event: str = ""         # MSH-9.2  (O01, R01, A04, ...)
    message_structure: str = ""     # MSH-9.3  (ORU_R01, ...)
    control_id: str = ""            # MSH-10
    processing_id: str = ""         # MSH-11   (P / T / D)
    version: str = ""               # MSH-12
    sequence_number: str = ""       # MSH-13
    accept_ack_type: str = ""       # MSH-15   (AL / NE / ER / SU)
    application_ack_type: str = ""  # MSH-16   (AL / NE / ER / SU)
    country_code: str = ""          # MSH-17
    charset: str = ""               # MSH-18
    delims: Delimiters = DEFAULT_DELIMS

    @property
    def message_type(self) -> str:
        """MSH-9 come stringa 'ORM^O01' (senza la struttura)."""
        if self.trigger_event:
            return f"{self.message_code}{self.delims.comp}{self.trigger_event}"
        return self.message_code

    @property
    def is_order(self) -> bool:
        return self.message_code in ORDER_MESSAGE_CODES

    @property
    def is_result(self) -> bool:
        return self.message_code in RESULT_MESSAGE_CODES

    @property
    def is_adt(self) -> bool:
        return self.message_code == "ADT"

    def as_dict(self) -> dict:
        return {
            "sending_app": self.sending_app, "sending_facility": self.sending_facility,
            "receiving_app": self.receiving_app, "receiving_facility": self.receiving_facility,
            "timestamp": self.timestamp, "message_type": self.message_type,
            "message_code": self.message_code, "trigger_event": self.trigger_event,
            "control_id": self.control_id, "processing_id": self.processing_id,
            "version": self.version, "accept_ack_type": self.accept_ack_type,
            "application_ack_type": self.application_ack_type,
        }


def parse_header(message: str) -> MessageHeader:
    """MSH -> MessageHeader. Non solleva: un header illeggibile da' campi vuoti,
    cosi' il chiamante puo' comunque rispondere un NACK con quel che ha."""
    delims = Delimiters.from_message(message)
    segs = split_segments(message, delims)
    msh = find(segs, "MSH")
    if not msh:
        return MessageHeader(delims=delims)

    def f(idx: int) -> str:
        # MSH-1 e' il separatore stesso: MSH-n vive in msh[n-1].
        return msh[idx - 1] if 0 < idx - 1 < len(msh) else ""

    mtype = f(9)
    return MessageHeader(
        sending_app=f(3), sending_facility=f(4),
        receiving_app=f(5), receiving_facility=f(6),
        timestamp=f(7),
        message_code=comp(mtype, 0, delims),
        trigger_event=comp(mtype, 1, delims),
        message_structure=comp(mtype, 2, delims),
        control_id=f(10), processing_id=f(11), version=f(12),
        sequence_number=f(13),
        accept_ack_type=f(15).upper(), application_ack_type=f(16).upper(),
        country_code=f(17), charset=f(18),
        delims=delims,
    )


class Message:
    """Messaggio HL7 gia' spezzato, consapevole dei propri delimitatori.

    I parser sotto lo usano al posto delle funzioni libere: cosi' un mittente
    che dichiara delimitatori non standard viene interpretato correttamente.
    """

    def __init__(self, raw: str):
        self.raw = raw
        self.delims = Delimiters.from_message(raw)
        self.segments = split_segments(raw, self.delims)
        self.header = parse_header(raw)

    def seg(self, name: str) -> list[str] | None:
        return find(self.segments, name)

    def segs(self, name: str) -> list[list[str]]:
        return find_all(self.segments, name)

    def field(self, seg_name: str, idx: int) -> str:
        return get(self.seg(seg_name) or [], idx)

    def comp(self, value: str, idx: int) -> str:
        return comp(value, idx, self.delims)

    def cfield(self, seg_name: str, idx: int, component: int = 0) -> str:
        return self.comp(self.field(seg_name, idx), component)


# --------------------------------------------------------------------------- batch / payload multiplo
BATCH_SEGMENTS = ("FHS", "FTS", "BHS", "BTS")


def split_messages(payload: str) -> list[str]:
    """Spezza un payload in messaggi HL7 singoli.

    Gestisce due casi che lo standard ammette e che un parser ingenuo perde:
      - batch protocol (cap. 2.10): FHS/BHS di testa, BTS/FTS di coda, N messaggi
        in mezzo — gli involucri vengono scartati;
      - piu' MSH concatenati nello stesso payload (alcuni mittenti li accodano
        in un unico blocco MLLP invece di aprire un frame per messaggio).

    Ritorna [] se non c'e' nessun MSH.
    """
    delims = Delimiters.from_message(payload)
    messages: list[list[str]] = []
    for line in re.split(r"[\r\n]+", payload):
        if not line.strip():
            continue
        name = line.split(delims.field)[0][:3]
        if name in BATCH_SEGMENTS:
            continue
        if name == "MSH":
            messages.append([line])
        elif messages:
            messages[-1].append(line)
        # segmenti prima del primo MSH: scartati (payload malformato)
    return [SEG.join(m) + SEG for m in messages]


def is_batch(payload: str) -> bool:
    return bool(re.match(r"\s*(FHS|BHS)", payload))


# --------------------------------------------------------------------------- inbound
def _patient(msg: Message) -> dict:
    pid = msg.seg("PID")
    if not pid:
        return {}
    return {
        "id": msg.comp(get(pid, 3), 0),
        "id_authority": msg.comp(get(pid, 3), 3),
        "last_name": msg.comp(get(pid, 5), 0),
        "first_name": msg.comp(get(pid, 5), 1),
        "birth_date": get(pid, 7),
        "sex": get(pid, 8),
    }


def parse_adt(message: str) -> dict:
    """ADT^A0x dal LIS (es. A04 registrazione nuovo paziente) -> dict paziente.

    Alcuni LIS (es. Citizen Care Connect) inviano un ADT^A04 di registrazione
    paziente prima o insieme all'ORM^O01 dell'ordine vero e proprio. Non produce
    un ordine: il chiamante decide se e come persistere/riscontrare l'evento
    (tipicamente un ACK positivo, senza creare una riga in 'orders')."""
    msg = Message(message)
    if msg.header.message_code != "ADT":
        raise Hl7Error(f"Tipo messaggio non gestito come ADT: {msg.header.message_type!r}",
                       error_code="200")
    evn = msg.seg("EVN")
    return {
        "message_control_id": msg.header.control_id,
        "message_type": msg.header.message_type,
        "event_type": get(evn or [], 1) or msg.header.trigger_event,
        "patient": _patient(msg),
        "raw": message,
    }


def parse_order(message: str) -> dict:
    """ORM^O01 / OML^O21 dal LIS -> dict ordine normalizzato.
    Estrae gli identificativi utili al matching (sample/placer/filler)."""
    msg = Message(message)
    if not msg.header.is_order:
        raise Hl7Error(f"Tipo messaggio non gestito come ordine: {msg.header.message_type!r}",
                       error_code="200")

    orc = msg.seg("ORC")
    obr = msg.seg("OBR")
    spm = msg.seg("SPM")

    placer = msg.comp(get(orc or [], 2), 0) or msg.comp(get(obr or [], 2), 0)
    filler = msg.comp(get(orc or [], 3), 0) or msg.comp(get(obr or [], 3), 0)
    specimen = msg.comp(get(spm or [], 2), 0) if spm else ""
    usi = get(obr or [], 4)

    return {
        "message_control_id": msg.header.control_id,
        "message_type": msg.header.message_type,
        "order_control": get(orc or [], 1),
        "patient": _patient(msg),
        "placer_order_number": placer,
        "filler_order_number": filler,
        "specimen_id": specimen,
        "sample_key": sample_key(specimen, filler, placer),
        "universal_service_id": {
            "code": msg.comp(usi, 0), "text": msg.comp(usi, 1), "system": msg.comp(usi, 2),
        },
        "ordering_provider": (msg.comp(get(obr or [], 16), 2) + " " +
                              msg.comp(get(obr or [], 16), 1)).strip(),
        "requested_datetime": get(obr or [], 6),
        "raw": message,
    }


def parse_result(message: str) -> dict:
    """ORU^R01 / OUL^R2x dallo strumento -> dict risultato (sample + lista OBX)."""
    msg = Message(message)
    if not msg.header.is_result:
        raise Hl7Error(f"Tipo messaggio non gestito come risultato: {msg.header.message_type!r}",
                       error_code="200")

    obr = msg.seg("OBR")
    spm = msg.seg("SPM")
    placer = msg.comp(get(obr or [], 2), 0)
    filler = msg.comp(get(obr or [], 3), 0)
    specimen = msg.comp(get(spm or [], 2), 0) if spm else ""

    results = []
    for s in msg.segs("OBX"):
        oid = get(s, 3)
        results.append({
            "code": msg.comp(oid, 0), "name": msg.comp(oid, 1),
            "value": get(s, 5), "unit": get(s, 6),
            "ref_range": get(s, 7), "flag": get(s, 8),
            "status": get(s, 11) or "F", "datetime": get(s, 14),
        })
    return {
        "message_control_id": msg.header.control_id,
        "sample_key": sample_key(specimen, filler, placer),
        "specimen_id": specimen, "placer_order_number": placer, "filler_order_number": filler,
        "results": results,
        "result_datetime": get(obr or [], 22) or now_ts(),
        "sending_application": msg.header.sending_app,
        "raw": message,
    }


# --------------------------------------------------------------------------- ACK / ERR
def build_err(error_code: str, error_text: str = "", severity: str = "E",
              user_message: str = "", version: str = "2.5",
              delims: Delimiters | None = None) -> str:
    """Segmento ERR (tabella HL7 0357).

    Dalla 2.4 l'informazione sta in ERR-3 (HL7 Error Code) con ERR-4 severity;
    fino alla 2.3.1 esisteva solo ERR-1 (Error Code and Location, tipo ELD).
    """
    d = delims or DEFAULT_DELIMS
    f, c, s = d.field, d.comp, d.sub
    code = esc_text(error_code, d)
    text = esc_text(error_text, d)
    if version_tuple(version) < (2, 4):
        return f"ERR{f}{c}{c}{c}{code}{s}{text}"
    err3 = f"{code}{c}{text}{c}HL70357"
    return (f"ERR{f}{f}{f}{err3}{f}{esc_text(severity, d)}{f}{f}{f}"
            f"{esc_text(user_message, d)}").rstrip(f)


def _ack_msh(header: MessageHeader, sending_app: str, sending_facility: str,
             version: str, structure: str = "ACK", trigger: str = "",
             ctrl: str = "") -> str:
    """MSH di una risposta, con mittente/destinatario invertiti rispetto a `header`.

    MSH-9 riporta il trigger event del messaggio originale (ACK^O01^ACK), come
    richiede lo standard dalla 2.3.1: alcuni LIS rifiutano un 'ACK' secco.
    MSH-15/16 restano vuoti: una risposta non chiede a sua volta riscontro.
    """
    d = DEFAULT_DELIMS
    f, c = d.field, d.comp
    ts = now_ts()
    trig = trigger or header.trigger_event
    if trig and version_tuple(version) >= (2, 3):
        mtype = f"{structure}{c}{trig}{c}{structure}"
    else:
        mtype = structure
    return (f"MSH{f}{d.encoding_chars}{f}{esc_text(sending_app)}{f}"
            f"{esc_text(sending_facility)}{f}{esc_text(header.sending_app)}{f}"
            f"{esc_text(header.sending_facility)}{f}{ts}{f}{f}{mtype}{f}"
            f"{ctrl or control_id()}{f}{header.processing_id or 'P'}{f}{version}")


def build_ack(message: str, code: str = "AA", text: str = "",
              sending_app: str = "HL7MW", sending_facility: str = "MIDDLEWARE",
              *, error_code: str = "", error_text: str = "", severity: str = "E",
              version: str = "", header: MessageHeader | None = None) -> str:
    """Costruisce un ACK in risposta a 'message', invertendo mittente/destinatario.

    - MSH-9  = ACK^<trigger originale>^ACK
    - MSH-11 = processing id del messaggio in ingresso (non forzato a 'P': un
               messaggio di test 'T' deve essere riscontrato come test)
    - MSH-12 = versione del messaggio in ingresso (fallback 2.5)
    - MSA-2  = MSH-10 del messaggio in ingresso
    - ERR    = presente solo se error_code e' valorizzato (tabella 0357)

    `code` accetta sia i codici applicativi (AA/AE/AR) sia quelli di commit
    dell'enhanced mode (CA/CE/CR): vedi hl7mw/ack.py.
    """
    h = header or parse_header(message)
    ver = version or h.version or "2.5"
    f = FLD
    msh = _ack_msh(h, sending_app, sending_facility, ver)
    msa = f"MSA{f}{code}{f}{h.control_id}" + (f"{f}{esc_text(text)}" if text else "")
    segments = [msh, msa]
    if error_code:
        segments.append(build_err(error_code, error_text or ERROR_MESSAGES.get(error_code, ""),
                                  severity, text, ver))
    return SEG.join(segments) + SEG


# Tabella HL7 0357 (Message Error Condition Codes), sottoinsieme usato qui.
ERROR_MESSAGES: dict[str, str] = {
    "0": "Message accepted",
    "100": "Segment sequence error",
    "101": "Required field missing",
    "102": "Data type error",
    "103": "Table value not found",
    "200": "Unsupported message type",
    "201": "Unsupported event code",
    "202": "Unsupported processing id",
    "203": "Unsupported version id",
    "204": "Unknown key identifier",
    "205": "Duplicate key identifier",
    "206": "Application record locked",
    "207": "Application internal error",
}


# --------------------------------------------------------------------------- risposte d'ordine
def build_order_response(message: str, order: dict | None, accepted: bool = True,
                         text: str = "", sending_app: str = "HL7MW",
                         sending_facility: str = "MIDDLEWARE",
                         error_code: str = "", header: MessageHeader | None = None,
                         ack_code: str = "") -> str:
    """Risposta applicativa a un ordine: ORR^O02 per ORM^O01, ORL^O22 per OML^O21.

    Molti LIS si accontentano dell'ACK generico, ma lo standard prevede per
    l'ordine una risposta che riporti anche l'esito per singolo ordine (ORC-1:
    'OK' accettato, 'UA' impossibile accettare). Attivabile via configurazione
    (`order_response_mode`), default ACK per non cambiare i comportamenti in campo.
    """
    h = header or parse_header(message)
    ver = h.version or "2.5"
    f, c = FLD, CMP
    is_oml = h.message_code == "OML"
    structure, trigger = ("ORL", "O22") if is_oml else ("ORR", "O02")
    # ack_code esplicito quando il chiamante distingue AE (errore) da AR (rifiuto).
    ack_code = ack_code or ("AA" if accepted else "AR")

    msh = _ack_msh(h, sending_app, sending_facility, ver, structure=structure, trigger=trigger)
    # MSH-9.3 per ORR/ORL e' ORR_O02 / ORL_O22: _ack_msh mette <struct>^<trig>^<struct>,
    # qui la struttura corretta e' con l'underscore.
    msh = msh.replace(f"{structure}{c}{trigger}{c}{structure}",
                      f"{structure}{c}{trigger}{c}{structure}_{trigger}")
    msa = f"MSA{f}{ack_code}{f}{h.control_id}" + (f"{f}{esc_text(text)}" if text else "")
    segments = [msh, msa]
    if error_code:
        segments.append(build_err(error_code, ERROR_MESSAGES.get(error_code, ""), "E", text, ver))
    o = order or {}
    orc_code = "OK" if accepted else "UA"
    segments.append(
        f"ORC{f}{orc_code}{f}{esc_text(o.get('placer_order_number', ''))}{f}"
        f"{esc_text(o.get('filler_order_number', ''))}"
    )
    usi = (o.get("universal_service_id") or {}) if order else {}
    if usi.get("code"):
        panel = f"{esc_text(usi.get('code'))}{c}{esc_text(usi.get('text', ''))}{c}{CODE_SYSTEM}"
        segments.append(
            f"OBR{f}1{f}{esc_text(o.get('placer_order_number', ''))}{f}"
            f"{esc_text(o.get('filler_order_number', ''))}{f}{panel}"
        )
    return SEG.join(segments) + SEG


# --------------------------------------------------------------------------- outbound (forward al LIS)
def build_oru(order: dict, results: list[dict], cfg: "OruConfig") -> tuple[str, str]:
    """Costruisce l'ORU^R01 da inoltrare al LIS, unendo l'ordine ai risultati associati."""
    patient = order.get("patient") or {}
    if not results:
        raise Hl7Error("Nessun risultato da inoltrare.", error_code="101")
    cid = control_id(order.get("filler_order_number") or order.get("sample_key"))
    ts = now_ts()
    usi = order.get("universal_service_id") or {}
    panel = f"{esc_text(usi.get('code', '58410-2'))}{CMP}{esc_text(usi.get('text', 'Emocromo'))}{CMP}{CODE_SYSTEM}"

    msh = (f"MSH{FLD}{ENCODING_CHARS}{FLD}{cfg.sending_app}{FLD}{cfg.sending_facility}{FLD}"
           f"{cfg.receiving_app}{FLD}{cfg.receiving_facility}{FLD}{ts}{FLD}{FLD}"
           f"ORU{CMP}R01{CMP}ORU_R01{FLD}{cid}{FLD}{cfg.processing_id}{FLD}{cfg.hl7_version}"
           # MSH-13 vuoto, MSH-14 vuoto, poi MSH-15/16: modalita' di riscontro
           # richiesta al LIS (enhanced mode). Vuoti = original mode (un solo ACK).
           f"{FLD}{FLD}{FLD}{cfg.accept_ack_type}{FLD}{cfg.application_ack_type}").rstrip(FLD)
    pid = (f"PID{FLD}1{FLD}{FLD}{esc_text(patient.get('id'))}{CMP}{CMP}{CMP}"
           f"{esc_text(patient.get('id_authority',''))}{CMP}MR{FLD}{FLD}"
           f"{esc_text(patient.get('last_name'))}{CMP}{esc_text(patient.get('first_name'))}{FLD}{FLD}"
           f"{to_ts(patient.get('birth_date'))}{FLD}{esc_text(patient.get('sex',''))}")
    obr = (f"OBR{FLD}1{FLD}{esc_text(order.get('placer_order_number',''))}{FLD}"
           f"{esc_text(order.get('filler_order_number',''))}{FLD}{panel}"
           f"{FLD * 17}{ts}{FLD}{FLD}{FLD}F")
    segments = [msh, pid, obr]
    for i, r in enumerate(results, 1):
        code = str(r.get("code", "")).upper()
        if code in CBC_ANALYTES:
            loinc, disp, unit_def = CBC_ANALYTES[code]
        else:
            loinc, disp, unit_def = r.get("code", ""), r.get("name", ""), ""
        oid = f"{esc_text(loinc)}{CMP}{esc_text(disp)}{CMP}{CODE_SYSTEM}"
        unit = r.get("unit") or unit_def
        segments.append(
            f"OBX{FLD}{i}{FLD}NM{FLD}{oid}{FLD}{FLD}{esc_text(r.get('value'))}{FLD}"
            f"{esc_text(unit)}{FLD}{esc_text(r.get('ref_range',''))}{FLD}{esc_text(r.get('flag',''))}"
            f"{FLD}{FLD}{FLD}{esc_text(r.get('status','F'))}{FLD}{FLD}{FLD}"
            f"{to_ts(r.get('datetime')) or ts}"
        )
    return SEG.join(segments) + SEG, cid


class OruConfig:
    def __init__(self, sending_app="HL7MW", sending_facility="MIDDLEWARE",
                 receiving_app="LIS", receiving_facility="OSP",
                 processing_id="P", hl7_version="2.5",
                 accept_ack_type="", application_ack_type=""):
        self.sending_app = sending_app
        self.sending_facility = sending_facility
        self.receiving_app = receiving_app
        self.receiving_facility = receiving_facility
        self.processing_id = processing_id
        self.hl7_version = hl7_version
        # MSH-15/MSH-16 dei messaggi che inviamo: "" = original mode (default),
        # "AL"/"AL" = enhanced mode (commit ACK + ACK applicativo separato).
        self.accept_ack_type = accept_ack_type
        self.application_ack_type = application_ack_type
