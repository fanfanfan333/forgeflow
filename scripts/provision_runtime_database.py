"""Provision a least-privilege runtime role after migrations; keep admin DSN separate.

Run deliberately with the migration/admin DSN in POSTGRES_MIGRATION_URL.
Credentials are written only to the existing local .env, never printed.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
from dotenv import dotenv_values
from psycopg import sql

from forgeflow.config import get_settings


def main() -> None:
    settings = get_settings()
    env = Path(__file__).resolve().parents[1] / ".env"
    admin = (
        os.environ.get("POSTGRES_MIGRATION_URL")
        or dotenv_values(env).get("POSTGRES_MIGRATION_URL")
        or settings.postgres_sync_url
    )
    admin = admin.replace("postgresql+psycopg://", "postgresql://")
    from langgraph.checkpoint.postgres import PostgresSaver

    with PostgresSaver.from_conn_string(admin) as saver:
        saver.setup()
    role = "forgeflow_runtime"
    runtime = urlsplit(settings.postgres_sync_url.replace("postgresql+psycopg://", "postgresql://"))
    password = (
        (runtime.password or secrets.token_urlsafe(40))
        if runtime.username == role
        else secrets.token_urlsafe(40)
    )
    password = os.environ.get("POSTGRES_RUNTIME_PASSWORD") or password
    with psycopg.connect(admin) as conn:
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION"
                ).format(sql.Identifier(role), sql.Literal(password))
            )
        else:
            conn.execute(
                sql.SQL(
                    "ALTER ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION"
                ).format(sql.Identifier(role), sql.Literal(password))
            )
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
        conn.execute(
            sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
            ).format(sql.Identifier(role))
        )
        conn.execute(
            sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(
                sql.Identifier(role)
            )
        )
    parsed = urlsplit(admin)
    authority = f"{role}:{quote(password, safe='')}@{parsed.hostname}:{parsed.port or 5432}"
    dsn = urlunsplit(("postgresql", authority, parsed.path, parsed.query, parsed.fragment))
    env = Path(__file__).resolve().parents[1] / ".env"
    values = {
        "POSTGRES_URL": dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
        "POSTGRES_SYNC_URL": dsn.replace("postgresql://", "postgresql+psycopg://", 1),
        "POSTGRES_MIGRATION_URL": admin,
        "POSTGRES_RUNTIME_PASSWORD": password,
    }
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    lines = [line for line in lines if line.split("=", 1)[0] not in values]
    lines.extend(f"{key}={value}" for key, value in values.items())
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
