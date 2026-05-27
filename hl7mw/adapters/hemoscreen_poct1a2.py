"""
hl7mw.adapters.hemoscreen_poct1a2 — Adapter POCT1-A2 per PixCell HemoScreen.

Riferimento: HS-IL-00067 Rev.06 «HemoScreen POCT1-A2 Connectivity Protocol».

Protocollo: XML su TCP con framing MLLP (0x0B … 0x1C 0x0D).
Il middleware agisce come Observation Reviewer (server).

Flusso di conversazione supportato
───────────────────────────────────
Modalità base (pull):
  Device → HEL.R01   → noi: ACK.R01(AA)
  Device → DST.R01   → noi: ACK.R01(AA) [+ REQ.R01(ROBS) se new_obs > 0]
  Device → ACK.R01   (per il nostro REQ, ignorato)
  Device → OBS.R01   → noi: ACK.R01(AA)   [ripetuto per ogni osservazione]
  Device → OBS.R02   → noi: ACK.R01(AA)   [QC/EQA]
  Device → EVS.R01   → noi: ACK.R01(AA)
  Device → EOT.R01   → noi: ACK.R01(AA) + END.R01(NRM)
  Device → ACK.R01   (per il nostro END) → chiusura connessione

Modalità continua (continuous_mode=True):
  Identica alla base fino alla risposta del DST.R01.
  Dopo REQ.R01(ROBS) + osservazioni pendenti + EOT, noi mandiamo
  DTV.R01(START_CONTINUOUS). Da quel punto il device invia OBS.R01
  autonomamente; noi ACKiamo ogni risultato.
  La connessione termina con END.R01 (da noi o dal device).

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
import logging
import socket
import socketserver
import threading
import xml.etree.ElementTree as ET
from typing import Iterator

from .. import hl7 as hl7mod
from ..mllp import SB, EB, CR
from ..pipeline import try_complete
from ..store import Store

LOG = logging.getLogger("hl7mw")

# ---------------------------------------------------------------------------
# Helpers timestamp
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Timestamp locale in formato ISO 8601 con offset timezone (es. 2024-01-03T12:00:00+01:00)."""
    return _dt.datetime.now(_dt.timezone.utc).astimezone().replace(microsecond=0).isoformat()


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

def _xml_ack(ctrl_id: str, ack_ctrl: str,
             type_cd: str = "AA", note: str = "") -> str:
    root = ET.Element("ACK.R01")
    hdr = ET.SubElement(root, "HDR")
    ET.SubElement(hdr, "HDR.control_id", V=ctrl_id)
    ET.SubElement(hdr, "HDR.version_id",  V="POCT1")
    ET.SubElement(hdr, "HDR.creation_dttm", V=_now_iso())
    ack = ET.SubElement(root, "ACK")
    ET.SubElement(ack, "ACK.type_cd",   V=type_cd)
    ET.SubElement(ack, "ACK.control_id", V=ack_ctrl)
    if note:
        ET.SubElement(ack, "ACK.note_txt", V=note)
    return ET.tostring(root, encoding="unicode")


def _xml_req_obs(ctrl_id: str) -> str:
    root = ET.Element("REQ.R01")
    hdr = ET.SubElement(root, "HDR")
    ET.SubElement(hdr, "HDR.control_id",   V=ctrl_id)
    ET.SubElement(hdr, "HDR.version_id",   V="POCT1")
    ET.SubElement(hdr, "HDR.creation_dttm", V=_now_iso())
    req = ET.SubElement(root, "REQ")
    ET.SubElement(req, "REQ.request_cd", V="ROBS")
    return ET.tostring(root, encoding="unicode")


def _xml_end(ctrl_id: str, reason: str = "NRM") -> str:
    root = ET.Element("END.R01")
    hdr = ET.SubElement(root, "HDR")
    ET.SubElement(hdr, "HDR.control_id",   V=ctrl_id)
    ET.SubElement(hdr, "HDR.version_id",   V="POCT1")
    ET.SubElement(hdr, "HDR.creation_dttm", V=_now_iso())
    trm = ET.SubElement(root, "TRM")
    ET.SubElement(trm, "TRM.reason_cd", V=reason)
    return ET.tostring(root, encoding="unicode")


def _xml_start_continuous(ctrl_id: str) -> str:
    root = ET.Element("DTV.R01")
    hdr = ET.SubElement(root, "HDR")
    ET.SubElement(hdr, "HDR.control_id",   V=ctrl_id)
    ET.SubElement(hdr, "HDR.version_id",   V="POCT1")
    ET.SubElement(hdr, "HDR.creation_dttm", V=_now_iso())
    dtv = ET.SubElement(root, "DTV")
    ET.SubElement(dtv, "DTV.command_cd", V="START_CONTINUOUS")
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
# Gestione conversazione POCT1-A2
# ---------------------------------------------------------------------------

