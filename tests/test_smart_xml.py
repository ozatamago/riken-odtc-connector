"""Stage 4-5: the remaining statements of the 01-07 scripts are rebuilt exactly from their values."""

import pathlib

import pytest

from riken_odtc_connector.fluent_backend import BackendError
from riken_odtc_connector.smart_xml import (
    channel_mask,
    detect_liquid_xml,
    individual_volumes_file,
    individual_volumes_xml,
    inline_filter_check_xml,
    labware_geometry,
    reagent_distribution_xml,
    sample_transfer_xml,
    set_clld_error_handling_xml,
    worklist_csv,
    worklist_xml,
)

FIX = pathlib.Path(__file__).parent / "fixtures" / "gui_statements"
ALL_96 = [f"{r}{c}" for c in range(1, 13) for r in "ABCDEFGH"]  # FluentControl's order: down the columns
PLATE = {"xcount": 12, "ycount": 8, "xspacing": 9, "yspacing": 9, "shape": "Ellipse", "height": 7, "width": 7,
         "tips_per_well": 1}


def obj(xml: str) -> list[str]:
    lines = [ln.strip() for ln in xml.splitlines() if ln.strip()]
    start = next(i for i, ln in enumerate(lines) if ln.startswith("<Object "))
    end = max(i for i, ln in enumerate(lines) if ln == "</Object>")
    return [ln.replace("&gt;", ">") for ln in lines[start:end + 1] if "LineNumber" not in ln]


def ref(name: str, **values) -> list[str]:
    """A 01-07 statement with its expressions replaced by the literal values a SiLA command sends."""
    text = (FIX / name).read_text(encoding="utf-8")
    for old, new in values.items():
        text = text.replace(old, new)
    return obj(text)


# Trimmed from the instrument's labware definitions (structure as in the .xcmp files)
TROUGH_XCMP = """<XNumberOfWells>6</XNumberOfWells><XSpacing>19</XSpacing><YNumberOfWells>1</YNumberOfWells>
<YSpacing>0</YSpacing><d5p1:ShapeList><d5p1:CavityShape i:type="d5p1:Trapezoid"><d5p1:Height>6</d5p1:Height>
<d5p1:Length>70</d5p1:Length><d5p1:WidthBase>2</d5p1:WidthBase><d5p1:WidthTop>16.9</d5p1:WidthTop></d5p1:CavityShape>
<d5p1:CavityShape i:type="d5p1:Cuboid"><d5p1:Height>35.75</d5p1:Height><d5p1:Length>70.7</d5p1:Length>
<d5p1:Width>16.9</d5p1:Width></d5p1:CavityShape></d5p1:ShapeList><TipsPerWell>8</TipsPerWell>"""
PLATE_XCMP = """<XNumberOfWells>12</XNumberOfWells><XSpacing>9</XSpacing><YNumberOfWells>8</YNumberOfWells>
<YSpacing>-9</YSpacing><d5p1:ShapeList><d5p1:CavityShape i:type="d5p1:Cylinder"><d5p1:Height>10.9</d5p1:Height>
<d5p1:DiameterBottom>6.58</d5p1:DiameterBottom><d5p1:DiameterTop>6.96</d5p1:DiameterTop></d5p1:CavityShape>
</d5p1:ShapeList><TipsPerWell>1</TipsPerWell>"""


def test_labware_pictures_from_definitions():
    # the 6 Well SBS Trough of 04 (TransferIndividualVolumes_2): 6 x 1, 20.9 x 74.7, rectangle 70.7 x 16.9, 8 tips
    assert labware_geometry(TROUGH_XCMP) == {"xcount": 6, "ycount": 1, "xspacing": 20.9, "yspacing": 74.7,
                                             "shape": "Rectangle", "height": 70.7, "width": 16.9, "tips_per_well": 8}
    assert labware_geometry(PLATE_XCMP) == {"xcount": 12, "ycount": 8, "xspacing": 9, "yspacing": 9,
                                            "shape": "Ellipse", "height": 6.96, "width": 6.96, "tips_per_well": 1}


