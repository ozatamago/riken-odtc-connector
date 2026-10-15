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
        "DoorController", "MethodController", "TemperatureController",
        "TrayController", "AbsorbanceReader", "FluentPlateHandling",
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
