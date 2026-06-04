"""
hl7mw.adapters.hemoscreen_config — Configurazione remota PixCell HemoScreen.

Riferimento: HS-IL-00067 Rev.06 «HemoScreen POCT1-A2 Connectivity Protocol»
(sezioni Directive / Operator List), allineato allo standard CLSI POCT1-A2.

Cosa copre
──────────
1. **Catalogo dei parametri configurabili** del device (`CONFIG_CATALOG`): per ogni
   parametro definisce tipo, default, descrizione ed eventuali valori ammessi.
   Serve a validare ciò che si scrive nello store e a costruire i messaggi.
2. **Lista operatori** (OPL.R01): l'elenco degli operatori autorizzati a eseguire
   test sul device, con livello di permesso e validità. Il middleware (Observation
   Reviewer / Data Manager) la invia al device su richiesta o all'handshake.
3. **Direttiva di configurazione** (DTV.R01 / command SET_CONFIG): spinge sul device
   i parametri di configurazione come coppie chiave/valore.

Il middleware agisce da Data Manager: *valida e costruisce* i messaggi. La
persistenza dei valori vive in `store.device_config` / `store.operators`.

Solo stdlib. Nessuna dipendenza esterna.
"""
from __future__ import annotations

import datetime as _dt
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# Catalogo parametri configurabili del device
# ---------------------------------------------------------------------------
# type: "bool" | "int" | "enum" | "str"
# values: solo per gli enum
CONFIG_CATALOG: dict[str, dict] = {
    "continuous_mode": {
        "type": "bool", "default": False,
        "desc": "Trasmissione continua dei risultati senza richiesta esplicita.",
    },
    "operator_auth_required": {
        "type": "bool", "default": True,
        "desc": "Richiede un operatore autorizzato per eseguire un test.",
    },
    "patient_id_required": {
        "type": "bool", "default": True,
        "desc": "Richiede l'inserimento dell'ID paziente prima del test.",
    },
    "auto_transmit": {
        "type": "bool", "default": True,
        "desc": "Invio automatico dei risultati al data manager appena pronti.",
    },
    "qc_lockout_enabled": {
        "type": "bool", "default": False,
        "desc": "Blocca i test pazienti se il QC è scaduto o fallito.",
    },
    "qc_interval_hours": {
        "type": "int", "default": 24, "min": 1, "max": 720,
        "desc": "Intervallo massimo (ore) tra due controlli di qualità.",
    },
    "application_timeout_seconds": {
        "type": "int", "default": 60, "min": 10, "max": 600,
        "desc": "Timeout applicativo POCT1-A2 lato device.",
    },
    "keepalive_interval_seconds": {
        "type": "int", "default": 30, "min": 5, "max": 300,
        "desc": "Intervallo di keep-alive (KPA) sulla connessione.",
    },
    "result_units": {
        "type": "enum", "default": "CONVENTIONAL", "values": ["CONVENTIONAL", "SI"],
        "desc": "Sistema di unità di misura dei risultati.",
    },
    "language": {
        "type": "enum", "default": "EN",
        "values": ["EN", "IT", "ES", "FR", "DE"],
        "desc": "Lingua dell'interfaccia del device.",
    },
    "date_format": {
        "type": "enum", "default": "ISO",
        "values": ["ISO", "DMY", "MDY"],
        "desc": "Formato data mostrato sul device.",
    },
    "reference_profile": {
        "type": "str", "default": "ADULT",
        "desc": "Profilo di range di riferimento applicato (es. ADULT, PEDIATRIC).",
    },
}


class ConfigError(ValueError):
    """Parametro o valore di configurazione non valido."""


def _coerce(key: str, value) -> str:
    """Valida e normalizza un valore secondo il catalogo. Ritorna la stringa canonica."""
    spec = CONFIG_CATALOG.get(key)
    if spec is None:
        raise ConfigError(f"Parametro di configurazione sconosciuto: {key!r}")
    t = spec["type"]
    if t == "bool":
        if isinstance(value, bool):
            return "true" if value else "false"
        s = str(value).strip().lower()
        if s in ("true", "1", "yes", "on"):
            return "true"
        if s in ("false", "0", "no", "off"):
            return "false"
        raise ConfigError(f"{key}: atteso booleano, ricevuto {value!r}")
    if t == "int":
        try:
            n = int(value)
        except (ValueError, TypeError):
            raise ConfigError(f"{key}: atteso intero, ricevuto {value!r}")
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and n < lo:
            raise ConfigError(f"{key}: {n} sotto il minimo {lo}")
        if hi is not None and n > hi:
            raise ConfigError(f"{key}: {n} sopra il massimo {hi}")
        return str(n)
    if t == "enum":
        s = str(value).strip().upper()
        allowed = spec["values"]
        if s not in allowed:
            raise ConfigError(f"{key}: {value!r} non in {allowed}")
        return s
    # str
    return str(value).strip()


