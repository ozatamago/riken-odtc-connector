import pytest


@pytest.fixture(autouse=True)
def _records_in_tmp(tmp_path, monkeypatch):
    """Run records and the definition mirror go to ~/.riken_fluent780; keep tests out of the real one."""
    monkeypatch.setenv("HOME", str(tmp_path))
