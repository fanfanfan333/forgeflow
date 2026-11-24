"""A bounded, restart-durable projection of a run's *detail* (BIZ-14).

Before this module, ``workspace_runs`` stored only the header + artifacts, so a
run hydrated after a restart came back with ``steps=[]`` and
``detail_retained=False`` — the UI could only say "执行明细未随本次进程保留".
That is honest, but it means the product forgets what it did, and "重启后任务
详情不完整" was a real, reproducible gap.

The projection here is deliberately **bounded**, because the run body is not the
header: a live run can carry megabytes of tool output. Rules:

* whitelisted keys only (a new field in ``steps`` never silently bloats the row);
* strings clipped, lists capped, whole sections dropped from the tail first;
* the serialised result must fit ``MAX_DETAIL_BYTES``; when it does not, the
  dropped part is reported via ``truncated``/``omitted`` instead of pretending
  the detail was complete;
* ``truncated=False`` therefore means "this really is the whole detail".

Nothing here invents a value: every field is copied from the record.
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = [
    "DETAIL_VERSION",
    "MAX_DETAIL_BYTES",
    "build_detail",
    "restore_detail",
    "sanitize_detail",
]

DETAIL_VERSION = 1

#: Hard ceiling for the serialised projection. Chosen to keep a row small enough
#: that listing history never drags a payload along (the list endpoints read the
#: same table), while still holding hundreds of steps.
MAX_DETAIL_BYTES = 128 * 1024

_MAX_STEPS = 200
_MAX_INVOCATIONS = 200
_MAX_ERRORS = 50
_CLIP = 600

_STEP_KEYS = ("step", "tool", "status", "latency_ms", "agent", "note", "error", "step_id")
_INVOCATION_KEYS = (
    "tool",
    "status",
    "latency_ms",
    "arguments_hash",
    "arguments",
    "error",
    "step_id",
    "run_id",
    "actor_user_id",
    "degraded",
    "reason",
)


def _clip(value: Any, limit: int = _CLIP) -> Any:
    """Truncate strings (and string leaves of containers) without inventing data."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"…[+{len(value) - limit} chars]"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        # FF-R1 P5 — the projection is served back in API responses, so
        # platform-internal keys (``_``-prefixed: reserved context,
        # streak state, injected bookkeeping) never cross into it.
        # ``_clip`` is the single choke point every projected value
        # passes through, so stripping here closes every section.
        return {str(k): _clip(v, limit) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, (list, tuple)):
        return [_clip(v, limit) for v in value[:50]]
    return str(value)[:limit]


def _pick(row: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    return {key: _clip(row[key]) for key in keys if key in row}


def _section(record: Any, name: str) -> dict[str, Any]:
    value = getattr(record, name, None)
    return _clip(value) if isinstance(value, dict) else {}


def _size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))


def build_detail(record: Any, *, max_bytes: int = MAX_DETAIL_BYTES) -> dict[str, Any]:
    """Project ``record``'s detail into a bounded, JSON-safe dict."""
    steps = [_pick(s, _STEP_KEYS) for s in (getattr(record, "steps", None) or [])][:_MAX_STEPS]
    invocations = [
        _pick(i, _INVOCATION_KEYS) for i in (getattr(record, "tool_invocations", None) or [])
    ][:_MAX_INVOCATIONS]
    observations = [
        _pick(o, ("step_id", "source", "status", "summary"))
        for o in (getattr(record, "observations", None) or [])
    ][:_MAX_STEPS]
    errors = [_clip(str(e)) for e in (getattr(record, "errors", None) or [])][:_MAX_ERRORS]

    payload: dict[str, Any] = {
        "version": DETAIL_VERSION,
        "steps": steps,
        "tool_invocations": invocations,
        "observations": observations,
        "errors": errors,
        "plan": _section(record, "plan"),
        "loop": _section(record, "loop"),
        "persistence": _section(record, "persistence"),
        "plan_source": str(getattr(record, "plan_source", "") or ""),
        "skills_used": [str(s) for s in (getattr(record, "skills_used", None) or [])][:50],
        "budget": _section(record, "llm").get("execution_budget") or {},
        "truncated": False,
        "omitted": [],
    }
    if not any(payload[key] for key in ("steps", "tool_invocations", "errors")):
        return sanitize_detail(payload)

    # Shrink from the least load-bearing end, recording exactly what was dropped.
    for key, keep in (
        ("observations", 50),
        ("tool_invocations", 50),
        ("steps", 50),
        ("tool_invocations", 10),
        ("steps", 10),
    ):
        if _size(payload) <= max_bytes:
            break
        rows = payload[key]
        if len(rows) > keep:
            payload[key] = rows[:keep]
            payload["omitted"].append(f"{key}:{len(rows) - keep}")
            payload["truncated"] = True
    if _size(payload) > max_bytes:
        # Still too big: the free-form sections go, the spine (steps) stays.
        for key in ("observations", "plan", "loop", "budget", "persistence", "skills_used"):
            if _size(payload) <= max_bytes:
                break
            if payload[key]:
                payload[key] = [] if isinstance(payload[key], list) else {}
                payload["omitted"].append(key)
                payload["truncated"] = True
    return sanitize_detail(payload)


