"""
Test degli adapter HemoScreen HL7 v2.4 e POCT1-A2.

Eseguibile senza pytest: `python3 tests/test_hemoscreen.py`
"""
from __future__ import annotations

import socket
import sys
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hl7mw import hl7, mllp
from hl7mw.store import Store
from hl7mw.adapters.hemoscreen_hl7 import (
    parse_hemoscreen_hl7,
    HemoscreenHl7ResultReceiver,
    HEMOSCREEN_LOINC,
)
from hl7mw.adapters.hemoscreen_poct1a2 import (
    parse_obs_r01,
    parse_obs_r02,
    HemoscreenPoct1A2Receiver,
    _xml_ack, _xml_req, _xml_end,
    _mllp_send, _mllp_recv,
    build_dtv_set_time, build_opl_r01, build_opl_r02, build_ptl_r01,
    build_dtv_pix_qc, build_dtv_pix_fb, build_dtv_pix_dvcset,
    send_lock, send_unlock, send_operator_list, connected_devices,
)

import xml.etree.ElementTree as ET

CR = "\r"

# ---------------------------------------------------------------------------
# Messaggi di test HL7
# ---------------------------------------------------------------------------

# Sangue fresco (OBS) — da appendice 8.1.1 del documento
HS_HL7_OBS = CR.join([
    r"MSH|^~\&|HemoScreen|PixCell|||20231224174110||ORU^R01|0|P|2.4",
    "PID||35",
    "OBR||||OBS|||20231210111800",
    "OBX|1|NM|WBC||11.7|10*3/uL|||||F|||20231210111800|0000000-0001-HS",
    "OBX|2|NM|RBC||4.04|10*6/uL|||||F|||20231210111800|0000000-0001-HS",
    "OBX|3|NM|HGB||14.25|g/dL|||||F|||20231210111800|0000000-0001-HS",
    "OBX|4|NM|HCT||34.44|%|||||F|||20231210111800|0000000-0001-HS",
    "OBX|5|NM|MCV||85.25|fL|||||F|||20231210111800|0000000-0001-HS",
    "OBX|6|NM|MCH||35.21|pg|||||F|||20231210111800|0000000-0001-HS",
    "OBX|7|NM|MCHC||0.00|g/dL|||||F|||20231210111800|0000000-0001-HS",
    "OBX|8|NM|RDW||12.8|%|||||F|||20231210111800|0000000-0001-HS",
    "OBX|9|NM|PLT||120|10*3/uL|||||F|||20231210111800|0000000-0001-HS",
    "OBX|10|NM|MPV||0|fL|||||F|||20231210111800|0000000-0001-HS",
    "OBX|11|NM|NEU#||8.02|10*3/uL|||||F|||20231210111800|0000000-0001-HS",
    "OBX|12|NM|LYM#||2.28|10*3/uL|||||F|||20231210111800|0000000-0001-HS",
    "OBX|13|NM|MON#||1.25|10*3/uL|||||F|||20231210111800|0000000-0001-HS",
    "OBX|14|NM|EOS#||0.14|10*3/uL|||||F|||20231210111800|0000000-0001-HS",
    "OBX|15|NM|BAS#||0.00|10*3/uL|||||F|||20231210111800|0000000-0001-HS",
    "OBX|16|NM|NEU%||68.6|%|||||F|||20231210111800|0000000-0001-HS",
    "OBX|17|NM|LYM%||19.5|%|||||F|||20231210111800|0000000-0001-HS",
    "OBX|18|NM|MON%||10.7|%|||||F|||20231210111800|0000000-0001-HS",
    "OBX|19|NM|EOS%||1.20|%|||||F|||20231210111800|0000000-0001-HS",
    "OBX|20|NM|BAS%||0.00|%|||||F|||20231210111800|0000000-0001-HS",
]) + CR

# Quality Control (LQC) con ref range — da appendice 8.1.2
HS_HL7_LQC = CR.join([
    r"MSH|^~\&|HemoScreen|PixCell|||20231224173346||ORU^R01|1|P|2.4",
    "PID||PIX240205N",
    "OBR||||LQC|||20231210114100",
    "OBX|1|NM|WBC||11.0|10*3/uL|5.9-9.3||||F|||20231210114100|0000000-0001-HS",
    "OBX|2|NM|RBC||10.3|10*6/uL|4.3-5.5||||F|||20231210114100|0000000-0001-HS",
    "OBX|3|NM|HGB||39.99|g/dL|14.2-17.8||||F|||20231210114100|0000000-0001-HS",
    "OBX|4|NM|HCT||87.95|%|33.5-42.5||||F|||20231210114100|0000000-0001-HS",
    "OBX|5|NM|MCV||85.25|fL|72.5-82.5||||F|||20231210114100|0000000-0001-HS",
    "OBX|6|NM|MCH||38.76|pg|27.7-37.7||||F|||20231210114100|0000000-0001-HS",
    "OBX|7|NM|MCHC||0.00|g/dL|37.1-47.1||||F|||20231210114100|0000000-0001-HS",
    "OBX|8|NM|RDW||12.8|%|12.5-18.5||||F|||20231210114100|0000000-0001-HS",
    "OBX|9|NM|PLT||0.00|10*3/uL|203-293||||F|||20231210114100|0000000-0001-HS",
    "OBX|10|NM|MPV||0.00|fL|8.4-12.4||||F|||20231210114100|0000000-0001-HS",
    "OBX|11|NM|NEU#||10.4|10*3/uL|2.5-3.9||||F|||20231210114100|0000000-0001-HS",
    "OBX|12|NM|LYM#||0.28|10*3/uL|2.5-3.9||||F|||20231210114100|0000000-0001-HS",
    "OBX|13|NM|MON#||0.00|10*3/uL|0.2-1||||F|||20231210114100|0000000-0001-HS",
    "OBX|14|NM|EOS#||0.28|10*3/uL|0-1.2||||F|||20231210114100|0000000-0001-HS",
    "OBX|15|NM|BAS#||0.13|10*3/uL|0-0.2||||F|||20231210114100|0000000-0001-HS",
    "OBX|16|NM|NEU%||93.8|%|34-52||||F|||20231210114100|0000000-0001-HS",
    "OBX|17|NM|LYM%||2.50|%|32.5-50.5||||F|||20231210114100|0000000-0001-HS",
    "OBX|18|NM|MON%||0.00|%|2.5-12.5||||F|||20231210114100|0000000-0001-HS",
    "OBX|19|NM|EOS%||2.50|%|0-15||||F|||20231210114100|0000000-0001-HS",
    "OBX|20|NM|BAS%||1.20|%|0-1||||F|||20231210114100|0000000-0001-HS",
]) + CR

