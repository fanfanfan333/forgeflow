"""INC46 T31 — 租户 / 用户记忆与偏好（显式、可见、可编辑记忆）。

A CLAUDE.md-style memory for the document-editing agent: the tenant (and each
user) can state durable preferences — writing style, a glossary, banned words,
document conventions — and those are injected into the next task's context.

Three invariants from the task book (§T31) are enforced **here**, not by
convention:

1. **来源受限（红线 14）** — a preference can only become *effective* when its
   source is ``explicit`` (the user stated it / edited it in settings) or
   ``confirmed_suggestion`` (the user confirmed a suggestion). Anything derived
   *from uploaded document content* is written with ``source="suggestion"`` and
   is therefore **inactive until confirmed** — document text is data, never an
   instruction, so it can never silently become a durable preference.
2. **两级 + 覆盖可审计** — tenant-level and user-level preferences both exist;
   a user-level preference **overrides** the tenant-level one on the same key and
   the override is recorded as a conflict (not silently dropped).
3. **租户 fail-closed（红线 5）** — every read/write takes the owning tenant
   first; an unresolved tenant reads the **empty** set and writes nothing.

The record is stored as a *Rule asset* (T02) shape via :meth:`Preference.to_rule_asset`
— ``scope="memory"``, carrying ``source`` and ``created_by`` — so the T06 Rules
page can list / delete it without a second vocabulary.

Two backends mirror the rest of the hub (see ``forgeflow.privacy.audit`` and
``forgeflow.hitl.pending``): an in-process dict store for the offline profile
and a ``psycopg`` sync store for the real ``memory_preferences`` table
(migration ``029``).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from forgeflow.repositories.base import new_id, utcnow

logger = logging.getLogger(__name__)

__all__ = [
    # scope / source / kind vocabulary
    "SCOPE_TENANT",
    "SCOPE_USER",
    "SCOPES",
    "SOURCE_EXPLICIT",
    "SOURCE_CONFIRMED_SUGGESTION",
    "SOURCE_SUGGESTION",
    "ACTIVE_SOURCES",
    "ALLOWED_SOURCES",
    "KIND_STYLE",
    "KIND_GLOSSARY",
    "KIND_BANNED_TERM",
    "KIND_DOC_CONVENTION",
    "PREFERENCE_KINDS",
    "RULE_SCOPE_MEMORY",
    # value objects
    "Preference",
    "ResolvedPreferences",
    # errors
    "PreferenceError",
    "PreferenceSourceError",
    # store
    "PreferenceStore",
    "InMemoryPreferenceStore",
    "PostgresPreferenceStore",
    "get_preference_store",
    "set_preference_store",
    "reset_preference_store",
    # high-level API
    "add_preference",
    "confirm_suggestion",
    "list_preferences",
    "delete_preference",
    "resolve_preferences",
    "document_derived_preference",
]

# --------------------------------------------------------------------------- #
# Vocabulary                                                                   #
# --------------------------------------------------------------------------- #
SCOPE_TENANT = "tenant"
SCOPE_USER = "user"
SCOPES: tuple[str, ...] = (SCOPE_TENANT, SCOPE_USER)

#: 用户明确陈述 / 在设置页编辑 / 用户确认过的建议 —— 只有这两种来源**生效**。
SOURCE_EXPLICIT = "explicit"
SOURCE_CONFIRMED_SUGGESTION = "confirmed_suggestion"
#: 推断出的（例如从上传文档内容派生）——只作建议，确认前不生效（红线 14）。
SOURCE_SUGGESTION = "suggestion"

#: Sources whose preference is effective immediately.
ACTIVE_SOURCES: frozenset[str] = frozenset({SOURCE_EXPLICIT, SOURCE_CONFIRMED_SUGGESTION})
#: Everything the store will accept (the inactive ``suggestion`` is stored but
#: never injected; an unknown source is rejected outright).
ALLOWED_SOURCES: frozenset[str] = ACTIVE_SOURCES | {SOURCE_SUGGESTION}

KIND_STYLE = "style"
KIND_GLOSSARY = "glossary"
KIND_BANNED_TERM = "banned_term"
KIND_DOC_CONVENTION = "doc_convention"
PREFERENCE_KINDS: tuple[str, ...] = (
    KIND_STYLE,
    KIND_GLOSSARY,
    KIND_BANNED_TERM,
    KIND_DOC_CONVENTION,
)

#: The T02 Rule-asset scope a memory preference materialises as.
RULE_SCOPE_MEMORY = "memory"

#: Human labels used when rendering a preference as an instruction line.
_KIND_LABELS: dict[str, str] = {
    KIND_STYLE: "写作风格",
    KIND_GLOSSARY: "术语表",
    KIND_BANNED_TERM: "禁用词",
    KIND_DOC_CONVENTION: "文档约定",
}


class PreferenceError(Exception):
    """Base class for preference-store failures."""


class PreferenceSourceError(PreferenceError):
    """A write was attempted with an unknown / disallowed source (fail-closed).

    This is the 红线 14 gate: an unrecognised source cannot become a preference,
    and a document-derived ``suggestion`` is stored inactive rather than active.
    """


# --------------------------------------------------------------------------- #
# Value objects                                                                #
# --------------------------------------------------------------------------- #
@dataclass
class Preference:
    """One durable memory preference (tenant- or user-scoped).

    ``key`` is the override key: two preferences with the same ``(kind, key)``
    compete, and a ``user`` one overrides a ``tenant`` one. For a glossary entry
    the key is the term; for the other kinds it is empty (the kind itself is the
    key), so "user style overrides tenant style".
    """

    tenant_id: str
    scope: str
    kind: str
    value: str
    source: str = SOURCE_EXPLICIT
    created_by: str = ""
    user_id: str | None = None
    key: str = ""
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)

    @property
    def active(self) -> bool:
        """Whether this preference is effective (``explicit`` / confirmed only)."""
        return self.source in ACTIVE_SOURCES

    @property
    def override_key(self) -> tuple[str, str]:
        """The (kind, key) pair user-level preferences override tenant-level on."""
        return (self.kind, self.key or "")

    def as_instruction(self) -> str:
        """Render as a one-line instruction for the context bundle."""
        label = _KIND_LABELS.get(self.kind, self.kind)
        if self.kind == KIND_GLOSSARY and self.key:
            return f"{label}：{self.key} = {self.value}"
        return f"{label}：{self.value}"

    def to_rule_asset(self) -> dict[str, Any]:
        """Materialise as a T02 Rule asset (``scope="memory"``, with provenance)."""
        return {
            "scope": RULE_SCOPE_MEMORY,
            "tenant_id": self.tenant_id,
            "rule_kind": "must",
            "rule_text": self.as_instruction(),
            "source": self.source,
            "created_by": self.created_by,
            "preference_id": self.id,
            "preference_kind": self.kind,
            "preference_scope": self.scope,
            "active": self.active,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "scope": self.scope,
            "user_id": self.user_id,
            "kind": self.kind,
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "created_by": self.created_by,
            "active": self.active,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class ResolvedPreferences:
    """The effective preference set for one (tenant, user) pair."""

    tenant_items: list[Preference] = field(default_factory=list)
    user_items: list[Preference] = field(default_factory=list)
    items: list[Preference] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    def instruction_lines(self) -> list[str]:
        return [p.as_instruction() for p in self.items]

    def rule_assets(self) -> list[dict[str, Any]]:
        return [p.to_rule_asset() for p in self.items]

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [p.to_dict() for p in self.items],
            "conflicts": list(self.conflicts),
            "count": len(self.items),
        }


# --------------------------------------------------------------------------- #
# Store — shared logic                                                          #
# --------------------------------------------------------------------------- #
class PreferenceStore:
    """Tenant-scoped store for :class:`Preference` rows (both backends share it)."""

    # --- storage primitives (per backend) ----------------------------------- #
    def _insert(self, row: dict[str, Any]) -> None:
        raise NotImplementedError

    def _scan(self, tenant: str, *, scope: str | None, user_id: str | None) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _delete(self, tenant: str, pref_id: str) -> bool:
        raise NotImplementedError

    def _update_source(self, tenant: str, pref_id: str, source: str) -> bool:
        raise NotImplementedError

    # --- writes ------------------------------------------------------------- #
    def record(self, pref: Preference) -> Preference:
        """Persist one preference.

        Fail-closed (红线 5 / 14): an unresolved tenant never writes, and an
        unknown ``source`` is rejected rather than defaulted.
        """
        if not pref.tenant_id:
            raise ValueError("tenant required to record a preference (fail closed)")
        if pref.source not in ALLOWED_SOURCES:
            raise PreferenceSourceError(
                f"unknown preference source {pref.source!r}; "
                f"allowed: {sorted(ALLOWED_SOURCES)}"
            )
        if pref.scope not in SCOPES:
            raise ValueError(f"unknown preference scope {pref.scope!r}; expected {SCOPES}")
        self._insert(
            {
                "id": pref.id,
                "tenant_id": str(pref.tenant_id),
                "scope": pref.scope,
                "user_id": pref.user_id,
                "kind": pref.kind,
                "pref_key": pref.key,
                "value": pref.value,
                "source": pref.source,
                "created_by": pref.created_by,
                "created_at": pref.created_at or utcnow(),
            }
        )
        return pref

    def list(
        self,
        tenant: str | None,
        *,
        scope: str | None = None,
        user_id: str | None = None,
        include_inactive: bool = True,
    ) -> list[Preference]:
        """List a tenant's preferences (newest first). Falsy tenant ⇒ ``[]``."""
        if not tenant:
            return []
        rows = [self._row_to_pref(r) for r in self._scan(tenant, scope=scope, user_id=user_id)]
        if not include_inactive:
            rows = [p for p in rows if p.active]
        return rows

    def delete(self, tenant: str | None, pref_id: str) -> bool:
        """Delete one preference. Falsy tenant deletes nothing (``False``)."""
        if not tenant:
            return False
        return self._delete(tenant, pref_id)

    def confirm(self, tenant: str | None, pref_id: str) -> bool:
        """Flip a stored ``suggestion`` to ``confirmed_suggestion`` (activate it)."""
        if not tenant:
            return False
        return self._update_source(tenant, pref_id, SOURCE_CONFIRMED_SUGGESTION)

    @staticmethod
    def _row_to_pref(row: dict[str, Any]) -> Preference:
        created = row.get("created_at")
        return Preference(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            scope=str(row.get("scope") or SCOPE_TENANT),
            user_id=(str(row["user_id"]) if row.get("user_id") else None),
            kind=str(row.get("kind") or KIND_STYLE),
            key=str(row.get("pref_key") or ""),
            value=str(row.get("value") or ""),
            source=str(row.get("source") or SOURCE_EXPLICIT),
            created_by=str(row.get("created_by") or ""),
            created_at=created if isinstance(created, datetime) else utcnow(),
        )


