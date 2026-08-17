"""
hl7mw.adapters.hemoscreen_poct1a2 — Adapter POCT1-A2 per PixCell HemoScreen.

Riferimento: HS-IL-00067 Rev.06 «HemoScreen POCT1-A2 Connectivity Protocol».

Protocollo: XML su TCP con framing MLLP (0x0B … 0x1C 0x0D).
Il middleware agisce come Observation Reviewer (server).

Flusso di conversazione supportato
───────────────────────────────────
Modalità base (pull):
  Device → HEL.R01   → noi: ACK.R01(AA)
  Device → DST.R01   → noi: ACK.R01(AA) [+ REQ.R01(ROBS) se new_obs>0,
                                          + REQ.R01(RDEV) se new_events>0]
  Device → OBS.R01   → noi: ACK.R01(AA)   [ripetuto per ogni osservazione]
  Device → OBS.R02   → noi: ACK.R01(AA)   [QC/EQA]
  Device → EVS.R01   → noi: ACK.R01(AA)   [evento persistito su audit_log]
  Device → EOT.R01   → noi: ACK.R01(AA), poi la prossima richiesta pendente
                        (es. RDEV dopo l'EOT di OBS) o END.R01 se non resta nulla
  Device → ACK.R01   (per il nostro END) → chiusura connessione
  Device → ESC.R01   → nessuna risposta prevista dal protocollo (loggato + audit)

Modalità continua (continuous_mode=True):
  Identica alla base fino all'esaurimento delle richieste pendenti (osservazioni
  ed eventi non ancora trasmessi). A quel punto inviamo DTV.R01(START_CONTINUOUS):
  se il device rifiuta risponde con ESC.R01 (continuous_active resta False), se
  accetta risponde con ACK.R01 positivo (continuous_active diventa True). Da quel
  momento il device invia OBS.R01/EVS.R01 autonomamente; noi ACKiamo ogni messaggio.
  In modalità continua il device può inoltre chiedere i dati di un paziente per
  Test ID con REQ.R01(RPAT): rispondiamo con PTL.R01 (lookup sull'ordine associato
  in Store, per sample_key). La connessione termina con END.R01 (da noi o dal device).

Direttive verso il device (Observation Reviewer → Device)
───────────────────────────────────────────────────────────
Oltre al flusso reattivo sopra, il middleware può inviare direttive al device
durante una conversazione attiva: LOCK/UNLOCK (DTV.R01), SET_TIME (DTV.R02),
liste operatori complete/incrementali (OPL.R01/OPL.R02), lotti di controllo
qualità (DTV.PIX.QC), range di normalità per genere (DTV.PIX.FB) e setup
strumento (DTV.PIX.DVCSET). DTV.PIX.FW (firmware) non è implementato: la spec
lo dichiara "draft e non rilasciato" per le versioni firmware supportate.

Poiché il device è sempre l'iniziatore della connessione TCP, le direttive non
possono essere "inviate" fuori da una conversazione in corso: vengono accodate
(coda per-conversazione, thread-safe) e recapitate al primo punto protocollarmente
sicuro (nessuna richiesta nostra o del device in sospeso), una alla volta, in
attesa del relativo ACK.R01 prima di procedere con l'eventuale successiva. Le
funzioni `send_*`/`send_directive` cercano la conversazione attiva per
`device_id`/serial (o la prima disponibile se non specificato) in un registro
in-process; ritornano False se nessun device è connesso in quel momento —
tipicamente richiamate da API/CLI mentre lo strumento è collegato.

OBS.R01 (sangue) → dict compatibile con hl7.parse_result()
  sample_key = PT.patient_id  (HemoScreen Test Identifier)
  analiti    = lista OBS con LOINC, valore, unità, flag, note

OBS.R02 (QC / EQA) → stesso formato
  sample_key = CTC.name (nome lotto QC)
  obs_type   = "LQC" o "PRF"

Solo stdlib. Nessuna dipendenza esterna.
"""
from __future__ import annotations

import datetime as _dt
import itertools
import json
import logging
import queue
import re
import socket
import socketserver
import threading
import xml.etree.ElementTree as ET
from typing import Callable, Iterator

from .. import hl7 as hl7mod
from ..mllp import SB, EB, CR
from ..monitor import DeviceMonitor
from ..pipeline import try_complete
from ..store import Store

LOG = logging.getLogger("hl7mw")

# ---------------------------------------------------------------------------
# Helpers timestamp
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Timestamp locale in formato ISO 8601 con offset timezone (es. 2024-01-03T12:00:00+01:00)."""
    return _dt.datetime.now(_dt.timezone.utc).astimezone().replace(microsecond=0).isoformat()


def _iso_date(value: str) -> str:
    """Converte una data HL7 ('YYYYMMDD'/'YYYYMMDDHHMMSS') in 'YYYY-MM-DD'.
    Se già in altro formato con separatori, la tronca a 10 caratteri."""
    v = (value or "").strip()
    if not v:
        return ""
    if re.fullmatch(r"\d{8,14}", v):
        return f"{v[0:4]}-{v[4:6]}-{v[6:8]}"
    return v[:10]


# ---------------------------------------------------------------------------
# XML helpers: lettura
# ---------------------------------------------------------------------------

def _attr(el: ET.Element | None, default: str = "") -> str:
    """Restituisce l'attributo V di un elemento XML, o default se assente."""
    if el is None:
        return default
    return el.get("V", default)