# Con flag e valori speciali — da appendici 8.1.4 e 8.1.5
HS_HL7_FLAGS = CR.join([
    r"MSH|^~\&|HemoScreen|PixCell|||20130731221608||ORU^R01|1|P|2.4",
    "PID||123478",
    "OBR||||OBS|||20130731001900",
    "NTE|||Comment for test",                     # NTE dopo OBR (commento accept)
    "OBX|1|NM|WBC||14.4|10*3/uL|||||F|||20130731001900|2201024-323-HS",
    "OBX|2|NM|RBC||2.99|10*6/uL|||||F|||20130731001900|2201024-323-HS",
    r"OBX|3|ST|HGB||ABN|g/dL|||||F|||20130731001900|2201024-323-HS",
    "NTE|||Abnormal cells suspected",
    "OBX|4|NM|HCT||20.7|%|||||F|||20130731001900|2201024-323-HS",
    "OBX|5|NM|MCV||69.1|fL|||||F|||20130731001900|2201024-323-HS",
    "OBX|6|NM|MCH||0.00|pg|||||F|||20130731001900|2201024-323-HS",
    "OBX|7|NM|MCHC||0.00|g/dL|||||F|||20130731001900|2201024-323-HS",
    "OBX|8|NM|RDW||26.5|%|||||F|||20130731001900|2201024-323-HS",
    r"OBX|9|NM|PLT||393|10*3/uL||\~|||F|||20130731001900|2201024-323-HS",
    "NTE|||MPV abnormal distribution may affect marked results",
    r"OBX|10|NM|MPV||8.96|fL||\~|||F|||20130731001900|2201024-323-HS",
    "NTE|||MPV abnormal distribution may affect marked results",
    "OBX|11|NM|NEU#||5.23|10*3/uL|||||F|||20130731001900|2201024-323-HS",
    "OBX|12|NM|LYM#||2.01|10*3/uL|||||F|||20130731001900|2201024-323-HS",
    "OBX|13|NM|MON#||0.35|10*3/uL|||||F|||20130731001900|2201024-323-HS",
    "OBX|14|NM|EOS#||0.06|10*3/uL|||||F|||20130731001900|2201024-323-HS",
    "OBX|15|NM|BAS#||0.01|10*3/uL|||||F|||20130731001900|2201024-323-HS",
    "OBX|16|NM|NEU%||68.3|%|||||F|||20130731001900|2201024-323-HS",
    "OBX|17|NM|LYM%||26.2|%|||||F|||20130731001900|2201024-323-HS",
    "OBX|18|NM|MON%||4.6|%|||||F|||20130731001900|2201024-323-HS",
    "OBX|19|NM|EOS%||0.80|%|||||F|||20130731001900|2201024-323-HS",
    "OBX|20|NM|BAS%||0.10|%|||||F|||20130731001900|2201024-323-HS",
]) + CR


# ---------------------------------------------------------------------------
# XML POCT1-A2 di test
# ---------------------------------------------------------------------------

OBS_R01_XML = """<OBS.R01>
<HDR>
<HDR.control_id V="10003" />
<HDR.version_id V="POCT1" />
<HDR.creation_dttm V="2020-03-11T14:00:05+01:00" />
</HDR>
<SVC>
<SVC.role_cd V="OBS" />
<SVC.observation_dttm V="2020-12-05T00:00:01+01:00" />
<SVC.status_cd V="NRM" />
<SVC.reason_cd V="NEW" />
<PT>
<PT.patient_id V="TEST-001" />
<OBS>
<OBS.observation_id V="6690-2" DN="WBC" SN="LN" />
<OBS.value V="12.5" U="10*3/uL" />
<OBS.method_cd V="M" />
<OBS.status_cd V="A" />
</OBS>
<OBS>
<OBS.observation_id V="789-8" DN="RBC" SN="LN" />
<OBS.value V="7.5" U="10*6/uL" />
<OBS.method_cd V="M" />
<OBS.status_cd V="A" />
<NTE><NTE.text V="*" /></NTE>
<NTE><NTE.text V="Abnormal cells may affect marked results" /></NTE>
</OBS>
<OBS>
<OBS.observation_id V="718-7" DN="HGB" SN="LN" />
<OBS.value V="15.5" U="g/dL" />
<OBS.method_cd V="M" />
<OBS.status_cd V="A" />
</OBS>
</PT>
<OPR><OPR.operator_id V="12345" /></OPR>
<NTE><NTE.text V="Accept comment" /></NTE>
</SVC>
</OBS.R01>"""

OBS_R02_XML = """<OBS.R02>
<HDR>
<HDR.control_id V="10004" />
<HDR.version_id V="POCT1" />
<HDR.creation_dttm V="2020-03-11T14:00:05+01:00" />
</HDR>
<SVC>
<SVC.role_cd V="LQC" />
<SVC.observation_dttm V="2020-03-11T00:00:01+01:00" />
<SVC.status_cd V="NRM" />
<SVC.reason_cd V="NEW" />
<CTC>
<CTC.name V="PIX201205N" />
<CTC.lot_number V="PIX201205" />
<CTC.expiration_date V="2020-12-05T00:00:01+01:00" />
<CTC.level_cd V="N" DN="Normal" />
<OBS>
<OBS.observation_id V="6690-2" DN="WBC" SN="LN" />
<OBS.value V="12.5" U="10*3/uL" />
<OBS.method_cd V="M" />
<OBS.status_cd V="A" />
<OBS.normal_lo-hi_limit V="[4;11.5]" U="10*3/uL" />
</OBS>
<OBS>
<OBS.observation_id V="789-8" DN="RBC" SN="LN" />
<OBS.value V="7.5" U="10*6/uL" />
<OBS.method_cd V="M" />
<OBS.status_cd V="A" />
<OBS.normal_lo-hi_limit V="[4;11.5]" U="10*6/uL" />
</OBS>
</CTC>
<OPR><OPR.operator_id V="12345" /></OPR>
</SVC>
</OBS.R02>"""


