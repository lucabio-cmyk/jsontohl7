"""
Test della configurazione remota HemoScreen (POCT1-A2) e della lista operatori.

Eseguibile senza pytest: `python3 tests/test_hemoscreen_config.py`
"""
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hl7mw.store import Store
from hl7mw.adapters import hemoscreen_config as hc
from hl7mw.adapters.hemoscreen_poct1a2 import HemoscreenConfigProvider


def test_config_catalog_defaults():
    defaults = hc.default_config()
    # tutti i parametri del catalogo hanno un default canonico (stringa)
    assert set(defaults) == set(hc.CONFIG_CATALOG)
    assert defaults["continuous_mode"] == "false"
    assert defaults["qc_interval_hours"] == "24"
    assert defaults["result_units"] == "CONVENTIONAL"


def test_validate_config_types():
    out = hc.validate_config({
        "continuous_mode": True,
        "qc_interval_hours": "12",
        "result_units": "si",
        "operator_auth_required": "no",
    })
    assert out["continuous_mode"] == "true"
    assert out["qc_interval_hours"] == "12"
    assert out["result_units"] == "SI"          # enum normalizzato in maiuscolo
    assert out["operator_auth_required"] == "false"


def test_validate_config_errors():
    for bad in (
        {"unknown_param": "x"},
        {"qc_interval_hours": "abc"},
        {"qc_interval_hours": "9999"},   # oltre il massimo
        {"result_units": "MOON"},
        {"continuous_mode": "maybe"},
    ):
        try:
            hc.validate_config(bad)
            assert False, f"atteso ConfigError per {bad}"
        except hc.ConfigError:
            pass


def test_build_config_directive_roundtrip():
    xml = hc.build_config_directive({"continuous_mode": True, "language": "it"})
    root = ET.fromstring(xml)
    assert root.tag == "DTV.R01"
    assert root.find(".//DTV.command_cd").get("V") == "SET_CONFIG"
    parsed = hc.parse_config_directive(xml)
    assert parsed["continuous_mode"] == "true"
    assert parsed["language"] == "IT"


def test_build_operator_list():
    operators = [
        {"operator_id": "op1", "full_name": "Mario Rossi", "poct_permission": "OPERATOR",
         "active": True, "valid_until": "2027-01-01"},
        {"operator_id": "op2", "full_name": "Lucia Bianchi", "poct_permission": "SUPERVISOR",
         "active": False},
    ]
    xml = hc.build_operator_list(operators, ctrl_id="7")
    root = ET.fromstring(xml)
    assert root.tag == "OPL.R01"
    assert root.find(".//HDR.control_id").get("V") == "7"
    oprs = root.findall("OPR")
    assert len(oprs) == 2
    assert oprs[0].find("OPR.operator_id").get("V") == "op1"
    assert oprs[0].find("OPR.permission_cd").get("V") == "OPERATOR"
    assert oprs[0].find("OPR.valid_until_dttm").get("V") == "2027-01-01"
    # operatore inattivo -> permesso DISABLED (revoca lato device)
    assert oprs[1].find("OPR.permission_cd").get("V") == "DISABLED"


def test_xml_escaping_in_operator_name():
    # caratteri speciali XML non devono rompere il messaggio
    xml = hc.build_operator_list([{"operator_id": "o", "full_name": "A & B <C>"}])
    root = ET.fromstring(xml)
    assert root.find(".//OPR.name").get("V") == "A & B <C>"


def test_store_device_config_history():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Store(Path(tmpdir) / "test.db")
        db.set_device_config("HS1", {"continuous_mode": "true"}, updated_by="admin")
        assert db.get_device_config("HS1") == {"continuous_mode": "true"}

        db.set_device_config("HS1", {"continuous_mode": "false", "language": "IT"},
                             updated_by="admin")
        cfg = db.get_device_config("HS1")
        assert cfg["continuous_mode"] == "false"
        assert cfg["language"] == "IT"

        # la history traccia la variazione di continuous_mode
        history = db.get_device_config_history("HS1")
        keys_changed = [h["param_key"] for h in history]
        assert "continuous_mode" in keys_changed
        # set idempotente: rimettere lo stesso valore non aggiunge history
        before = len(db.get_device_config_history("HS1"))
        db.set_device_config("HS1", {"language": "IT"})
        assert len(db.get_device_config_history("HS1")) == before


def test_config_provider_builds_messages():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Store(Path(tmpdir) / "test.db")
        db.upsert_operator("op1", "Mario", role="OPERATOR", password="p",
                           poct_permission="OPERATOR")
        db.set_device_config("HS1", {"continuous_mode": "true"})

        provider = HemoscreenConfigProvider(db, "HS1")
        opl = provider.operator_list_xml("1")
        assert ET.fromstring(opl).find(".//OPR.operator_id").get("V") == "op1"

        cfg = provider.config_xml("2")
        assert cfg is not None
        assert hc.parse_config_directive(cfg)["continuous_mode"] == "true"

        assert provider.is_operator_authorized("op1") is True
        assert provider.is_operator_authorized("ghost") is False

        # nessuna config salvata -> config_xml None
        assert HemoscreenConfigProvider(db, "HS-EMPTY").config_xml("3") is None


if __name__ == "__main__":
    test_config_catalog_defaults()
    test_validate_config_types()
    test_validate_config_errors()
    test_build_config_directive_roundtrip()
    test_build_operator_list()
    test_xml_escaping_in_operator_name()
    test_store_device_config_history()
    test_config_provider_builds_messages()
    print("TUTTI I TEST OK")