def test_reagent_distribution_of_03():
    medium = {"xcount": 1, "ycount": 1, "xspacing": 129, "yspacing": 87, "shape": "Rectangle", "height": 83,
              "width": 125, "tips_per_well": 96}
    xml = reagent_distribution_xml("Medium[001]", "A1", medium, "IncubationDPW[001]", ALL_96, PLATE, 150,
                                   "Water Free Single", "FCA, 1000ul SBS", list(range(1, 9)), 12, 1)
    assert obj(xml) == ref("LihaReagentDistributionSmartCommand_5.xml",
                           **{">VolMedium<": ">150<", ">NrOfSample<": ">96<"})


def test_reagent_distribution_with_other_values():
    trough = labware_geometry(TROUGH_XCMP)
    xml = reagent_distribution_xml("6 Well SBS Trough[001]", "A2", trough, "TestPlate[001]", ["A1", "B1", "C1"],
                                   PLATE, 50, "Water Free Multi", "FCA, 200ul SBS", [1, 2, 3], 4, 3)
    assert "<TargetVolumePerWell>50</TargetVolumePerWell>" in xml and "<SampleCount>3</SampleCount>" in xml
    assert "TOOLNAME:FCA, 200ul SBS<" in xml and "<MaxNumberOfMultiDispense>3</MaxNumberOfMultiDispense>" in xml
    assert "<SelectedWellsString>A1 - C1</SelectedWellsString>" in xml
    source = xml[xml.index("<PropertyPrefix>Source"):xml.index("<PropertyPrefix>Target")]
    assert "<int>1</int>" in source and "<SelectedWellsString>A2</SelectedWellsString>" in source  # lane 2 = index 1
    assert xml.count("<LabwareName>6 Well SBS Trough[001]</LabwareName>") == 4  # source and left-over tip content


def test_sample_transfer_of_06():
    xml = sample_transfer_xml("CollectionPlate", ALL_96, PLATE, "ElutionPlate", ALL_96,
                              {**PLATE, "height": 5.5, "width": 5.5}, 50, "Zymo Supernatant Transfer",
                              "FCA, 200ul Filtered SBS", list(range(1, 9)))
    expected = ref("LihaSampleTransferSmartCommand_2.xml", **{">ZEBVolume<": ">50<", ">SampleNum<": ">96<"})
    # 06 takes the picture of the collection plate from a plate with 8.1 mm wells; only these lines differ
    got = obj(xml)
    diff = [(a, b) for a, b in zip(got, expected) if a != b]
    assert len(got) == len(expected) and {b for _, b in diff} <= {"<Height>8.1</Height>", "<Width>8.1</Width>",
                                                                   "<X>0.45000000000000018</X>",
                                                                   "<Y>0.45000000000000018</Y>"}


def test_transfer_individual_volumes_of_04():
    trough = labware_geometry(TROUGH_XCMP)
    plate = {**PLATE, "height": 6.96, "width": 6.96}
    xml = individual_volumes_xml("6 Well SBS Trough[001]", trough, "CulturePlate[001]", plate,
                                 r"C:\Users\Tecan\OFML\records\runs\x\files\v.csv", "Water Free Multi",
                                 "FCA, 200ul SBS", list(range(1, 9)), 6, 12)
    assert obj(xml) == ref("LihaTransferIndividualVolumesSmartCommand_2.xml",
                           **{">NameImportFile<": '>"C:\\Users\\Tecan\\OFML\\records\\runs\\x\\files\\v.csv"<'})
    # the same form as 04's files (C:\FluentData\04_SelectionMediumPreparation\Medium1.csv)
    assert individual_volumes_file([(1, 1, 10), (1, 2, 12.5)]) == \
        "Source well index;Target well index;Volume [ul]\r\n1;1;10\r\n1;2;12.5\r\n"


