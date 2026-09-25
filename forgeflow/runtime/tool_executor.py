"""ToolExecutor — the single, honest execution entry point (INC12 A1).

The defect this module fixes
----------------------------
``orchestrator._llm_executor`` (line ~411) and ``orchestrator._default_executor``
(line ~515) both recorded every planned step as ``status="ok"`` **without ever
calling a tool**. A plan naming ``research.search`` was gated by RBAC → ABAC →
Policy, then "executed" — the platform logged *executed ✅* while the tool never
ran. RBAC/ABAC/Policy/HITL governed a fiction.

:class:`ToolExecutor.execute` is the **only** path a runtime step may use to
invoke a tool, and it may record ``status="ok"`` **only** when a real handler
actually ran and returned a result. Everything else is an honest
``error`` / ``unavailable`` / ``refused`` / ``skipped`` with a stated reason.

Status semantics (the contract; tests pin every row)
----------------------------------------------------
============  =========  ===========================================  ==========
status        executed   meaning                                      run failed
============  =========  ===========================================  ==========
``ok``        ``True``   the handler really ran and returned a result no
``error``     ``True``   the handler ran but raised / returned failure yes
``unavailable`` ``False``  the tool has no implementation at all        yes
``refused``   ``False``  an implementation exists but an environment  yes
                         gate refused it (prod must not mock)
``skipped``   ``False``  an implementation exists but had no valid    no
                         input, so it did not run
============  =========  ===========================================  ==========

Execution order (fixed; never reordered):

1. ``resolve(tool)`` — unresolved ⇒ ``unavailable`` (never ``ok``).
2. Environment gate — a ``development`` binding outside ``dev`` ⇒ ``refused``
   (never a silent fallback to a mock).
3. Time and call the handler; **any** exception ⇒ ``error`` (never swallowed,
   never downgraded to ``ok``).
4. ``{"ok": False, "not_executed": True}`` ⇒ ``skipped``; any other
   ``{"ok": False}`` ⇒ ``error``.
5. ``{"ok": True}`` ⇒ ``ok``.
6. Compute latency, ``arguments_hash``, ``result_ref`` and a bounded ``summary``.
7. Bound ``payload`` (4000 chars) and, for external (networked) tools, sanitise
   it through :func:`forgeflow.security.tool_output_guard.sanitize_tool_output`.
8. ``data_scope`` from a real source or ``None`` — never invented.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from forgeflow.config import get_settings
from forgeflow.security.tool_output_guard import sanitize_tool_output
from forgeflow.runtime.tool_registry import resolve

logger = logging.getLogger(__name__)

__all__ = [
    "ToolCallContext",
    "ToolInvocation",
    "ToolExecutor",
    "MAX_PAYLOAD_CHARS",
]

#: Hard ceiling on the stored ``payload`` (characters of its JSON form). A larger
#: result is replaced by a bounded ``{"truncated": True, ...}`` envelope so a
#: flood of tool output can never bloat an event/record.
MAX_PAYLOAD_CHARS = 4000

#: Tools whose output is *external* (networked) and therefore must pass the
#: indirect-prompt-injection guard before it is stored / shown.
_EXTERNAL_TOOLS: frozenset[str] = frozenset({"research.search"})


# --------------------------------------------------------------------------- #
# Canonical serialisation helpers                                              #
# --------------------------------------------------------------------------- #
def _canonical(value: Any) -> str:
    """Stable canonical JSON for hashing (sorted keys, no incidental spaces)."""
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return repr(value)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bound_payload(value: Any) -> tuple[Any, bool]:
    """Return ``(bounded_value, truncated)`` for a payload.

    ``<= MAX_PAYLOAD_CHARS`` (as JSON) is returned unchanged. Larger payloads
    become a compact, self-describing envelope so the truncation is explicit.
    """
    text = _canonical(value)
    if len(text) <= MAX_PAYLOAD_CHARS:
        return value, False
    return (
        {
            "truncated": True,
            "original_length": len(text),
            "preview": text[:MAX_PAYLOAD_CHARS],
        },
        True,
    )


def _dev_tools_allowed() -> bool:
    """Whether development-only tool bindings may run in this environment."""
    settings = get_settings()
    fn = getattr(settings, "allows_development_tools", None)
    if callable(fn):
        return bool(fn())
    # Fallback for a Settings that predates the ``app_env`` field (default dev).
    env = str(getattr(settings, "app_env", "dev") or "dev").strip().lower()
    return env in ("", "dev", "development")


def _env_name() -> str:
    """The normalised environment name for honest messages (``dev`` fallback)."""
    settings = get_settings()
    fn = getattr(settings, "environment", None)
    if callable(fn):
        try:
            return str(fn())
        except Exception:  # noqa: BLE001 — a label must never break execution
            return "unknown"
    return str(getattr(settings, "app_env", "dev") or "dev").strip().lower() or "dev"


# --------------------------------------------------------------------------- #
# Value objects                                                                #
# --------------------------------------------------------------------------- #
@dataclass
class ToolCallContext:
    """Identity + inputs for one tool call."""

    run_id: str
    step_id: str
    tenant_id: str | None
    user_id: str
    role: str
    intent: str = ""
    attempt: int = 0
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolInvocation:
    """One uniform tool-call record (fields mirror the target architecture)."""

    run_id: str
    step_id: str
    agent_id: str | None
    tool: str
    arguments_hash: str
    tenant_id: str | None
    data_scope: str | None
    policy_decision: str
    approval_id: str | None
    status: str
    executed: bool
    development_stub: bool
    provider: str
    summary: str
    result_ref: str | None
    latency_ms: int
    error: str | None
    started_at: str
    attempt: int = 0
    payload: Any = None
    #: INC12 A5 (additive, default-safe): who triggered this call. A tool
    #: invocation is evidence that must be attributable **on its own** — once a
    #: record leaves the run context (exported to audit, replayed in a review,
    #: joined across runs) an invocation with no actor cannot answer "谁触发的？".
    #: ``ToolCallContext`` already carried both values; ``to_dict()`` simply
    #: never emitted them, so this is information completion, not a new
    #: mechanism. ``None`` keeps every pre-A5 construction site valid.
    actor_user_id: str | None = None
    actor_role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form (persisted on the run + emitted on events)."""
        return {
            "run_id": self.run_id,
            "step_id": self.step_id,
            "agent_id": self.agent_id,
            "tool": self.tool,
            "arguments_hash": self.arguments_hash,
            "tenant_id": self.tenant_id,
            "data_scope": self.data_scope,
            "policy_decision": self.policy_decision,
            "approval_id": self.approval_id,
            "status": self.status,
            "executed": self.executed,
            "development_stub": self.development_stub,
            "provider": self.provider,
            "summary": self.summary,
            "result_ref": self.result_ref,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "started_at": self.started_at,
            "attempt": self.attempt,
            "payload": self.payload,
            # INC12 A5 — the actor, so a single invocation is attributable
            # without having to re-read the run it belongs to.
            "actor_user_id": self.actor_user_id,
            "actor_role": self.actor_role,
        }


