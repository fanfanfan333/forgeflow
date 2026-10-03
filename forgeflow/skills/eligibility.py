"""INC46 T03 — Skill **eligibility** (tenant / role / capability, fail-closed).

Why this module exists
----------------------
``SkillRegistry.select`` (``forgeflow/skills/registry.py::select``) is a *keyword*
ranking over ``name`` / ``domain`` / ``description`` — it answers "which skills
look relevant to this intent?", never "may **this** identity use **this** skill?".
The runtime therefore had no tenant / role / capability gate on the *selection*
side at all (audit F13). This module closes that gap with a single, pure,
deterministic predicate that sits **below** RBAC (like
``tenant_scope`` — the same fail-closed philosophy) and is applied *before* a
skill is offered to the runtime.

Design rules (fail-closed by construction)
------------------------------------------
* **Tenant is mandatory.** An unresolved tenant (``None`` / empty) yields an
  empty eligible set — the repo layer's ``tenant_id IS NOT DISTINCT FROM``
  isolation rule is mirrored here on the in-memory record set. A skill whose
  ``tenant_id`` is ``None`` matches **no** tenant (``tenant_scope.scope_filter``),
  so a legacy / global row can never leak into a tenant-scoped selection.
* **Only published skills are executable.** A ``draft`` / ``evaluating`` /
  ``retired`` skill is not a runtime asset; it is honestly ineligible rather than
  silently run.
* **Role / capability are declaration-driven.** A skill restricts its audience
  only by *declaring* it, via two tag conventions:
    - ``role:<role>``  — if a skill carries **any** ``role:`` tag, the caller's
      ``role`` must be one of them; an undeclared role set means "open to any
      resolved role".
    - ``cap:<capability>`` — every declared capability must be held by the caller
      (``capabilities``).
  This is deliberately **not** a hard-coded deny-list (which would drift from
  the platform's RBAC); it reads only real, on-record declarations. Everything is
  still fail-closed: an unresolved role is ineligible, and a declared capability
  the caller lacks is ineligible.
* **Pure.** No I/O, no LLM, no settings — trivially unit-testable and safe on
  every read path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from forgeflow.skills.tenant_scope import scope_filter

__all__ = [
    "Eligibility",
    "ROLE_TAG_PREFIX",
    "CAPABILITY_TAG_PREFIX",
    "EXECUTABLE_STATUS",
    "is_eligible",
    "filter_eligible",
]

#: A skill tag that restricts the skill to a set of roles (``"role:manager"``).
ROLE_TAG_PREFIX = "role:"
#: A skill tag that requires the caller to hold a capability (``"cap:skills:execute"``).
CAPABILITY_TAG_PREFIX = "cap:"
#: The only skill status that may be executed at runtime.
EXECUTABLE_STATUS = "published"


@dataclass
class Eligibility:
    """The verdict for one skill + caller pair.

    ``eligible`` is the boolean decision; ``reason`` is the **verbatim**,
    human-readable justification (empty when eligible) so a denial is never
    silent and can be asserted on directly in a test.
    """

    eligible: bool
    reason: str = ""


def _attr(skill: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a ``SkillRecord`` **or** a plain mapping (same value)."""
    if isinstance(skill, dict):
        return skill.get(name, default)
    return getattr(skill, name, default)


def _tags(skill: Any) -> list[str]:
    """The skill's declared tags, normalised to non-empty strings."""
    raw = _attr(skill, "tags")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(t).strip() for t in raw if str(t or "").strip()]


def _tag_values(tags: list[str], prefix: str) -> list[str]:
    """The values of every ``prefix`` tag (e.g. ``cap:`` ⇒ the capabilities)."""
    lower = prefix.lower()
    return [t[len(prefix):].strip() for t in tags if t.lower().startswith(lower)]


def is_eligible(
    skill: Any,
    *,
    tenant_id: str | None,
    role: str,
    capabilities: list[str],
) -> Eligibility:
    """Return whether ``skill`` may be offered to an identity, fail-closed.

    The gates are evaluated in a fixed order so the returned ``reason`` is
    deterministic: **tenant → status → role → capability**.

    Args:
        skill: a ``SkillRecord`` (or a mapping with the same fields).
        tenant_id: the caller's resolved tenant. ``None`` / empty ⇒ ineligible.
        role: the caller's role. Empty ⇒ ineligible (fail-closed).
        capabilities: the capabilities the caller holds.

    Returns:
        An :class:`Eligibility`; ``reason`` is verbatim when not eligible.
    """
    # 1. Tenant — mandatory and strict (no ``None`` ⇒ global leak).
    if not tenant_id:
        return Eligibility(False, "租户未解析，技能不可用（fail-closed）")
    if not scope_filter(_attr(skill, "tenant_id"), tenant_id):
        return Eligibility(False, "技能不属于当前租户，已按租户隔离拦截")

    # 2. Status — only a published skill is a runtime asset.
    status = str(_attr(skill, "status", "") or "")
    if status != EXECUTABLE_STATUS:
        return Eligibility(
            False,
            f"技能未发布（status={status or 'unknown'}），不可在运行期执行",
        )

    # 3. Role — declaration-driven; an unresolved role is fail-closed.
    if not role:
        return Eligibility(False, "调用方角色未解析，技能不可用（fail-closed）")
    role_tags = _tag_values(_tags(skill), ROLE_TAG_PREFIX)
    if role_tags and role not in role_tags:
        return Eligibility(
            False,
            f"技能仅对角色 {sorted(role_tags)} 开放，当前角色 '{role}' 不可用",
        )

    # 4. Capability — every declared capability must be held.
    held = {str(c) for c in (capabilities or []) if str(c or "").strip()}
    needed = [c for c in _tag_values(_tags(skill), CAPABILITY_TAG_PREFIX) if c]
    missing = sorted({c for c in needed if c not in held})
    if missing:
        return Eligibility(
            False,
            f"调用方缺能力 {'、'.join(missing)}，技能不可用（fail-closed）",
        )

    return Eligibility(True, "")


def filter_eligible(
    skills: list[Any],
    *,
    tenant_id: str | None,
    role: str,
    capabilities: list[str],
) -> list[Any]:
    """Return only the eligible skills (order preserved), fail-closed on tenant.

    An unresolved ``tenant_id`` short-circuits to ``[]`` — the whole set is
    ineligible, never "all tenants".
    """
    if not tenant_id:
        return []
    out: list[Any] = []
    for skill in skills or []:
        verdict = is_eligible(
            skill, tenant_id=tenant_id, role=role, capabilities=capabilities
        )
        if verdict.eligible:
            out.append(skill)
    return out
