"""Checks that the XML for temperature-controlled devices matches the macros in GUI scripts 01-07. No device access."""

import pathlib

import pytest

from riken_odtc_connector.temperature_devices import (
    bioshake_macro_xml,
    bioshake_set_temp_xml,
    bioshake_shake_on_xml,
    incubator1_macro_xml,
    incubator1_start_shaker_xml,
    inheco_incubator_macro_xml,
    inheco_incubator_shake_on_xml,
    bioshake_temp_off_xml,
    incubator1_set_temperature_xml,
    inheco_incubator_set_temp_xml,
    mtc_set_temperature_xml,
)

GUI = pathlib.Path(__file__).parent / "fixtures" / "gui_macros"


def obj(xml: str) -> list[str]:
    """Extract only the <Object> in the ScriptGroup, leading whitespace stripped (the form of the GUI script templates)."""
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


@pytest.mark.parametrize(
    ("xml", "fixture"),
    [
        (bioshake_macro_xml("BioShake_ElmLock"), "BioShake_ElmLock_1.xml"),
        (bioshake_macro_xml("BioShake_ElmUnlock"), "BioShake_ElmUnlock_1.xml"),
        (bioshake_macro_xml("BioShake_ShakeOff"), "BioShake_ShakeOff_1.xml"),
        (inheco_incubator_macro_xml("InhecoIncubator_Close"), "InhecoIncubator_Close_1.xml"),
        (inheco_incubator_shake_on_xml(), "InhecoIncubator_ShakeOn_1.xml"),          # 03: "14,1.0,14,1.0,180"
        (inheco_incubator_macro_xml("InhecoIncubator_ShakeOff"), "InhecoIncubator_ShakeOff_1.xml"),
        (incubator1_macro_xml("Incubator1_CloseDoor"), "Incubator1_CloseDoor_1.xml"),
        (incubator1_start_shaker_xml(5), "Incubator1_StartShaker_1.xml"),            # Sub_KineticMeasurement: "5"
        (incubator1_macro_xml("Incubator1_StopShaker"), "Incubator1_StopShaker_1.xml"),
    ],
)
def test_moving_macros_match_gui_script(xml, fixture):
    assert obj(xml) == gui(fixture)


def test_bioshake_shake_on_matches_sub_skake_with_the_speed_filled_in():
    # Sub_Skake uses "~SpeedShake~,5" (the speed variable is filled in at run time)
    expected = [ln.replace("~SpeedShake~", "1400") for ln in gui("BioShake_ShakeOn_2.xml")]
    assert obj(bioshake_shake_on_xml(1400)) == expected


def test_unlisted_moving_macros_are_refused():
    from riken_odtc_connector.fluent_backend import BackendError

    for fn, name in [(bioshake_macro_xml, "BioShake_Shake"), (inheco_incubator_macro_xml, "InhecoIncubator_Open"),
                     (incubator1_macro_xml, "Incubator1_InsertPlate")]:
        with pytest.raises(BackendError):
            fn(name)
