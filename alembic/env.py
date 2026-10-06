"""Alembic environment — reads POSTGRES_SYNC_URL from environment."""

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The DSN MUST come from the environment. The alembic.ini default
# (`...@localhost:5432/forgeflow`) is the *portable* default (docker-compose.yml
# publishes :5432, matching .env.example), but this machine's dev database runs
# on :5433 (docker-compose.override.yml, .env, tests/conftest.py). A silent fall
# back to the ini value therefore connects to the WRONG PostgreSQL — or a
# stale/foreign one — and either hangs or migrates the wrong schema. Failing
# fast makes that mistake impossible and self-explaining.
sync_url = os.environ.get("POSTGRES_SYNC_URL") or os.environ.get("SQLALCHEMY_URL")
if not sync_url:
    raise RuntimeError(
        "POSTGRES_SYNC_URL (or SQLALCHEMY_URL) is not set. Alembic refuses to "
        "silently fall back to the alembic.ini default, which may point at the "
        "wrong PostgreSQL (the dev database runs on :5433, the portable default "
        "is :5432). Export it, e.g. "
        "POSTGRES_SYNC_URL=postgresql+psycopg://forgeflow:forgeflow@localhost:5433/forgeflow"
    )
config.set_main_option("sqlalchemy.url", sync_url)

target_metadata = None


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
