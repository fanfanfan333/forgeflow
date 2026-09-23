"""Five-layer hierarchical Memory scope (docs §3.1, PRD P0-13).

Layers:
  * ``USER``     — private to one person
  * ``TEAM``     — shared within a team, invisible to other teams
  * ``EPISODIC`` — per-run event trace
  * ``SEMANTIC`` — distilled facts / embeddings
  * ``ORG``      — organisation-wide, downstream-shared knowledge

Each scope declares its write rules, read visibility and promotion policy so
the Memory Hub page and the extractor agree on where a fact belongs.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class MemoryScope(str, Enum):
    """The five memory layers."""

    USER = "user"
    TEAM = "team"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    ORG = "org"


# Write/read/promotion rules per layer. Kept declarative so the UI + API can
# render them without duplicating logic (PRD: "写入/读取规则明确").
SCOPE_RULES: dict[str, dict[str, Any]] = {
    MemoryScope.USER.value: {
        "label": "用户记忆",
        "description": "个人偏好与私有上下文，仅本人可读写。",
        "writable_by": ["owner", "system"],
        "readable_by": ["owner"],
        "promotable": True,
    },
    MemoryScope.TEAM.value: {
        "label": "团队记忆",
        "description": "团队共享经验，团队内可读，跨团队不可见。",
        "writable_by": ["team_member", "system"],
        "readable_by": ["team_member"],
        "promotable": True,
    },
    MemoryScope.EPISODIC.value: {
        "label": "情景记忆",
        "description": "一次任务执行的事件轨迹（Run / RunStep）。",
        "writable_by": ["system"],
        "readable_by": ["owner", "team_member", "admin"],
        "promotable": False,
    },
    MemoryScope.SEMANTIC.value: {
        "label": "语义记忆",
        "description": "抽象后的稳定事实与可检索向量。",
        "writable_by": ["system", "team_member"],
        "readable_by": ["owner", "team_member", "admin"],
        "promotable": True,
    },
    MemoryScope.ORG.value: {
        "label": "组织记忆",
        "description": "组织级可复用的知识，需显式操作下沉。",
        "writable_by": ["admin"],
        "readable_by": ["everyone"],
        "promotable": False,
    },
}


def scope_namespace(
    scope: MemoryScope | str,
    tenant_id: str | None,
    *,
    actor_id: str | None = None,
    team_id: str | None = None,
) -> str:
    """Build the canonical namespace for a (scope, tenant, owner) triple.

    Namespaces are prefixed with the tenant so the existing
    ``memory_vectors.namespace`` prefix guard in the memory router keeps
    working (cross-tenant reads can't slip through).
    """
    scope_value = scope.value if isinstance(scope, MemoryScope) else str(scope)
    tenant = tenant_id or "global"
    if scope_value == MemoryScope.USER.value:
        return f"workspace/{tenant}/{scope_value}/{actor_id or 'anon'}"
    if scope_value == MemoryScope.TEAM.value:
        return f"workspace/{tenant}/{scope_value}/{team_id or 'default'}"
    return f"workspace/{tenant}/{scope_value}"


def is_valid_scope(scope: str) -> bool:
    return scope in {s.value for s in MemoryScope}


def list_scopes() -> list[dict[str, Any]]:
    """Scope catalogue for the Memory Hub UI."""
    return [
        {"scope": s.value, **SCOPE_RULES[s.value]} for s in MemoryScope
    ]