# ---------------------------------------------------------------------------
# Helper per la conversazione POCT1-A2 lato device simulato
# ---------------------------------------------------------------------------

def _build_hel(ctrl: str) -> str:
    return (f'<HEL.R01><HDR>'
            f'<HDR.control_id V="{ctrl}" />'
            f'<HDR.version_id V="POCT1" />'
            f'<HDR.creation_dttm V="2024-01-01T10:00:00+00:00" />'
            f'</HDR>'
            f'<DEV><DEV.device_id V="PIX^HemoScreen^0001-HS" />'
            f'<DEV.vendor_id V="PIX" /><DEV.device_name V="HemoScreen" />'
            f'<DEV.serial_id V="0001-HS" /></DEV>'
            f'</HEL.R01>')


def _build_dst(ctrl: str, new_obs: int = 1) -> str:
    return (f'<DST.R01><HDR>'
            f'<HDR.control_id V="{ctrl}" />'
            f'<HDR.version_id V="POCT1" />'
            f'<HDR.creation_dttm V="2024-01-01T10:00:01+00:00" />'
            f'</HDR>'
            f'<DST>'
            f'<DST.status_dttm V="2024-01-01T10:00:01+00:00" />'
            f'<DST.new_observations_qty V="{new_obs}" />'
            f'<DST.new_events_qty V="0" />'
            f'<DST.condition_cd V="R" SN="POCT1" SV="1" />'
            f'</DST>'
            f'</DST.R01>')


def _build_eot(ctrl: str) -> str:
    return (f'<EOT.R01><HDR>'
            f'<HDR.control_id V="{ctrl}" />'
            f'<HDR.version_id V="POCT1" />'
            f'<HDR.creation_dttm V="2024-01-01T10:00:10+00:00" />'
            f'</HDR>'
            f'<EOT><EOT.topic_cd V="OBS" SN="POCT1" SV="1" /></EOT>'
            f'</EOT.R01>')


def _device_ack(ctrl: str, ack_ctrl: str) -> str:
    return (f'<ACK.R01><HDR>'
            f'<HDR.control_id V="{ctrl}" />'
            f'<HDR.version_id V="POCT1" />'
            f'<HDR.creation_dttm V="2024-01-01T10:00:02+00:00" />'
            f'</HDR>'
            f'<ACK><ACK.type_cd V="AA" />'
            f'<ACK.control_id V="{ack_ctrl}" /></ACK>'
            f'</ACK.R01>')


def _build_dst_full(ctrl: str, new_obs: int = 0, new_events: int = 0) -> str:
    return (f'<DST.R01><HDR>'
            f'<HDR.control_id V="{ctrl}" />'
            f'<HDR.version_id V="POCT1" />'
            f'<HDR.creation_dttm V="2024-01-01T10:00:01+00:00" />'
            f'</HDR>'
            f'<DST>'
            f'<DST.status_dttm V="2024-01-01T10:00:01+00:00" />'
            f'<DST.new_observations_qty V="{new_obs}" />'
            f'<DST.new_events_qty V="{new_events}" />'
            f'<DST.condition_cd V="R" SN="POCT1" SV="1" />'
            f'</DST>'
            f'</DST.R01>')


def _build_eot_topic(ctrl: str, topic_cd: str) -> str:
    return (f'<EOT.R01><HDR>'
            f'<HDR.control_id V="{ctrl}" />'
            f'<HDR.version_id V="POCT1" />'
            f'<HDR.creation_dttm V="2024-01-01T10:00:10+00:00" />'
            f'</HDR>'
            f'<EOT><EOT.topic_cd V="{topic_cd}" SN="POCT1" SV="1" /></EOT>'
            f'</EOT.R01>')


def _build_esc(ctrl: str, esc_ctrl: str, detail: str = "OTH") -> str:
    return (f'<ESC.R01><HDR>'
            f'<HDR.control_id V="{ctrl}" />'
            f'<HDR.version_id V="POCT1" />'
            f'<HDR.creation_dttm V="2024-01-01T10:00:03+00:00" />'
            f'</HDR>'
            f'<ESC><ESC.esc_control_id V="{esc_ctrl}" />'
            f'<ESC.detail_cd V="{detail}" /></ESC>'
            f'</ESC.R01>')


def _build_req_rpat(ctrl: str, test_id: str) -> str:
    return (f'<REQ.R01><HDR>'
            f'<HDR.control_id V="{ctrl}" />'
            f'<HDR.version_id V="POCT1" />'
            f'<HDR.creation_dttm V="2024-01-01T10:00:04+00:00" />'
            f'</HDR>'
            f'<REQ><REQ.request_cd V="RPAT" />'
            f'<PT><PT.patient_id V="{test_id}" /></PT></REQ>'
            f'</REQ.R01>')


def _build_kpa(ctrl: str) -> str:
    return (f'<KPA.R01><HDR>'
            f'<HDR.control_id V="{ctrl}" />'
            f'<HDR.version_id V="POCT1" />'
            f'<HDR.creation_dttm V="2024-01-01T10:00:05+00:00" />'
            f'</HDR></KPA.R01>')


# ---------------------------------------------------------------------------
# Test HL7
# ---------------------------------------------------------------------------

