"""Hackathon demo: one command pipettes coloured water by column, measures at three wavelengths and returns."""

import asyncio

import pytest

from riken_odtc_connector.colormix_demo import DemoFailed, pipetting_steps, plan_wells, summarize
from riken_odtc_connector.infinite.asc import AscResult

COLUMN_1 = [f"{r}1" for r in "ABCDEFGH"]


def test_one_column_is_four_rounds_of_eight_channels():
    planned = plan_wells(COLUMN_1, [0, 10, 20, 30, 40, 50, 60, 70], [5] * 8, [0] * 8, 150)
    steps = pipetting_steps(planned)
    labels = [label for label, _ in steps]
    # water and red into all 8 wells (red 0 in A1 leaves channel 1 out), yellow into all, blue nowhere
    assert len(steps) == 12
    assert labels[0] == "GetTips FCA, 200ul SBS ch[1, 2, 3, 4, 5, 6, 7, 8] for water column 1 (A1,B1,C1,D1,E1,F1,G1,H1)"
    assert labels[1] == "Aspirate 150/150/150/150/150/150/150/150 ul water from 4 Well SBS Trough[001] A1"
    assert labels[4].startswith("GetTips FCA, 200ul SBS ch[2, 3, 4, 5, 6, 7, 8] for red")
    assert "Trough[001] A2" in labels[5] and "Trough[001] A3" in labels[9]
    asp = steps[5][1]
    assert "<SelectedWellsString>7 * A2</SelectedWellsString>" in asp  # 7 tips into the red lane
    dis = steps[6][1]
    assert "<SelectedWellsString>B1 - H1</SelectedWellsString>" in dis


def test_wells_are_checked():
    with pytest.raises(DemoFailed, match="one red, one yellow and one blue"):
        plan_wells(["A1"], [1, 2], [0], [0], 150)
    with pytest.raises(DemoFailed, match="more than 300"):
        plan_wells(["A1"], [100], [0], [100], 150)
    with pytest.raises(DemoFailed, match="only once"):
        plan_wells(["A1", "a1"], [0, 0], [0, 0], [0, 0], 150)
    with pytest.raises(Exception, match="outside"):
        plan_wells(["I1"], [0], [0], [0], 150)


def test_summary_line_per_well():
    planned = plan_wells(["A1", "B1"], [0, 20], [0, 0], [0, 0], 150)
    asc = AscResult(labels=["Label1", "Label2", "Label3"],
                    values=[{"A1": 0.041, "B1": 0.9}, {"A1": 0.04, "B1": 0.5}, {"A1": 0.039, "B1": 0.05}], metadata=[])
    assert summarize(planned, asc, asc.labels) == [
        "A1: 430 nm 0.0410, 520 nm 0.0400, 630 nm 0.0390 (water 150 ul)",
        "B1: 430 nm 0.9000, 520 nm 0.5000, 630 nm 0.0500 (water 150, red 20 ul)"]


def test_the_command_in_a_dry_run(monkeypatch):
    from test_liquid_handling import FakePc, _S, vx

    from riken_odtc_connector.fluent780 import Fluent780ConnectorConfig, build_features
    from riken_odtc_connector.infinite.reader import FluentInfiniteReader

    f = {type(x).__name__: x for x in build_features(Fluent780ConnectorConfig())}  # dry run
    demo, backend = f["ColorMixDemo"], f["RunController"].backend

    class Pc(FakePc):
        def index(self):
            idx = super().index()
            return idx

    demo.liquid.pc = Pc()
    monkeypatch.setattr(demo.liquid, "_labware_type", lambda name: "96 Well Flat_Custom" if "Flat" in name
                        else "4 Well SBS Trough")

    async def methods(self):
        return ["OFML_ColorMix3.mth"]

    monkeypatch.setattr(FluentInfiniteReader, "list_methods", methods)

    async def scenario():
        lines, result, steps = await demo.mix_colors_and_measure(COLUMN_1, [0] * 8, [0] * 8, [20] * 8, 150, status=_S())
        assert lines[0] == "A1: not measured (dry run) (water 150, blue 20 ul)"
        assert result.method == "OFML_ColorMix3.mth" and result.result_file == ""
        assert steps[:4][0].startswith("GetTips") and "Magellan_Measure OFML_ColorMix3.mth" in steps
        assert steps[-2] == "RGA 96 Well Flat_Custom[001] -> base"
        assert not backend.run_open  # the run it opened is closed again
        with pytest.raises(DemoFailed, match="more than 300"):
            await demo.mix_colors_and_measure(["A1"], [200], [0], [0], 150, status=_S())

    asyncio.run(scenario())