def _parse_obs_children(obs_list: list[ET.Element], obs_dttm: str) -> list[dict]:
    """Converte elementi OBS in lista analiti per il dict risultato."""
    results = []
    for obs in obs_list:
        obs_id_el = obs.find("OBS.observation_id")
        val_el    = obs.find("OBS.value")
        lim_el    = obs.find("OBS.normal_lo-hi_limit")

        loinc = _attr(obs_id_el)
        name  = obs_id_el.get("DN", "") if obs_id_el is not None else ""
        value = _attr(val_el)
        unit  = val_el.get("U", "") if val_el is not None else ""

        # Range riferimento da "[low;high]" → "low-high"
        ref_range = ""
        if lim_el is not None:
            raw_lim = _attr(lim_el).strip("[]")
            ref_range = raw_lim.replace(";", "-")

        # Flag e note: NTE figli dell'OBS
        # Prima NTE con valore singolo char = flag; le altre = note descrittive
        flag  = ""
        notes = []
        for nte in obs.findall("NTE"):
            nte_txt = nte.find("NTE.text")
            val = nte_txt.get("V", "") if nte_txt is not None else ""
            if val in ("*", "!", "~", "^") and not flag:
                flag = val
            elif val:
                notes.append(val)

        results.append({
            "code":      loinc,
            "name":      name,
            "value":     value,
            "unit":      unit,
            "ref_range": ref_range,
            "flag":      flag,
            "status":    "F",
            "datetime":  obs_dttm,
            "notes":     notes,
        })
    return results


def parse_obs_r01(root: ET.Element, raw_xml: str) -> dict:
    """OBS.R01 (sangue) → dict compatibile con hl7.parse_result()."""
    ctrl_id  = _attr(root.find(".//HDR.control_id"))
    svc      = root.find("SVC")
    obs_dttm = _attr(svc.find("SVC.observation_dttm") if svc is not None else None)

    pt         = svc.find("PT") if svc is not None else None
    patient_id = _attr(pt.find("PT.patient_id") if pt is not None else None)

    obs_children = pt.findall("OBS") if pt is not None else []
    results = _parse_obs_children(obs_children, obs_dttm)

    # NTE diretti di SVC = commento accept
    svc_notes = []
    if svc is not None:
        for nte in svc.findall("NTE"):
            nte_txt = nte.find("NTE.text")
            if nte_txt is not None and nte_txt.get("V"):
                svc_notes.append(nte_txt.get("V", ""))

    return {
        "message_control_id":  ctrl_id,
        "sample_key":          hl7mod.sample_key(patient_id),
        "specimen_id":         patient_id,
        "placer_order_number": "",
        "filler_order_number": "",
        "observation_type":    "OBS",
        "result_datetime":     obs_dttm or hl7mod.now_ts(),
        "results":             results,
        "svc_notes":           svc_notes,
        "raw":                 raw_xml,
        "source":              "hemoscreen_poct1a2",
    }


def parse_obs_r02(root: ET.Element, raw_xml: str) -> dict:
    """OBS.R02 (QC / EQA) → dict compatibile con hl7.parse_result()."""
    ctrl_id  = _attr(root.find(".//HDR.control_id"))
    svc      = root.find("SVC")
    role     = _attr(svc.find("SVC.role_cd") if svc is not None else None)   # LQC / PRF
    obs_dttm = _attr(svc.find("SVC.observation_dttm") if svc is not None else None)

    ctc        = svc.find("CTC") if svc is not None else None
    lot_name   = _attr(ctc.find("CTC.name") if ctc is not None else None)
    lot_number = _attr(ctc.find("CTC.lot_number") if ctc is not None else None)
    level_el   = ctc.find("CTC.level_cd") if ctc is not None else None
    level      = level_el.get("DN", "") if level_el is not None else ""

    obs_children = ctc.findall("OBS") if ctc is not None else []
    results = _parse_obs_children(obs_children, obs_dttm)

    # sample_key = nome lotto (es. "PIX201205N"), fallback al numero lotto
    sample_id = lot_name or lot_number

    return {
        "message_control_id":  ctrl_id,
        "sample_key":          hl7mod.sample_key(sample_id),
        "specimen_id":         sample_id,
        "placer_order_number": "",
        "filler_order_number": "",
        "observation_type":    role,            # "LQC" o "PRF"
        "lot_name":            lot_name,
        "lot_number":          lot_number,
        "qc_level":            level,
        "result_datetime":     obs_dttm or hl7mod.now_ts(),
        "results":             results,
        "raw":                 raw_xml,
        "source":              "hemoscreen_poct1a2",
    }


# ---------------------------------------------------------------------------
# XML builders (usano ET per l'escaping automatico dei caratteri speciali)
# ---------------------------------------------------------------------------

def _hdr(root: ET.Element, ctrl_id: str) -> None:
    hdr = ET.SubElement(root, "HDR")
    ET.SubElement(hdr, "HDR.control_id", V=ctrl_id)
    ET.SubElement(hdr, "HDR.version_id", V="POCT1")
    ET.SubElement(hdr, "HDR.creation_dttm", V=_now_iso())


def _xml_ack(ctrl_id: str, ack_ctrl: str,
             type_cd: str = "AA", note: str = "") -> str:
    root = ET.Element("ACK.R01")
    _hdr(root, ctrl_id)
    ack = ET.SubElement(root, "ACK")
    ET.SubElement(ack, "ACK.type_cd",   V=type_cd)
    ET.SubElement(ack, "ACK.control_id", V=ack_ctrl)
    if note:
        ET.SubElement(ack, "ACK.note_txt", V=note)
    return ET.tostring(root, encoding="unicode")


def _xml_req(ctrl_id: str, request_cd: str) -> str:
    """REQ.R01 da noi al device: ROBS (osservazioni pendenti) o RDEV (eventi pendenti)."""
    root = ET.Element("REQ.R01")
    _hdr(root, ctrl_id)
    req = ET.SubElement(root, "REQ")
    ET.SubElement(req, "REQ.request_cd", V=request_cd)
    return ET.tostring(root, encoding="unicode")


