"""INC46 T03 — Skill **Runtime**: eligibility → select → load procedure → drive.

Why this module exists
----------------------
A selected skill declares **what to do** in two distinct forms, and the platform
must never confuse them:

* ``spec["steps"]`` — a ``list[str]`` of human-readable step *labels*. It is a
  **suggestion** rendered into the code plane's context block
  (``forgeflow/codeplane/runner/context_block.py::render_context_block``) and
  collected into ``ctx.injected_skills``
  (``forgeflow/runtime/orchestrator.py::_resolve_injected_skills``). Its type and
  meaning are frozen — changing it would break both consumers (audit §9-2).
* ``spec["procedure"]`` — a **new, structured** list
  ``[{purpose, tool, input[], output[], validation}]`` whose ``tool`` names a
  **platform tool**. This is the only thing the runtime may *drive* step by step:
  each ``procedure[i].tool`` is handed to the existing ``ToolExecutor`` (through
  the existing RBAC / Policy / whitelist seams — there is no second gate here).

This module owns the deterministic projection from a declared ``procedure`` to
(a) :class:`SkillStep` value objects, (b) plan **candidates** for the
orchestrator (whitelist-filtered — the tool is taken *verbatim* from the step,
**never** fuzzy-matched from the intent), and (c) the **honest degradation
reason** when a skill cannot be executed step by step.

Honest degradation (verbatim, assertable — design §1.3)
------------------------------------------------------
===============================  ==============================================
situation                        outcome
===============================  ==============================================
no ``procedure`` (or empty)      ``"技能未声明 procedure，无法逐步执行"`` — nothing
                                 is driven; the legacy ``steps`` text still
                                 injects as before; the runtime never pretends
                                 to execute.
a step's ``tool`` ∉ whitelist    ``"技能声明的工具 '<t>' 不在平台白名单内，拒绝执行"``
                                 — the step is **not** executed and is **never**
                                 substituted with another tool.
the role lacks the step's         the existing ``gate.describe_denial`` text —
permission                        the step is ``blocked`` and the run errors
                                 (HTTP < 500, never 5xx).
===============================  ==============================================

``SkillNotExecutable`` carries the verbatim reason + an honest ``status_code``
(< 500) so a caller that must surface the degradation as an error never turns it
into a server fault.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forgeflow.runtime.gate import (
    PLATFORM_PLAN_TOOLS,
    check_tool_permission,
    describe_denial,
)
from forgeflow.skills.eligibility import filter_eligible
from forgeflow.skills.registry import SkillRegistry
from forgeflow.skills.tenant_scope import require_tenant

__all__ = [
    "SkillNotExecutable",
    "SkillStep",
    "UNDECLARED_PROCEDURE_REASON",
    "load_procedure",
    "to_plan_candidates",
    "SkillRuntime",
]

#: Verbatim reason for a skill that declares no ``procedure`` at all.
UNDECLARED_PROCEDURE_REASON = "技能未声明 procedure，无法逐步执行"


def _not_in_whitelist_reason(tool: str) -> str:
    """Verbatim fail-closed reason for a step tool outside the platform whitelist."""
    return f"技能声明的工具 '{tool}' 不在平台白名单内，拒绝执行"


class SkillNotExecutable(Exception):
    """A skill's declared procedure cannot be driven (fail-closed, HTTP < 500).

    ``reason`` is the verbatim, assertable explanation; ``status_code`` is the
    honest client-facing status (``403`` for a permission denial, otherwise a
    non-5xx ``409`` conflict — the request is understood but the skill is not in
    an executable state). It is deliberately **never** a 5xx: a skill that cannot
    run is a declared-state outcome, not a server fault.
    """

    def __init__(self, reason: str, *, status_code: int = 409) -> None:
        self.reason = str(reason)
        # Guard the red line structurally: a degradation is never a server fault.
        self.status_code = int(status_code) if int(status_code) < 500 else 409
        super().__init__(self.reason)


@dataclass
class SkillStep:
    """One declared, executable step of a skill's ``procedure``.

    Attributes:
        purpose: what the step is for (human-readable; never a tool selector).
        tool: the **platform tool id** this step drives (taken verbatim).
        input_keys: the input keys the step reads (declaration; advisory).
        output_keys: the output keys the step produces (declaration; advisory).
        validation: the step's stated validation (advisory).
    """

    purpose: str = ""
    tool: str = ""
    input_keys: list[str] = field(default_factory=list)
    output_keys: list[str] = field(default_factory=list)
    validation: str = ""


def _as_str_list(value: Any) -> list[str]:
    """Normalise a declared string list (a scalar string ⇒ a one-item list)."""
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v or "").strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def load_procedure(spec: dict[str, Any] | None) -> list[SkillStep]:
    """Load the structured ``procedure`` from a skill spec (explicit).

    Reads the **new** key ``spec["procedure"]`` only; the legacy
    ``spec["steps"]`` is untouched and never consulted. Undeclared / empty /
    non-list ⇒ ``[]`` (an honest "nothing to drive"). Non-dict entries are
    skipped; a missing ``tool`` is preserved as ``""`` so the downstream
    whitelist check refuses it honestly instead of dropping it silently.
    """
    if not isinstance(spec, dict):
        return []
    raw = spec.get("procedure")
    if not isinstance(raw, (list, tuple)):
        return []
    steps: list[SkillStep] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        steps.append(
            SkillStep(
                purpose=str(item.get("purpose") or ""),
                tool=str(item.get("tool") or "").strip(),
                input_keys=_as_str_list(item.get("input")),
                output_keys=_as_str_list(item.get("output")),
                validation=str(item.get("validation") or ""),
            )
        )
    return steps


def _skill_name(skill: Any) -> str:
    """The skill's display name from a record or a mapping (``""`` when none)."""
    if isinstance(skill, dict):
        return str(skill.get("name") or "")
    return str(getattr(skill, "name", "") or "")


