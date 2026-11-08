from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import Settings


class Base(DeclarativeBase):
    pass


def create_database_engine(settings: Settings):
    # Construct lazily: imports and the liveness endpoint do not require a DB.
    url = settings.database_url.get_secret_value()
    connect_args = {}
    if url.startswith("postgresql"):
        options = make_url(url).query.get("options", "")
        connect_args = {"connect_timeout": 3, "options": options + " -c statement_timeout=2000 -c lock_timeout=1000"}
    return create_engine(url, pool_pre_ping=True, pool_timeout=2, connect_args=connect_args)


def create_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)