def _xml_end(ctrl_id: str, reason: str = "NRM") -> str:
    root = ET.Element("END.R01")
    _hdr(root, ctrl_id)
    trm = ET.SubElement(root, "TRM")
    ET.SubElement(trm, "TRM.reason_cd", V=reason)
    return ET.tostring(root, encoding="unicode")


def _xml_eot(ctrl_id: str, topic_cd: str) -> str:
    """EOT.R01 da noi al device: chiude un topic che abbiamo aperto (es. liste operatori)."""
    root = ET.Element("EOT.R01")
    _hdr(root, ctrl_id)
    eot = ET.SubElement(root, "EOT")
    ET.SubElement(eot, "EOT.topic_cd", V=topic_cd)
    return ET.tostring(root, encoding="unicode")


def _xml_dtv_simple(ctrl_id: str, command_cd: str) -> str:
    """DTV.R01 a comando singolo: START_CONTINUOUS, LOCK, UNLOCK."""
    root = ET.Element("DTV.R01")
    _hdr(root, ctrl_id)
    dtv = ET.SubElement(root, "DTV")
    ET.SubElement(dtv, "DTV.command_cd", V=command_cd)
    return ET.tostring(root, encoding="unicode")


def build_dtv_set_time(ctrl_id: str, dt: "_dt.datetime | None" = None) -> str:
    """DTV.R02(SET_TIME): imposta data/ora del device (ora locale dell'Observation
    Reviewer; il device la traduce nel proprio fuso, che non viene modificato).
    La sezione TZ è dichiarata dalla spec come "Not handled in HemoScreen": omessa."""
    root = ET.Element("DTV.R02")
    _hdr(root, ctrl_id)
    dtv = ET.SubElement(root, "DTV")
    ET.SubElement(dtv, "DTV.command_cd", V="SET_TIME")
    tm = ET.SubElement(root, "TM")
    when = dt or _dt.datetime.now(_dt.timezone.utc).astimezone()
    ET.SubElement(tm, "TM.dttm", V=when.replace(microsecond=0).isoformat())
    return ET.tostring(root, encoding="unicode")


def build_opl_r01(ctrl_id: str, operators: list[dict]) -> str:
    """OPL.R01: lista operatori completa (sostituisce quella nel device).
    operators: [{"operator_id": str, "permission_level_cd": "1"|"2"|"4", "method_cd": "ALL"}]."""
    root = ET.Element("OPL.R01")
    _hdr(root, ctrl_id)
    for op in operators:
        opr = ET.SubElement(root, "OPR")
        ET.SubElement(opr, "OPR.operator_id", V=str(op["operator_id"]))
        acc = ET.SubElement(opr, "ACC")
        ET.SubElement(acc, "ACC.method_cd", V=op.get("method_cd", "ALL"))
        ET.SubElement(acc, "ACC.permission_level_cd", V=str(op["permission_level_cd"]))
    return ET.tostring(root, encoding="unicode")


def build_opl_r02(ctrl_id: str, updates: list[dict]) -> str:
    """OPL.R02: aggiornamento incrementale operatori.
    updates: [{"action_cd": "D"|"I", "operators": [{"operator_id", "permission_level_cd" (se I), "method_cd"}]}]."""
    root = ET.Element("OPL.R02")
    _hdr(root, ctrl_id)
    for upd in updates:
        action_cd = upd["action_cd"]
        upd_el = ET.SubElement(root, "UPD")
        ET.SubElement(upd_el, "UPD.action_cd", V=action_cd)
        for op in upd.get("operators", []):
            opr = ET.SubElement(upd_el, "OPR")
            ET.SubElement(opr, "OPR.operator_id", V=str(op["operator_id"]))
            if action_cd == "I":
                acc = ET.SubElement(opr, "ACC")
                ET.SubElement(acc, "ACC.method_cd", V=op.get("method_cd", "ALL"))
                ET.SubElement(acc, "ACC.permission_level_cd", V=str(op["permission_level_cd"]))
    return ET.tostring(root, encoding="unicode")


def build_ptl_r01(ctrl_id: str, patient: dict | None) -> str:
    """PTL.R01: risposta a REQ.R01(RPAT) del device. Lista con un solo paziente
    (o vuota se il Test ID non corrisponde ad alcun ordine noto — la spec vieta
    più di un paziente per messaggio)."""
    root = ET.Element("PTL.R01")
    _hdr(root, ctrl_id)
    if patient:
        pt = ET.SubElement(root, "PT")
        ET.SubElement(pt, "PT.patient_id", V=str(patient.get("patient_id", "")))
        if patient.get("location"):
            ET.SubElement(pt, "PT.location", V=patient["location"])
        last, first = patient.get("last_name", ""), patient.get("first_name", "")
        if last or first:
            full = " ".join(p for p in (first, last) if p)
            name = ET.SubElement(pt, "PT.name", V=full)
            if last:
                ET.SubElement(name, "FAM", V=last)
            if first:
                ET.SubElement(name, "GIV", V=first)
        if patient.get("birth_date"):
            ET.SubElement(pt, "PT.birth_date", V=patient["birth_date"])
        if patient.get("gender_cd"):
            ET.SubElement(pt, "PT.gender_cd", V=patient["gender_cd"])
        if patient.get("weight"):
            ET.SubElement(pt, "PT.weight", V=str(patient["weight"]), U=patient.get("weight_unit", "kg"))
        if patient.get("height"):
            ET.SubElement(pt, "PT.height", V=str(patient["height"]), U=patient.get("height_unit", "cm"))
    return ET.tostring(root, encoding="unicode")


