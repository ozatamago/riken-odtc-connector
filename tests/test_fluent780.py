"""Checks the assembly of the combined "Tecan Fluent 780" server. Connects to neither the devices nor the Fluent."""

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
        "RgaController", "LabwareController",
        "DoorController", "MethodController", "TemperatureController",
        "TrayController", "AbsorbanceReader", "FluentPlateHandling",
        "BioShakeController", "ShakingIncubatorController", "TemperaturePositionController", "Incubator1Controller",
        "RunController", "StatusController",
    ]
    assert len(set(names)) == len(names)


def test_one_backend_is_shared_by_every_feature():
    features = build_features(Fluent780ConnectorConfig())
    backends = {id(b) for f in features for b in backends_of(f)}
    assert len(backends) == 1
    assert all(backends_of(f) for f in features)


def test_second_operation_is_refused_and_says_what_is_running():
    backend = FluentOdtcBackend(Fluent780ConnectorConfig().fluent)  # dry_run by default

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
    # moving from the base position needs no commands to align the record
    assert [l for l, _ in t.move_steps(plate, "Regrip_Pos", 1)] == [f"RGA {plate} -> Regrip_Pos[1]"]
    t.record(plate, "Regrip_Pos", 1)
    # returning it in another run first aligns FluentControl's record with the actual position (Regrip_Pos[1])
    labels = [l for l, _ in t.move_steps(plate, "Nest7mm_Pos", 6)]
    assert labels == [
        f"RemoveLabware {plate} (bookkeeping only)",
        f"AddLabware {plate} at Regrip_Pos[1] (bookkeeping only)",
        f"RGA {plate} -> Nest7mm_Pos[6]",
    ]
    add_xml = t.move_steps(plate, "Nest7mm_Pos", 6)[1][1]
    assert "<LabwareType>96 Well Flat_Custom</LabwareType>" in add_xml and "<Location>Regrip_Pos</Location>" in add_xml
    # the position is also saved to the file and survives a restart
    assert LabwareTracker(PlateTransferConfig(positions_file=str(tmp_path / "pos.json"))).where(plate) == ("Regrip_Pos", 1)
    t.record(plate, "Nest7mm_Pos", 6)
    assert not t.moved_away(plate)


def test_rga_refuses_unregistered_labware(tmp_path):
    from riken_odtc_connector.rga_controller import LabwareTracker, PlateTransferConfig

    t = LabwareTracker(PlateTransferConfig(positions_file=str(tmp_path / "pos.json")))
    with pytest.raises(BackendError):
        t.move_steps("Some Plate[001]", "Regrip_Pos", 1)


def test_features_can_be_turned_into_sila_definitions():
    # the server does not start if the CDK cannot read the Command types
    # (2026-10-15: startup failed with from __future__ import annotations)
    from unitelabs.cdk import Connector

    config = Fluent780ConnectorConfig()
    app = Connector(config)
    for feature in build_features(config):
        app.register(feature)


class _FakePcFiles:
    def __init__(self):
        self.files = {}

    def read_file(self, path):
        return self.files.get(path)

    def write_file(self, path, text):
        self.files[path] = text


def test_positions_live_on_the_fluent_pc_and_dry_runs_do_not_change_them():
    from riken_odtc_connector.rga_controller import LabwareTracker, PlateTransferConfig

    pc = _FakePcFiles()
    plate = "96 Well Flat_Custom[001]"
    live = LabwareTracker(PlateTransferConfig(), pc)
    assert live.remote and live.where(plate) == ("Nest7mm_Pos", 6)
    live.record(plate, "Regrip_Pos", 1)
    path = PlateTransferConfig().positions_file
    assert '"Regrip_Pos"' in pc.files[path]
    # another connector process (e.g. after a restart) reads the same state
    assert LabwareTracker(PlateTransferConfig(), pc).where(plate) == ("Regrip_Pos", 1)
    dry = LabwareTracker(PlateTransferConfig(), pc, persist=False)
    dry.record(plate, "Nest7mm_Pos", 6)
    assert dry.where(plate) == ("Nest7mm_Pos", 6) and '"Regrip_Pos"' in pc.files[path]


def test_unreachable_fluent_pc_refuses_instead_of_guessing():
    from riken_odtc_connector.definitions import DefinitionsConfig, FluentPc
    from riken_odtc_connector.rga_controller import LabwareTracker, PlateTransferConfig

    t = LabwareTracker(PlateTransferConfig(), FluentPc(DefinitionsConfig()))  # offline in tests
    with pytest.raises(BackendError, match="Cannot read the labware positions"):
        t.where("96 Well Flat_Custom[001]")
