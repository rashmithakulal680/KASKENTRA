import pytest

from app import store


@pytest.fixture(autouse=True)
def temp_database(tmp_path, monkeypatch):
    """Every test gets its own empty SQLite file, so tests never touch data/kaskentra.db."""
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "test.db"))


@pytest.fixture(autouse=True)
def no_real_ai(monkeypatch):
    """Tests never call Groq: a key in the developer's .env is ignored unless a test sets one."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("KASKENTRA_AI", raising=False)
