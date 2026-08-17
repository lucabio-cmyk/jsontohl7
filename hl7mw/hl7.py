"""
hl7mw.hl7 — utilita' HL7v2 per il middleware.

Contiene:
  - parsing generico di messaggi/segmenti
  - parse_order():  ORM^O01 / OML^O21  -> dict ordine (dal LIS)
  - parse_result(): ORU^R01            -> dict risultato (dagli strumenti)
  - build_ack():    ACK^... da rispondere al mittente
  - build_oru():    dict ordine+risultati -> ORU^R01 (inoltro verso il LIS)

Solo stdlib.
"""
from __future__ import annotations

import datetime as _dt
import random
import re
import string
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


class Hl7Error(ValueError):
    pass


# --------------------------------------------------------------------------- parsing
def split_segments(message: str) -> list[list[str]]:
    """Spezza un messaggio HL7 in lista di segmenti, ognuno lista di campi."""
    segs = []
    for raw in re.split(r"[\r\n]+", message):
        if raw.strip():
            segs.append(raw.split(FLD))
    return segs


def get(seg: list[str], idx: int) -> str:
    return seg[idx] if 0 <= idx < len(seg) else ""


def comp(value: str, idx: int) -> str:
    parts = value.split(CMP)
    return parts[idx] if 0 <= idx < len(parts) else ""


def find(segs: list[list[str]], name: str) -> list[str] | None:
    for s in segs:
        if s and s[0] == name:
            return s
    return None


def msh_field(segs: list[list[str]], idx: int) -> str:
    """idx secondo numerazione HL7 (MSH-9 -> idx 9). MSH-1 e' '|' implicito."""
    msh = find(segs, "MSH")
    if not msh:
        return ""
    # In split, MSH[0]='MSH', MSH[1]=encoding chars (=MSH-2). MSH-9 -> indice 8.
    return msh[idx - 1] if idx - 1 < len(msh) else ""


def esc_text(value: Any) -> str:
    if value is None:
        return ""
    s = str(value)
    for a, b in ((ESC, "\\E\\"), (FLD, "\\F\\"), (CMP, "\\S\\"), (REP, "\\R\\"), (SUB, "\\T\\")):
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


# --------------------------------------------------------------------------- inbound
def _patient(segs) -> dict:
    pid = find(segs, "PID")
    if not pid:
        return {}
    return {
        "id": comp(get(pid, 3), 0),
        "id_authority": comp(get(pid, 3), 3),
        "last_name": comp(get(pid, 5), 0),
        "first_name": comp(get(pid, 5), 1),
        "birth_date": get(pid, 7),
        "sex": get(pid, 8),
    }


def parse_adt(message: str) -> dict:
    """ADT^A0x dal LIS (es. A04 registrazione nuovo paziente) -> dict paziente.

    Alcuni LIS (es. Citizen Care Connect) inviano un ADT^A04 di registrazione
    paziente prima o insieme all'ORM^O01 dell'ordine vero e proprio. Non produce
    un ordine: il chiamante decide se e come persistere/riscontrare l'evento
    (tipicamente un ACK positivo, senza creare una riga in 'orders')."""
    segs = split_segments(message)
    mtype = msh_field(segs, 9)
    if not mtype.startswith("ADT"):
        raise Hl7Error(f"Tipo messaggio non gestito come ADT: {mtype!r}")
    evn = find(segs, "EVN")
    return {
        "message_control_id": msh_field(segs, 10),
        "message_type": mtype,
        "event_type": get(evn or [], 1) or mtype.split(CMP)[-1],
        "patient": _patient(segs),
        "raw": message,
    }


def parse_order(message: str) -> dict:
    """ORM^O01 / OML^O21 dal LIS -> dict ordine normalizzato.
    Estrae gli identificativi utili al matching (sample/placer/filler)."""
    segs = split_segments(message)
    mtype = msh_field(segs, 9)
    if not (mtype.startswith("ORM") or mtype.startswith("OML")):
        raise Hl7Error(f"Tipo messaggio non gestito come ordine: {mtype!r}")

    orc = find(segs, "ORC")
    obr = find(segs, "OBR")
    spm = find(segs, "SPM")

    placer = comp(get(orc or [], 2), 0) or comp(get(obr or [], 2), 0)
    filler = comp(get(orc or [], 3), 0) or comp(get(obr or [], 3), 0)
    specimen = comp(get(spm or [], 2), 0) if spm else ""
    usi = get(obr or [], 4)

    return {
        "message_control_id": msh_field(segs, 10),
        "message_type": mtype,
        "patient": _patient(segs),
        "placer_order_number": placer,
        "filler_order_number": filler,
        "specimen_id": specimen,
        "sample_key": sample_key(specimen, filler, placer),
        "universal_service_id": {
            "code": comp(usi, 0), "text": comp(usi, 1), "system": comp(usi, 2),
        },
        "ordering_provider": (comp(get(obr or [], 16), 2) + " " + comp(get(obr or [], 16), 1)).strip(),
        "requested_datetime": get(obr or [], 6),
        "raw": message,
    }


