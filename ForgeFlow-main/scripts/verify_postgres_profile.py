"""Run the PostgreSQL test profile in an isolated schema, preserving user rows.

Reuses the existing database and its types; never truncates the public schema.
The isolated schema is retained as verification evidence for inspection.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import asyncpg
import psycopg
from psycopg import sql

from forgeflow.config import get_settings


def main():
    settings = get_settings()
    schema = "agentflow2_qa_" + uuid4().hex[:10]
    base = settings.postgres_sync_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(base) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        tables = connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"
        ).fetchall()
        for (table,) in tables:
            connection.execute(
                sql.SQL("CREATE TABLE {}.{} (LIKE public.{} INCLUDING ALL)").format(
                    sql.Identifier(schema), sql.Identifier(table), sql.Identifier(table)
                )
            )
        connection.execute(
            sql.SQL("INSERT INTO {}.alembic_version SELECT * FROM public.alembic_version").format(
                sql.Identifier(schema)
            )
        )
        connection.execute(
            sql.SQL("SET LOCAL search_path TO {},public").format(sql.Identifier(schema))
        )
        foreign_keys = connection.execute(
            "SELECT c.relname, con.conname, pg_get_constraintdef(con.oid) FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND con.contype='f'"
        ).fetchall()
        for table, name, definition in foreign_keys:
            connection.execute(
                sql.SQL("ALTER TABLE {}.{} ADD CONSTRAINT {} {}").format(
                    sql.Identifier(schema),
                    sql.Identifier(table),
                    sql.Identifier(name),
                    sql.SQL(definition.replace("public.", schema + ".")),
                )
            )
    options = "?options=" + quote("-c search_path=" + schema + ",public", safe="")
    async_dsn = settings.postgres_url.replace("postgresql+asyncpg://", "postgresql://") + options

    async def probe():
        connection = await asyncpg.connect(async_dsn)
        actual = await connection.fetchval("SELECT current_schema()")
        await connection.close()
        assert actual == schema

    asyncio.run(probe())
    print("Isolated PostgreSQL schema: " + schema, flush=True)
    env = dict(os.environ)
    env.update(
        STORAGE_BACKEND="postgres",
        LLM_PROVIDER="mock",
        EMBEDDING_PROVIDER="mock",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        POSTGRES_URL=async_dsn,
        POSTGRES_SYNC_URL=settings.postgres_sync_url + options,
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "pytest_asyncio.plugin",
            "tests/unit",
            "tests/integration",
            "-q",
            "--junitxml=_audit/agentflow2-postgres.xml",
        ],
        env=env,
        check=False,
    )
    Path("_audit/postgres-profile-schema.txt").write_text(schema, encoding="utf-8")
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