class _Conversation:
    """Gestisce una singola conversazione POCT1-A2 su una connessione TCP persistente."""

    def __init__(self, sock: socket.socket, addr: tuple,
                 store: Store, continuous_mode: bool, timeout: float):
        self._sock = sock
        self._addr = addr
        self._store = store
        self._continuous_mode = continuous_mode
        self._timeout = timeout
        self._ctrl_gen: Iterator[int] = itertools.count(1)
        self._pending_end_ack = False    # True dopo che abbiamo inviato END.R01
        self._continuous_active = False  # True dopo aver avviato la modalità continua
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
            self._store.add_unmatched(result)
            return

        order = self._store.get_order(key)
        if not order:
            self._store.add_unmatched(result)
            LOG.warning(
                "POCT1-A2 %s: nessun ordine per sample=%s (obs_type=%s) -> unmatched",
                self._addr, key, result.get("observation_type"),
            )
        else:
            self._store.add_result(key, result)
            try_complete(self._store, key)
            LOG.info(
                "POCT1-A2 %s: risultato abbinato sample=%s obs_type=%s analiti=%d",
                self._addr, key, result.get("observation_type"),
                len(result.get("results", [])),
            )

    # --- loop principale ---------------------------------------------------

    def run(self) -> None:
        LOG.info("POCT1-A2: nuova connessione da %s", self._addr)
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
                    LOG.debug("POCT1-A2 %s: HEL.R01", self._addr)
                    self._send(_xml_ack(self._next_ctrl(), ctrl_id))

                # ---- DST.R01 ------------------------------------------------
                elif msg_type == "DST.R01":
                    dst = root.find("DST")
                    new_obs_str = _attr(
                        dst.find("DST.new_observations_qty") if dst is not None else None,
                        "0",
                    )
                    new_obs = int(new_obs_str or "0")
                    cond    = _attr(dst.find("DST.condition_cd") if dst is not None else None)
                    LOG.debug("POCT1-A2 %s: DST.R01 new_obs=%d cond=%s",
                              self._addr, new_obs, cond)
                    self._send(_xml_ack(self._next_ctrl(), ctrl_id))
                    if new_obs > 0:
                        # Richiedi le osservazioni pendenti
                        self._send(_xml_req_obs(self._next_ctrl()))

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

                # ---- EOT.R01 ------------------------------------------------
                elif msg_type == "EOT.R01":
                    eot_count += 1
                    LOG.debug("POCT1-A2 %s: EOT.R01 #%d", self._addr, eot_count)
                    self._send(_xml_ack(self._next_ctrl(), ctrl_id))

                    if self._continuous_mode and not self._continuous_active:
                        # Primo EOT in modalità continua: lancia la modalità continua
                        self._send(_xml_start_continuous(self._next_ctrl()))
                        self._continuous_active = True
                        LOG.info("POCT1-A2 %s: avviata modalità continua", self._addr)
                    elif not self._continuous_mode:
                        # Modalità base: dopo EOT invia END e attendi l'ACK finale
                        self._send(_xml_end(self._next_ctrl()))
                        self._pending_end_ack = True

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

                # ---- KPA.R01 (keep-alive) -----------------------------------
                elif msg_type == "KPA.R01":
                    self._send(_xml_ack(self._next_ctrl(), ctrl_id))
                    LOG.debug("POCT1-A2 %s: KPA.R01 -> ACK", self._addr)

                # ---- EVS.R01 (eventi strumento) -----------------------------
                elif msg_type == "EVS.R01":
                    for evt in root.findall("EVT"):
                        desc = _attr(evt.find("EVT.description"))
                        sev  = _attr(evt.find("EVT.severity_cd"))
                        num  = _attr(evt.find("EVT.number"))
                        LOG.info("POCT1-A2 evento %s [%s] #%s: %s",
                                 self._addr, sev, num, desc)
                    self._send(_xml_ack(self._next_ctrl(), ctrl_id))

                # ---- Messaggi non riconosciuti -------------------------------
                else:
                    LOG.warning("POCT1-A2 %s: tipo messaggio sconosciuto %r",
                                self._addr, msg_type)
                    self._send(_xml_ack(self._next_ctrl(), ctrl_id, type_cd="AE",
                                       note=f"Messaggio non supportato: {msg_type}"))

        except Exception:
            LOG.exception("POCT1-A2 %s: errore inatteso nella conversazione", self._addr)
        finally:
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
    continuous_mode  : se True, dopo le obs pendenti avvia START_CONTINUOUS
    timeout          : secondi di attesa per ogni messaggio in ingresso (default 65 s,
                       leggermente superiore all'application_timeout del device = 60 s)
    """

    def __init__(self, store: Store, host: str, port: int,
                 continuous_mode: bool = False, timeout: float = 65.0):
        self.store = store
        self.host, self.port = host, port
        self.continuous_mode = continuous_mode
        self.timeout = timeout
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