# --------------------------------------------------------------------------- #
# FF-R1 P5-FIX — the ONE sanitizer every detail output path passes through.
#
# P5's projection stripped ``_``-prefixed keys, but only inside the
# persisted projection: the live response never went through it and
# the ``llm`` column was copied verbatim on hydration, so internal
# keys, secret-shaped values and absolute server paths leaked.
# ``sanitize_detail`` is applied at write time (the projection and
# the header columns), at hydration, and at the API boundary, so no
# provenance of a run record can serve them.
# --------------------------------------------------------------------------- #

_SK_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{8,}")
_BEARER_PATTERN = re.compile(r"(?i)(bearer)\s+\S+")
_KEY_VALUE_SECRET_PATTERN = re.compile(
    r"(?i)(token|password|passwd|pwd|secret|api[_-]?key)(\s*[:=]\s*)\S+"
)
_ABS_PATH_PATTERN = re.compile(r"(?<![\w:./])/(?:[\w.+-]+/)+[\w.+-]*")
_REDACTED = "***"


def _configured_secret_values() -> tuple[str, ...]:
    """Secret VALUES from the live settings (never logged, never returned).

    Read defensively: any settings attribute whose name marks it as
    a credential contributes its value, so a configured secret is
    redacted wherever it appears — not just where its shape is
    recognised.
    """
    try:
        from forgeflow.config import get_settings

        settings = get_settings()
    except Exception:  # noqa: BLE001 — sanitising must never break a read
        return ()
    values: list[str] = []
    for name in dir(settings):
        lowered = name.lower()
        if not any(tag in lowered for tag in ("password", "secret", "token", "api_key", "apikey")):
            continue
        value = getattr(settings, name, None)
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
        if isinstance(value, str) and len(value) >= 8:
            values.append(value)
    return tuple(values)


def _sanitize_text(text: str, secrets: tuple[str, ...]) -> str:
    out = text
    for secret in secrets:
        out = out.replace(secret, _REDACTED)
    out = _SK_PATTERN.sub(f"sk-{_REDACTED}", out)
    out = _BEARER_PATTERN.sub(rf"\1 {_REDACTED}", out)
    out = _KEY_VALUE_SECRET_PATTERN.sub(rf"\1\2{_REDACTED}", out)
    return _ABS_PATH_PATTERN.sub("<path>", out)


def sanitize_detail(value: Any, *, _secrets: tuple[str, ...] | None = None, _depth: int = 0) -> Any:
    """Recursively clean a detail payload for output or persistence.

    * dict keys starting with ``_`` are dropped (internal state);
    * strings are redacted: configured secret values, the ``sk-`` /
      ``Bearer`` / ``key=value`` secret shapes, and absolute paths
      (replaced by ``<path>``);
    * everything else passes through unchanged (on copies — the
      caller's structures are never mutated).
    """
    if _depth > 16:  # a pathological nest is returned as-is rather than recursing forever
        return value
    secrets = _configured_secret_values() if _secrets is None else _secrets
    if isinstance(value, dict):
        return {
            str(key): sanitize_detail(item, _secrets=secrets, _depth=_depth + 1)
            for key, item in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_detail(item, _secrets=secrets, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value, secrets)
    return value


def restore_detail(record: Any, detail: dict[str, Any] | None) -> bool:
    """Copy a persisted projection back onto ``record``.

    Returns ``True`` when the record now carries a real (possibly truncated)
    detail — the caller uses that to decide ``detail_retained``, so a pre-042
    header (empty dict) stays ``False`` instead of looking restored.
    """
    if not isinstance(detail, dict) or not detail:
        return False
    # Rows written before P5-FIX were never sanitized; clean on the
    # way out so a historical row cannot leak either.
    detail = sanitize_detail(detail)
    steps = detail.get("steps")
    invocations = detail.get("tool_invocations")
    observations = detail.get("observations")
    errors = detail.get("errors")
    if isinstance(steps, list):
        record.steps = [dict(s) for s in steps if isinstance(s, dict)]
    if isinstance(invocations, list):
        record.tool_invocations = [dict(i) for i in invocations if isinstance(i, dict)]
    if isinstance(observations, list):
        record.observations = [dict(o) for o in observations if isinstance(o, dict)]
    if isinstance(errors, list) and errors and not record.errors:
        record.errors = [str(e) for e in errors]
    for key in ("plan", "loop"):
        value = detail.get(key)
        if isinstance(value, dict) and value:
            setattr(record, key, dict(value))
    plan_source = detail.get("plan_source")
    if isinstance(plan_source, str) and plan_source:
        # The record being restored was just built from a header, so its
        # ``plan_source`` is the dataclass default ("fallback") — a *fabricated*
        # value for a react run. The persisted one wins.
        record.plan_source = plan_source
    skills = detail.get("skills_used")
    if isinstance(skills, list) and skills and not record.skills_used:
        record.skills_used = [str(s) for s in skills]
    budget = detail.get("budget")
    if isinstance(budget, dict) and budget:
        llm = dict(getattr(record, "llm", None) or {})
        llm.setdefault("execution_budget", dict(budget))
        record.llm = llm
    return bool(steps or invocations or errors)
