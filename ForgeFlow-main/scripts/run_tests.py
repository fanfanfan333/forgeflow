"""Run regression tests in a fresh database when an admin DSN is configured."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
from dotenv import dotenv_values
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import sql


def database_url(value: str, name: str) -> str:
    parsed = urlsplit(
        value.replace("postgresql+asyncpg://", "postgresql://").replace(
            "postgresql+psycopg://", "postgresql://"
        )
    )
    return urlunsplit(parsed._replace(path="/" + name))


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    values = dotenv_values(root / ".env")
    env = dict(os.environ)
    admin = env.get("POSTGRES_MIGRATION_URL") or values.get("POSTGRES_MIGRATION_URL")
    command = [sys.executable, "-m", "pytest", *(sys.argv[1:] or ["tests"])]
    if not admin:
        return subprocess.call(command, cwd=root, env=env)
    runtime = env.get("POSTGRES_URL") or values.get("POSTGRES_URL")
    if not runtime:
        raise RuntimeError("Provision the non-superuser runtime role before database tests")
    name = "forgeflow_audit_tests_" + uuid.uuid4().hex
    admin = database_url(admin, urlsplit(admin).path.lstrip("/"))
    with psycopg.connect(admin, autocommit=True) as control:
        control.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            migration = database_url(admin, name)
            env["SSRF_DNS_OVER_HTTPS"] = "false"
            env.update(
                POSTGRES_URL=database_url(runtime, name),
                POSTGRES_SYNC_URL=database_url(runtime, name),
                POSTGRES_MIGRATION_URL=migration,
                PYTHONUTF8="1",
            )
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=root, env=env, check=True
            )
            with PostgresSaver.from_conn_string(migration) as saver:
                saver.setup()
            role = urlsplit(database_url(runtime, name)).username
            if not role:
                raise RuntimeError("Runtime DSN must name its database role")
            with psycopg.connect(migration) as conn:
                permissions = conn.execute(
                    "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=%s", (role,)
                ).fetchone()
                if not permissions or any(permissions):
                    raise RuntimeError(
                        "Database tests require a NOSUPERUSER/NOBYPASSRLS runtime role"
                    )
                for grant in (
                    "GRANT USAGE ON SCHEMA public TO {}",
                    "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO {}",
                    "GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {}",
                ):
                    conn.execute(sql.SQL(grant).format(sql.Identifier(role)))
            return subprocess.call(command, cwd=root, env=env)
        finally:
            assert (
                name.startswith("forgeflow_audit_tests_")
                and len(name) == len("forgeflow_audit_tests_") + 32
            )
            control.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


if __name__ == "__main__":
    raise SystemExit(main())
