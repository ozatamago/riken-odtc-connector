"""温度制御つき装置の XML が、01〜07 の GUI スクリプトのマクロと同じかを確かめる。装置にはつながない。"""

import pathlib

import pytest

from riken_odtc_connector.temperature_devices import (
    bioshake_set_temp_xml,
    bioshake_temp_off_xml,
    incubator1_set_temperature_xml,
    inheco_incubator_set_temp_xml,
    mtc_set_temperature_xml,
)

GUI = pathlib.Path(__file__).parent / "fixtures" / "gui_macros"


def obj(xml: str) -> list[str]:
    """ScriptGroup の中の <Object> だけを、行頭の空白を除いて取り出す（GUI スクリプトの雛形と比べる形）。"""
    lines = [ln.strip() for ln in xml.splitlines()]
    return lines[lines.index(next(ln for ln in lines if ln.startswith("<Object "))):][: 5]


def gui(name: str) -> list[str]:
    return [ln.strip() for ln in (GUI / name).read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.mark.parametrize(
    ("xml", "fixture"),
    [
        (bioshake_set_temp_xml(4), "BioShake_SetTemp_1.xml"),               # 03: "4"
        (bioshake_temp_off_xml(), "BioShake_TempOff_1.xml"),               # 02
        (inheco_incubator_set_temp_xml(37), "InhecoIncubator_SetTemp_1.xml"),  # 03: "37,0.0,0"
        (inheco_incubator_set_temp_xml(25, "PT1M40S"), "InhecoIncubator_SetTemp_2.xml"),  # Sub_Cleanup: "25,0.0,0"
        (mtc_set_temperature_xml(1, True, 4), "inhecoMTC_SetTemperature_3.xml"),     # 03: "1,True,4.0,0"
        (mtc_set_temperature_xml(3, True, 65), "inhecoMTC_SetTemperature_1.xml"),    # 06: "3,True,65.0,0"
        (mtc_set_temperature_xml(1, False, 37.5), "inhecoMTC_SetTemperature_6.xml"),  # 03: "1,False,37.5,0"
        (incubator1_set_temperature_xml(37), "Incubator1_SetTemperature_1.xml"),  # 05: "0,37"
        (incubator1_set_temperature_xml(25), "Incubator1_SetTemperature_2.xml"),  # 05: "0,25"
    ],
)
def test_matches_gui_script_macro(xml, fixture):
    assert obj(xml) == gui(fixture)
