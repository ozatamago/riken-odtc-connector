"""Stage 4-4 pipetting commands: XML equal to what ran in August 2026, and the checks before sending."""

import asyncio
import hashlib
import pathlib

import pytest

from riken_odtc_connector.fluent780 import Fluent780ConnectorConfig, build_features
from riken_odtc_connector.liquid_handling import (
    LiquidHandlingFailed,
    drop_tips_xml,
    get_tips_xml,
    move_arm_xml,
    pipetting_xml,
    well_index,
    wells_fields,
)

FIX = pathlib.Path(__file__).parent / "fixtures"


def obj(xml: str) -> list[str]:
    lines = [ln.strip() for ln in xml.splitlines() if ln.strip()]
    start = next(i for i, ln in enumerate(lines) if ln.startswith("<Object "))
    end = max(i for i, ln in enumerate(lines) if ln == "</Object>")
    # "&gt;" and ">" are the same text in XML (the GUI writes "&gt;", the August XML had ">")
    return [ln.replace("&gt;", ">") for ln in lines[start:end + 1] if not ln.startswith("<LineNumber>")]


def ref(path: str) -> list[str]:
    return obj((FIX / path).read_text(encoding="utf-8"))


@pytest.mark.parametrize(("xml", "fixture"), [
    (get_tips_xml("FCA, 200ul SBS", [1]), "liha_verified/gettips_1ch.xml"),
    (get_tips_xml("FCA, 1000ul SBS", list(range(1, 9))), "liha_verified/gettips_8ch_1000.xml"),
    (pipetting_xml("Aspirate", "1.5ml Sarstedt[001]", ["A1"], [20], "Water Free Multi", [1], 4, 6), "liha_verified/aspirate_1ch.xml"),
    (pipetting_xml("Dispense", "OFML_TestPlate", ["A1"], [20], "Water Free Multi", [1], 8, 12), "liha_verified/dispense_1ch.xml"),
    (pipetting_xml("Mix", "OFML_TestPlate", ["A1"], [15], "Water Mix_1", [1], 8, 12, cycles=5), "liha_verified/mix_1ch.xml"),
    (pipetting_xml("Aspirate", "4 Well SBS Trough[001]", ["A1"] * 8, [960] * 8, "Water Free Multi", list(range(1, 9)), 1, 4),
     "liha_verified/asp_trough_8ch.xml"),
    (pipetting_xml("Dispense", "96 Well Flat_Custom[001]", [f"{r}1" for r in "ABCDEFGH"], [150] * 8, "Water Free Multi",
                   list(range(1, 9)), 8, 12), "liha_verified/dis_flat_col1.xml"),
    (drop_tips_xml(list(range(1, 9))), "gui_statements/LihaDropTipsScriptCommandDataV2_3.xml"),
    (move_arm_xml("100ml_Custom_1", "A1", list(range(1, 9)), 1, 1), "fluentcontrol_scripts/movearm_airfca_leakage_test.xml"),
])
def test_matches_xml_that_ran_on_the_instrument(xml, fixture):
    assert obj(xml) == ref(fixture)


def test_get_tips_can_name_the_rack():
    assert "<LabwareName>FCA_ 1000ul SBS[002]</LabwareName>" in get_tips_xml("FCA, 1000ul SBS", [1], "FCA_ 1000ul SBS[002]")


def test_move_arm_one_channel_at_travel_height():
    xml = move_arm_xml("4 Well SBS Trough[001]", "A1", [1], 1, 4)
    assert "<SelectedWellsString>1 * A1</SelectedWellsString>" in xml
    assert "<LabwareName>4 Well SBS Trough[001]</LabwareName>" in xml
    assert "<MovementType>travelToLabware</MovementType>" in xml and "<ZCustomVector>ZTravel</ZCustomVector>" in xml
    assert xml.count("<int>") == 1


def test_well_numbering_is_column_major():
    assert well_index("A1", 8, 12) == 0 and well_index("H1", 8, 12) == 7 and well_index("A2", 8, 12) == 8
    assert well_index("A3", 1, 4) == 2
    assert wells_fields(["A1"] * 8, 1, 4) == ("0;0;0;0;0;0;0;0;", "8 * A1")
    assert wells_fields([f"{r}1" for r in "ABCDEFGH"], 8, 12) == ("0>1>7;", "A1 - H1")
    with pytest.raises(Exception, match="outside the labware"):
        well_index("I1", 8, 12)


# ---- the checks, with a stand-in for the FluentControl definitions
def vx(name, body):
    return f"<VxData><Payload><ObjectName>{name}</ObjectName><PayloadData>{body}</PayloadData></Payload></VxData>".encode()


LC_200 = ("<Tecan.Core.Pipetting.LiquidClassEquationSet><DeviceType><PipettingDeviceType>AirFca</PipettingDeviceType>"
          "</DeviceType><DiTiTypeGroup>FCA DiTi 200µl</DiTiTypeGroup><AspiratePipettingSpeed>150</AspiratePipettingSpeed>"
          "</Tecan.Core.Pipetting.LiquidClassEquationSet>")