# --------------------------------------------------------------------------- #
# In-memory backend                                                            #
# --------------------------------------------------------------------------- #
class InMemoryPreferenceStore(PreferenceStore):
    """Process-local dict store (offline profile; a restart drops it)."""

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []

    def clear(self) -> None:
        self._rows.clear()

    def _insert(self, row: dict[str, Any]) -> None:
        self._rows.append(dict(row))

    def _scan(self, tenant: str, *, scope: str | None, user_id: str | None) -> list[dict[str, Any]]:
        rows = [dict(r) for r in self._rows if r.get("tenant_id") == tenant]
        if scope is not None:
            rows = [r for r in rows if r.get("scope") == scope]
        if user_id is not None:
            rows = [r for r in rows if r.get("user_id") == user_id]
        rows.sort(key=lambda r: r.get("created_at") or utcnow(), reverse=True)
        return rows

    def _delete(self, tenant: str, pref_id: str) -> bool:
        before = len(self._rows)
        self._rows = [
            r for r in self._rows if not (r.get("tenant_id") == tenant and str(r.get("id")) == pref_id)
        ]
        return len(self._rows) < before

    def _update_source(self, tenant: str, pref_id: str, source: str) -> bool:
        for row in self._rows:
            if row.get("tenant_id") == tenant and str(row.get("id")) == pref_id:
                row["source"] = source
                return True
        return False