def _build_param(parent: ET.Element, p: dict) -> None:
    """Elemento PARAM (osservazione + range normale) comune a DTV.PIX.QC/FB."""
    param_el = ET.SubElement(parent, "PARAM")
    id_attrs = {"V": str(p["observation_id"])}
    if p.get("dn"):
        id_attrs["DN"] = p["dn"]
    id_attrs["SN"] = p.get("sn", "LN")
    ET.SubElement(param_el, "PARAM.observation_id", **id_attrs)
    limit_attrs = {"V": f"[{p['lo']};{p['hi']}]"}
    if p.get("unit"):
        limit_attrs["U"] = p["unit"]
    ET.SubElement(param_el, "PARAM.normal_lo-hi_limit", **limit_attrs)


def build_dtv_pix_qc(ctrl_id: str, lot_number: str, expiration_date: str,
                      revision: str, levels: dict[str, list[dict]]) -> str:
    """DTV.PIX.QC: un lotto di controllo qualità (fino a 3 livelli H/N/L, 20 parametri
    ciascuno). levels: {"H"|"N"|"L": [{"observation_id","dn","sn","lo","hi","unit"}]}.
    Un solo lotto per messaggio (limite di protocollo)."""
    root = ET.Element("DTV.PIX.QC")
    _hdr(root, ctrl_id)
    lot = ET.SubElement(root, "LOT")
    ET.SubElement(lot, "LOT.lot_number", V=lot_number)
    ET.SubElement(lot, "LOT.expiration_date", V=expiration_date)
    ET.SubElement(lot, "LOT.revision", V=revision)
    for level_cd, params in levels.items():
        level_el = ET.SubElement(lot, "LEVEL")
        ET.SubElement(level_el, "LEVEL.level_cd", V=level_cd)
        for p in params:
            _build_param(level_el, p)
    return ET.tostring(root, encoding="unicode")


def build_dtv_pix_fb(ctrl_id: str, effective_date: str, genders: dict[str, list[dict]]) -> str:
    """DTV.PIX.FB: range di normalità per genere (fino a 20 parametri per genere M/F).
    genders: {"M"|"F": [{"observation_id","dn","sn","lo","hi","unit"}]}."""
    root = ET.Element("DTV.PIX.FB")
    _hdr(root, ctrl_id)
    fbnr = ET.SubElement(root, "FBNR")
    ET.SubElement(fbnr, "FBNR.effective_date", V=effective_date)
    for gender_cd, params in genders.items():
        gender_el = ET.SubElement(fbnr, "GENDER")
        ET.SubElement(gender_el, "GENDER.gender_cd", V=gender_cd)
        for p in params:
            _build_param(gender_el, p)
    return ET.tostring(root, encoding="unicode")


def _set_fields(parent: ET.Element, prefix: str, fields: dict) -> None:
    """Aggiunge <prefix.chiave V="valore" /> per ogni voce non vuota di fields."""
    for key, value in fields.items():
        if value is None or value == "":
            continue
        ET.SubElement(parent, f"{prefix}.{key}", V=str(value))


def build_dtv_pix_dvcset(ctrl_id: str, dvcset: dict) -> str:
    """DTV.PIX.DVCSET: setup strumento (modo operativo, lingua, unità di misura,
    parametri visualizzati, info demografiche, lockdown). Il messaggio ha ~60+
    campi opzionali/obbligatori documentati in HS-IL-00067 §4.4.4: qui la
    struttura è generica, il chiamante passa esattamente le chiavi/valori attesi
    (senza prefisso di sezione), es.:
        {"opermode_cd": "CBC_5part", "language_cd": "English",
         "unit": {"wbc5part_cd": "10*3/uL", ...},
         "prmdis": {"wbc_cd": "SHOW", ...},
         "demogra": {"gender_cd": "ENABLE", "age_cd": "DISABLE", "display_ref18_cd": "ENABLE"},
         "lockdown": {"lockdown_mode_cd": "ENABLE", "time_inter_cd": "0012", "wbc_cd": "ENABLE", ...}}
    """
    root = ET.Element("DTV.PIX.DVCSET")
    _hdr(root, ctrl_id)
    dvc = ET.SubElement(root, "DVCSET")
    if dvcset.get("opermode_cd"):
        ET.SubElement(dvc, "DVCSET.opermode_cd", V=dvcset["opermode_cd"])
    if dvcset.get("language_cd"):
        ET.SubElement(dvc, "DVCSET.language_cd", V=dvcset["language_cd"])
    for section, prefix in (("unit", "UNIT"), ("prmdis", "PRMDIS"),
                            ("demogra", "DEMOGRA"), ("lockdown", "LOCKDOWN")):
        if dvcset.get(section):
            el = ET.SubElement(dvc, prefix)
            _set_fields(el, prefix, dvcset[section])
    return ET.tostring(root, encoding="unicode")


# ---------------------------------------------------------------------------
# Trasporto: framing MLLP su TCP (riuso costanti da mllp.py)
# ---------------------------------------------------------------------------

def _mllp_recv(sock: socket.socket, timeout: float = 65.0, buf: bytearray | None = None) -> bytes:
    """Legge un frame MLLP (0x0B…0x1C) dal socket. Ritorna b"" su connessione chiusa/timeout."""
    sock.settimeout(timeout)
    if buf is None:
        buf = bytearray()
    while True:
        if SB[0] in buf:
            sb_idx = buf.index(SB[0])
            if EB[0] in buf[sb_idx:]:
                eb_idx = buf.index(EB[0], sb_idx)
                data = buf[sb_idx + 1:eb_idx]
                del buf[:eb_idx + 1]
                return bytes(data)
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            return b""
        if not chunk:
            return b""
        buf.extend(chunk)

