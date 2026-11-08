from alembic import context

from app.config import Settings
from app.db import Base, create_database_engine
from app.logging import configure_logging
from app import models  # Register model metadata for autogeneration.

settings = Settings()
configure_logging(settings.log_level)
target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(
        url=settings.database_url.get_secret_value(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    engine = create_database_engine(settings)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
