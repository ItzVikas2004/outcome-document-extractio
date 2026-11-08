import pytest
from pydantic import ValidationError

from app.config import Settings
from app.db import create_database_engine, create_session_factory


def test_settings_read_environment_and_hide_credentials(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-only-placeholder")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:test-password@localhost/db")
    settings = Settings(_env_file=None)
    assert settings.llm_api_key.get_secret_value() == "test-only-placeholder"
    assert "test-only-placeholder" not in repr(settings)
    assert "test-password" not in repr(settings)
    engine = create_database_engine(settings)
    try:
        assert engine.url.database == "db"
        assert create_session_factory(engine).kw["bind"] is engine
    finally:
        engine.dispose()


@pytest.mark.parametrize("interval", [0, -1])
def test_worker_interval_must_be_positive(interval):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, worker_poll_interval_seconds=interval)