def _mllp_send(sock: socket.socket, xml_str: str) -> None:
    """Invia un frame MLLP con contenuto XML."""
    sock.sendall(SB + xml_str.encode("utf-8") + EB + CR)


# ---------------------------------------------------------------------------
# Registro conversazioni attive (per l'invio di direttive da API/CLI)
# ---------------------------------------------------------------------------

_REGISTRY_LOCK = threading.Lock()
_REGISTRY: dict[str, "_Conversation"] = {}


def _find_conversation(target: str | None) -> "_Conversation | None":
    with _REGISTRY_LOCK:
        if target:
            conv = _REGISTRY.get(target)
            if conv:
                return conv
            for key, c in _REGISTRY.items():
                if key.startswith("addr:") and target in key:
                    return c
            return None
        for key, c in _REGISTRY.items():
            if not key.startswith("addr:"):
                return c
        for c in _REGISTRY.values():
            return c
        return None


def connected_devices() -> list[str]:
    """Elenco device_id/serial delle conversazioni POCT1-A2 attualmente connesse."""
    with _REGISTRY_LOCK:
        return [k for k in _REGISTRY if not k.startswith("addr:")]


def send_directive(target: str | None, builder: Callable[[str], str],
                    topic_cd_after_ack: str | None = None, label: str = "") -> bool:
    """Accoda una direttiva per la conversazione POCT1-A2 attiva identificata da
    `target` (device_id/serial_id, o None per la prima connessione disponibile).
    La direttiva viene inviata al primo punto protocollarmente sicuro della
    conversazione (nessuna richiesta nostra/del device in sospeso), una alla
    volta. Ritorna False se nessun device corrispondente è connesso al momento
    della chiamata (tipico se richiamata da API/CLI e lo strumento è spento)."""
    conv = _find_conversation(target)
    if conv is None:
        return False
    conv.enqueue_directive(builder, topic_cd_after_ack, label)
    return True


def send_lock(target: str | None = None) -> bool:
    return send_directive(target, lambda c: _xml_dtv_simple(c, "LOCK"), None, "LOCK")


def send_unlock(target: str | None = None) -> bool:
    return send_directive(target, lambda c: _xml_dtv_simple(c, "UNLOCK"), None, "UNLOCK")


def send_set_time(target: str | None = None, dt: "_dt.datetime | None" = None) -> bool:
    return send_directive(target, lambda c: build_dtv_set_time(c, dt), None, "SET_TIME")


def send_operator_list(target: str | None, operators: list[dict]) -> bool:
    return send_directive(target, lambda c: build_opl_r01(c, operators), "OP_LST", "OPL.R01")


def send_operator_list_incremental(target: str | None, updates: list[dict]) -> bool:
    return send_directive(target, lambda c: build_opl_r02(c, updates), "OP_LST_I", "OPL.R02")


def send_qc_lot(target: str | None, lot_number: str, expiration_date: str,
                revision: str, levels: dict[str, list[dict]]) -> bool:
    return send_directive(
        target, lambda c: build_dtv_pix_qc(c, lot_number, expiration_date, revision, levels),
        None, "DTV.PIX.QC",
    )


def send_gender_normal_range(target: str | None, effective_date: str,
                              genders: dict[str, list[dict]]) -> bool:
    return send_directive(
        target, lambda c: build_dtv_pix_fb(c, effective_date, genders), None, "DTV.PIX.FB",
    )


def send_device_setup(target: str | None, dvcset: dict) -> bool:
    return send_directive(target, lambda c: build_dtv_pix_dvcset(c, dvcset), None, "DTV.PIX.DVCSET")


# ---------------------------------------------------------------------------
# Gestione conversazione POCT1-A2
# ---------------------------------------------------------------------------

