import pytest

from app import store


@pytest.fixture(autouse=True)
def temp_database(tmp_path, monkeypatch):
    """Every test gets its own empty SQLite file, so tests never touch data/kaskentra.db."""
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "test.db"))
