"""Credential-store facade — one auth path, two storage backends.

``api/routers/auth.py`` depends on :func:`get_auth_store` instead of
``get_pool``. The router therefore runs the *same* login / refresh / logout
logic everywhere and only the backing store differs:

  * ``PgAuthStore``      — PostgreSQL (``auth.users`` + ``auth.tokens``)
  * ``MemoryAuthStore``  — in-process (``auth.memory_store``), offline profile

This is the project's established "Repository abstraction + dual
implementation" pattern applied to auth (the same shape as
``graph/checkpointer.py`` choosing ``AsyncPostgresSaver`` vs ``InMemorySaver``),
and it is what keeps ``auth.py`` free of ``if offline:`` branches.
"""

from __future__ import annotations

import logging
import uuid

import asyncpg
from fastapi import HTTPException, Request

from forgeflow.auth import memory_store, tokens
from forgeflow.auth import users as pg_users
from forgeflow.auth.membership import user_is_member
from forgeflow.auth.memory_store import MemoryAuthStore as _MemoryBackend
from forgeflow.config import get_settings

logger = logging.getLogger(__name__)


class PgAuthStore:
    """PostgreSQL-backed store — thin delegation to the existing helpers."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self.backend = "postgres"

    async def get_by_username(self, username: str):
        return await pg_users.get_by_username(self._pool, username)

    async def get_by_id(self, user_id: uuid.UUID):
        return await pg_users.get_by_id(self._pool, user_id)

    async def get_by_external_subject(self, subject: str):
        return await pg_users.get_by_external_subject(self._pool, subject)

    async def upsert_local_user(
        self, username: str, password_hash: str, role: str, workspace_id=None
    ):
        return await pg_users.upsert_local_user(
            self._pool, username, password_hash, role, workspace_id
        )

    async def provision_oidc_user(self, subject: str, username: str, role: str):
        return await pg_users.provision_oidc_user(self._pool, subject, username, role)

    async def set_mfa_secret(self, user_id: uuid.UUID, secret: str) -> None:
        return await pg_users.set_mfa_secret(self._pool, user_id, secret)

    async def enable_mfa(self, user_id: uuid.UUID) -> None:
        return await pg_users.enable_mfa(self._pool, user_id)

    async def is_member(self, user_id: str, workspace_id: str) -> bool:
        return await user_is_member(self._pool, user_id, workspace_id)

    async def issue_refresh_token(self, user_id: uuid.UUID, family_id=None) -> str:
        return await tokens.issue_refresh_token(self._pool, user_id, family_id)

    async def revoke_family(self, family_id: uuid.UUID) -> None:
        return await tokens.revoke_family(self._pool, family_id)

    async def revoke_by_token(self, token: str) -> None:
        return await tokens.revoke_by_token(self._pool, token)

    async def rotate(self, presented: str) -> dict:
        return await tokens.rotate(self._pool, presented)


class MemoryAuthAdapter:
    """Adapts the offline backend to the same method names as :class:`PgAuthStore`."""

    def __init__(self, backend: _MemoryBackend | None = None) -> None:
        self._backend = backend or memory_store.get_memory_store()
        self.backend = "memory"

    async def get_by_username(self, username: str):
        return await self._backend.get_by_username(username)

    async def get_by_id(self, user_id: uuid.UUID):
        return await self._backend.get_by_id(user_id)

    async def get_by_external_subject(self, subject: str):
        return await self._backend.get_by_external_subject(subject)

    async def upsert_local_user(
        self, username: str, password_hash: str, role: str, workspace_id=None
    ):
        return await self._backend.upsert_local_user(
            username, password_hash, role, workspace_id
        )

    async def provision_oidc_user(self, subject: str, username: str, role: str):
        return await self._backend.provision_oidc_user(subject, username, role)

    async def set_mfa_secret(self, user_id: uuid.UUID, secret: str) -> None:
        return await self._backend.set_mfa_secret(user_id, secret)

    async def enable_mfa(self, user_id: uuid.UUID) -> None:
        return await self._backend.enable_mfa(user_id)

    async def is_member(self, user_id: str, workspace_id: str) -> bool:
        return await self._backend.is_member(user_id, workspace_id)

    async def issue_refresh_token(self, user_id: uuid.UUID, family_id=None) -> str:
        return await self._backend.issue_refresh_token(user_id, family_id)

    async def revoke_family(self, family_id: uuid.UUID) -> None:
        return await self._backend.revoke_family(family_id)

    async def revoke_by_token(self, token: str) -> None:
        return await self._backend.revoke_by_token(token)

    async def rotate(self, presented: str) -> dict:
        return await self._backend.rotate(presented)


async def get_auth_store(request: Request):
    """Resolve this request's credential store.

    * PostgreSQL pool present  → ``PgAuthStore`` (unchanged production path).
    * No pool + offline profile → ``MemoryAuthAdapter`` (login works offline).
    * Neither                   → 503 (never silently degrade to a weaker store).
    """
    settings = get_settings()
    pool = getattr(request.app.state, "pool", None)

    if pool is not None:
        return PgAuthStore(pool)

    if memory_store.memory_auth_allowed(settings):
        return MemoryAuthAdapter()

    logger.warning(
        "Credential store unavailable (pool=None, memory auth not permitted for "
        "storage_backend=%s)",
        settings.storage_backend,
    )
    raise HTTPException(status_code=503, detail="Credential store not initialised")
