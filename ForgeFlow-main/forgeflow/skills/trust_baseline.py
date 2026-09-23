"""INC2-13 — pre-publication trust baseline for a skill spec.

Review finding #8: a skill could be promoted (or listed on the marketplace)
with no check that it stays inside the tool whitelist, stays inside the
publisher's own permissions, or is free of PII. ``verify_trust_baseline`` runs
exactly those three checks and returns a report; callers decide the status code.

Design constraints:

* **Zero LLM** — pure static analysis, so it is deterministic and instant.
* **Fail closed** — an unknown tool is not in the allowed set and therefore
  fails; a missing permission fails; a DLP error fails.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from forgeflow.governance.dlp import DlpGate

__all__ = [
    "TrustReport",
    "allowed_tool_set",
    "verify_trust_baseline",
]

# Keys a spec may use to declare the tools it intends to call.
_TOOL_KEYS = ("tools", "allowed_tools", "tool_whitelist", "toolset")

# Keys a spec may use to declare the permissions it needs at run time.
_PERMISSION_KEYS = ("required_permissions", "permissions", "scopes")


@dataclass
class TrustReport:
    """Verdict of the three baseline checks."""

    ok: bool = True
    failures: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "failures": list(self.failures),
            "checks": dict(self.checks),
        }

    @property
    def reason(self) -> str:
        """Single-line reason for a 403 body."""
        return "; ".join(self.failures) if self.failures else "trust baseline passed"


def allowed_tool_set() -> frozenset[str]:
    """The platform tool catalogue a spec may reference.

    Union of the runtime's governed tools: the catalogue the platform ships
    (``PLATFORM_TOOL_CATALOGUE``), the default plan steps (``PLATFORM_TOOLS``)
    and the narrowly granted ones in ``TOOL_PERMISSION_MAP``. Importing the
    runtime is cheap and keeps one source of truth; if it ever fails we degrade
    to an empty set, which fails closed.
    """
    try:
        from forgeflow.runtime.gate import (
            PLATFORM_TOOL_CATALOGUE,
            PLATFORM_TOOLS,
            TOOL_PERMISSION_MAP,
        )

        return (
            frozenset(PLATFORM_TOOL_CATALOGUE)
            | frozenset(PLATFORM_TOOLS)
            | frozenset(TOOL_PERMISSION_MAP)
        )
    except Exception:  # noqa: BLE001 — fail closed on an unexpected import error
        return frozenset()


def _declared(spec: dict[str, Any], keys: Iterable[str]) -> list[str]:
    for key in keys:
        value = spec.get(key)
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple, set)):
            return [str(v) for v in value]
        if isinstance(value, dict):
            return [str(v) for v in value.keys()]
    return []


def _has_permission(actor_permissions: Iterable[str], permission: str) -> bool:
    grants = {str(p) for p in actor_permissions}
    if "*:*" in grants:
        return True
    if permission in grants:
        return True
    action, _, resource = permission.partition(":")
    return f"{action}:*" in grants


def verify_trust_baseline(
    spec: dict[str, Any],
    actor_permissions: Iterable[str] | None = None,
    *,
    allowed_tools: Iterable[str] | None = None,
) -> TrustReport:
    """Run the three checks and return the report (never raises).

    Args:
        spec: the skill spec / draft_spec under review.
        actor_permissions: ``"action:resource"`` grants of the publishing actor.
        allowed_tools: override of the platform tool catalogue (tests).

    Checks:
        1. ``tools_whitelisted``  — declared tools ⊆ allowed set.
        2. ``no_privilege_escalation`` — declared permissions ⊆ actor's grants.
        3. ``no_pii`` — ``DlpGate`` finds no PII in the serialised spec.
    """
    spec = spec if isinstance(spec, dict) else {}
    permissions = list(actor_permissions or [])
    catalogue = (
        frozenset(str(t) for t in allowed_tools)
        if allowed_tools is not None
        else allowed_tool_set()
    )

    failures: list[str] = []
    checks: dict[str, bool] = {}

    # --- 1. tool whitelist ----------------------------------------------
    declared_tools = _declared(spec, _TOOL_KEYS)
    illegal = sorted({t for t in declared_tools if t not in catalogue}) if catalogue else sorted(
        set(declared_tools)
    )
    checks["tools_whitelisted"] = not illegal
    if illegal:
        failures.append(f"工具越权：{', '.join(illegal)} 不在平台白名单内")

    # --- 2. privilege escalation ----------------------------------------
    declared_perms = _declared(spec, _PERMISSION_KEYS)
    escalations = sorted({p for p in declared_perms if not _has_permission(permissions, p)})
    checks["no_privilege_escalation"] = not escalations
    if escalations:
        failures.append(f"权限越权：发布者不具备 {', '.join(escalations)}")

    # --- 3. DLP ----------------------------------------------------------
    serialised = json.dumps(spec, ensure_ascii=False, sort_keys=True, default=str)
    try:
        result = DlpGate().scan(serialised)
        pii_hit = bool(result.pii_found)
        categories = list(getattr(result, "categories", []) or [])
    except Exception as exc:  # noqa: BLE001 — a DLP crash must not imply "clean"
        pii_hit = True
        categories = ["scan_error"]
        failures.append(f"DLP 扫描失败，按失败处理：{exc}")
    checks["no_pii"] = not pii_hit
    if pii_hit and categories != ["scan_error"]:
        failures.append(f"敏感信息：spec 命中 PII（{', '.join(categories)}）")

    return TrustReport(ok=not failures, failures=failures, checks=checks)