def to_plan_candidates(skill: Any, procedure: list[SkillStep]) -> list[dict[str, Any]]:
    """Project a procedure into orchestrator plan candidates (whitelist-filtered).

    Every returned candidate names ``procedure[i].tool`` **verbatim** — there is
    no intent keyword match and no fuzzy substitution (the platform's
    "No fuzzy tool selection" contract). A step whose tool is empty or is not in
    :data:`forgeflow.runtime.gate.PLATFORM_PLAN_TOOLS` is **omitted**: it is not
    executed and it is never replaced by a different tool (the honest reason is
    :meth:`SkillRuntime.degrade_reason`).
    """
    name = _skill_name(skill)
    out: list[dict[str, Any]] = []
    for index, step in enumerate(procedure or []):
        if not isinstance(step, SkillStep):
            continue
        if not step.tool or step.tool not in PLATFORM_PLAN_TOOLS:
            continue
        label = f"技能步骤 {index + 1}：{step.purpose}" if step.purpose else f"技能步骤 {index + 1}"
        note = f"技能「{name}」{label}" if name else label
        out.append({"tool": step.tool, "step_type": "skill", "note": note})
    return out


class SkillRuntime:
    """The runtime facade over the skill registry (eligibility + procedure).

    All repository reads go through :func:`require_tenant` **first**, so an
    unresolved tenant fails closed with ``403`` before anything is read — the
    same ordering rule the skill API routers use (``require_tenant`` "before any
    read").
    """

    def __init__(self, registry: SkillRegistry | None = None) -> None:
        self._registry = registry

    def _registry_or_default(self) -> SkillRegistry:
        return self._registry if self._registry is not None else SkillRegistry()

    async def eligible(
        self, tenant_id: str | None, *, role: str, capabilities: list[str]
    ) -> list[Any]:
        """The tenant's published skills this identity may use (fail-closed)."""
        tenant = require_tenant(tenant_id)
        skills, _total = await self._registry_or_default().list_skills(tenant, limit=200)
        return filter_eligible(
            skills, tenant_id=tenant, role=role, capabilities=capabilities
        )

    async def select(
        self,
        tenant_id: str | None,
        intent: str,
        *,
        role: str,
        capabilities: list[str],
        k: int = 3,
    ) -> Any | None:
        """The best **eligible** skill for ``intent``, or ``None``.

        The ranking is the registry's existing keyword ``select``; eligibility is
        applied on top of the ranked candidates, so an ineligible top hit never
        shadows a lower eligible one.
        """
        tenant = require_tenant(tenant_id)
        ranked = await self._registry_or_default().select(tenant, intent, k=k)
        eligible = filter_eligible(
            ranked, tenant_id=tenant, role=role, capabilities=capabilities
        )
        return eligible[0] if eligible else None

    async def load(self, tenant_id: str | None, skill_id: str) -> list[SkillStep]:
        """Load the **current version's** declared procedure (``[]`` when none).

        Mirrors ``orchestrator._resolve_injected_skills``: ``name`` /
        ``current_version`` come from the registry, and the version record
        matching that semver carries ``spec["procedure"]``. A skill whose
        procedure cannot be resolved yields ``[]`` — never a fabricated step.
        """
        tenant = require_tenant(tenant_id)
        registry = self._registry_or_default()
        record = await registry.get(tenant, skill_id)
        if record is None:
            return []
        version = str(getattr(record, "current_version", "") or "")
        if not version:
            return []
        try:
            versions = await registry.versions(tenant, skill_id)
        except Exception:  # noqa: BLE001 — a version read must never gate a load
            versions = []
        match = next(
            (v for v in versions if str(getattr(v, "semver", "")) == version), None
        )
        spec = getattr(match, "spec", None) if match is not None else None
        return load_procedure(spec)

    def execution_candidates(self, procedure: list[SkillStep]) -> list[dict[str, Any]]:
        """Plan candidates for ``procedure`` (whitelist-filtered; see module doc)."""
        return to_plan_candidates(None, procedure)

    def degrade_reason(self, procedure: list[SkillStep], *, role: str) -> str | None:
        """The verbatim reason a procedure cannot be driven, or ``None``.

        Precedence (fixed, so the reason is deterministic): an empty procedure
        first (nothing to drive), then the first non-whitelisted tool (fail-closed
        — never substituted), then the first step the role cannot run (the
        existing ``gate.describe_denial`` text).
        """
        steps = list(procedure or [])
        if not steps:
            return UNDECLARED_PROCEDURE_REASON
        for step in steps:
            if step.tool not in PLATFORM_PLAN_TOOLS:
                return _not_in_whitelist_reason(step.tool)
        for step in steps:
            if not check_tool_permission(role, step.tool):
                return describe_denial(role, step.tool)
        return None

    def require(self, procedure: list[SkillStep], *, role: str) -> None:
        """Raise :class:`SkillNotExecutable` when ``procedure`` cannot be driven.

        A no-op when the procedure is executable; otherwise the verbatim
        :meth:`degrade_reason` is carried out with an honest, non-5xx status.
        """
        reason = self.degrade_reason(procedure, role=role)
        if reason is None:
            return
        status_code = 403 if reason.startswith("权限不足") else 409
        raise SkillNotExecutable(reason, status_code=status_code)