def test_detect_liquid_of_02():
    xml = detect_liquid_xml("Alpaqua POGO_Custom[001]", ["A1"], [1], 4, 6, 18.03)
    assert obj(xml) == ref("LihaDetectLiquidScriptCommandDataV3_1.xml")
    assert obj(detect_liquid_xml("Alpaqua POGO_Custom[001]", ["A6"], [1], 4, 6, 18.03)) == \
        ref("LihaDetectLiquidScriptCommandDataV3_2.xml")


def test_inline_filter_check_of_02():
    assert obj(inline_filter_check_xml(list(range(1, 9)))) == ref("LihaInlineFilterCheckScriptCommandDataV1_1.xml")
    assert channel_mask([1]) == 1 and channel_mask([1, 8]) == 129


def test_error_handling_of_06():
    assert obj(set_clld_error_handling_xml({})) == ref("LihaSetClldErrorHandlingScriptCommandDataV4_2.xml")
    xml = set_clld_error_handling_xml({"Aspirate": "AspirateAvailable", "ContactDispense": "IgnoreAndContinue"})
    expected = ref("LihaSetClldErrorHandlingScriptCommandDataV4_1.xml")
    # 06's first statement also prompts the user on the other errors; the SiLA command does not offer that
    assert [a for a, b in zip(obj(xml), expected) if a != b] == ["<Action>UseMicroscriptSettings</Action>"] * 4
    with pytest.raises(BackendError, match="UserPrompt"):
        set_clld_error_handling_xml({"Aspirate": "UserPrompt"})
    with pytest.raises(BackendError, match="Unknown error kind"):
        set_clld_error_handling_xml({"Other": "IgnoreAndContinue"})


def test_worklist_of_07_and_05():
    csv, gwl = r"C:\r\files\w.csv", r"C:\r\files\w.gwl"
    (_, imp), (_, load), (_, run) = worklist_xml(csv, gwl, "FCA, 200ul SBS", "Water Free Single", list(range(1, 9)))
    assert obj(imp) == ref("WorklistImportStatementDataV2_1.xml",
                           **{">NameImportFile<": '>"C:\\r\\files\\w.csv"<', ">NameExportFile<": '>"C:\\r\\files\\w.gwl"<'})
    assert obj(load) == ref("LoadWorklistStatementDataV4_2.xml", **{">NameExportFile<": '>"C:\\r\\files\\w.gwl"<'})
    assert obj(run) == ref("ExecuteWorklistStatementDataV1_1.xml")
    assert worklist_csv([("Trough[001]", "A1", "TestPlate[001]", "B2", 20)]) == \
        "SourceLabel,SourcePosition,DestLabel,DestPosition,Volume\r\nTrough[001],A1,TestPlate[001],B2,20\r\n"
    with pytest.raises(BackendError, match="commas"):
        worklist_csv([("a,b", "A1", "c", "A1", 1)])