def parse_result(message: str) -> dict:
    """ORU^R01 dallo strumento -> dict risultato (sample + lista OBX)."""
    segs = split_segments(message)
    mtype = msh_field(segs, 9)
    if not mtype.startswith("ORU"):
        raise Hl7Error(f"Tipo messaggio non gestito come risultato: {mtype!r}")

    obr = find(segs, "OBR")
    spm = find(segs, "SPM")
    placer = comp(get(obr or [], 2), 0)
    filler = comp(get(obr or [], 3), 0)
    specimen = comp(get(spm or [], 2), 0) if spm else ""

    results = []
    for s in segs:
        if s and s[0] == "OBX":
            oid = get(s, 3)
            results.append({
                "code": comp(oid, 0), "name": comp(oid, 1),
                "value": get(s, 5), "unit": get(s, 6),
                "ref_range": get(s, 7), "flag": get(s, 8),
                "status": get(s, 11) or "F", "datetime": get(s, 14),
            })
    return {
        "message_control_id": msh_field(segs, 10),
        "sample_key": sample_key(specimen, filler, placer),
        "specimen_id": specimen, "placer_order_number": placer, "filler_order_number": filler,
        "results": results,
        "result_datetime": get(obr or [], 22) or now_ts(),
        "sending_application": msh_field(segs, 3),
        "raw": message,
    }


# --------------------------------------------------------------------------- ACK
def build_ack(message: str, code: str = "AA", text: str = "",
              sending_app: str = "HL7MW", sending_facility: str = "MIDDLEWARE") -> str:
    """Costruisce un ACK in risposta a 'message', invertendo mittente/destinatario."""
    segs = split_segments(message)
    in_send_app = msh_field(segs, 3)
    in_send_fac = msh_field(segs, 4)
    ctrl = msh_field(segs, 10)
    ts = now_ts()
    msh = (f"MSH{FLD}{ENCODING_CHARS}{FLD}{esc_text(sending_app)}{FLD}"
           f"{esc_text(sending_facility)}{FLD}{esc_text(in_send_app)}{FLD}"
           f"{esc_text(in_send_fac)}{FLD}{ts}{FLD}{FLD}ACK{FLD}{ts}{FLD}P{FLD}2.5")
    msa = f"MSA{FLD}{code}{FLD}{ctrl}" + (f"{FLD}{esc_text(text)}" if text else "")
    return msh + SEG + msa + SEG


# --------------------------------------------------------------------------- outbound (forward al LIS)
def build_oru(order: dict, results: list[dict], cfg: "OruConfig") -> tuple[str, str]:
    """Costruisce l'ORU^R01 da inoltrare al LIS, unendo l'ordine ai risultati associati."""
    patient = order.get("patient") or {}
    if not results:
        raise Hl7Error("Nessun risultato da inoltrare.")
    cid = control_id(order.get("filler_order_number") or order.get("sample_key"))
    ts = now_ts()
    usi = order.get("universal_service_id") or {}
    panel = f"{esc_text(usi.get('code', '58410-2'))}{CMP}{esc_text(usi.get('text', 'Emocromo'))}{CMP}{CODE_SYSTEM}"

    msh = (f"MSH{FLD}{ENCODING_CHARS}{FLD}{cfg.sending_app}{FLD}{cfg.sending_facility}{FLD}"
           f"{cfg.receiving_app}{FLD}{cfg.receiving_facility}{FLD}{ts}{FLD}{FLD}"
           f"ORU{CMP}R01{CMP}ORU_R01{FLD}{cid}{FLD}{cfg.processing_id}{FLD}{cfg.hl7_version}")
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
                 processing_id="P", hl7_version="2.5"):
        self.sending_app = sending_app
        self.sending_facility = sending_facility
        self.receiving_app = receiving_app
        self.receiving_facility = receiving_facility
        self.processing_id = processing_id
        self.hl7_version = hl7_version
