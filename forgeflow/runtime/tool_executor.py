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
``error`` / ``unavailable`` / ``refused`` / ``blocked`` with a stated reason.

Status semantics (the contract; tests pin every row)
----------------------------------------------------
==============  =========  =========================================  ==========
status          executed   meaning                                    run failed
==============  =========  =========================================  ==========
``ok``          ``True``   the handler really ran and returned a     no
                           result
``error``       ``True``   the handler ran but raised / returned      yes
                           failure
``unavailable`` ``False``  the tool has no implementation at all      yes
``refused``     ``False``  an implementation exists but an environment yes
                           gate refused it (prod must not mock)
``blocked``     ``False``  the step is needed but a required input    no
                           is missing, so the handler was never called
``awaiting_approval`` ``False`` the handler ran but the step is gated on  no
                           a human decision (INC25 ``code.commit``); a
                           non-terminal, non-failure state
==============  =========  =========================================  ==========

INC15 — ``skipped`` is **retired** as a writer status. A handler that reports
``{"ok": False, "not_executed": True}`` (no valid input) is recorded ``blocked``,
never ``skipped``; a caller may also block a step explicitly via
``blocked_reason=``. Readers still accept the legacy ``skipped`` value through
:func:`forgeflow.runtime.planning.normalize_status` (``skipped → blocked``), so a
historical run / export keeps rendering honestly.

Latency contract (INC15): ``latency_ms`` is ``float | None`` — ``None`` means
"never measured" (never a fabricated ``0``); a real measurement keeps
sub-millisecond precision because the timer is ``round((…) * 1000, 3)`` with no
``int()`` truncation.

Execution order (fixed; never reordered):

1. ``resolve(tool)`` — unresolved ⇒ ``unavailable`` (never ``ok``).
2. Environment gate — a ``development`` binding outside ``dev`` ⇒ ``refused``
   (never a silent fallback to a mock).
3. A caller-supplied ``blocked_reason`` ⇒ ``blocked`` **without calling the
   handler** (the step is known to lack its input).
4. Time and call the handler; **any** exception ⇒ ``error`` (never swallowed,
   never downgraded to ``ok``).
5. ``{"ok": False, "not_executed": True}`` ⇒ ``blocked``; any other
   ``{"ok": False}`` ⇒ ``error``.
6. ``{"ok": True}`` ⇒ ``ok``.
7. Compute latency, ``arguments_hash``, ``result_ref`` and a bounded ``summary``.
8. Bound ``payload`` (4000 chars) and, for external (networked) tools, sanitise
   it through :func:`forgeflow.security.tool_output_guard.sanitize_tool_output`.
9. ``data_scope`` from a real source or ``None`` — never invented.
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
#: *unstructured* result is replaced by a bounded ``{"truncated": True, ...}``
#: envelope so a flood of tool output can never bloat an event/record.
MAX_PAYLOAD_CHARS = 4000

#: INC25 P0-A — per-field ceilings for a **code-plane** payload. A code-plane
#: result carries the ``codeplane`` sub-dict (engine / workspace / timeline /
#: tests / diff) that ``RunRecord.codeplane`` is assembled from. The blanket
#: envelope above would replace the *whole* dict and erase that evidence — and the
#: more the engine produced, the larger the payload, the more certain the
#: erasure, so a run that really changed code looked like it did nothing. These
#: caps trim *inside* the codeplane sub-dict instead, keeping every required key
#: present and marking each truncation verbatim.
BOUND_CODE_TIMELINE_MAX_ITEMS = 50
BOUND_CODE_TIMELINE_DETAIL_CHARS = 500
BOUND_CODE_DIFF_CHARS = 2000
BOUND_CODE_TEST_STDOUT_CHARS = 2000

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