# --------------------------------------------------------------------------- #
# PostgreSQL backend                                                           #
# --------------------------------------------------------------------------- #
def _normalise_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+psycopg://", "postgresql://")


class PostgresPreferenceStore(PreferenceStore):
    """The real ``memory_preferences`` table (migration ``029``) via ``psycopg``."""

    def __init__(self, dsn: str) -> None:
        self._dsn = _normalise_dsn(dsn)

    def _connect(self):  # noqa: ANN202 — psycopg connection, imported lazily
        import psycopg

        return psycopg.connect(self._dsn)

    def _insert(self, row: dict[str, Any]) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memory_preferences
                    (id, tenant_id, scope, user_id, kind, pref_key, value, source,
                     created_by, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(row["id"]),
                    str(row["tenant_id"]),
                    str(row["scope"]),
                    row.get("user_id"),
                    str(row["kind"]),
                    str(row.get("pref_key") or ""),
                    str(row["value"]),
                    str(row["source"]),
                    str(row.get("created_by") or ""),
                    row.get("created_at") or utcnow(),
                ),
            )
            conn.commit()

    def _scan(self, tenant: str, *, scope: str | None, user_id: str | None) -> list[dict[str, Any]]:
        sql = (
            "SELECT id, tenant_id, scope, user_id, kind, pref_key, value, source, "
            "created_by, created_at FROM memory_preferences WHERE tenant_id = %s"
        )
        params: list[Any] = [tenant]
        if scope is not None:
            sql += " AND scope = %s"
            params.append(scope)
        if user_id is not None:
            sql += " AND user_id = %s"
            params.append(user_id)
        sql += " ORDER BY created_at DESC"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        names = (
            "id", "tenant_id", "scope", "user_id", "kind", "pref_key", "value",
            "source", "created_by", "created_at",
        )
        return [dict(zip(names, r)) for r in rows]

    def _delete(self, tenant: str, pref_id: str) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM memory_preferences WHERE tenant_id = %s AND id = %s",
                (tenant, pref_id),
            )
            deleted = cur.rowcount > 0
            conn.commit()
        return bool(deleted)

    def _update_source(self, tenant: str, pref_id: str, source: str) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE memory_preferences SET source = %s WHERE tenant_id = %s AND id = %s",
                (source, tenant, pref_id),
            )
            updated = cur.rowcount > 0
            conn.commit()
        return bool(updated)


