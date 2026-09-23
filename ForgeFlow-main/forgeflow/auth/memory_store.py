"""Offline credential store — the memory-profile counterpart of ``auth.users``.

Why this exists
---------------
``forgeflow/auth/users.py`` and ``forgeflow/auth/tokens.py`` are PostgreSQL-only
(asyncpg). In the offline profile (``STORAGE_BACKEND=memory``) the app starts
with ``app.state.pool = None``, so ``POST /auth/login`` answered **503** and the
front-end could not get past the login screen at all.

Design rule followed here (team-lead directive): **swap the storage backend, do
not branch the auth logic.** This module implements the *same* operations the
router already calls, and returns row objects that support ``row["column"]`` so
``api/routers/auth.py`` needs no ``if offline:`` fork — it simply receives a
different store.

Security posture
----------------
* Passwords are hashed with the **same** Argon2id helpers as the PG path
  (``auth.passwords``) — no weaker hash offline.
* Refresh tokens keep the same guarantees: stored only as SHA-256 hashes,
  rotating families, and **reuse detection** that burns the whole family.
* Available **only** when ``storage_backend == "memory"`` **and** the deployment
  is not production-shaped (``Settings.is_production()``) — see
  :func:`memory_auth_allowed`. A production deployment must never fall back to
  an in-process credential store.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from forgeflow.auth.demo_users import DEMO_USERS
from forgeflow.config import Settings, get_settings

logger = logging.getLogger(__name__)


class MemoryAuthDisabledError(RuntimeError):
    """Raised when the offline store is requested on a non-eligible deployment."""


@dataclass
class _Row:
    """A tiny stand-in for ``asyncpg.Record`` — mapping-style access.

    Exposing ``__getitem__`` is deliberate: it lets ``api/routers/auth.py``
    keep using ``user["username"]`` / ``user["id"]`` / ``user["role"]``
    unchanged, which is what makes this a *storage swap* rather than a
    second, parallel authentication path.
    """

    id: uuid.UUID
    username: str
    password_hash: str
    role: str
    disabled: bool = False
    mfa_enabled: bool = False
    mfa_secret: str | None = None
    external_subject: str | None = None
    workspace_id: uuid.UUID | None = None

    def __getitem__(self, key: str) -> object:
        try:
            return getattr(self, key)
        except AttributeError as exc:  # pragma: no cover — defensive
            raise KeyError(key) from exc

    def get(self, key: str, default: object = None) -> object:
        return getattr(self, key, default)

    def keys(self) -> list[str]:
        return list(self.__dataclass_fields__)


@dataclass
class _RefreshToken:
    id: uuid.UUID
    user_id: uuid.UUID
    family_id: uuid.UUID
    token_hash: str
    expires_at: datetime
    used_at: datetime | None = None
    revoked: bool = False


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_opaque() -> str:
    return secrets.token_urlsafe(48)


class MemoryAuthStore:
    """In-process credential + refresh-token store (offline profile only)."""

    def __init__(self) -> None:
        self._users: dict[str, _Row] = {}
        self._tokens: dict[str, _RefreshToken] = {}
        self.backend = "memory"

    # ------------------------------------------------------------------ #
    # Users
    # ------------------------------------------------------------------ #
    async def get_by_username(self, username: str) -> _Row | None:
        return self._users.get((username or "").strip())

    async def get_by_id(self, user_id: uuid.UUID) -> _Row | None:
        for row in self._users.values():
            if row.id == user_id:
                return row
        return None

    async def get_by_external_subject(self, subject: str) -> _Row | None:
        for row in self._users.values():
            if row.external_subject == subject:
                return row
        return None

    async def upsert_local_user(
        self,
        username: str,
        password_hash: str,
        role: str,
        workspace_id: uuid.UUID | None = None,
    ) -> _Row:
        existing = self._users.get(username)
        if existing is not None:
            existing.password_hash = password_hash
            existing.role = role
            existing.workspace_id = workspace_id
            return existing
        row = _Row(
            id=uuid.uuid4(),
            username=username,
            password_hash=password_hash,
            role=role,
            workspace_id=workspace_id,
        )
        self._users[username] = row
        return row

    async def provision_oidc_user(
        self, subject: str, username: str, role: str
    ) -> _Row:
        existing = await self.get_by_external_subject(subject)
        if existing is not None:
            return existing
        return await self.upsert_local_user(username, "", role)

    async def set_mfa_secret(self, user_id: uuid.UUID, secret: str) -> None:
        row = await self.get_by_id(user_id)
        if row is not None:
            row.mfa_secret = secret

    async def enable_mfa(self, user_id: uuid.UUID) -> None:
        row = await self.get_by_id(user_id)
        if row is not None:
            row.mfa_enabled = True

    async def is_member(self, user_id: str, workspace_id: str) -> bool:
        """Workspace claim check — same fail-closed posture as the PG path.

        The offline profile has no ``workspace_members`` table (migration 005 is
        never applied when ``STORAGE_BACKEND=memory``), which is exactly the
        "pre-migration" case where :func:`auth.membership.user_is_member` denies.
        So: a claim is honoured only when it matches the user's own default
        workspace; anything else is refused rather than silently allowed.
        """
        try:
            uuid.UUID(str(user_id))
            uuid.UUID(str(workspace_id))
        except (ValueError, TypeError):
            return False
        row = await self.get_by_id(uuid.UUID(str(user_id)))
        if row is None or row.disabled or row.workspace_id is None:
            return False
        return str(row.workspace_id) == str(workspace_id)

    # ------------------------------------------------------------------ #
    # Refresh tokens — same rotation + reuse-detection semantics as PG
    # ------------------------------------------------------------------ #
    async def issue_refresh_token(
        self, user_id: uuid.UUID, family_id: uuid.UUID | None = None
    ) -> str:
        token = _new_opaque()
        ttl_days = get_settings().refresh_token_ttl_days
        self._tokens[_hash(token)] = _RefreshToken(
            id=uuid.uuid4(),
            user_id=user_id,
            family_id=family_id or uuid.uuid4(),
            token_hash=_hash(token),
            expires_at=datetime.now(UTC) + timedelta(days=ttl_days),
        )
        return token

    async def revoke_family(self, family_id: uuid.UUID) -> None:
        for tok in self._tokens.values():
            if tok.family_id == family_id:
                tok.revoked = True

    async def revoke_by_token(self, token: str) -> None:
        tok = self._tokens.get(_hash(token))
        if tok is not None:
            await self.revoke_family(tok.family_id)

    async def rotate(self, presented: str) -> dict:
        from forgeflow.auth.tokens import RefreshError

        tok = self._tokens.get(_hash(presented))
        if tok is None:
            raise RefreshError("unknown refresh token")

        # Reuse detection: already used or revoked ⇒ burn the whole family.
        if tok.revoked or tok.used_at is not None:
            await self.revoke_family(tok.family_id)
            logger.warning(
                "Refresh-token reuse detected — revoked family %s", tok.family_id
            )
            raise RefreshError("refresh token reuse detected")

        if tok.expires_at < datetime.now(UTC):
            raise RefreshError("refresh token expired")

        user = await self.get_by_id(tok.user_id)
        if user is None or user.disabled:
            raise RefreshError("user disabled or missing")

        tok.used_at = datetime.now(UTC)
        new_token = await self.issue_refresh_token(user.id, family_id=tok.family_id)
        return {"user": user, "refresh_token": new_token}

    # ------------------------------------------------------------------ #
    # Test/maintenance helpers
    # ------------------------------------------------------------------ #
    def clear(self) -> None:
        """Drop all state (used by tests)."""
        self._users.clear()
        self._tokens.clear()


_STORE = MemoryAuthStore()


def get_memory_store() -> MemoryAuthStore:
    """Process-wide offline store (mirrors how the memory run-store works)."""
    return _STORE


def reset_memory_store() -> MemoryAuthStore:
    """Replace the process-wide store with an empty one (tests)."""
    global _STORE
    _STORE = MemoryAuthStore()
    return _STORE


def memory_auth_allowed(settings: Settings | None = None) -> bool:
    """True only for the offline, non-production profile.

    Deliberately strict: an in-process credential store must never become a
    production fallback — that would be an invisible downgrade of the auth
    posture (same fail-closed spirit as ``DEV_LOGIN_ENABLED`` in
    ``Settings.validate_runtime``).
    """
    settings = settings or get_settings()
    if settings.is_production():
        return False
    return str(settings.storage_backend).lower() == "memory"


async def seed_demo_users(password: str | None, workspace_id: str | None = None) -> int:
    """Seed ``DEMO_USERS`` with a single shared dev password (Argon2-hashed).

    ``workspace_id`` binds each seeded user to the default tenant so that a
    workspace claim at login can be verified (see :meth:`MemoryAuthStore.is_member`)
    instead of being refused as an unverifiable claim.
    """
    from forgeflow.auth import passwords

    if not password:
        logger.warning("DEV_LOGIN_PASSWORD unset — offline demo users not seeded")
        return 0
    password_hash = passwords.hash_password(password)
    ws: uuid.UUID | None = None
    if workspace_id:
        try:
            ws = uuid.UUID(str(workspace_id))
        except (ValueError, TypeError):
            logger.warning("default_tenant_id=%r is not a UUID — skipping workspace bind", workspace_id)
    store = get_memory_store()
    for username, role in DEMO_USERS.items():
        row = await store.upsert_local_user(username, password_hash, role, ws)
        row.workspace_id = ws
    logger.info(
        "Offline (memory) credential store ready — seeded %d demo users (workspace=%s)",
        len(DEMO_USERS),
        ws,
    )
    return len(DEMO_USERS)