class _Conversation:
    """Gestisce una singola conversazione POCT1-A2 su una connessione TCP persistente."""

    def __init__(self, sock: socket.socket, addr: tuple,
                 store: Store, continuous_mode: bool, timeout: float,
                 monitor: DeviceMonitor | None = None):
        self._sock = sock
        self._addr = addr
        self._store = store
        self._continuous_mode = continuous_mode
        self._timeout = timeout
        self._monitor = monitor
        self._ctrl_gen: Iterator[int] = itertools.count(1)
        self._pending_end_ack = False    # True dopo che abbiamo inviato END.R01
        self._continuous_active = False  # True dopo ACK positivo a START_CONTINUOUS
        self._continuous_start_ctrl: str | None = None  # ctrl_id del nostro START_CONTINUOUS in attesa
        self._pending_new_events = 0     # new_events_qty ancora da richiedere (RDEV)
        self._requested_topics: list[str] = []  # topic di cui attendiamo l'EOT dal device
        self._pending_ack_topics: dict[str, str | None] = {}  # ctrl_id direttiva -> topic EOT da inviare dopo l'ACK
        self._directive_queue: "queue.Queue[tuple[Callable[[str], str], str | None, str]]" = queue.Queue()
        self._device_id: str | None = None
        self._fallback_name = f"HemoScreen@{addr[0]}:{addr[1]}"
        self._ibuf = bytearray()

    # --- helpers interni ----------------------------------------------------

    def _next_ctrl(self) -> str:
        return str(next(self._ctrl_gen))

    def _send(self, xml_str: str) -> None:
        _mllp_send(self._sock, xml_str)

    def _recv(self) -> tuple[str | None, ET.Element | None]:
        """Riceve il prossimo messaggio XML. Ritorna (tipo_msg, root) o (None, None)."""
        raw = _mllp_recv(self._sock, self._timeout, self._ibuf)
        if not raw:
            return None, None

        xml_text = raw.decode("utf-8", errors="replace").strip()
        # Rimuovi eventuale dichiarazione <?xml … ?>
        if xml_text.startswith("<?"):
            idx = xml_text.find("?>")
            xml_text = xml_text[idx + 2:].lstrip() if idx != -1 else xml_text

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            LOG.warning("POCT1-A2 %s: errore parsing XML: %s", self._addr, exc)
            return "PARSE_ERROR", None

        return root.tag, root

    def _store_result(self, result: dict) -> None:
        """Salva il risultato nel store, tenta completamento ordine."""
        key = result.get("sample_key", "")
        if not key:
            LOG.warning("POCT1-A2 %s: risultato senza sample_key -> unmatched", self._addr)
            self._store.add_unmatched(result, source_instrument=self._device_id or self._fallback_name)
            return

        order = self._store.get_order(key)
        if not order:
            self._store.add_unmatched(result, source_instrument=self._device_id or self._fallback_name)
            LOG.warning(
                "POCT1-A2 %s: nessun ordine per sample=%s (obs_type=%s) -> unmatched",
                self._addr, key, result.get("observation_type"),
            )
        else:
            self._store.add_result(key, result, source_instrument=self._device_id or self._fallback_name)
            try_complete(self._store, key)
            LOG.info(
                "POCT1-A2 %s: risultato abbinato sample=%s obs_type=%s analiti=%d",
                self._addr, key, result.get("observation_type"),
                len(result.get("results", [])),
            )

    def _lookup_patient(self, test_id: str) -> dict | None:
        """Cerca l'ordine associato al Test ID (sample_key) per rispondere a
        REQ.R01(RPAT) con PTL.R01. None se non trovato (-> lista vuota)."""
        if not test_id:
            return None
        order = self._store.get_order(hl7mod.sample_key(test_id))
        if not order:
            return None
        try:
            patient = json.loads(order.get("patient_json") or "{}")
        except (TypeError, ValueError):
            patient = {}
        if not patient or not (patient.get("last_name") or patient.get("first_name")):
            return {"patient_id": test_id} if patient else None
        result = {
            "patient_id": test_id,
            "last_name": patient.get("last_name", ""),
            "first_name": patient.get("first_name", ""),
        }
        if patient.get("birth_date"):
            result["birth_date"] = _iso_date(patient["birth_date"])
        sex = (patient.get("sex") or "").upper()
        if sex in ("M", "F"):
            result["gender_cd"] = sex
        return result

    def enqueue_directive(self, builder: Callable[[str], str],
                          topic_cd_after_ack: str | None, label: str) -> None:
        self._directive_queue.put((builder, topic_cd_after_ack, label))

    def _drain_directives(self) -> None:
        """Invia al più una direttiva accodata, solo se non siamo in attesa di
        risposta (né nostra né del device) — vincolo richiesto dalla spec per
        Update Lists e messaggi vendor-specific."""
        if self._pending_end_ack or self._pending_ack_topics or self._requested_topics:
            return
        try:
            builder, topic_cd, label = self._directive_queue.get_nowait()
        except queue.Empty:
            return
        ctrl_id = self._next_ctrl()
        self._send(builder(ctrl_id))
        self._pending_ack_topics[ctrl_id] = topic_cd
        LOG.info("POCT1-A2 %s: inviata direttiva %s (ctrl=%s)", self._addr, label or "?", ctrl_id)

    def _finish_basic_or_start_continuous(self) -> None:
        """Nessuna richiesta pendente: in modalità continua avvia il continuo
        (se non già attivo), altrimenti (modalità base) termina la conversazione."""
        if self._continuous_mode and not self._continuous_active and not self._continuous_start_ctrl:
            ctrl = self._next_ctrl()
            self._send(_xml_dtv_simple(ctrl, "START_CONTINUOUS"))
            self._continuous_start_ctrl = ctrl
            LOG.info("POCT1-A2 %s: richiesta avvio modalita' continua (ctrl=%s)", self._addr, ctrl)
        elif not self._continuous_mode:
            self._send(_xml_end(self._next_ctrl()))
            self._pending_end_ack = True

    # --- loop principale ---------------------------------------------------

    def run(self) -> None:
        LOG.info("POCT1-A2: nuova connessione da %s", self._addr)
        addr_key = f"addr:{self._addr}"
        with _REGISTRY_LOCK:
            _REGISTRY[addr_key] = self
        eot_count = 0

        try:
            while True:
                msg_type, root = self._recv()

                if msg_type is None:
                    LOG.debug("POCT1-A2: connessione chiusa da %s", self._addr)
                    break

                if msg_type == "PARSE_ERROR" or root is None:
                    continue

                ctrl_id = _attr(root.find(".//HDR.control_id"))

                # ---- HEL.R01 ------------------------------------------------
                if msg_type == "HEL.R01":
                    dev = root.find("DEV")
                    device_id = _attr(dev.find("DEV.device_id") if dev is not None else None)
                    serial_id = _attr(dev.find("DEV.serial_id") if dev is not None else None)
                    self._device_id = serial_id or device_id or None
                    if self._device_id:
                        with _REGISTRY_LOCK:
                            _REGISTRY[self._device_id] = self
                    LOG.debug("POCT1-A2 %s: HEL.R01 device_id=%s", self._addr, self._device_id)
                    self._send(_xml_ack(self._next_ctrl(), ctrl_id))

                # ---- DST.R01 ------------------------------------------------
                elif msg_type == "DST.R01":
                    dst = root.find("DST")
                    new_obs = int(_attr(
                        dst.find("DST.new_observations_qty") if dst is not None else None, "0") or "0")
                    new_events = int(_attr(
                        dst.find("DST.new_events_qty") if dst is not None else None, "0") or "0")
                    cond = _attr(dst.find("DST.condition_cd") if dst is not None else None)
                    LOG.debug("POCT1-A2 %s: DST.R01 new_obs=%d new_events=%d cond=%s",
                              self._addr, new_obs, new_events, cond)
                    self._send(_xml_ack(self._next_ctrl(), ctrl_id))
                    self._pending_new_events = new_events
                    if new_obs > 0:
                        self._send(_xml_req(self._next_ctrl(), "ROBS"))
                        self._requested_topics.append("OBS")
                    elif new_events > 0:
                        self._send(_xml_req(self._next_ctrl(), "RDEV"))
                        self._requested_topics.append("D_EV")
                        self._pending_new_events = 0
                    else:
                        self._finish_basic_or_start_continuous()

                # ---- OBS.R01 (sangue) ---------------------------------------
                elif msg_type == "OBS.R01":
                    raw_xml = ET.tostring(root, encoding="unicode")
                    result  = parse_obs_r01(root, raw_xml)
                    self._store_result(result)
                    self._send(_xml_ack(self._next_ctrl(), ctrl_id))

                # ---- OBS.R02 (QC / EQA) -------------------------------------
                elif msg_type == "OBS.R02":
                    raw_xml = ET.tostring(root, encoding="unicode")
                    result  = parse_obs_r02(root, raw_xml)
                    self._store_result(result)
                    self._send(_xml_ack(self._next_ctrl(), ctrl_id))

                # ---- EOT.R01 (il device chiude un topic) ---------------------
                elif msg_type == "EOT.R01":
                    eot_count += 1
                    topic_cd = _attr(root.find(".//EOT.topic_cd"))
                    LOG.debug("POCT1-A2 %s: EOT.R01 #%d topic=%s", self._addr, eot_count, topic_cd)
                    self._send(_xml_ack(self._next_ctrl(), ctrl_id))

                    if topic_cd in self._requested_topics:
                        self._requested_topics.remove(topic_cd)
                    elif self._requested_topics:
                        self._requested_topics.pop(0)

                    if topic_cd == "OBS" and self._pending_new_events > 0:
                        self._send(_xml_req(self._next_ctrl(), "RDEV"))
                        self._requested_topics.append("D_EV")
                        self._pending_new_events = 0
                    elif not self._requested_topics:
                        self._finish_basic_or_start_continuous()

                # ---- END.R01 (il device vuole terminare) --------------------
                elif msg_type == "END.R01":
                    self._send(_xml_ack(self._next_ctrl(), ctrl_id))
                    LOG.info("POCT1-A2 %s: conversazione terminata su richiesta device",
                             self._addr)
                    break

                # ---- ACK.R01 (risposta ai nostri messaggi) ------------------
                elif msg_type == "ACK.R01":
                    ack_el  = root.find("ACK")
                    type_cd = _attr(ack_el.find("ACK.type_cd") if ack_el is not None else None)
                    echoed  = _attr(ack_el.find("ACK.control_id") if ack_el is not None else None)
                    LOG.debug("POCT1-A2 %s: ACK.R01 type=%s per ctrl=%s",
                              self._addr, type_cd, echoed)
                    if self._pending_end_ack:
                        # ACK al nostro END.R01: chiudi la connessione
                        LOG.info("POCT1-A2 %s: END.R01 confermato, conversazione chiusa",
                                 self._addr)
                        break
                    if echoed and echoed == self._continuous_start_ctrl:
                        if type_cd == "AA":
                            self._continuous_active = True
                            LOG.info("POCT1-A2 %s: modalita' continua avviata", self._addr)
                        self._continuous_start_ctrl = None
                    elif echoed in self._pending_ack_topics:
                        topic = self._pending_ack_topics.pop(echoed)
                        LOG.info("POCT1-A2 %s: direttiva (ctrl=%s) ACKata (%s)",
                                 self._addr, echoed, type_cd)
                        if topic:
                            self._send(_xml_eot(self._next_ctrl(), topic))

                # ---- KPA.R01 (keep-alive) -----------------------------------
                elif msg_type == "KPA.R01":
                    self._send(_xml_ack(self._next_ctrl(), ctrl_id))
                    LOG.debug("POCT1-A2 %s: KPA.R01 -> ACK", self._addr)

                # ---- EVS.R01 (eventi strumento) -----------------------------
                elif msg_type == "EVS.R01":
                    severity_map = {"C": "CRITICAL", "W": "WARNING", "N": "INFO"}
                    for evt in root.findall(".//EVT"):
                        desc      = _attr(evt.find("EVT.description"))
                        sev       = _attr(evt.find("EVT.severity_cd"))
                        num       = _attr(evt.find("EVT.number"))
                        mode      = _attr(evt.find("EVT.mode"))
                        sample_id = _attr(evt.find("EVT.sample_id"))
                        opr       = evt.find("OPR")
                        operator  = _attr(opr.find("OPR.operator_id") if opr is not None else None)
                        LOG.info("POCT1-A2 evento %s [%s] #%s %s", self._addr, sev, num, desc)
                        self._store.audit_log(
                            "poct1a2_device_event",
                            sample_key=hl7mod.sample_key(sample_id) if sample_id else None,
                            instrument=self._device_id or self._fallback_name,
                            details=f"#{num} {desc} (mode={mode}, operator={operator})".strip(),
                            severity=severity_map.get(sev, "INFO"),
                        )
                    self._send(_xml_ack(self._next_ctrl(), ctrl_id))

                # ---- ESC.R01 (escape: nessuna risposta prevista) ------------
                elif msg_type == "ESC.R01":
                    esc = root.find("ESC")
                    esc_ctrl = _attr(esc.find("ESC.esc_control_id") if esc is not None else None)
                    detail   = _attr(esc.find("ESC.detail_cd") if esc is not None else None)
                    note     = _attr(esc.find("ESC.note_txt") if esc is not None else None)
                    LOG.warning("POCT1-A2 %s: ESC.R01 ricevuto (esc_control_id=%s detail=%s note=%s)",
                                self._addr, esc_ctrl, detail, note)
                    self._store.audit_log(
                        "poct1a2_escape", instrument=self._device_id or self._fallback_name,
                        details=f"esc_control_id={esc_ctrl} detail={detail} note={note}".strip(),
                        severity="WARNING",
                    )
                    if esc_ctrl and esc_ctrl == self._continuous_start_ctrl:
                        LOG.warning("POCT1-A2 %s: device ha rifiutato la modalita' continua "
                                    "(ESC su START_CONTINUOUS)", self._addr)
                        self._continuous_start_ctrl = None
                    self._pending_ack_topics.pop(esc_ctrl, None)
                    # Nessuna risposta prevista dal protocollo per ESC.R01.

                # ---- REQ.R01 (il device ci chiede qualcosa, es. RPAT) -------
                elif msg_type == "REQ.R01":
                    req = root.find("REQ")
                    request_cd = _attr(req.find("REQ.request_cd") if req is not None else None)
                    if request_cd == "RPAT":
                        pt = req.find("PT") if req is not None else None
                        test_id = _attr(pt.find("PT.patient_id") if pt is not None else None)
                        patient = self._lookup_patient(test_id)
                        ptl_ctrl = self._next_ctrl()
                        self._send(build_ptl_r01(ptl_ctrl, patient))
                        # In attesa dell'ACK del device al nostro PTL.R01: nessun EOT
                        # da inviare dopo, ma blocca comunque _drain_directives()
                        # finché il device non conferma di averlo ricevuto.
                        self._pending_ack_topics[ptl_ctrl] = None
                        LOG.info("POCT1-A2 %s: PTL.R01 inviato per test_id=%s (%s)",
                                 self._addr, test_id, "trovato" if patient else "non trovato")
                    else:
                        LOG.warning("POCT1-A2 %s: REQ.R01 dal device con request_cd inatteso %r",
                                    self._addr, request_cd)

                # ---- Messaggi non riconosciuti -------------------------------
                else:
                    LOG.warning("POCT1-A2 %s: tipo messaggio sconosciuto %r",
                                self._addr, msg_type)
                    self._send(_xml_ack(self._next_ctrl(), ctrl_id, type_cd="AE",
                                       note=f"Messaggio non supportato: {msg_type}"))

                # Dopo lo smistamento: se questo messaggio era HEL.R01, self._device_id
                # e' gia' risolto dal serial/device_id del device, cosi' l'heartbeat
                # (incluso il primo, per questa stessa connessione) usa sempre la
                # stessa identita' — registrarlo prima dello smistamento creava due
                # righe strumento distinte (una "fallback" per l'HEL.R01, una per
                # serial_id da qui in poi) per lo stesso device fisico.
                if self._monitor:
                    self._monitor.record_message(
                        self._device_id or self._fallback_name,
                        self._addr[0], self._addr[1], "POCT1-A2",
                    )

                self._drain_directives()

        except Exception:
            LOG.exception("POCT1-A2 %s: errore inatteso nella conversazione", self._addr)
        finally:
            with _REGISTRY_LOCK:
                _REGISTRY.pop(addr_key, None)
                if self._device_id:
                    _REGISTRY.pop(self._device_id, None)
            try:
                self._sock.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# HemoscreenPoct1A2Receiver
