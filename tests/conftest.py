import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.db import Base, create_session_factory
from app.main import app


@pytest.fixture(scope="session")
def postgres_url():
    """Use a unique schema, never existing application tables, for integration tests."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        yield None
        return
    namespace = "test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{namespace}"'))
    isolated = make_url(url).update_query_dict({"options": f"-csearch_path={namespace}"}).render_as_string(hide_password=False)
    old = os.environ.get("DATABASE_URL")
    try:
        os.environ["DATABASE_URL"] = isolated
        command.upgrade(Config("alembic.ini"), "head")
        yield isolated
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{namespace}" CASCADE'))
        admin.dispose()


@pytest.fixture
def client(tmp_path, monkeypatch, postgres_url):
    url = postgres_url or f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    engine = create_engine(url)
    if postgres_url:
        with engine.begin() as connection:
            connection.execute(text("TRUNCATE TABLE jobs"))
    else:
        Base.metadata.create_all(engine)
    try:
        with TestClient(app) as test_client:
            test_client.session_factory = create_session_factory(engine)
            test_client.upload_dir = tmp_path / "uploads"
            yield test_client
    finally:
        engine.dispose()