def _trim_codeplane(value: Any) -> dict[str, Any] | None:
    """Structurally bound a code-plane payload **without erasing its evidence**.

    INC25 P0-A — a code-plane ``code.execute`` / ``code.commit`` payload carries
    the ``codeplane`` sub-dict (timeline / diff / tests) that
    ``RunRecord.codeplane`` is assembled from. The blanket envelope in
    :func:`_bound_payload` replaced the whole dict, so the more the engine
    produced the more certainly the evidence vanished. This trims *inside* the
    sub-dict instead: the timeline keeps its first N entries (each ``detail``
    capped), the diff keeps its head, the test stdout keeps its head — and every
    truncation is flagged verbatim (``*_truncated`` / ``*_original_length`` /
    ``timeline_omitted``). Every required key stays present.

    Returns ``None`` when ``value`` is not a code-plane payload (the caller then
    uses the honest envelope).
    """
    if not isinstance(value, dict):
        return None
    codeplane = value.get("codeplane")
    if not isinstance(codeplane, dict):
        return None

    trimmed_cp: dict[str, Any] = dict(codeplane)
    cp_truncated = False

    # timeline — keep the first N entries; cap each detail; count the omission.
    timeline = codeplane.get("timeline")
    if isinstance(timeline, list):
        kept: list[Any] = []
        for item in timeline[:BOUND_CODE_TIMELINE_MAX_ITEMS]:
            if isinstance(item, dict):
                entry = dict(item)
                detail = entry.get("detail")
                if isinstance(detail, str) and len(detail) > BOUND_CODE_TIMELINE_DETAIL_CHARS:
                    entry["detail"] = detail[:BOUND_CODE_TIMELINE_DETAIL_CHARS]
                    entry["detail_truncated"] = True
                    entry["detail_original_length"] = len(detail)
                    cp_truncated = True
                kept.append(entry)
            else:
                kept.append(item)
        if len(timeline) > BOUND_CODE_TIMELINE_MAX_ITEMS:
            trimmed_cp["timeline_omitted"] = len(timeline) - BOUND_CODE_TIMELINE_MAX_ITEMS
            cp_truncated = True
        trimmed_cp["timeline"] = kept

    # diff — keep the head, mark the truncation verbatim.
    diff = codeplane.get("diff")
    if isinstance(diff, str) and len(diff) > BOUND_CODE_DIFF_CHARS:
        trimmed_cp["diff"] = diff[:BOUND_CODE_DIFF_CHARS]
        trimmed_cp["diff_truncated"] = True
        trimmed_cp["diff_original_length"] = len(diff)
        cp_truncated = True

    # tests — keep the head of the raw stdout (+ its own marker).
    tests = codeplane.get("tests")
    if isinstance(tests, dict):
        tests_copy = dict(tests)
        raw = tests_copy.get("raw_stdout")
        if isinstance(raw, str) and len(raw) > BOUND_CODE_TEST_STDOUT_CHARS:
            tests_copy["raw_stdout"] = raw[:BOUND_CODE_TEST_STDOUT_CHARS]
            tests_copy["raw_stdout_truncated"] = True
            tests_copy["raw_stdout_original_length"] = len(raw)
            cp_truncated = True
        trimmed_cp["tests"] = tests_copy

    if cp_truncated:
        trimmed_cp["truncated"] = True

    result = dict(value)
    result["codeplane"] = trimmed_cp
    return result


