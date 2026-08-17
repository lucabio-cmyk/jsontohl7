"""
hl7mw.adapters.hemoscreen_hl7 — Adapter HL7 v2.4 per PixCell HemoScreen.

Riferimento: GEN-SW-HS-00001-P01 Rev.02 «HemoScreen HL7 Connectivity Protocol».

Differenze rispetto al parser HL7 generico del middleware:
  - sample_key estratta da PID-2 (HemoScreen Test Identifier), non da SPM/OBR
  - OBX-3 contiene il nome diretto del parametro (WBC, RBC, …), non la tripla LOINC
  - OBX-8 flag HL7-escaped (\\~ → ~, \\^ → ^)
  - NTE dopo OBR = commento accept; NTE dopo OBX = nota/descrizione del flag
  - OBR-4 = tipo osservazione: "OBS" | "LQC" | "PRF"
  - OBX-15 = serial ID del dispositivo
  - ACK risposto con version HL7 2.4 (richiesto dal firmware HemoScreen)

Solo stdlib. Nessuna dipendenza esterna.
"""
from __future__ import annotations

import logging

from .. import hl7
from ..mllp import MllpServer
from ..monitor import DeviceMonitor
from ..pipeline import try_complete
from ..store import Store

LOG = logging.getLogger("hl7mw")

# ---------------------------------------------------------------------------
# Tabella nome parametro HemoScreen → LOINC
# (da sezione 2.4 del documento e tabella POCT1-A2 section 2.5)
# ---------------------------------------------------------------------------
HEMOSCREEN_LOINC: dict[str, str] = {
    "WBC":   "6690-2",
    "RBC":   "789-8",
    "HGB":   "718-7",
    "HCT":   "4544-3",
    "MCV":   "787-2",
    "MCH":   "785-6",
    "MCHC":  "786-4",
    "RDW":   "788-0",
    "PLT":   "777-3",
    "MPV":   "32623-1",
    "NEU#":  "751-8",
    "LYM#":  "731-0",
    "MON#":  "742-7",
    "EOS#":  "711-2",
    "BAS#":  "704-7",
    "NEU%":  "770-8",
    "LYM%":  "736-9",
    "MON%":  "5905-5",
    "EOS%":  "713-8",
    "BAS%":  "706-2",
}

# Valori speciali (non numerici) ammessi in OBX-5
SPECIAL_VALUES = {"LL", "HH", "ABN", "---"}


def _unescape_flag(val: str) -> str:
    """Decodifica le sequenze di escape HL7 usate nei flag (\\~ → ~, \\^ → ^)."""
    return (val
            .replace(r"\~", "~")
            .replace(r"\^", "^")
            .replace("\\\\F\\\\", "|")
            .replace("\\\\S\\\\", "^")
            .replace("\\\\R\\\\", "~")
            .replace("\\\\T\\\\", "&")
            .replace("\\\\E\\\\", "\\\\"))


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_hemoscreen_hl7(message: str) -> dict:
    """ORU^R01 HemoScreen → dict risultato compatibile con hl7.parse_result().

    Estrae:
      sample_key      : da PID-2 (HemoScreen Test Identifier)
      observation_type: da OBR-4 (OBS / LQC / PRF)
      results         : lista 20 analiti (code=LOINC, name, value, unit, …)
      device_serial   : da OBX-15 (serial ID dispositivo)
      obr_notes       : NTE tra OBR e primo OBX (commento accept)

    Raises hl7.Hl7Error se il tipo di messaggio non è ORU.
    """
    segs = [s for s in hl7.split_segments(message) if s]
    mtype = hl7.msh_field(hl7.split_segments(message), 9)
    if not mtype.startswith("ORU"):
        raise hl7.Hl7Error(
            f"HemoScreen HL7: atteso ORU^R01, ricevuto {mtype!r}"
        )

    # --- PID-2: HemoScreen Test Identifier ----------------------------------
    pid = next((s for s in segs if s[0] == "PID"), None)
    sample_id = hl7.get(pid or [], 2).strip()   # PID[2] = PID-2

    # --- OBR -----------------------------------------------------------------
    obr = next((s for s in segs if s[0] == "OBR"), None)
    obs_type     = hl7.get(obr or [], 4).strip()   # OBR-4: OBS/LQC/PRF
    obs_datetime = hl7.get(obr or [], 7).strip()   # OBR-7: datetime osservazione

    # --- NTE tra OBR e primo OBX (commento accept) --------------------------
    obr_idx   = next((i for i, s in enumerate(segs) if s[0] == "OBR"), -1)
    first_obx = next((i for i, s in enumerate(segs) if s[0] == "OBX"), len(segs))
    obr_notes = []
    if obr_idx != -1:
        obr_notes = [
            hl7.get(segs[i], 3)
            for i in range(obr_idx + 1, first_obx)
            if segs[i][0] == "NTE"
        ]

    # --- OBX (+ NTE seguenti) -----------------------------------------------
    results: list[dict] = []
    device_serial = ""
    i = first_obx
    while i < len(segs):
        s = segs[i]
        if s[0] != "OBX":
            i += 1
            continue

        param     = hl7.get(s, 3).strip()          # OBX-3: nome parametro
        value     = hl7.get(s, 5)                  # OBX-5: valore (o LL/HH/ABN/---)
        unit      = hl7.get(s, 6)                  # OBX-6: unità
        ref_range = hl7.get(s, 7)                  # OBX-7: range riferimento (LQC)
        flag_raw  = hl7.get(s, 8)                  # OBX-8: flag (*, !, \\~, \\^)
        flag      = _unescape_flag(flag_raw) if flag_raw else ""
        status    = hl7.get(s, 11) or "F"          # OBX-11: sempre "F" per HemoScreen
        dt        = hl7.get(s, 14)                 # OBX-14: datetime osservazione
        dev_ser   = hl7.get(s, 15).strip()         # OBX-15: serial dispositivo

        if dev_ser:
            device_serial = dev_ser

        # NTE immediatamente seguenti a questo OBX
        notes: list[str] = []
        j = i + 1
        while j < len(segs) and segs[j][0] == "NTE":
            notes.append(hl7.get(segs[j], 3))
            j += 1
        i = j

        loinc = HEMOSCREEN_LOINC.get(param.upper(), "")
        results.append({
            "code":      loinc,
            "name":      param,
            "value":     value,
            "unit":      unit,
            "ref_range": ref_range,
            "flag":      flag,
            "status":    status,
            "datetime":  dt,
            "notes":     notes,
        })

    return {
        "message_control_id":  hl7.msh_field(hl7.split_segments(message), 10),
        "sample_key":          hl7.sample_key(sample_id),
        "specimen_id":         sample_id,
        "placer_order_number": "",
        "filler_order_number": "",
        "observation_type":    obs_type,          # OBS / LQC / PRF
        "result_datetime":     obs_datetime or hl7.now_ts(),
        "results":             results,
        "device_serial":       device_serial,
        "obr_notes":           obr_notes,
        "raw":                 message,
        "source":              "hemoscreen_hl7",
    }