# --------------------------------------------------------------------------- #
# Factory                                                                      #
# --------------------------------------------------------------------------- #
_STORE: PreferenceStore | None = None


def get_preference_store() -> PreferenceStore:
    """Process-wide store: PG when a DSN is configured, else in-memory."""
    global _STORE
    if _STORE is not None:
        return _STORE
    dsn = os.environ.get("POSTGRES_SYNC_URL") or os.environ.get("POSTGRES_DSN")
    _STORE = PostgresPreferenceStore(dsn) if dsn else InMemoryPreferenceStore()
    return _STORE


def set_preference_store(store: PreferenceStore | None) -> None:
    """Test helper — pin an explicit store."""
    global _STORE
    _STORE = store


def reset_preference_store() -> None:
    """Test helper — drop the cached store (next call re-resolves it)."""
    global _STORE
    _STORE = None


# --------------------------------------------------------------------------- #
# High-level API                                                               #
# --------------------------------------------------------------------------- #
def add_preference(
    tenant_id: str | None,
    *,
    kind: str,
    value: str,
    source: str = SOURCE_EXPLICIT,
    scope: str = SCOPE_TENANT,
    user_id: str | None = None,
    key: str = "",
    created_by: str = "",
    store: PreferenceStore | None = None,
) -> Preference:
    """Record one preference.

    The **only** gateway that writes a preference. A document-derived call passes
    ``source=SOURCE_SUGGESTION`` and lands **inactive** (never injected); an
    unknown source is rejected (red line 14). A falsy tenant is fail-closed.
    """
    if not tenant_id:
        raise ValueError("tenant required to add a preference (fail closed)")
    if source not in ALLOWED_SOURCES:
        raise PreferenceSourceError(
            f"cannot activate preference from source {source!r}; "
            f"only {sorted(ACTIVE_SOURCES)} are effective"
        )
    if kind not in PREFERENCE_KINDS:
        raise ValueError(f"unknown preference kind {kind!r}; expected {PREFERENCE_KINDS}")
    pref = Preference(
        tenant_id=str(tenant_id),
        scope=scope,
        user_id=user_id,
        kind=kind,
        key=key,
        value=value,
        source=source,
        created_by=created_by,
    )
    return (store or get_preference_store()).record(pref)


