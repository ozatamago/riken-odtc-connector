"""Infinite 200 PRO（Fluent 経由）の XML と ASC の読み取りの確認。装置にも Fluent にもつながない。

fixtures/ の XML は、実機で送ったファイルそのもの（qpcr_water_demo.py の probe_out から写した）。
ASC は 2026-08 に実機で測った結果ファイル。
"""

import math
import pathlib

import pytest

from riken_odtc_connector.fluent_backend import BackendError
from riken_odtc_connector.infinite.asc import parse_asc
from riken_odtc_connector.infinite.reader import TEMP_LABEL, FluentInfiniteReader, InfiniteConfig
from riken_odtc_connector.infinite.xml import add_labware_xml, magellan_xml, remove_labware_xml, rga_xml

FIX = pathlib.Path(__file__).parent / "fixtures"


def read(name):
    return (FIX / name).read_text(encoding="utf-8")


def test_magellan_open_matches_device():
    assert magellan_xml("Magellan_Open") == read("magellan_open_20261009.xml")


def test_magellan_measure_matches_device():
    reader = FluentInfiniteReader(backend=None, config=InfiniteConfig())
    [(_, xml)] = reader.measure_steps("OFML_ColorMix3.mth", "qpcr_water_measure-pcr_261009_175940")
    assert xml == read("magellan_measure_20261009.xml")


def test_rga_matches_device():
    xml = rga_xml("96 Well Skirted PCR_Custom[001]", "ODTC_96_Pos", "1")
    assert xml == read("rga_plate_to_odtc_20261009.xml")


def test_measure_refuses_angle_brackets_and_unknown_macros():
    with pytest.raises(BackendError):
        magellan_xml("Magellan_Measure", r"C:\x\<YYMMDD-NNN>,Abs600.mth,0,0,0")
    with pytest.raises(BackendError):
        magellan_xml("Magellan_Shake")
    with pytest.raises(BackendError):
        magellan_xml("Magellan_Measure")


def test_load_measure_unload_is_one_run_in_the_proven_order():
    reader = FluentInfiniteReader(backend=None, config=InfiniteConfig())
    steps = reader.load_measure_unload_steps("96 Well Flat_Custom", "Nest7mm_Pos", 7, "OFML_Abs405.mth", "riken_x")
    assert [label for label, _ in steps] == [
        "AddLabware 96 Well Flat_Custom at Nest7mm_Pos[7]",
        "Magellan_Open",
        "RGA -> Regrip_Pos[1]",
        "RGA -> InfiniteM200_New[1]",
        "Magellan_Close",
        "Magellan_Measure OFML_Abs405.mth",
        "Magellan_Open",
        "RGA -> Regrip_Pos[1]",
        "RGA -> Nest7mm_Pos[7]",
        "Magellan_Close",
        f"RemoveLabware {TEMP_LABEL}",
    ]
    assert all(xml.startswith("<ScriptGroup>") for _, xml in steps)


def test_labware_statements():
    add = add_labware_xml("96 Well Flat_Custom", TEMP_LABEL, "Nest7mm_Pos", 7)
    assert "<LabwareLable>SiLA_ReaderPlate[001]</LabwareLable>" in add and "<Position>7</Position>" in add
    assert "<LabwareName>SiLA_ReaderPlate[001]</LabwareName>" in remove_labware_xml(TEMP_LABEL)


def test_asc_grid_format_three_labels():
    r = parse_asc((FIX / "asc_colormix3_20260818.asc").read_text(encoding="latin-1"))
    assert r.labels == ["Label1", "Label2", "Label3"]
    assert all(len(g) == 96 for g in r.values)
    assert r.metadata[1] == "OFML_ColorMix3.mth"


def test_asc_list_format_is_row_major():
    # 2026-08-13: A4-D4 に黄色インク 10/20/50/100 uL。記録の OD 0.0404/0.0422/0.0425/0.0425 と一致するのは行順
    r = parse_asc((FIX / "asc_list_format.asc").read_text(encoding="latin-1"))
    g = r.values[0]
    assert [g["A4"], g["B4"], g["C4"], g["D4"]] == [0.0404, 0.0422, 0.0425, 0.0425]
    assert r.metadata[1] == "Abs600.mth"
    assert not any(math.isnan(v) for v in g.values())
