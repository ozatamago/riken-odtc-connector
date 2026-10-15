"""Checks the XML sent to the device and the loading of the method list. Connects to neither the device nor the Fluent.

The XML in fixtures/ is exactly what was sent in successful runs on the device (copied from qpcr_water_demo.py's probe_out).
"""

import pathlib

import pytest

from riken_odtc_connector.fluent_backend import BackendError, odtc_macro_xml
from riken_odtc_connector.method_store import load_methods, premethods_for

FIX = pathlib.Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("macro", "settings", "fixture"),
    [
        ("SiLA-ODTC_OpenDoor", None, "opendoor_20261013.xml"),
        ("SiLA-ODTC_CloseDoor", None, "closedoor_20261013.xml"),
        ("SiLA-ODTC_ExecuteMethod", "ini95", "executemethod_ini95_20261009.xml"),
        ("SiLA-ODTC_ExecuteMethod", "95-30s_50-60s_72-30s_30cycle", "executemethod_pcr_20261009.xml"),
    ],
)
def test_xml_matches_what_worked_on_the_device(macro, settings, fixture):
    assert odtc_macro_xml(macro, settings) == (FIX / fixture).read_text(encoding="utf-8")


@pytest.mark.parametrize("macro", ["SiLA-ODTC_SetParameters", "SiLA-ODTC_StopMethod", "SiLA-ODTC_GetParameters"])
def test_dangerous_or_unproven_macros_are_refused(macro):
    with pytest.raises(BackendError):
        odtc_macro_xml(macro)


def test_execute_method_needs_a_name_and_doors_take_none():
    with pytest.raises(BackendError):
        odtc_macro_xml("SiLA-ODTC_ExecuteMethod")
    with pytest.raises(BackendError):
        odtc_macro_xml("SiLA-ODTC_OpenDoor", "ini95")


def test_method_names_are_escaped():
    assert "<ExecutionSettings>a&amp;b&lt;c</ExecutionSettings>" in odtc_macro_xml("SiLA-ODTC_ExecuteMethod", "a&b<c")


def test_method_store_snapshot():
    methods = {(m.kind, m.name): m for m in load_methods()}
    assert len(methods) == 12
    assert {n for k, n in methods if k == "PreMethod"} == {"ini4", "ini25", "ini37", "ini42", "ini95"}
    pcr = methods[("Method", "95-30s_50-60s_72-30s_30cycle")]
    # matches the nominal 84.4 min recorded on 10/9 (measured 90.5 min)
    assert round((pcr.estimated_seconds + pcr.ramp_seconds) / 60, 1) == 84.4
    assert methods[("Method", "test")].estimated_seconds == 15


@pytest.mark.parametrize(
    ("method", "premethod"),
    [("test", "ini25"), ("HeatShock", "ini4"), ("EnzymeReaction", "ini37"), ("95-30s_50-60s_72-30s_30cycle", "ini95"),
     ("For 1 insert", "ini42")],
)
def test_matching_premethod(method, premethod):
    ms = load_methods()
    m = next(x for x in ms if x.name == method)
    assert premethods_for(m, ms) == [premethod]