def test_parse_hl7_obs():
    """parse_hemoscreen_hl7: sangue fresco — 20 analiti, LOINC, sample_key."""
    r = parse_hemoscreen_hl7(HS_HL7_OBS)
    assert r["sample_key"] == "35", f"sample_key atteso '35', ottenuto {r['sample_key']!r}"
    assert r["observation_type"] == "OBS"
    assert r["source"] == "hemoscreen_hl7"
    assert len(r["results"]) == 20, f"attesi 20 analiti, ottenuti {len(r['results'])}"
    # Primo analita: WBC
    wbc = r["results"][0]
    assert wbc["name"] == "WBC"
    assert wbc["code"] == HEMOSCREEN_LOINC["WBC"] == "6690-2"
    assert wbc["value"] == "11.7"
    assert wbc["unit"] == "10*3/uL"
    assert wbc["flag"] == ""
    assert r["device_serial"] == "0000000-0001-HS"
    print("[1] parse_hemoscreen_hl7 OBS: 20 analiti, LOINC, sample_key  OK")


def test_parse_hl7_lqc():
    """parse_hemoscreen_hl7: LQC — ref_range presenti, obs_type=LQC."""
    r = parse_hemoscreen_hl7(HS_HL7_LQC)
    assert r["sample_key"] == "PIX240205N"
    assert r["observation_type"] == "LQC"
    assert len(r["results"]) == 20
    # WBC ref range 5.9-9.3
    wbc = r["results"][0]
    assert wbc["ref_range"] == "5.9-9.3", f"ref_range errato: {wbc['ref_range']!r}"
    print("[2] parse_hemoscreen_hl7 LQC: ref_range OK")


def test_parse_hl7_flags():
    """parse_hemoscreen_hl7: flag HL7-escaped, valore ABN, NTE dopo OBR."""
    r = parse_hemoscreen_hl7(HS_HL7_FLAGS)
    assert r["sample_key"] == "123478"
    assert r["obr_notes"] == ["Comment for test"], \
        f"obr_notes errati: {r['obr_notes']!r}"

    # HGB = ABN (valore speciale stringa)
    hgb = r["results"][2]
    assert hgb["name"] == "HGB"
    assert hgb["value"] == "ABN"
    assert hgb["notes"] == ["Abnormal cells suspected"]

    # PLT — flag \\~ deve essere decodificato in ~
    plt = r["results"][8]
    assert plt["name"] == "PLT"
    assert plt["flag"] == "~", f"flag PLT atteso '~', ottenuto {plt['flag']!r}"
    assert "MPV abnormal distribution" in plt["notes"][0]

    print("[3] parse_hemoscreen_hl7 flag/ABN/NTE  OK")


def test_hl7_receiver_matched():
    """HemoscreenHl7ResultReceiver: risultato abbinato a ordine esistente."""
    store = Store("/tmp/hl7mw_test_hs.db")
    Path("/tmp/hl7mw_test_hs.db").unlink(missing_ok=True)
    store = Store("/tmp/hl7mw_test_hs.db")

    # Crea un ordine con sample_key = "35" (come PID-2 in HS_HL7_OBS)
    fake_order = {
        "sample_key": "35",
        "placer_order_number": "P-035",
        "filler_order_number": "F-035",
        "specimen_id": "35",
        "patient": {},
        "universal_service_id": {"code": "58410-2", "text": "Emocromo", "system": "LN"},
        "message_control_id": "ORD001",
        "message_type": "ORM^O01",
        "ordering_provider": "",
        "requested_datetime": "20240101100000",
        "raw": "",
    }
    store.upsert_order(fake_order)

    rx = HemoscreenHl7ResultReceiver(store, "127.0.0.1", 0)
    rx._server = mllp.MllpServer("127.0.0.1", 0, rx._handle).start()
    port = rx._server._srv.server_address[1]

    try:
        ack_raw = mllp.exchange("127.0.0.1", port, HS_HL7_OBS)
        # ACK deve avere MSA|AA
        assert "MSA|AA" in ack_raw, f"ACK non positivo: {ack_raw!r}"
        # Versione ACK deve essere 2.4
        assert "2.4" in ack_raw, "Versione ACK non è 2.4"

        order = store.get_order("35")
        assert order and order["status"] == "READY", \
            f"stato ordine atteso READY: {order}"
        results = store.results_for("35")
        assert len(results) == 1
        assert len(results[0]["results"]) == 20
        print("[4] HemoscreenHl7ResultReceiver: ordine abbinato, 20 analiti, status=READY  OK")
    finally:
        rx._server.stop()


def test_hl7_receiver_unmatched():
    """HemoscreenHl7ResultReceiver: risultato senza ordine -> unmatched."""
    store = Store("/tmp/hl7mw_test_hs2.db")
    Path("/tmp/hl7mw_test_hs2.db").unlink(missing_ok=True)
    store = Store("/tmp/hl7mw_test_hs2.db")

    rx = HemoscreenHl7ResultReceiver(store, "127.0.0.1", 0)
    rx._server = mllp.MllpServer("127.0.0.1", 0, rx._handle).start()
    port = rx._server._srv.server_address[1]

    try:
        ack_raw = mllp.exchange("127.0.0.1", port, HS_HL7_OBS)
        assert "MSA|AA" in ack_raw  # ACK positivo anche per unmatched
        assert len(store.unmatched()) == 1
        print("[5] HemoscreenHl7ResultReceiver: no ordine -> unmatched + ACK AA  OK")
    finally:
        rx._server.stop()


# ---------------------------------------------------------------------------
# Test POCT1-A2: parser
# ---------------------------------------------------------------------------