# ---------------------------------------------------------------------------
# ACK con version 2.4 (richiesta dal firmware HemoScreen, sezione 6.1.2)
# ---------------------------------------------------------------------------

def _build_ack_hs(message: str, code: str = "AA", text: str = "",
                  sending_app: str = "HL7MW",
                  sending_facility: str = "MIDDLEWARE") -> str:
    """ACK per HemoScreen con HL7 version 2.4 (invece della 2.5 del core)."""
    segs = hl7.split_segments(message)
    in_app = hl7.msh_field(segs, 3)
    in_fac = hl7.msh_field(segs, 4)
    ctrl   = hl7.msh_field(segs, 10)
    ts     = hl7.now_ts()
    sep    = hl7.FLD
    enc    = hl7.ENCODING_CHARS
    msh = (
        f"MSH{sep}{enc}{sep}{hl7.esc_text(sending_app)}{sep}"
        f"{hl7.esc_text(sending_facility)}{sep}{hl7.esc_text(in_app)}{sep}"
        f"{hl7.esc_text(in_fac)}{sep}{ts}{sep}{sep}ACK{sep}{ts}{sep}P{sep}2.4"
    )
    msa = f"MSA{sep}{code}{sep}{ctrl}"
    if text:
        msa += f"{sep}{hl7.esc_text(text)}"
    return msh + hl7.SEG + msa + hl7.SEG


# ---------------------------------------------------------------------------
# HemoscreenHl7ResultReceiver
# ---------------------------------------------------------------------------

class HemoscreenHl7ResultReceiver:
    """Riceve ORU^R01 HemoScreen tramite MLLP.

    Variante di ResultReceiver specializzata per il formato HemoScreen HL7 v2.4:
    - usa parse_hemoscreen_hl7() al posto di hl7.parse_result()
    - risponde ACK con version 2.4
    - estrae sample_key da PID-2 (non da SPM/OBR)

    Può ricevere risultati di tipo OBS (sangue), LQC (quality control), PRF (EQA).
    I risultati senza ordine corrispondente sono registrati in unmatched_results.
    """

    def __init__(self, store: Store, host: str, port: int,
                 sending_app: str = "HL7MW",
                 sending_facility: str = "MIDDLEWARE",
                 monitor: DeviceMonitor | None = None):
        self.store = store
        self.host, self.port = host, port
        self.sending_app = sending_app
        self.sending_facility = sending_facility
        self.monitor = monitor
        self._server: MllpServer | None = None

    def _handle(self, message: str) -> str:
        try:
            result = parse_hemoscreen_hl7(message)
        except hl7.Hl7Error as e:
            LOG.warning("HemoScreen HL7: messaggio non valido: %s", e)
            return _build_ack_hs(message, "AE", str(e),
                                  self.sending_app, self.sending_facility)

        device_name = result.get("device_serial") or "UNKNOWN"
        if self.monitor:
            self.monitor.record_message(device_name)

        key = result["sample_key"]
        if not key:
            # Nessun Test Identifier: registra come non abbinato, ACK positivo
            self.store.add_unmatched(result, source_instrument=device_name)
            LOG.warning("HemoScreen HL7: risultato senza sample_key -> unmatched")
            return _build_ack_hs(message, "AA", "",
                                  self.sending_app, self.sending_facility)

        order = self.store.get_order(key)
        if not order:
            self.store.add_unmatched(result, source_instrument=device_name)
            LOG.warning(
                "HemoScreen HL7: risultato senza ordine (sample=%s, obs_type=%s) -> unmatched",
                key, result.get("observation_type"),
            )
        else:
            self.store.add_result(key, result, source_instrument=device_name)
            try_complete(self.store, key)
            LOG.info(
                "HemoScreen HL7: risultato associato sample=%s obs_type=%s analiti=%d",
                key, result.get("observation_type"), len(result["results"]),
            )

        return _build_ack_hs(message, "AA", "",
                              self.sending_app, self.sending_facility)

    def start(self) -> "HemoscreenHl7ResultReceiver":
        self._server = MllpServer(self.host, self.port, self._handle).start()
        LOG.info(
            "HemoscreenHl7ResultReceiver in ascolto su %s:%s (HL7 v2.4 via MLLP)",
            self.host, self.port,
        )
        return self

    def stop(self) -> None:
        if self._server:
            self._server.stop()