def document_derived_preference(
    tenant_id: str | None,
    *,
    kind: str,
    value: str,
    key: str = "",
    created_by: str = "document",
    store: PreferenceStore | None = None,
) -> Preference | None:
    """Record a preference *hinted by document content* — always **inactive**.

    This is the 红线 14 surface: uploaded document text is data, so a hint it
    contains may only ever be stored as an unconfirmed ``suggestion``. It is
    visible in the settings page (the user can confirm or delete it) but is
    **never** injected into a task's context until confirmed. An unresolved
    tenant stores nothing (``None``).
    """
    if not tenant_id:
        return None
    return add_preference(
        tenant_id,
        kind=kind,
        value=value,
        key=key,
        source=SOURCE_SUGGESTION,
        scope=SCOPE_TENANT,
        created_by=created_by,
        store=store,
    )


def confirm_suggestion(
    tenant_id: str | None, pref_id: str, *, store: PreferenceStore | None = None
) -> bool:
    """Activate a stored suggestion (``suggestion`` → ``confirmed_suggestion``)."""
    if not tenant_id:
        return False
    return (store or get_preference_store()).confirm(tenant_id, pref_id)


def list_preferences(
    tenant_id: str | None,
    *,
    scope: str | None = None,
    user_id: str | None = None,
    include_inactive: bool = True,
    store: PreferenceStore | None = None,
) -> list[Preference]:
    """List a tenant's preferences. Falsy tenant ⇒ ``[]`` (fail-closed)."""
    if not tenant_id:
        return []
    return (store or get_preference_store()).list(
        tenant_id, scope=scope, user_id=user_id, include_inactive=include_inactive
    )


def delete_preference(
    tenant_id: str | None, pref_id: str, *, store: PreferenceStore | None = None
) -> bool:
    """Delete one preference (visible + deletable, per the task book)."""
    if not tenant_id:
        return False
    return (store or get_preference_store()).delete(tenant_id, pref_id)


def resolve_preferences(
    tenant_id: str | None,
    *,
    user_id: str | None = None,
    store: PreferenceStore | None = None,
) -> ResolvedPreferences:
    """Resolve the effective preference set for one (tenant, user) pair.

    Only **active** preferences (``explicit`` / ``confirmed_suggestion``) are
    effective. A user-level preference overrides a tenant-level one on the same
    ``(kind, key)`` and the override is recorded as a conflict — the output is
    deterministic (user wins) and auditable.
    """
    result = ResolvedPreferences()
    if not tenant_id:
        return result

    store = store or get_preference_store()
    tenant_items = store.list(
        tenant_id, scope=SCOPE_TENANT, include_inactive=False
    )
    user_items = (
        store.list(tenant_id, scope=SCOPE_USER, user_id=user_id, include_inactive=False)
        if user_id
        else []
    )
    result.tenant_items = tenant_items
    result.user_items = user_items

    effective: dict[tuple[str, str], Preference] = {p.override_key: p for p in tenant_items}
    for pref in user_items:
        existing = effective.get(pref.override_key)
        if existing is not None and existing.value != pref.value:
            result.conflicts.append(
                {
                    "kind": pref.kind,
                    "key": pref.key,
                    "tenant_value": existing.value,
                    "user_value": pref.value,
                    "winner": "user",
                }
            )
        effective[pref.override_key] = pref

    result.items = list(effective.values())
    return result