def validate_config(params: dict) -> dict[str, str]:
    """Valida una mappa di parametri e ritorna i valori canonici (stringa).

    Solleva ConfigError sul primo parametro o valore non valido.
    """
    return {k: _coerce(k, v) for k, v in params.items()}


def default_config() -> dict[str, str]:
    """Configurazione di default canonica per un HemoScreen."""
    return {k: _coerce(k, spec["default"]) for k, spec in CONFIG_CATALOG.items()}


# ---------------------------------------------------------------------------
# Helpers timestamp / XML
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).astimezone().replace(microsecond=0).isoformat()


def _hdr(parent: ET.Element, ctrl_id: str) -> None:
    hdr = ET.SubElement(parent, "HDR")
    ET.SubElement(hdr, "HDR.control_id", V=ctrl_id)
    ET.SubElement(hdr, "HDR.version_id", V="POCT1")
    ET.SubElement(hdr, "HDR.creation_dttm", V=_now_iso())


# ---------------------------------------------------------------------------
# Costruzione messaggi POCT1-A2
# ---------------------------------------------------------------------------

def build_operator_list(operators: list[dict], ctrl_id: str = "1") -> str:
    """Costruisce un OPL.R01 (Operator List) per il device.

    `operators` è una lista di dict con almeno `operator_id` e `full_name`; campi
    opzionali: `poct_permission` (OPERATOR/SUPERVISOR/TRAINER), `valid_from`,
    `valid_until`, `active`. Gli operatori inattivi vengono comunque inviati con
    permission_cd="DISABLED" così il device li revoca.
    """
    root = ET.Element("OPL.R01")
    _hdr(root, ctrl_id)
    for op in operators:
        opr = ET.SubElement(root, "OPR")
        ET.SubElement(opr, "OPR.operator_id", V=str(op.get("operator_id", "")))
        ET.SubElement(opr, "OPR.name", V=str(op.get("full_name", "")))
        active = op.get("active", True)
        perm = "DISABLED" if not active else str(op.get("poct_permission") or "OPERATOR")
        ET.SubElement(opr, "OPR.permission_cd", V=perm)
        if op.get("valid_from"):
            ET.SubElement(opr, "OPR.valid_from_dttm", V=str(op["valid_from"]))
        if op.get("valid_until"):
            ET.SubElement(opr, "OPR.valid_until_dttm", V=str(op["valid_until"]))
    return ET.tostring(root, encoding="unicode")


def build_config_directive(params: dict, ctrl_id: str = "1") -> str:
    """Costruisce un DTV.R01 (Directive) con command SET_CONFIG e i parametri dati.

    I parametri vengono validati con `validate_config`; ConfigError su valore non
    ammesso. Ogni parametro diventa un elemento CFG con chiave/valore canonici.
    """
    canonical = validate_config(params)
    root = ET.Element("DTV.R01")
    _hdr(root, ctrl_id)
    dtv = ET.SubElement(root, "DTV")
    ET.SubElement(dtv, "DTV.command_cd", V="SET_CONFIG")
    for key, value in canonical.items():
        cfg = ET.SubElement(dtv, "CFG")
        ET.SubElement(cfg, "CFG.name", V=key)
        ET.SubElement(cfg, "CFG.value", V=value)
    return ET.tostring(root, encoding="unicode")


def parse_config_directive(xml_text: str) -> dict[str, str]:
    """Parsa un DTV.R01 SET_CONFIG e ritorna la mappa {param_key: value} (per i test)."""
    root = ET.fromstring(xml_text)
    out: dict[str, str] = {}
    for cfg in root.findall(".//CFG"):
        name_el = cfg.find("CFG.name")
        val_el = cfg.find("CFG.value")
        if name_el is not None and name_el.get("V"):
            out[name_el.get("V")] = val_el.get("V", "") if val_el is not None else ""
    return out