def test_the_commands_in_a_dry_run():
    import asyncio

    from riken_odtc_connector.liquid_handling import LiquidHandlingFailed
    from test_liquid_handling import FakePc, _S, _setup, vx

    class Pc(FakePc):
        files = {**FakePc.files,
                 r"C\trough.xcmp": vx("4 Well SBS Trough", TROUGH_XCMP.replace(">6<", ">4<").replace("16.9", "25.35")),
                 r"C\flat.xcmp": vx("96 Well Flat_Custom", PLATE_XCMP)}

    lh, lc, backend = _setup()
    lh.pc = Pc()
    S = _S()

    async def scenario():
        await backend.start_run()
        await lc.add_labware("Water[001]", "4 Well SBS Trough", "Nest7mm_Pos", 5, status=S)
        await lc.add_labware("Plate[001]", "96 Well Flat_Custom", "Nest7mm_Pos", 2, status=S)
        await lh.check_inline_filters([1, 2], status=S)
        with pytest.raises(LiquidHandlingFailed, match="UserPrompt"):
            await lh.set_liquid_detection_error_handling("UserPrompt", "", "", "", "", "", "", status=S)
        await lh.set_liquid_detection_error_handling("AspirateAvailable", "", "", "", "", "", "", status=S)
        with pytest.raises(LiquidHandlingFailed, match="have no tips"):
            await lh.detect_liquid("Water[001]", ["A1"], [1], status=S)
        await lh.get_tips("FCA, 200ul SBS", [1], "", status=S)
        await lh.detect_liquid("Water[001]", ["A1"], [1], status=S)
        with pytest.raises(LiquidHandlingFailed, match="takes and drops its own tips"):
            await lh.distribute_reagent("Water[001]", "A1", "Plate[001]", ["A1"], 50, "Water Free Single",
                                        "FCA, 200ul SBS", [1], 1, 1, status=S)
        await lh.drop_tips([1], status=S)
        with pytest.raises(LiquidHandlingFailed, match="1000ul SBS\\[001\\]"):
            await lh.distribute_reagent("Water[001]", "A1", "Plate[001]", ["A1"], 50, "Water Free Single",
                                        "FCA, 1000ul SBS", [1], 1, 1, status=S)
        with pytest.raises(LiquidHandlingFailed, match="about 200 ul .* tip holds 170 ul"):
            await lh.distribute_reagent("Water[001]", "A1", "Plate[001]", ["A1", "B1"], 100, "Water Free Single",
                                        "FCA, 200ul SBS", [1], 1, 2, status=S)
        await lh.distribute_reagent("Water[001]", "A1", "Plate[001]", ["A1", "B1", "C1"], 50, "Water Free Single",
                                    "FCA, 200ul SBS", [1, 2, 3], 1, 1, status=S)
        await lh.transfer_samples("Plate[001]", ["A1", "B1"], "Plate[001]", ["A2", "B2"], 20, "Water Free Single",
                                  "FCA, 200ul SBS", [1, 2], status=S)
        path = await lh.transfer_individual_volumes("Water[001]", "Plate[001]", ["A1", "A1"], ["A3", "B3"], [10, 20],
                                                    "Water Free Single", "FCA, 200ul SBS", [1, 2], 1, 1, status=S)
        assert path.endswith("individual_volumes.csv") and "\\files\\" in path
        with pytest.raises(LiquidHandlingFailed, match="outside the labware"):
            await lh.run_worklist(["Water[001]"], ["A5"], ["Plate[001]"], ["A4"], [20], "FCA, 200ul SBS",
                                  "Water Free Single", [1], status=S)
        path = await lh.run_worklist(["Water[001]", "Water[001]"], ["A1", "A1"], ["Plate[001]", "Plate[001]"],
                                     ["A4", "B4"], [20, 30], "FCA, 200ul SBS", "Water Free Single", [1], status=S)
        assert path.endswith("worklist.csv")
        # 2 AddLabware, filter check, error handling, GetTips, DetectLiquid, DropTips, 3 Smart Commands, worklist
        assert await backend.end_run() == 11

    asyncio.run(scenario())


def test_planned_aspiration_from_the_liquid_class():
    from riken_odtc_connector.liquid_handling import corrected_volume, planned_aspiration

    adjust = "AdjustAccuracy(volume,2,0.922,5,3.615,10,8.118,50,46.478,100,95.291,200,192.453)"
    assert abs(corrected_volume(adjust, 50) - 53.5) < 0.2  # FluentControl logged 53.52 (2026-10-16)
    multi = {"CorrectionVolumeFormular": adjust, "ExcessVolumeFormular": "0.25*volume", "ConditioningVolumeFormular": "0"}
    plan = planned_aspiration(multi, 30, 3)
    assert plan["dispensed"] == 90 and plan["excess"] == 22.5 and 120 < plan["liquid_in_tip"] < 122  # 3 x 32.8 corrected + 22.5
    odd = planned_aspiration({"ExcessVolumeFormular": "if(volume>5,1,2)"}, 10, 1)
    assert odd["excess"] is None and not odd["formulas_understood"]