# ---------------------------------------------------------------------------

class HemoscreenPoct1A2Receiver:
    """Server POCT1-A2 per PixCell HemoScreen (HS-IL-00067 Rev.06).

    Accetta connessioni TCP e gestisce il protocollo POCT1-A2 XML con framing MLLP.
    Per ogni connessione viene avviato un thread che esegue la conversazione completa.

    Parametri
    ---------
    store            : Store SQLite del middleware
    host / port      : indirizzo di ascolto
    continuous_mode  : se True, dopo le richieste pendenti avvia START_CONTINUOUS
    timeout          : secondi di attesa per ogni messaggio in ingresso (default 65 s,
                       leggermente superiore all'application_timeout del device = 60 s)
    monitor          : DeviceMonitor opzionale, per comparire nella dashboard/CLI
                       strumenti (stesso pattern di OrderReceiver/ResultReceiver)
    """

    def __init__(self, store: Store, host: str, port: int,
                 continuous_mode: bool = False, timeout: float = 65.0,
                 monitor: DeviceMonitor | None = None):
        self.store = store
        self.host, self.port = host, port
        self.continuous_mode = continuous_mode
        self.timeout = timeout
        self.monitor = monitor
        self._srv: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> "HemoscreenPoct1A2Receiver":
        outer = self

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                conv = _Conversation(
                    self.request,
                    self.client_address,
                    outer.store,
                    outer.continuous_mode,
                    outer.timeout,
                    outer.monitor,
                )
                conv.run()

        class _Srv(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads      = True

        self._srv = _Srv((self.host, self.port), _Handler)
        self._thread = threading.Thread(
            target=self._srv.serve_forever, daemon=True,
            name="poct1a2-server",
        )
        self._thread.start()
        LOG.info(
            "HemoscreenPoct1A2Receiver in ascolto su %s:%s "
            "(POCT1-A2 XML, framing MLLP, continuous=%s)",
            self.host, self.port, self.continuous_mode,
        )
        return self

    def stop(self) -> None:
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()
