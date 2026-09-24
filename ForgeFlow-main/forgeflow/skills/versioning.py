"""Skill versioning — semver, changelog, diff, rollback (docs §2 ⑤)."""

from __future__ import annotations

from typing import Any

from forgeflow.repositories.base import utcnow
from forgeflow.skills.models import SkillRecord, SkillVersionRecord

_BUMPS = ("major", "minor", "patch")


def parse_semver(value: str) -> tuple[int, int, int]:
    """Parse ``x.y.z`` into a tuple, tolerating a leading ``v`` and short forms."""
    raw = (value or "0.0.0").strip().lstrip("vV")
    parts = raw.split(".")
    numbers: list[int] = []
    for part in parts[:3]:
        digits = "".join(ch for ch in part if ch.isdigit())
        numbers.append(int(digits) if digits else 0)
    while len(numbers) < 3:
        numbers.append(0)
    return numbers[0], numbers[1], numbers[2]


def format_semver(parts: tuple[int, int, int]) -> str:
    return f"{parts[0]}.{parts[1]}.{parts[2]}"


def bump_semver(current: str | None, bump: str = "patch") -> str:
    """Return the next semver. Unknown ``bump`` falls back to ``patch``."""
    major, minor, patch = parse_semver(current or "0.0.0")
    kind = bump if bump in _BUMPS else "patch"
    if kind == "major":
        return format_semver((major + 1, 0, 0))
    if kind == "minor":
        return format_semver((major, minor + 1, 0))
    return format_semver((major, minor, patch + 1))


def diff_specs(old: dict[str, Any] | None, new: dict[str, Any] | None) -> dict[str, Any]:
    """Structural diff between two specs (steps / tools / changed keys)."""
    old = old or {}
    new = new or {}
    old_steps = list(old.get("steps", []))
    new_steps = list(new.get("steps", []))
    old_tools = set(old.get("tools", []))
    new_tools = set(new.get("tools", []))
    changed_keys = sorted(
        key
        for key in set(old) | set(new)
        if old.get(key) != new.get(key)
    )
    return {
        "steps_added": [s for s in new_steps if s not in old_steps],
        "steps_removed": [s for s in old_steps if s not in new_steps],
        "tools_added": sorted(new_tools - old_tools),
        "tools_removed": sorted(old_tools - new_tools),
        "changed_keys": changed_keys,
        "is_empty": (
            old_steps == new_steps
            and old_tools == new_tools
            and not changed_keys
        ),
    }


def change_summary(diff: dict[str, Any]) -> str:
    """Human-readable one-liner for a changelog entry."""
    parts: list[str] = []
    if diff.get("steps_added"):
        parts.append(f"+{len(diff['steps_added'])} 步")
    if diff.get("steps_removed"):
        parts.append(f"-{len(diff['steps_removed'])} 步")
    if diff.get("tools_added"):
        parts.append(f"+工具 {', '.join(diff['tools_added'])}")
    if diff.get("tools_removed"):
        parts.append(f"-工具 {', '.join(diff['tools_removed'])}")
    if diff.get("changed_keys"):
        parts.append(f"改动字段 {', '.join(diff['changed_keys'])}")
    return "；".join(parts) if parts else "无结构性改动"


def build_changelog(
    semver: str, actor: str, summary: str, diff: dict[str, Any]
) -> str:
    """Compose a multi-line changelog entry."""
    return (
        f"[{semver}] by {actor}\n"
        f"- {summary}\n"
        f"- {change_summary(diff)}"
    )


async def create_version(
    repo: Any,
    tenant_id: str | None,
    skill: SkillRecord,
    spec: dict[str, Any],
    *,
    bump: str = "patch",
    actor: str = "system",
    summary: str = "版本更新",
    approved_by: str | None = None,
    eval_score: float | None = None,
    source_experience_ids: list[str] | None = None,
    changelog: str | None = None,
    publish: bool = True,
) -> SkillVersionRecord:
    """Create the next semver version of ``skill`` and (by default) point at it.

    ``publish`` defaults to ``True`` — the historical behaviour: the new version
    becomes ``skill.current_version`` at once. INC9 B1 passes ``publish=False``
    when a canary release is enabled, so the version is recorded but the
    incumbent stays current until an A/B verdict promotes it. With the default
    it is byte-for-byte the previous behaviour.
    """
    previous = await repo.get_version(tenant_id, skill.id, skill.current_version or "")
    old_spec = previous.spec if previous else {}
    diff = diff_specs(old_spec, spec)
    new_semver = bump_semver(skill.current_version, bump)

    version = SkillVersionRecord(
        tenant_id=tenant_id,
        skill_id=skill.id,
        semver=new_semver,
        spec=spec,
        changelog=changelog or build_changelog(new_semver, actor, summary, diff),
        eval_score=eval_score,
        source_experience_ids=list(source_experience_ids or []),
        approved_by=approved_by,
        created_at=utcnow(),
    )
    await repo.add_version(tenant_id, version)

    if publish:  # default True — the existing all-at-once behaviour
        skill.current_version = new_semver
        skill.updated_at = utcnow()
        await repo.update_skill(skill)
    return version


async def rollback(
    repo: Any,
    tenant_id: str | None,
    skill_id: str,
    to_version: str,
    actor: str = "system",
) -> tuple[SkillRecord, dict[str, Any]]:
    """Repoint ``skill.current_version`` at an existing version.

    Returns ``(skill, diff)`` where ``diff`` compares the current spec to the
    target version's spec (so the caller can show what a rollback changes).
    """
    from forgeflow.skills.errors import GovernanceError

    skill = await repo.get_skill(tenant_id, skill_id)
    if skill is None:
        raise GovernanceError("skill not found", status_code=404)

    target = await repo.get_version(tenant_id, skill_id, to_version)
    if target is None:
        raise GovernanceError(f"version {to_version} not found", status_code=404)

    current = await repo.get_version(tenant_id, skill_id, skill.current_version or "")
    diff = diff_specs(current.spec if current else {}, target.spec)

    skill.current_version = to_version
    skill.updated_at = utcnow()
    await repo.update_skill(skill)
    return skill, diff
