import pytest


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    """Tests never reach the Fluent PC, and dry-run records go to a temporary home folder."""
    from riken_odtc_connector.definitions import DefinitionCheckFailed, FluentPc

    monkeypatch.setenv("HOME", str(tmp_path))

    def offline(*_a, **_k):
        raise DefinitionCheckFailed("offline (tests)")

    for name in ("index", "read", "read_baseline", "write_baseline", "store_record", "run_ps"):
        monkeypatch.setattr(FluentPc, name, offline)
