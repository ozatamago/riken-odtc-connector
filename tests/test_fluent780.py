"""1 つにまとめたサーバ「Tecan Fluent 780」の組み立ての確認。装置にも Fluent にもつながない。"""

import asyncio

import pytest

from riken_odtc_connector.fluent780 import Fluent780ConnectorConfig, build_features
from riken_odtc_connector.fluent_backend import BackendError, FluentOdtcBackend


def backends_of(feature):
    found = []
    for value in vars(feature).values():
        if isinstance(value, FluentOdtcBackend):
            found.append(value)
        elif hasattr(value, "backend") and isinstance(value.backend, FluentOdtcBackend):
            found.append(value.backend)
    return found


def test_all_features_are_there_with_unique_names():
    names = [type(f).__name__ for f in build_features(Fluent780ConnectorConfig())]
    assert names == [
        "RgaController",
        "DoorController", "MethodController", "TemperatureController",
        "TrayController", "AbsorbanceReader", "FluentPlateHandling",
        "BioShakeController", "ShakingIncubatorController", "TemperaturePositionController", "Incubator1Controller",
        "StatusController",
    ]
    assert len(set(names)) == len(names)


def test_one_backend_is_shared_by_every_feature():
    features = build_features(Fluent780ConnectorConfig())
    backends = {id(b) for f in features for b in backends_of(f)}
    assert len(backends) == 1
    assert all(backends_of(f) for f in features)


def test_second_operation_is_refused_and_says_what_is_running():
    backend = FluentOdtcBackend(Fluent780ConnectorConfig().fluent)  # 既定は dry_run

    async def scenario():
        backend.config.dry_run_seconds = 0.5
        first = asyncio.create_task(backend.run_macro("SiLA-ODTC_OpenDoor"))
        await asyncio.sleep(0.1)
        with pytest.raises(BackendError, match="ODTC: SiLA-ODTC_OpenDoor"):
            await backend.run_steps([("Magellan_Open", "<ScriptGroup/>")], what="Infinite: Magellan_Open")
        await first
        assert backend.current_operation == ""

    asyncio.run(scenario())


def test_rga_tracker_resyncs_memory_after_a_move(tmp_path):
    from riken_odtc_connector.rga_controller import LabwareTracker, PlateTransferConfig

    t = LabwareTracker(PlateTransferConfig(positions_file=str(tmp_path / "pos.json")))
    plate = "96 Well Flat_Custom[001]"
    assert t.where(plate) == ("Nest7mm_Pos", 6) and not t.moved_away(plate)
    # 元の位置から運ぶときは、記憶を合わせる命令は要らない
    assert [l for l, _ in t.move_steps(plate, "Regrip_Pos", 1)] == [f"RGA {plate} -> Regrip_Pos[1]"]
    t.record(plate, "Regrip_Pos", 1)
    # 別のランで戻すときは、先に FluentControl の記憶を実際の位置（Regrip_Pos[1]）に合わせる
    labels = [l for l, _ in t.move_steps(plate, "Nest7mm_Pos", 6)]
    assert labels == [
        f"RemoveLabware {plate}（記憶のみ）",
        f"AddLabware {plate} at Regrip_Pos[1]（記憶のみ）",
        f"RGA {plate} -> Nest7mm_Pos[6]",
    ]
    add_xml = t.move_steps(plate, "Nest7mm_Pos", 6)[1][1]
    assert "<LabwareType>96 Well Flat_Custom</LabwareType>" in add_xml and "<Location>Regrip_Pos</Location>" in add_xml
    # 位置はファイルにも残り、起動し直しても覚えている
    assert LabwareTracker(PlateTransferConfig(positions_file=str(tmp_path / "pos.json"))).where(plate) == ("Regrip_Pos", 1)
    t.record(plate, "Nest7mm_Pos", 6)
    assert not t.moved_away(plate)


def test_rga_refuses_unregistered_labware(tmp_path):
    from riken_odtc_connector.rga_controller import LabwareTracker, PlateTransferConfig

    t = LabwareTracker(PlateTransferConfig(positions_file=str(tmp_path / "pos.json")))
    with pytest.raises(BackendError):
        t.move_steps("Some Plate[001]", "Regrip_Pos", 1)


def test_features_can_be_turned_into_sila_definitions():
    # CDK が Command の型を読めないとサーバが起動しない（2026-10-15: from __future__ import annotations で起動失敗）
    from unitelabs.cdk import Connector

    config = Fluent780ConnectorConfig()
    app = Connector(config)
    for feature in build_features(config):
        app.register(feature)