class FakePc:
    files = {
        r"C\tip200.xcmp": vx("FCA, 200ul SBS", "<Key>DiTiTypeGroup</Key><Value>FCA DiTi 200µl</Value>"
                                               "<Key>MaxNettoVolumeForTransferPhase</Key><Value>170</Value>"),
        r"C\tip10.xcmp": vx("FCA, 10ul SBS", "<Key>DiTiTypeGroup</Key><Value>FCA DiTi 10µl</Value>"
                                             "<Key>MaxNettoVolumeForTransferPhase</Key><Value>10</Value>"),
        r"C\trough.xcmp": vx("4 Well SBS Trough", "<XNumberOfWells>4</XNumberOfWells><YNumberOfWells>1</YNumberOfWells>"),
        r"C\flat.xcmp": vx("96 Well Flat_Custom", "<XNumberOfWells>12</XNumberOfWells><YNumberOfWells>8</YNumberOfWells>"),
        r"C\pcr.xcmp": vx("96 Well Skirted PCR_Custom", "<XNumberOfWells>12</XNumberOfWells><YNumberOfWells>8</YNumberOfWells>"),
        r"L\wfs.xlqc": vx("Water Free Single", LC_200),
    }

    def index(self):
        return {p: {"path": p, "sha256": hashlib.sha256(d).hexdigest(), "name": d.decode().split("<ObjectName>")[1].split("<")[0]}
                for p, d in self.files.items()}

    def read(self, rel):
        return self.files[rel]


class _S:
    def update(self, *a, **k):
        pass


def _setup():
    f = {type(x).__name__: x for x in build_features(Fluent780ConnectorConfig())}  # dry run
    lh, lc, backend = f["LiquidHandling"], f["LabwareController"], f["RunController"].backend
    lh.pc = FakePc()
    return lh, lc, backend


def test_a_water_transfer_and_the_refusals():
    lh, lc, backend = _setup()
    S = _S()

    async def scenario():
        with pytest.raises(LiquidHandlingFailed, match="Start a run first"):
            await lh.get_tips("FCA, 200ul SBS", [1], "", status=S)
        await backend.start_run()
        await lc.add_labware("Water[001]", "4 Well SBS Trough", "Nest7mm_Pos", 5, status=S)
        await lc.add_labware("Plate[001]", "96 Well Flat_Custom", "Nest7mm_Pos", 2, status=S)
        with pytest.raises(LiquidHandlingFailed, match="have no tips"):
            await lh.aspirate("Water[001]", ["A1"], [50], "Water Free Single", [1], status=S)
        with pytest.raises(LiquidHandlingFailed, match="rack \\[001\\] does not fit"):
            await lh.get_tips("FCA, 1000ul SBS", [1], "", status=S)
        await lh.get_tips("FCA, 200ul SBS", [1], "", status=S)
        with pytest.raises(LiquidHandlingFailed, match="already have tips"):
            await lh.get_tips("FCA, 200ul SBS", [1], "", status=S)
        with pytest.raises(LiquidHandlingFailed, match="more than a FCA, 200ul SBS tip holds"):
            await lh.aspirate("Water[001]", ["A1"], [180], "Water Free Single", [1], status=S)
        with pytest.raises(LiquidHandlingFailed, match="neither added in this run"):
            await lh.aspirate("Elsewhere[001]", ["A1"], [50], "Water Free Single", [1], status=S)
        await lh.aspirate("Water[001]", ["A1"], [50], "Water Free Single", [1], status=S)
        with pytest.raises(LiquidHandlingFailed, match="already aspirated with these tips"):
            await lh.aspirate("Water[001]", ["A1"], [20], "Water Free Single", [1], status=S)
        with pytest.raises(LiquidHandlingFailed, match="holds 50 ul; cannot dispense 60"):
            await lh.dispense("Plate[001]", ["A1"], [60], "Water Free Single", [1], status=S)
        await lh.dispense("Plate[001]", ["A1"], [50], "Water Free Single", [1], status=S)
        with pytest.raises(Exception, match="Tips are still mounted"):
            await backend.end_run()
        await lh.drop_tips([1], status=S)
        assert await backend.end_run() == 6

    asyncio.run(scenario())


def test_liquid_class_without_settings_for_the_tip_and_10ul_on_pcr_are_refused():
    lh, lc, backend = _setup()
    S = _S()

    async def scenario():
        await backend.start_run()
        await lc.add_labware("Water[001]", "4 Well SBS Trough", "Nest7mm_Pos", 5, status=S)
        await lc.add_labware("Pcr[001]", "96 Well Skirted PCR_Custom", "Nest7mm_Pos", 2, status=S)
        await lh.get_tips("FCA, 10ul SBS", [1], "", status=S)
        with pytest.raises(LiquidHandlingFailed, match="no Air FCA settings for FCA DiTi 10µl"):
            await lh.aspirate("Water[001]", ["A1"], [5], "Water Free Single", [1], status=S)
        with pytest.raises(LiquidHandlingFailed, match="10 ul tips do not reach"):
            await lh.aspirate("Pcr[001]", ["A1"], [5], "Water Free Single", [1], status=S)
        await lh.drop_tips([1], status=S)
        await backend.end_run()

    asyncio.run(scenario())