def _bound_payload(value: Any) -> tuple[Any, bool]:
    """Return ``(bounded_value, truncated)`` for a payload.

    ``<= MAX_PAYLOAD_CHARS`` (as JSON) is returned unchanged. A larger
    **code-plane** payload is trimmed *structurally* (see :func:`_trim_codeplane`)
    so its ``codeplane`` evidence survives; any other larger payload becomes a
    compact, self-describing envelope so the truncation is explicit.
    """
    text = _canonical(value)
    if len(text) <= MAX_PAYLOAD_CHARS:
        return value, False
    trimmed = _trim_codeplane(value)
    if trimmed is not None:
        return trimmed, True
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
    #: INC15 — whether the handler was actually *invoked*. ``ok``/``error`` ⇒
    #: ``True``; ``unavailable``/``refused``/``blocked`` ⇒ ``False``. Distinct
    #: from ``executed`` (which is the "produced a real outcome" flag) so a
    #: consumer can tell "the handler ran" apart from "a terminal state was
    #: recorded".
    invoked: bool
    development_stub: bool
    provider: str
    summary: str
    result_ref: str | None
    #: INC15 — milliseconds the handler really took, or ``None`` when the handler
    #: was never measured (blocked / unavailable / refused). Keeps sub-ms
    #: precision (``round(…, 3)``); never ``int()``-truncated, never a fake ``0``.
    latency_ms: float | None
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
            "invoked": self.invoked,
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
        blocked_reason: str | None = None,
    ) -> ToolInvocation:
        """Invoke ``tool`` for ``ctx`` and record exactly what happened.

        See the module docstring for the fixed execution order and the status
        contract. ``policy_decision`` is the real gate verdict for this call
        (``"allow"`` / ``"approval_required"`` / ``"deny"`` / ``"not_evaluated"``)
        and is recorded verbatim — the executor does not re-decide policy.

        ``blocked_reason`` (INC15): when non-empty the step is known to lack a
        required input, so the handler is **never called** and an honest
        ``blocked`` record is returned (``executed=False``, ``invoked=False``,
        ``latency_ms=None``, the reason in ``error``/``summary``). This is how a
        dynamically-planned step that was declared but under-specified stays
        visible without ever being executed.
        """
        started_at = datetime.now(timezone.utc).isoformat()
        arguments_hash = _sha256(_canonical(ctx.args))
        binding = resolve(tool)

        # 3. Caller-declared block — the step is known to lack its input; do NOT
        #    call the handler (an honest ``blocked`` with the stated reason).
        if blocked_reason:
            reason = str(blocked_reason)
            return self._record(
                tool=tool,
                ctx=ctx,
                started_at=started_at,
                arguments_hash=arguments_hash,
                policy_decision=policy_decision,
                approval_id=approval_id,
                status="blocked",
                executed=False,
                invoked=False,
                development_stub=(binding.kind == "development" if binding else False),
                provider=(binding.provider if binding else "none"),
                summary=reason[:200],
                result_ref=None,
                latency_ms=None,
                error=reason,
                payload={"ok": False, "blocked": True, "reason": reason},
            )

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
                invoked=False,
                development_stub=False,
                provider="none",
                summary=f"工具 '{tool}' 未绑定任何实现",
                result_ref=None,
                latency_ms=None,
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
                invoked=False,
                development_stub=True,
                provider=binding.provider,
                summary=f"开发态工具被环境闸门拒绝（env={env}）",
                result_ref=None,
                latency_ms=None,
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
                invoked=True,
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
                invoked=True,
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

        # 4. Business failure: blocked (needed but no valid input) vs error.
        if not bool(raw.get("ok")):
            # INC25 W2 — ``awaiting_approval``: the handler ran but the step is
            # gated on a human decision (``tool_handlers.code_commit``). It is a
            # **non-terminal, non-failure** state: ``executed=False`` (no terminal
            # outcome yet), ``invoked=True`` (the handler really ran), and
            # ``latency_ms=None`` (a duration is only reported for a real outcome).
            # The validator already counts ``awaiting_approval`` as unrun ⇒ the run
            # is honestly ``partial``, and the orchestrator surfaces
            # ``status="awaiting_approval"`` (never "已完成").
            if bool(raw.get("awaiting_approval")):
                reason = str(raw.get("reason") or "等待人工审批")
                bounded, _ = _bound_payload(raw)
                return self._record(
                    tool=tool,
                    ctx=ctx,
                    started_at=started_at,
                    arguments_hash=arguments_hash,
                    policy_decision=policy_decision,
                    approval_id=approval_id,
                    status="awaiting_approval",
                    executed=False,
                    invoked=True,
                    development_stub=dev_stub,
                    provider=provider,
                    summary=reason,
                    result_ref=None,
                    latency_ms=None,
                    error=None,
                    payload=bounded,
                )
            # An explicit capability outage (e.g. the code-execution engine is not
            # installed) is distinct from a generic business error: record the
            # honest ``unavailable`` with the verbatim reason so the read side can
            # tell "the tool has no working implementation right now" apart from
            # "the tool ran and returned bad output".
            if bool(raw.get("unavailable")):
                reason = str(raw.get("reason") or raw.get("error") or "工具当前不可用")
                bounded, _ = _bound_payload(raw)
                return self._record(
                    tool=tool,
                    ctx=ctx,
                    started_at=started_at,
                    arguments_hash=arguments_hash,
                    policy_decision=policy_decision,
                    approval_id=approval_id,
                    status="unavailable",
                    executed=False,
                    invoked=True,
                    development_stub=dev_stub,
                    provider=provider,
                    summary=reason[:200],
                    result_ref=None,
                    latency_ms=None,
                    error=reason,
                    payload=bounded,
                )
            if bool(raw.get("not_executed")) or bool(raw.get("blocked")):
                reason = str(raw.get("reason") or "无有效输入，未执行")
                bounded, _ = _bound_payload(raw)
                return self._record(
                    tool=tool,
                    ctx=ctx,
                    started_at=started_at,
                    arguments_hash=arguments_hash,
                    policy_decision=policy_decision,
                    approval_id=approval_id,
                    status="blocked",
                    executed=False,
                    # The handler *was* entered (that is how it reported
                    # ``not_executed``); ``executed`` stays False because it
                    # produced no real outcome. ``latency_ms`` is ``None``: a
                    # duration is only reported for a real outcome (ok/error).
                    invoked=True,
                    development_stub=dev_stub,
                    provider=provider,
                    summary=reason,
                    result_ref=None,
                    latency_ms=None,
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
                invoked=True,
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
            invoked=True,
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
        invoked: bool,
        development_stub: bool,
        provider: str,
        summary: str,
        result_ref: str | None,
        latency_ms: float | None,
        error: str | None,
        payload: Any,
    ) -> ToolInvocation:
        """Assemble the invocation record.

        ``data_scope`` is read from a real source or set to ``None`` — the
        platform has no DataScope module and the gate verdict carries no scope,
        so an honest ``None`` (registered as a gap in the INC12 design doc) is
        used rather than an invented string. ``latency_ms`` is passed through
        verbatim as a ``float | None`` — ``None`` means "never measured" and is
        never coerced to ``0``.
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
            invoked=invoked,
            development_stub=development_stub,
            provider=provider,
            summary=summary[:200],
            result_ref=result_ref,
            latency_ms=latency_ms,
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


def _elapsed_ms(started: float) -> float:
    """Milliseconds elapsed since ``started`` (a ``perf_counter`` sample).

    INC15 — the value keeps **sub-millisecond precision** (``round(…, 3)``) and
    is never ``int()``-truncated: a sub-ms handler must read e.g. ``0.062``, not
    ``0`` (the truncation that made the report's latency column claim "never
    measured" about a step that was measured).
    """
    return round((time.perf_counter() - started) * 1000, 3)
