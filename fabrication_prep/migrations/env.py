"""Alembic environment: owner connection from DATABASE_URL, migrations are plain SQL."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from fabrication_prep.settings import get_settings


def _sqlalchemy_url(url: str) -> str:
    # Explicit driver: SQLAlchemy 2.1 changed the default for plain postgresql:// URLs.
    for prefix in ("postgres://", "postgresql://", "postgresql+psycopg://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    raise RuntimeError("DATABASE_URL must be a postgres:// or postgresql:// URL")


def run_migrations_online() -> None:
    url = context.config.attributes.get("database_url") or get_settings().database_url
    if not url:
        raise RuntimeError("DATABASE_URL (the schema owner's connection) is not set")
    engine = create_engine(_sqlalchemy_url(url), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, transactional_ddl=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline (SQL script) mode is not supported; run against a database")
run_migrations_online()
