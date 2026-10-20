"""Alembic environment: URL from DATABASE_URL (dispatch.core.config), metadata from dispatch.store.schema."""

import sqlalchemy as sa
from alembic import context

from dispatch.core.config import load
from dispatch.store.schema import metadata

url = context.config.get_main_option("sqlalchemy.url") or load().database_url


def run_offline() -> None:
    context.configure(url=url, target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    engine = sa.create_engine(url)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=metadata, render_as_batch=conn.dialect.name == "sqlite")
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_offline() if context.is_offline_mode() else run_online()
