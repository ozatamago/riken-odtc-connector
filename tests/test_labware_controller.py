"""Stage 4-3 labware commands: the XML must equal the statements of the 01-07 GUI scripts. Nothing is sent."""

import asyncio
import pathlib

import pytest

from riken_odtc_connector.fluent780 import Fluent780ConnectorConfig, build_features
from riken_odtc_connector.labware_controller import (
    LabwareCommandFailed,
    drop_fingers_xml,
    get_fingers_xml,
    lid_on_xml,
    to_base_xml,
    transfer_xml,
)
from riken_odtc_connector.infinite.xml import add_labware_xml, remove_labware_xml

FIX = pathlib.Path(__file__).parent / "fixtures"


def obj(xml: str) -> list[str]:
    """The <Object> ... </Object> part, line by line without indentation."""
    lines = [ln.strip() for ln in xml.splitlines() if ln.strip()]
    start = next(i for i, ln in enumerate(lines) if ln.startswith("<Object "))
    end = max(i for i, ln in enumerate(lines) if ln == "</Object>")
    return lines[start:end + 1]


def gui(path: str) -> list[str]:
    return obj((FIX / path).read_text(encoding="utf-8"))


@pytest.mark.parametrize(("xml", "fixture"), [
    (transfer_xml("Template_Lid_6mm[001]", "HotelMP_Pos", 2), "gui_macros/RGA1_TransferLabware_17.xml"),  # 03 line 33
    (to_base_xml("IncubationLid_6mm[001]"), "gui_macros/RGA1_TransferLabware_20.xml"),                     # 03 line 56
    (lid_on_xml("ODTC_Lid[001]", "TemplatePlate[001]"), "gui_macros/RGA1_TransferLabware_18.xml"),         # 03 line 45
    (get_fingers_xml("Eccentric[001]"), "gui_statements/CgaGetFingersScriptCommandDataV1_2.xml"),          # 03 line 29
    (get_fingers_xml("Centric[001]"), "gui_statements/CgaGetFingersScriptCommandDataV1_1.xml"),            # 06 line 59 (04 has the same fields in another order)
    (drop_fingers_xml(), "gui_statements/CgaDropFingersScriptCommandDataV1_2.xml"),                         # 03 line 28
    (remove_labware_xml("TemplatePlate[001]"), "gui_statements/RemoveLabwareDataV1_1.xml"),
])
def test_matches_gui_script_statement(xml, fixture):
    assert obj(xml) == gui(fixture)


def test_add_labware_has_the_gui_fields():
    xml = add_labware_xml("96 Well Skirted PCR_Custom_Template", "TemplatePlate[001]", "BioShake_Q1_Adapter_Site_PCR", 1)
    fields = [ln for ln in obj(xml) if ln.startswith(("<LabwareType>", "<LabwareLable>", "<Location>", "<Position>",
                                                       "<Rotation>", "<HasLid>"))]
    expected = [ln for ln in gui("gui_statements/AddLabwareDataV1_1.xml") if ln.startswith(
        ("<LabwareType>", "<LabwareLable>", "<Location>", "<Position>", "<Rotation>", "<HasLid>"))]
    assert fields == expected


def _features():
    cfg = Fluent780ConnectorConfig()  # dry run
    f = {type(x).__name__: x for x in build_features(cfg)}
    return f["LabwareController"], f["RunController"].backend


class _Status:
    def update(self, *a, **k):
        pass


def test_needs_an_open_run_and_refuses_tracked_labware():
    lc, backend = _features()

    async def scenario():
        with pytest.raises(LabwareCommandFailed, match="Start a run first"):
            await lc.transfer_labware("Template_Lid_6mm[001]", "HotelMP_Pos", 2, status=_Status())
        await backend.start_run()
        with pytest.raises(LabwareCommandFailed, match="tracked by RgaController"):
            await lc.transfer_labware("96 Well Flat_Custom[001]", "Regrip_Pos", 1, status=_Status())
        with pytest.raises(LabwareCommandFailed, match="Fingers must be one of"):
            await lc.get_fingers("Tube[001]", status=_Status())
        await lc.add_labware("TemplatePlate[001]", "96 Well Skirted PCR_Custom", "Nest7mm_Pos", 1, status=_Status())
        await lc.get_fingers("Eccentric[001]", status=_Status())
        await lc.transfer_labware("TemplatePlate[001]", "Regrip_Pos", 1, status=_Status())
        await lc.drop_fingers(status=_Status())
        assert await backend.end_run() == 4

    asyncio.run(scenario())


def test_finger_statements_record_the_rga_settings():
    from riken_odtc_connector.definitions import extract_references

    refs = extract_references(get_fingers_xml("Eccentric[001]"))
    assert refs["arms"] == ["RGA 1"] and refs["labware"] == ["Eccentric[001]"]