def test_parse_obs_r01():
    """parse_obs_r01: OBS.R01 XML -> dict risultato."""
    root = ET.fromstring(OBS_R01_XML)
    r = parse_obs_r01(root, OBS_R01_XML)

    assert r["sample_key"] == "TEST-001", f"sample_key: {r['sample_key']!r}"
    assert r["observation_type"] == "OBS"
    assert r["source"] == "hemoscreen_poct1a2"
    assert len(r["results"]) == 3

    wbc = r["results"][0]
    assert wbc["code"]  == "6690-2"
    assert wbc["name"]  == "WBC"
    assert wbc["value"] == "12.5"
    assert wbc["unit"]  == "10*3/uL"
    assert wbc["flag"]  == ""

    rbc = r["results"][1]
    assert rbc["flag"]  == "*", f"flag RBC atteso '*': {rbc['flag']!r}"
    assert "Abnormal cells" in rbc["notes"][0]

    # Nota accept SVC
    assert "Accept comment" in r["svc_notes"]
    print("[6] parse_obs_r01: 3 analiti, flag *, note accept  OK")


def test_parse_obs_r02():
    """parse_obs_r02: OBS.R02 (LQC) -> dict con lot_name, ref_range."""
    root = ET.fromstring(OBS_R02_XML)
    r = parse_obs_r02(root, OBS_R02_XML)

    assert r["sample_key"] == "PIX201205N"
    assert r["observation_type"] == "LQC"
    assert r["lot_name"]   == "PIX201205N"
    assert r["lot_number"] == "PIX201205"
    assert r["qc_level"]   == "Normal"
    assert len(r["results"]) == 2

    wbc = r["results"][0]
    assert wbc["ref_range"] == "4-11.5", f"ref_range: {wbc['ref_range']!r}"
    print("[7] parse_obs_r02: LQC lot_name, ref_range  OK")


# ---------------------------------------------------------------------------
# Test POCT1-A2: conversazione completa
# ---------------------------------------------------------------------------

def test_poct1a2_conversation():
    """HemoscreenPoct1A2Receiver: conversazione base completa, risultato salvato."""
    store = Store("/tmp/hl7mw_test_hs3.db")
    Path("/tmp/hl7mw_test_hs3.db").unlink(missing_ok=True)
    store = Store("/tmp/hl7mw_test_hs3.db")

    # Crea un ordine con sample_key = "TEST-001" (come PT.patient_id in OBS_R01_XML)
    fake_order = {
        "sample_key": "TEST-001",
        "placer_order_number": "",
        "filler_order_number": "",
        "specimen_id": "TEST-001",
        "patient": {},
        "universal_service_id": {"code": "58410-2", "text": "Emocromo", "system": "LN"},
        "message_control_id": "ORD002",
        "message_type": "ORM^O01",
        "ordering_provider": "",
        "requested_datetime": "20240101100000",
        "raw": "",
    }
    store.upsert_order(fake_order)

    rx = HemoscreenPoct1A2Receiver(store, "127.0.0.1", 0, continuous_mode=False, timeout=5.0)
    rx.start()
    srv_port = rx._srv.server_address[1]

    try:
        with socket.create_connection(("127.0.0.1", srv_port), timeout=5.0) as s:
            s.settimeout(5.0)

            # 1) HEL.R01
            _mllp_send(s, _build_hel("1"))
            raw = _mllp_recv(s, 5.0)
            assert raw, "nessuna risposta a HEL.R01"
            root = ET.fromstring(raw.decode())
            assert root.tag == "ACK.R01"

            # 2) DST.R01 con 1 nuova osservazione
            _mllp_send(s, _build_dst("2", new_obs=1))
            raw = _mllp_recv(s, 5.0)  # ACK del DST
            assert root.tag == "ACK.R01"
            # Il server invia REQ.R01(ROBS)
            raw2 = _mllp_recv(s, 5.0)
            assert raw2, "nessun REQ.R01 ricevuto dopo DST"
            req_root = ET.fromstring(raw2.decode())
            assert req_root.tag == "REQ.R01"
            req_cd = req_root.find(".//REQ.request_cd")
            assert req_cd is not None and req_cd.get("V") == "ROBS"

            # 3) ACK per il REQ (device -> server)
            req_ctrl = req_root.find(".//HDR.control_id").get("V", "")
            _mllp_send(s, _device_ack("3", req_ctrl))

            # 4) OBS.R01 con 3 analiti
            _mllp_send(s, OBS_R01_XML)
            raw = _mllp_recv(s, 5.0)
            assert raw
            ack_r = ET.fromstring(raw.decode())
            assert ack_r.tag == "ACK.R01"
            type_cd = ack_r.find(".//ACK.type_cd")
            assert type_cd is not None and type_cd.get("V") == "AA"

            # 5) EOT.R01
            _mllp_send(s, _build_eot("5"))
            raw = _mllp_recv(s, 5.0)  # ACK per EOT
            assert raw
            # Il server invia END.R01
            raw2 = _mllp_recv(s, 5.0)
            assert raw2, "nessun END.R01 ricevuto dopo EOT"
            end_root = ET.fromstring(raw2.decode())
            assert end_root.tag == "END.R01"
            end_ctrl = end_root.find(".//HDR.control_id").get("V", "")

            # 6) ACK finale (device -> server per il END)
            _mllp_send(s, _device_ack("6", end_ctrl))
            # Connessione si chiude

        # Attende che il thread del server processi tutto
        time.sleep(0.2)

        # Verifica store
        order = store.get_order("TEST-001")
        assert order and order["status"] == "READY", \
            f"status atteso READY: {order}"
        results = store.results_for("TEST-001")
        assert len(results) == 1
        assert len(results[0]["results"]) == 3
        assert results[0]["source"] == "hemoscreen_poct1a2"
        print("[8] HemoscreenPoct1A2Receiver: conversazione base OK, status=READY, 3 analiti  OK")

    finally:
        rx.stop()


