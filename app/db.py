from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import Settings


class Base(DeclarativeBase):
    pass


def create_database_engine(settings: Settings):
    # Construct lazily: imports and the liveness endpoint do not require a DB.
    return create_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)


def create_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)