# --------------------------------------------------------------------------- #
# The executor                                                                 #
# --------------------------------------------------------------------------- #
class ToolExecutor:
    """Executes one tool call and returns a truthful :class:`ToolInvocation`."""

    async def execute(
        self,
        tool: str,
        *,
        ctx: ToolCallContext,
        policy_decision: str = "not_evaluated",
        approval_id: str | None = None,
    ) -> ToolInvocation:
        """Invoke ``tool`` for ``ctx`` and record exactly what happened.

        See the module docstring for the fixed execution order and the status
        contract. ``policy_decision`` is the real gate verdict for this call
        (``"allow"`` / ``"approval_required"`` / ``"deny"`` / ``"not_evaluated"``)
        and is recorded verbatim — the executor does not re-decide policy.
        """
        started_at = datetime.now(timezone.utc).isoformat()
        arguments_hash = _sha256(_canonical(ctx.args))
        binding = resolve(tool)

        # 1. No implementation ⇒ unavailable. NEVER ok.
        if binding is None:
            return self._record(
                tool=tool,
                ctx=ctx,
                started_at=started_at,
                arguments_hash=arguments_hash,
                policy_decision=policy_decision,
                approval_id=approval_id,
                status="unavailable",
                executed=False,
                development_stub=False,
                provider="none",
                summary=f"工具 '{tool}' 未绑定任何实现",
                result_ref=None,
                latency_ms=0,
                error=f"未绑定任何实现的工具：{tool}",
                payload=None,
            )

        development_stub = binding.kind == "development"

        # 2. Environment gate — development bindings are refused outside dev.
        if development_stub and not _dev_tools_allowed():
            env = _env_name()
            msg = (
                f"环境 {env} 禁止使用开发态工具 '{tool}'"
                f"（provider={binding.provider}），拒绝并如实上报"
            )
            logger.warning("ToolExecutor refused %s in env=%s (provider=%s)", tool, env, binding.provider)
            return self._record(
                tool=tool,
                ctx=ctx,
                started_at=started_at,
                arguments_hash=arguments_hash,
                policy_decision=policy_decision,
                approval_id=approval_id,
                status="refused",
                executed=False,
                development_stub=True,
                provider=binding.provider,
                summary=f"开发态工具被环境闸门拒绝（env={env}）",
                result_ref=None,
                latency_ms=0,
                error=msg,
                payload=None,
            )

        # 3. Call the handler under a timer; never swallow an exception.
        started = time.perf_counter()
        try:
            raw = await binding.handler(dict(ctx.args), ctx)
        except Exception as exc:  # noqa: BLE001 — must be reported, not swallowed
            latency_ms = _elapsed_ms(started)
            logger.warning("ToolExecutor handler %s raised: %s", tool, exc)
            return self._record(
                tool=tool,
                ctx=ctx,
                started_at=started_at,
                arguments_hash=arguments_hash,
                policy_decision=policy_decision,
                approval_id=approval_id,
                status="error",
                executed=True,
                development_stub=development_stub,
                provider=binding.provider,
                summary=f"handler 抛出异常：{exc}",
                result_ref=None,
                latency_ms=latency_ms,
                error=str(exc),
                payload=None,
            )
        latency_ms = _elapsed_ms(started)

        if not isinstance(raw, dict):
            return self._record(
                tool=tool,
                ctx=ctx,
                started_at=started_at,
                arguments_hash=arguments_hash,
                policy_decision=policy_decision,
                approval_id=approval_id,
                status="error",
                executed=True,
                development_stub=development_stub,
                provider=binding.provider,
                summary="handler 返回非 dict 结果",
                result_ref=None,
                latency_ms=latency_ms,
                error="handler returned a non-dict result",
                payload=None,
            )

        dev_stub = bool(raw.get("development_stub", development_stub))
        provider = str(raw.get("provider") or binding.provider)

        # 4. Business failure: skipped (no valid input) vs error (ran but failed).
        if not bool(raw.get("ok")):
            if bool(raw.get("not_executed")):
                reason = str(raw.get("reason") or "无有效输入，未执行")
                bounded, _ = _bound_payload(raw)
                return self._record(
                    tool=tool,
                    ctx=ctx,
                    started_at=started_at,
                    arguments_hash=arguments_hash,
                    policy_decision=policy_decision,
                    approval_id=approval_id,
                    status="skipped",
                    executed=False,
                    development_stub=dev_stub,
                    provider=provider,
                    summary=reason,
                    result_ref=None,
                    latency_ms=latency_ms,
                    error=None,
                    payload=bounded,
                )
            detail = str(raw.get("error") or raw.get("reason") or "工具返回业务失败")
            bounded, _ = _bound_payload(raw)
            return self._record(
                tool=tool,
                ctx=ctx,
                started_at=started_at,
                arguments_hash=arguments_hash,
                policy_decision=policy_decision,
                approval_id=approval_id,
                status="error",
                executed=True,
                development_stub=dev_stub,
                provider=provider,
                summary=detail[:200],
                result_ref=None,
                latency_ms=latency_ms,
                error=detail,
                payload=bounded,
            )

        # 5. Success — the handler really ran and returned a result.
        payload: Any = raw
        if tool in _EXTERNAL_TOOLS:
            # 7. External output must be sanitised (indirect prompt injection).
            payload = sanitize_tool_output(tool, payload)
        payload, _truncated = _bound_payload(payload)

        result_ref = raw.get("result_ref")
        if not isinstance(result_ref, str) or not result_ref:
            result_ref = _sha256(_canonical(payload))[:32]

        summary = str(raw.get("summary") or f"{tool} 已执行（{provider}）")[:200]
        return self._record(
            tool=tool,
            ctx=ctx,
            started_at=started_at,
            arguments_hash=arguments_hash,
            policy_decision=policy_decision,
            approval_id=approval_id,
            status="ok",
            executed=True,
            development_stub=dev_stub,
            provider=provider,
            summary=summary,
            result_ref=result_ref,
            latency_ms=latency_ms,
            error=None,
            payload=payload,
        )

    # -- internals ---------------------------------------------------------- #
    @staticmethod
    def _record(
        *,
        tool: str,
        ctx: ToolCallContext,
        started_at: str,
        arguments_hash: str,
        policy_decision: str,
        approval_id: str | None,
        status: str,
        executed: bool,
        development_stub: bool,
        provider: str,
        summary: str,
        result_ref: str | None,
        latency_ms: int,
        error: str | None,
        payload: Any,
    ) -> ToolInvocation:
        """Assemble the invocation record.

        ``data_scope`` is read from a real source or set to ``None`` — the
        platform has no DataScope module and the gate verdict carries no scope,
        so an honest ``None`` (registered as a gap in the INC12 design doc) is
        used rather than an invented string.
        """
        return ToolInvocation(
            run_id=ctx.run_id,
            step_id=ctx.step_id,
            agent_id=None,
            tool=tool,
            arguments_hash=arguments_hash,
            tenant_id=ctx.tenant_id,
            data_scope=None,
            policy_decision=str(policy_decision or "not_evaluated"),
            approval_id=approval_id,
            status=status,
            executed=executed,
            development_stub=development_stub,
            provider=provider,
            summary=summary[:200],
            result_ref=result_ref,
            latency_ms=int(latency_ms),
            error=error,
            started_at=started_at,
            attempt=int(ctx.attempt or 0),
            payload=payload,
            # INC12 A5 — the actor comes straight off the call context (which is
            # where identity has always lived); recording it here is what lets a
            # single invocation answer "谁触发的？" on its own.
            actor_user_id=ctx.user_id,
            actor_role=ctx.role,
        )


def _elapsed_ms(started: float) -> int:
    """Milliseconds elapsed since ``started`` (a ``perf_counter`` sample)."""
    return int((time.perf_counter() - started) * 1000)