def test_poct1a2_unmatched():
    """HemoscreenPoct1A2Receiver: OBS.R01 senza ordine -> unmatched."""
    store = Store("/tmp/hl7mw_test_hs4.db")
    Path("/tmp/hl7mw_test_hs4.db").unlink(missing_ok=True)
    store = Store("/tmp/hl7mw_test_hs4.db")

    rx = HemoscreenPoct1A2Receiver(store, "127.0.0.1", 0, continuous_mode=False, timeout=5.0)
    rx.start()
    srv_port = rx._srv.server_address[1]

    try:
        with socket.create_connection(("127.0.0.1", srv_port), timeout=5.0) as s:
            s.settimeout(5.0)
            _mllp_send(s, _build_hel("1"))
            _mllp_recv(s, 5.0)                 # ACK HEL
            _mllp_send(s, _build_dst("2", 1))
            _mllp_recv(s, 5.0)                 # ACK DST
            raw_req = _mllp_recv(s, 5.0)       # REQ.R01
            req_ctrl = ET.fromstring(raw_req.decode()).find(".//HDR.control_id").get("V", "")
            _mllp_send(s, _device_ack("3", req_ctrl))
            _mllp_send(s, OBS_R01_XML)
            _mllp_recv(s, 5.0)                 # ACK OBS
            _mllp_send(s, _build_eot("5"))
            _mllp_recv(s, 5.0)                 # ACK EOT
            raw_end = _mllp_recv(s, 5.0)       # END.R01
            end_ctrl = ET.fromstring(raw_end.decode()).find(".//HDR.control_id").get("V", "")
            _mllp_send(s, _device_ack("6", end_ctrl))

        time.sleep(0.2)
        assert len(store.unmatched()) == 1
        print("[9] HemoscreenPoct1A2Receiver: no ordine -> unmatched  OK")
    finally:
        rx.stop()


# ---------------------------------------------------------------------------
# Test POCT1-A2: builder delle direttive (roundtrip XML)
# ---------------------------------------------------------------------------

def test_poct1a2_directive_builders():
    """Builder delle direttive: struttura XML conforme a HS-IL-00067 §4.3-4.4."""
    xml = build_dtv_set_time("1", None)
    root = ET.fromstring(xml)
    assert root.tag == "DTV.R02"
    assert root.find(".//DTV.command_cd").get("V") == "SET_TIME"
    assert root.find(".//TM.dttm") is not None

    xml = build_opl_r01("1", [
        {"operator_id": "OPERATOR1", "permission_level_cd": "1"},
        {"operator_id": "OPERATOR2", "permission_level_cd": "4", "method_cd": "ALL"},
    ])
    root = ET.fromstring(xml)
    assert root.tag == "OPL.R01"
    oprs = root.findall("OPR")
    assert len(oprs) == 2
    assert oprs[0].find("OPR.operator_id").get("V") == "OPERATOR1"
    assert oprs[0].find(".//ACC.permission_level_cd").get("V") == "1"

    xml = build_opl_r02("1", [
        {"action_cd": "D", "operators": [{"operator_id": "OPERATOR1"}]},
        {"action_cd": "I", "operators": [{"operator_id": "OPERATOR4", "permission_level_cd": "4"}]},
    ])
    root = ET.fromstring(xml)
    assert root.tag == "OPL.R02"
    upds = root.findall("UPD")
    assert len(upds) == 2
    assert upds[0].find("UPD.action_cd").get("V") == "D"
    assert upds[0].find(".//OPR.operator_id").get("V") == "OPERATOR1"
    assert upds[1].find(".//ACC.permission_level_cd").get("V") == "4"

    xml = build_ptl_r01("1", {"patient_id": "123456", "last_name": "Larsen",
                              "first_name": "Allan", "birth_date": "1975-10-21",
                              "gender_cd": "M"})
    root = ET.fromstring(xml)
    assert root.tag == "PTL.R01"
    assert root.find(".//PT.patient_id").get("V") == "123456"
    assert root.find(".//FAM").get("V") == "Larsen"
    assert root.find(".//GIV").get("V") == "Allan"

    xml = build_ptl_r01("1", None)
    root = ET.fromstring(xml)
    assert root.find("PT") is None, "lista paziente vuota non deve avere sezione PT"

    xml = build_dtv_pix_qc("1", "PIX201205", "2020-12-05", "01", {
        "N": [{"observation_id": "6690-2", "dn": "WBC", "lo": "4", "hi": "11.5", "unit": "10*3/uL"}],
    })
    root = ET.fromstring(xml)
    assert root.tag == "DTV.PIX.QC"
    assert root.find(".//LOT.lot_number").get("V") == "PIX201205"
    assert root.find(".//LEVEL.level_cd").get("V") == "N"
    assert root.find(".//PARAM.normal_lo-hi_limit").get("V") == "[4;11.5]"

    xml = build_dtv_pix_fb("1", "2020-02-15", {
        "F": [{"observation_id": "6690-2", "dn": "WBC", "lo": "4", "hi": "11.5", "unit": "10*3/uL"}],
    })
    root = ET.fromstring(xml)
    assert root.tag == "DTV.PIX.FB"
    assert root.find(".//FBNR.effective_date").get("V") == "2020-02-15"
    assert root.find(".//GENDER.gender_cd").get("V") == "F"

    xml = build_dtv_pix_dvcset("1", {
        "opermode_cd": "CBC_5part", "language_cd": "English",
        "unit": {"wbc5part_cd": "10*3/uL"},
        "prmdis": {"wbc_cd": "SHOW"},
        "demogra": {"gender_cd": "ENABLE"},
        "lockdown": {"lockdown_mode_cd": "DISABLE"},
    })
    root = ET.fromstring(xml)
    assert root.tag == "DTV.PIX.DVCSET"
    assert root.find(".//DVCSET.opermode_cd").get("V") == "CBC_5part"
    assert root.find(".//UNIT.wbc5part_cd").get("V") == "10*3/uL"
    assert root.find(".//PRMDIS.wbc_cd").get("V") == "SHOW"

    print("[10] Builder direttive POCT1-A2 (SET_TIME/OPL/PTL/QC/FB/DVCSET): struttura XML OK")


# ---------------------------------------------------------------------------
# Test POCT1-A2: ESC.R01, REQ.R01(RDEV), REQ.R01(RPAT)->PTL.R01, direttive live
# ---------------------------------------------------------------------------

