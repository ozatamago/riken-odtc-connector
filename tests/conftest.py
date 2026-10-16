import pytest


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    """Tests never reach the Fluent PC, and dry-run records go to a temporary home folder."""
    from riken_odtc_connector.definitions import DefinitionCheckFailed, FluentPc

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # what expanduser uses on Windows (the Fluent PC)

    def offline(*_a, **_k):
        raise DefinitionCheckFailed("offline (tests)")

    for name in ("index", "read", "read_baseline", "write_baseline", "store_record", "run_ps", "read_file", "write_file", "_ssh", "_scp"):
        monkeypatch.setattr(FluentPc, name, offline)

    from riken_odtc_connector.deck_camera import CameraFailed, DeckCameras

    def cameras_offline(*_a, **_k):
        raise CameraFailed("offline (tests)")

    for name in ("_ssh", "_ps", "_put", "_get", "_get_back"):
        monkeypatch.setattr(DeckCameras, name, cameras_offline)