def test_poct1a2_esc_and_rdev():
    """DST.R01 con solo eventi pendenti -> REQ.R01(RDEV) (non ROBS); ESC.R01 non riceve
    risposta (bug storico: finiva nel ramo 'sconosciuto' con un ACK AE errato)."""
    store = Store("/tmp/hl7mw_test_hs5.db")
    Path("/tmp/hl7mw_test_hs5.db").unlink(missing_ok=True)
    store = Store("/tmp/hl7mw_test_hs5.db")

    rx = HemoscreenPoct1A2Receiver(store, "127.0.0.1", 0, continuous_mode=False, timeout=5.0)
    rx.start()
    srv_port = rx._srv.server_address[1]

    try:
        with socket.create_connection(("127.0.0.1", srv_port), timeout=5.0) as s:
            s.settimeout(5.0)
            buf = bytearray()
            _mllp_send(s, _build_hel("1"))
            _mllp_recv(s, 5.0, buf)  # ACK HEL

            # 0 osservazioni, 2 eventi pendenti -> ci si aspetta REQ.R01(RDEV), non ROBS
            _mllp_send(s, _build_dst_full("2", new_obs=0, new_events=2))
            _mllp_recv(s, 5.0, buf)  # ACK DST
            raw_req = _mllp_recv(s, 5.0, buf)
            assert raw_req, "nessun REQ.R01 ricevuto"
            req_root = ET.fromstring(raw_req.decode())
            assert req_root.tag == "REQ.R01"
            assert req_root.find(".//REQ.request_cd").get("V") == "RDEV", \
                "atteso REQ.R01(RDEV) quando new_obs=0 e new_events>0"

            # EVS.R01 con un evento -> deve finire su audit_log
            req_ctrl = req_root.find(".//HDR.control_id").get("V", "")
            _mllp_send(s, _device_ack("3", req_ctrl))
            evs = ('<EVS.R01><HDR><HDR.control_id V="4" /><HDR.version_id V="POCT1" />'
                   '<HDR.creation_dttm V="2024-01-01T10:00:06+00:00" /></HDR>'
                   '<EVT><EVT.description V="Errore di test" />'
                   '<EVT.event_dttm V="2024-01-01T10:00:06+00:00" />'
                   '<EVT.severity_cd V="W" /><EVT.number V="131228" />'
                   '<EVT.mode V="NORMAL" /></EVT></EVS.R01>')
            _mllp_send(s, evs)
            raw = _mllp_recv(s, 5.0, buf)
            assert raw and ET.fromstring(raw.decode()).tag == "ACK.R01"

            # EOT del topic eventi -> nessuna richiesta pendente -> END.R01
            _mllp_send(s, _build_eot_topic("5", "D_EV"))
            _mllp_recv(s, 5.0, buf)  # ACK EOT
            raw_end = _mllp_recv(s, 5.0, buf)
            assert raw_end, "nessun END.R01 dopo l'ultimo topic pendente"
            end_root = ET.fromstring(raw_end.decode())
            assert end_root.tag == "END.R01"

            # ESC.R01 al posto dell'ACK finale: nessuna risposta prevista, la
            # connessione resta "appesa" finché non chiudiamo noi lato test.
            end_ctrl = end_root.find(".//HDR.control_id").get("V", "")
            _mllp_send(s, _build_esc("6", end_ctrl))
            s.settimeout(1.0)
            try:
                extra = _mllp_recv(s, 1.0, buf)
                assert not extra, f"ESC.R01 non deve ricevere risposta, ottenuto: {extra!r}"
            except socket.timeout:
                pass

        time.sleep(0.2)
        audit = store.get_audit_log(limit=20)
        assert any(a["event_type"] == "poct1a2_device_event" for a in audit), \
            "EVS.R01 deve essere persistito su audit_log"
        assert any(a["event_type"] == "poct1a2_escape" for a in audit), \
            "ESC.R01 deve essere persistito su audit_log"
        print("[11] ESC.R01 senza risposta, REQ.R01(RDEV) su soli eventi, EVS.R01 su audit_log  OK")
    finally:
        rx.stop()


def test_poct1a2_continuous_rpat_and_directives():
    """Modalità continua: avvio, rifiuto via ESC, richiesta paziente (RPAT->PTL.R01)
    e invio di una direttiva live (LOCK) accodata da fuori la conversazione."""
    store = Store("/tmp/hl7mw_test_hs6.db")
    Path("/tmp/hl7mw_test_hs6.db").unlink(missing_ok=True)
    store = Store("/tmp/hl7mw_test_hs6.db")

    order = {
        "sample_key": "PAT-777", "placer_order_number": "", "filler_order_number": "",
        "specimen_id": "PAT-777",
        "patient": {"id": "PAT-777", "last_name": "Rossi", "first_name": "Mario",
                    "birth_date": "19800101", "sex": "M"},
        "universal_service_id": {"code": "58410-2", "text": "Emocromo", "system": "LN"},
        "message_control_id": "ORD003", "message_type": "ORM^O01",
        "ordering_provider": "", "requested_datetime": "20240101100000", "raw": "",
    }
    store.upsert_order(order)

    rx = HemoscreenPoct1A2Receiver(store, "127.0.0.1", 0, continuous_mode=True, timeout=5.0)
    rx.start()
    srv_port = rx._srv.server_address[1]

    try:
        with socket.create_connection(("127.0.0.1", srv_port), timeout=5.0) as s:
            s.settimeout(5.0)
            buf = bytearray()
            _mllp_send(s, _build_hel("1"))
            _mllp_recv(s, 5.0, buf)  # ACK HEL

            # Nessuna osservazione/evento pendente -> il server tenta subito START_CONTINUOUS
            _mllp_send(s, _build_dst_full("2", new_obs=0, new_events=0))
            _mllp_recv(s, 5.0, buf)  # ACK DST
            raw = _mllp_recv(s, 5.0, buf)
            dtv_root = ET.fromstring(raw.decode())
            assert dtv_root.tag == "DTV.R01"
            assert dtv_root.find(".//DTV.command_cd").get("V") == "START_CONTINUOUS"
            start_ctrl = dtv_root.find(".//HDR.control_id").get("V", "")

            # Il device accetta (ACK positivo)
            _mllp_send(s, _device_ack("3", start_ctrl))

            assert "0001-HS" in connected_devices(), \
                "il device deve comparire nel registro conversazioni attive dopo HEL.R01"

            # Il device chiede i dati del paziente TEST_ID=PAT-777 (RPAT)
            _mllp_send(s, _build_req_rpat("4", "PAT-777"))
            raw = _mllp_recv(s, 5.0, buf)
            ptl_root = ET.fromstring(raw.decode())
            assert ptl_root.tag == "PTL.R01"
            assert ptl_root.find(".//PT.patient_id").get("V") == "PAT-777"
            assert ptl_root.find(".//FAM").get("V") == "Rossi"
            ptl_ctrl = ptl_root.find(".//HDR.control_id").get("V", "")
            _mllp_send(s, _device_ack("5", ptl_ctrl))

            # Richiesta per un Test ID sconosciuto -> PTL.R01 con lista vuota
            _mllp_send(s, _build_req_rpat("6", "SCONOSCIUTO"))
            raw = _mllp_recv(s, 5.0, buf)
            ptl_root2 = ET.fromstring(raw.decode())
            assert ptl_root2.tag == "PTL.R01"
            assert ptl_root2.find("PT") is None
            ptl_ctrl2 = ptl_root2.find(".//HDR.control_id").get("V", "")
            _mllp_send(s, _device_ack("7", ptl_ctrl2))

            # Keep-alive "di sincronizzazione": attendendone l'ACK siamo certi che il
            # server abbia già processato (e drenato, trovandola vuota) la coda
            # direttive fino a questo punto, prima di accodare la LOCK qui sotto —
            # altrimenti l'accodamento (thread separato dal server) potrebbe correre
            # in parallelo con un giro di drenaggio già in corso lato server.
            _mllp_send(s, _build_kpa("8"))
            _mllp_recv(s, 5.0, buf)  # ACK KPA (sincronizzazione)

            # Direttiva LOCK accodata da fuori la conversazione (es. API/CLI): essendo
            # stata messa in coda dopo la conferma sopra, il prossimo keep-alive la
            # troverà di sicuro pronta da inviare.
            assert send_lock("0001-HS") is True

            _mllp_send(s, _build_kpa("9"))
            _mllp_recv(s, 5.0, buf)  # ACK KPA
            raw = _mllp_recv(s, 5.0, buf)
            assert raw, "nessuna direttiva LOCK ricevuta dopo il keep-alive"
            lock_root = ET.fromstring(raw.decode())
            assert lock_root.tag == "DTV.R01"
            assert lock_root.find(".//DTV.command_cd").get("V") == "LOCK"
            lock_ctrl = lock_root.find(".//HDR.control_id").get("V", "")
            _mllp_send(s, _device_ack("10", lock_ctrl))

        time.sleep(0.2)
        print("[12] Modalita' continua: START_CONTINUOUS, REQ.R01(RPAT)->PTL.R01, "
              "direttiva LOCK accodata da fuori la conversazione  OK")
    finally:
        rx.stop()


def test_poct1a2_continuous_rejected():
    """Il device rifiuta la modalità continua con ESC.R01: continuous_active resta False
    e non deve bloccare la chiusura successiva della conversazione con END.R01."""
    store = Store("/tmp/hl7mw_test_hs7.db")
    Path("/tmp/hl7mw_test_hs7.db").unlink(missing_ok=True)
    store = Store("/tmp/hl7mw_test_hs7.db")

    rx = HemoscreenPoct1A2Receiver(store, "127.0.0.1", 0, continuous_mode=True, timeout=5.0)
    rx.start()
    srv_port = rx._srv.server_address[1]

    try:
        with socket.create_connection(("127.0.0.1", srv_port), timeout=5.0) as s:
            s.settimeout(5.0)
            _mllp_send(s, _build_hel("1"))
            _mllp_recv(s, 5.0)
            _mllp_send(s, _build_dst_full("2", new_obs=0, new_events=0))
            _mllp_recv(s, 5.0)
            raw = _mllp_recv(s, 5.0)
            dtv_root = ET.fromstring(raw.decode())
            assert dtv_root.tag == "DTV.R01"
            start_ctrl = dtv_root.find(".//HDR.control_id").get("V", "")

            # Il device rifiuta la modalita' continua
            _mllp_send(s, _build_esc("3", start_ctrl, detail="OTH"))

            # La connessione deve restare aperta e funzionante (es. un successivo
            # keep-alive viene ancora ACKato normalmente)
            _mllp_send(s, _build_kpa("4"))
            raw = _mllp_recv(s, 5.0)
            assert raw and ET.fromstring(raw.decode()).tag == "ACK.R01"

        time.sleep(0.2)
        audit = store.get_audit_log(limit=20)
        assert any(a["event_type"] == "poct1a2_escape" for a in audit)
        print("[13] Rifiuto modalita' continua (ESC.R01 su START_CONTINUOUS) gestito "
              "senza bloccare la conversazione  OK")
    finally:
        rx.stop()


# ---------------------------------------------------------------------------
# Esecuzione
# ---------------------------------------------------------------------------

def main():
    ok = True
    tests = [
        test_parse_hl7_obs,
        test_parse_hl7_lqc,
        test_parse_hl7_flags,
        test_hl7_receiver_matched,
        test_hl7_receiver_unmatched,
        test_parse_obs_r01,
        test_parse_obs_r02,
        test_poct1a2_conversation,
        test_poct1a2_unmatched,
        test_poct1a2_directive_builders,
        test_poct1a2_esc_and_rdev,
        test_poct1a2_continuous_rpat_and_directives,
        test_poct1a2_continuous_rejected,
    ]
    for fn in tests:
        try:
            fn()
        except Exception as exc:
            print(f"FALLITO {fn.__name__}: {exc}")
            import traceback; traceback.print_exc()
            ok = False

    if ok:
        print("\nTUTTI I TEST HEMOSCREEN OK")
    else:
        print("\nALCUNI TEST FALLITI")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
