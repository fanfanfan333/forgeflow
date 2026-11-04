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

Trace materialisation (INC46 T01, additive): :meth:`ToolExecutor.execute` now
wraps the original body (moved verbatim to :meth:`ToolExecutor._execute`) and,
**after** the invocation is built, hands it to
:func:`forgeflow.runtime.trace_store.persist_invocation` as a strictly
best-effort side effect that writes one ``run_steps`` row. The trace never
changes the returned ``ToolInvocation`` (byte-identical), never fails a run
(errors swallowed), is gated off for the ``memory`` backend, and is
tenant-fail-closed (no tenant ⇒ no write).

Execution order (fixed; never reordered):

0. Circuit breaker — a tool that already failed 3 times consecutively
   in this run is refused without calling its handler (see
   ``ExecutionBudget.circuit_failures``); a success resets the count.
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

import asyncio
import hashlib
import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from forgeflow.config import get_settings
from forgeflow.runtime.tool_registry import resolve
from forgeflow.runtime.trace_store import persist_invocation
from forgeflow.security.tool_output_guard import sanitize_tool_output

#: Tool ids whose handler runs under its own ceiling and must never be cut
#: by the generic per-tool timeout: code.execute has the codeplane wall
#: clock, agent.delegate has the shared run budget (children run model
#: loops), report.render is the terminal step (also breaker-exempt).
_TIMEOUT_EXEMPT_TOOLS = frozenset({"code.execute", "agent.delegate", "report.render"})

#: Prefixes that mark a tool read-only (safe to auto-retry after a
#: transient failure, because the tool cannot have written anything).
_READ_ONLY_TOOL_PREFIXES = (
    "textfile.inspect",
    "document.inspect",
    "sheet.inspect",
    "pdf.inspect",
    "research.",
    "evidence.",
    "skills.",
    "tool_result.",
    "code.lint",
    "analysis.",
)

#: Transient retries after the first attempt (LangGraph RetryPolicy
#: pattern: bounded retries with exponential backoff + jitter, not one
#: immediate retry). The retry itself never consumes extra tool budget
#: — budget.consume("tool") runs once per execute() call.
_TRANSIENT_MAX_RETRIES = 2
_TRANSIENT_BACKOFF_BASE_SECONDS = 0.5


def _is_read_only_tool(tool: str) -> bool:
    return any(tool.startswith(prefix) for prefix in _READ_ONLY_TOOL_PREFIXES)


def _transient_retry_delay(attempt: int) -> float:
    """Backoff for retry number ``attempt`` (0 = first retry)."""
    return _TRANSIENT_BACKOFF_BASE_SECONDS * (2**attempt) + random.uniform(0, 0.25)


async def _sleep(seconds: float) -> None:
    """Indirection so tests can observe or skip retry delays."""
    await asyncio.sleep(seconds)


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

#: Page size for paged tool results. Deliberately below the payload ceiling:
#: a fetched page travels inside the page tool's own result envelope
#: (content + metadata), so a full-ceiling page would exceed the ceiling
#: again and the page fetch itself would be paged. Page 0 of a paged
#: envelope is exactly its ``preview``.
PAGE_CHARS = 3000

#: Process-lifetime store for paged tool results: page_ref -> pages.
#: A page_ref is the sha256 of the result's canonical JSON, so refs are
#: content-addressed and unguessable without the content. Bounded (FIFO)
#: so a flood of large results cannot grow memory without limit. A
#: process restart or eviction makes a ref honestly unresolvable — the
#: page tool reports that, never fabricated content.
_PAGE_STORE_MAX_ENTRIES = 100
_PAGE_STORE: dict[str, list[str]] = {}


def store_paged_result(text: str) -> tuple[str, list[str]]:
    """Split canonical result text into pages, store, return (ref, pages)."""
    pages = [text[i:i + PAGE_CHARS] for i in range(0, len(text), PAGE_CHARS)] or [""]
    ref = _sha256(text)
    if ref not in _PAGE_STORE and len(_PAGE_STORE) >= _PAGE_STORE_MAX_ENTRIES:
        _PAGE_STORE.pop(next(iter(_PAGE_STORE)))
    _PAGE_STORE[ref] = pages
    return ref, pages


def fetch_page(page_ref: str, page: int) -> dict[str, Any]:
    """One page of a stored paged result, or an honest failure."""
    pages = _PAGE_STORE.get(str(page_ref or ""))
    if pages is None:
        return {"ok": False, "reason": "未知的 page_ref：分页结果只在进程内保留，产生它的运行可能已结束或结果已被逐出"}
    if page < 0 or page >= len(pages):
        return {"ok": False, "reason": f"页码超出范围：共 {len(pages)} 页，请求第 {page} 页"}
    return {"ok": True, "page": page, "pages": len(pages), "content": pages[page]}

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
_EXTERNAL_TOOLS: frozenset[str] = frozenset({"research.search", "research.read", "research.verify", "evidence.read"})


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
                data = entry.get("data")
                if isinstance(data, dict) and len(_canonical(data)) > BOUND_CODE_TIMELINE_DETAIL_CHARS:
                    serialized = _canonical(data)
                    entry["data"] = {"truncated": True, "preview": serialized[:BOUND_CODE_TIMELINE_DETAIL_CHARS],
                                     "original_length": len(serialized)}
                    cp_truncated = True
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


def _bound_payload(value: Any, *, tool: str = "") -> tuple[Any, bool]:
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
    # Paged results: keep the preview envelope keys (backward compatible)
    # and add the page handle so the model can fetch the rest instead of
    # losing it. ``preview`` is exactly page 0 (PAGE_CHARS).
    page_ref, pages = store_paged_result(text)
    bounded = {
        "truncated": True,
        "paged": True,
        "page": 0,
        "pages": len(pages),
        "page_size": PAGE_CHARS,
        "page_ref": page_ref,
        "original_length": len(text),
        "preview": text[:PAGE_CHARS],
    }
    if tool == "agent.delegate" and isinstance(value, dict):
        from forgeflow.runtime.acceptance import _delegated_research_evidence

        for entry in _delegated_research_evidence(value):
            evidence = {key: entry.get(key) for key in ("step_id", "tool", "result_ref")}
            if all(isinstance(v, str) and 0 < len(v) <= 512 for v in evidence.values()):
                bounded["results"] = [{"role": "research", "ok": True, "evidence": [evidence]}]
                break
    return bounded, True


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
    runtime_context: Any = None
    task_context: Any = None


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

    async def _pre_approval_gate(
        self, tool: str, ctx: ToolCallContext, policy_decision: str, approval_id: str | None
    ) -> tuple[ToolInvocation | None, str | None]:
        """Phase 4 B-track: pre-execution approval for risky tools.

        Returns ``(invocation, approval_id)``. ``invocation`` is not None
        when the call must NOT execute yet: either it pauses
        (``awaiting_approval`` — the orchestrator turns any such step
        into a paused run, never a completion) or it is refused
        (rejected / spent / gate failure). ``None`` means the call may
        proceed, with ``approval_id`` set to the verified grant (or the
        caller's original value when the tool is not gated).

        Grant verification (all required): the action is a
        ``confirm_permission`` for this tool with the same approved-call
        hash (approval keys stripped, so the staged call and the
        resumed call hash identically); it is RESOLVED-approved;
        its stored owner is the current user; and no ``ok`` record in
        this run already carries its id (success consumes the grant —
        a failed attempt may retry it, a second success may not).
        A rejection stands for identical args in this run (no approval
        spam); an expired grant needs a fresh bare call, never a
        silent re-approval.
        """
        from forgeflow.runtime.approval_policy import (
            requires_pre_approval,
            strip_approval_args,
        )

        if not requires_pre_approval(tool):
            return None, approval_id
        if resolve(tool) is None:
            # Unbound tools never execute at all — they fail closed to
            # ``unavailable`` downstream. The gate must not turn that
            # into an approval round (an approval for a tool with no
            # implementation would be a lie, and it would break the
            # UNBOUND contract pinned by the executor tests).
            return None, approval_id
        staged = strip_approval_args(dict(ctx.args))
        args_hash = _sha256(_canonical(staged))
        started = datetime.now(UTC).isoformat()

        def _gate_record(status: str, reason: str, payload: dict[str, Any]) -> ToolInvocation:
            return self._record(
                tool=tool, ctx=ctx, started_at=started,
                arguments_hash=args_hash, policy_decision=policy_decision,
                approval_id=approval_id, status=status, executed=False, invoked=False,
                development_stub=False, provider="pre_approval_gate", summary=reason,
                result_ref=None, latency_ms=None,
                error=reason if status == "refused" else None,
                payload=payload,
            )

        try:
            from forgeflow.hitl.pending import (
                CONFIRM_PERMISSION,
                RESOLVED,
                WAITING,
                get_pending_store,
                is_expired,
            )

            store = get_pending_store()
            tenant = ctx.tenant_id
            if not tenant:
                raise ValueError("tenant required for pre-approval gate (fail closed)")
            actions = await asyncio.to_thread(store.list, tenant, run_id=ctx.run_id)
        except Exception as exc:  # noqa: BLE001 — fail closed, never execute unverified
            return _gate_record(
                "refused", f"预审批门控不可用，拒绝执行：{exc}",
                {"ok": False, "pre_approval_gate_unavailable": True, "reason": str(exc)},
            ), approval_id

        def _matches(action: Any) -> bool:
            payload = action.payload if isinstance(action.payload, dict) else {}
            return (
                action.kind == CONFIRM_PERMISSION
                and payload.get("tool") == tool
                and payload.get("args_hash") == args_hash
            )

        def _consumed(pending_id: str) -> bool:
            # Success consumes the grant; the run's own records are the
            # audit trail (no schema change). Unverifiable (no record
            # list on this path) allows a presented grant — documented;
            # the react path always carries the list.
            try:
                task_context = getattr(ctx, "task_context", None)
                context = getattr(task_context, "context", None)
                records = context.get("tool_invocations") if isinstance(context, dict) else None
                if not isinstance(records, list):
                    return False
                return any(
                    isinstance(record, dict)
                    and record.get("approval_id") == pending_id
                    and record.get("status") == "ok"
                    for record in records
                )
            except Exception:  # noqa: BLE001 — fail open only on unreadable records
                return False

        def _verified(action: Any) -> tuple[bool, str]:
            payload = action.payload if isinstance(action.payload, dict) else {}
            if action.status != RESOLVED or action.resolution != "approve":
                return False, "批准未通过"
            if not payload.get("owner"):
                return False, "批准缺少 owner 绑定"
            if payload["owner"] != ctx.user_id:
                return False, "批准人与当前用户不一致"
            if _consumed(action.pending_id):
                return False, "批准已使用过，需重新批准"
            return True, ""

        presented = dict(ctx.args).get("pending_id")
        if presented:
            try:
                action = await asyncio.to_thread(store.get, tenant, str(presented))
            except Exception as exc:  # noqa: BLE001 — fail closed
                return _gate_record(
                    "refused", f"预审批验证不可用，拒绝执行：{exc}",
                    {"ok": False, "pre_approval_gate_unavailable": True, "reason": str(exc)},
                ), approval_id
            if action is None or not _matches(action):
                return _gate_record(
                    "refused", "提交的批准与本次调用不匹配（工具或参数已变），需重新批准",
                    {"ok": False, "pre_approval_mismatch": True},
                ), approval_id
            # Anti-replay across runs: a grant belongs to its run. The
            # ONLY cross-run path is a platform resume, marked by the
            # ``_pending_approval`` mapping on the task context (set by
            # POST /pending-actions/{id}/resume, injected as a
            # platform-owned arg). A bare presentation from another run
            # is refused, never auto-accepted.
            if action.run_id != ctx.run_id:
                try:
                    task_context = getattr(ctx, "task_context", None)
                    context = getattr(task_context, "context", None)
                    mapping = context.get("_pending_approval") if isinstance(context, dict) else None
                    resume_carried = isinstance(mapping, dict) and mapping.get(tool) == str(presented)
                except Exception:  # noqa: BLE001 — unreadable mapping is no mapping
                    resume_carried = False
                if not resume_carried:
                    return _gate_record(
                        "refused", "批准属于另一 run，未经恢复流程携带，拒绝执行",
                        {"ok": False, "pre_approval_cross_run": True},
                    ), approval_id
            if action.status == WAITING and not is_expired(datetime.now(UTC), action.expires_at):
                return _gate_record(
                    "awaiting_approval", "该调用仍在等待人工审批；批准前不执行",
                    {"ok": False, "awaiting_approval": True, "pending_id": action.pending_id, "tool": tool},
                ), approval_id
            if action.status == RESOLVED and action.resolution == "reject":
                return _gate_record(
                    "refused", "该调用已被人工拒绝；请更换方案，不要原样重试",
                    {"ok": False, "pre_approval_rejected": True, "pending_id": action.pending_id},
                ), approval_id
            ok, reason = _verified(action)
            if not ok:
                return _gate_record(
                    "refused", f"批准无效，拒绝执行：{reason}",
                    {"ok": False, "pre_approval_invalid": True, "reason": reason},
                ), approval_id
            ctx.args = staged
            return None, str(presented)

        for action in actions:
            if not _matches(action):
                continue
            if action.status == WAITING and not is_expired(datetime.now(UTC), action.expires_at):
                return _gate_record(
                    "awaiting_approval", "该调用已在等待人工审批；批准前不执行",
                    {"ok": False, "awaiting_approval": True, "pending_id": action.pending_id, "tool": tool},
                ), approval_id
            if action.status == RESOLVED and action.resolution == "reject":
                return _gate_record(
                    "refused", "该调用已被人工拒绝；请更换方案，不要原样重试",
                    {"ok": False, "pre_approval_rejected": True, "pending_id": action.pending_id},
                ), approval_id
            if action.status == RESOLVED and action.resolution == "approve":
                ok, reason = _verified(action)
                if ok:
                    ctx.args = staged
                    return None, action.pending_id
                if "已使用过" in reason:
                    return _gate_record(
                        "refused", f"批准无效，拒绝执行：{reason}",
                        {"ok": False, "pre_approval_invalid": True, "reason": reason},
                    ), approval_id
                # An approved-but-unverifiable grant (owner mismatch, ...)
                # falls through to a fresh approval round below.
                continue
        try:
            created = await asyncio.to_thread(
                store.create, tenant, run_id=ctx.run_id, kind=CONFIRM_PERMISSION,
                payload={"tool": tool, "args": staged, "args_hash": args_hash, "owner": ctx.user_id},
            )
        except Exception as exc:  # noqa: BLE001 — fail closed
            return _gate_record(
                "refused", f"预审批创建失败，拒绝执行：{exc}",
                {"ok": False, "pre_approval_gate_unavailable": True, "reason": str(exc)},
            ), approval_id
        return _gate_record(
            "awaiting_approval", "该调用需人工事前批准；已创建等待项，批准前不执行",
            {"ok": False, "awaiting_approval": True, "pending_id": created.pending_id, "tool": tool},
        ), approval_id

    async def execute(
        self,
        tool: str,
        *,
        ctx: ToolCallContext,
        policy_decision: str = "not_evaluated",
        approval_id: str | None = None,
        blocked_reason: str | None = None,
    ) -> ToolInvocation:
        """Invoke ``tool`` and materialise the trace **without changing the result**.

        INC46 T01 — this is the single choke point every runtime tool call flows
        through (all three executor bodies: deterministic / llm / react), and it
        is the only place the trace is written. It delegates the real work to
        :meth:`_execute` and then, **after** the outcome is decided, hands the
        invocation to :func:`forgeflow.runtime.trace_store.persist_invocation`
        as a strictly best-effort side effect. The returned ``ToolInvocation`` is
        the byte-identical object ``_execute`` produced: the trace is an
        enhancement, never a gate, so a trace failure can never alter a run
        (persistence swallows its own errors and is gated/tenant-fail-closed).
        """
        from forgeflow.runtime.execution_budget import current_budget
        from forgeflow.runtime.tool_hooks import run_post_hooks, run_pre_hooks

        # Tool hooks (Claude PreToolUse pattern): a pre hook may deny
        # or rewrite args before anything else runs; a deny is a
        # refused invocation, never an executed one.
        effective_args, deny_reason = await run_pre_hooks(tool, dict(ctx.args))
        if deny_reason is not None:
            invocation = self._record(
                tool=tool, ctx=ctx, started_at=datetime.now(UTC).isoformat(),
                arguments_hash=_sha256(_canonical(ctx.args)), policy_decision=policy_decision,
                approval_id=approval_id, status="refused", executed=False, invoked=False,
                development_stub=False, provider="tool_hook", summary=deny_reason,
                result_ref=None, latency_ms=None, error=deny_reason,
                payload={"ok": False, "hook_denied": True, "reason": deny_reason},
            )
            await _persist_best_effort(invocation, ctx)
            return invocation
        ctx.args = effective_args

        # Phase 4 B-track — pre-execution approval, after pre-hooks (so
        # the gate verifies the final args) but before the breaker and
        # the budget (so an unapproved call consumes neither).
        gate_invocation, approval_id = await self._pre_approval_gate(
            tool, ctx, policy_decision, approval_id
        )
        if gate_invocation is not None:
            await _persist_best_effort(gate_invocation, ctx)
            return gate_invocation

        budget = current_budget.get()
        # Tool circuit breaker (2026-10-31 upgrade round): after 3
        # consecutive failures of one tool in this run — any arguments,
        # any error — the tool is refused without calling its handler
        # again, so a flailing model cannot burn the tool budget on a
        # broken tool. A success resets the count; blocked and
        # awaiting_approval outcomes neither count nor reset (the
        # handler did not fail). report.render is exempt: it is the
        # terminal step, not a tool the model retries. No budget is
        # consumed by a circuit-open refusal, mirroring a hook deny.
        # Without a budget there is no run scope to key on, so the
        # breaker is inert (same as the repeat-failure guard).
        circuit_key = (ctx.tenant_id, ctx.user_id, ctx.run_id, tool)
        if (
            budget is not None
            and tool != "report.render"
            and budget.circuit_failures.get(circuit_key, 0) >= 3
        ):
            reason = (
                f"工具熔断已断开：工具 '{tool}' 在本轮已连续失败 3 次，"
                "不再重复调用；请修正输入或改用其他工具，不要重试该工具"
            )
            invocation = self._record(
                tool=tool, ctx=ctx, started_at=datetime.now(UTC).isoformat(),
                arguments_hash=_sha256(_canonical(ctx.args)), policy_decision=policy_decision,
                approval_id=approval_id, status="error", executed=False, invoked=False,
                development_stub=False, provider="circuit_breaker", summary=reason,
                result_ref=None, latency_ms=None, error=reason,
                payload={"ok": False, "circuit_open": True, "tool": tool},
            )
            await run_post_hooks("post_failure", tool, dict(ctx.args), invocation)
            await _persist_best_effort(invocation, ctx)
            return invocation
        if budget is not None and tool != "report.render":
            budget.consume("tool")
        scope = (ctx.tenant_id, ctx.user_id, ctx.run_id)
        evidence = budget.evidence_receipts.setdefault(scope, set()) if budget else set()
        failure_key = (*scope, tool, _sha256(_canonical(ctx.args)), tuple(sorted(evidence)))
        previous = budget.failure_receipts.get(failure_key) if budget else None
        if previous and previous[1] >= 2 and not blocked_reason:
            reason = "重复失败已停止：参数、错误及已读取证据未变化，请修改策略或重新读取变化后的输入"
            invocation = self._record(
                tool=tool, ctx=ctx, started_at=datetime.now(UTC).isoformat(),
                arguments_hash=_sha256(_canonical(ctx.args)), policy_decision=policy_decision,
                approval_id=approval_id, status="error", executed=False, invoked=False,
                development_stub=False, provider="retry_guard", summary=reason,
                result_ref=None, latency_ms=None, error=reason,
                payload={"ok": False, "repeat_failure": True},
            )
        else:
            invocation = await self._execute_with_transient_retry(
                tool,
                ctx=ctx,
                policy_decision=policy_decision,
                approval_id=approval_id,
                blocked_reason=blocked_reason,
            )
            if budget and invocation.status in {"error", "unavailable", "refused"}:
                error_hash = _sha256(invocation.error or invocation.summary)
                count = previous[1] + 1 if previous and previous[0] == error_hash else 1
                budget.failure_receipts[failure_key] = (error_hash, count)
                budget.circuit_failures[circuit_key] = budget.circuit_failures.get(circuit_key, 0) + 1
            elif budget and invocation.status == "outcome_unknown":
                # Dispatch may have happened — do not replay, but do
                # count it: further calls are exactly what must stop.
                budget.circuit_failures[circuit_key] = budget.circuit_failures.get(circuit_key, 0) + 1
            elif budget and invocation.status == "ok" and invocation.executed:
                evidence.add(_sha256(_canonical([tool, ctx.args, invocation.payload])))
                budget.circuit_failures.pop(circuit_key, None)
        if invocation.status == "ok":
            await run_post_hooks("post", tool, dict(ctx.args), invocation)
        else:
            await run_post_hooks("post_failure", tool, dict(ctx.args), invocation)
        await _persist_best_effort(invocation, ctx)
        return invocation

    async def _execute_with_transient_retry(
        self, tool: str, *, ctx: Any, policy_decision: str, approval_id: str | None, blocked_reason: str | None
    ) -> Any:
        """Automatic retries for transient failures on read-only tools.

        A handler exception used to become one ``error`` invocation and
        the model had to decide to re-invoke — and if it repeated the
        identical call it hit the repeat-failure guard instead. A
        transient failure (timeout / 429 / 5xx / connection) on a
        read-only tool (inspect / read / search / verify / lint /
        profile) is safe to retry, because the tool cannot have
        written anything; write tools are never auto-retried, so a
        failed edit/commit is never applied twice by the platform.
        Retries are bounded (two) with exponential backoff + jitter
        instead of one immediate retry, so a flapping backend gets a
        real chance to recover without hammering it.
        """
        invocation = await self._execute(
            tool, ctx=ctx, policy_decision=policy_decision, approval_id=approval_id, blocked_reason=blocked_reason
        )
        if invocation.status != "error" or not _is_read_only_tool(tool):
            return invocation
        for attempt in range(_TRANSIENT_MAX_RETRIES):
            message = f"{invocation.error or ''} {invocation.summary or ''}".lower()
            transient = any(
                token in message
                for token in ("timeout", "timed out", "超时", "temporar", "429", "502", "503", "504", "connection", "rate limit", "unavailable")
            )
            if not transient:
                return invocation
            await _sleep(_transient_retry_delay(attempt))
            retry = await self._execute(
                tool, ctx=ctx, policy_decision=policy_decision, approval_id=approval_id, blocked_reason=blocked_reason
            )
            retry.attempt = max(retry.attempt, invocation.attempt + 1)
            invocation = retry
            if invocation.status != "error":
                return invocation
        return invocation

    async def _execute(
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
        started_at = datetime.now(UTC).isoformat()
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
        # A per-tool wall clock (LangGraph TimeoutPolicy pattern): a hung
        # tool used to stall the whole run until the 600s run budget
        # fired. Exempt tools run under their own ceilings (see above).
        started = time.perf_counter()
        timeout: float | None = None
        if tool not in _TIMEOUT_EXEMPT_TOOLS:
            try:
                timeout = float(get_settings().tool_timeout_seconds)
            except Exception:  # noqa: BLE001 — a bad setting must not break execution
                timeout = None
        try:
            if timeout:
                try:
                    raw = await asyncio.wait_for(binding.handler(dict(ctx.args), ctx), timeout=timeout)
                except TimeoutError:
                    # A timeout on a read-only tool is a retryable error
                    # (the tool cannot have written anything); on a
                    # write tool the outcome is genuinely unknown — the
                    # write may have landed — so it is outcome_unknown
                    # and never auto-retried.
                    read_only = _is_read_only_tool(tool)
                    return self._record(
                        tool=tool,
                        ctx=ctx,
                        started_at=started_at,
                        arguments_hash=arguments_hash,
                        policy_decision=policy_decision,
                        approval_id=approval_id,
                        status="error" if read_only else "outcome_unknown",
                        executed=read_only,
                        invoked=True,
                        development_stub=development_stub,
                        provider=binding.provider,
                        summary=f"工具 '{tool}' 执行超时（timeout, 已超过 {timeout:g} 秒）",
                        result_ref=None,
                        latency_ms=_elapsed_ms(started),
                        error=f"工具 '{tool}' 执行超时（timeout, 已超过 {timeout:g} 秒）",
                        payload={"ok": False, "timeout": True, "timeout_seconds": timeout},
                    )
            else:
                raw = await binding.handler(dict(ctx.args), ctx)
        except Exception as exc:  # noqa: BLE001 — must be reported, not swallowed
            from forgeflow.mcp.client.sessions import OutcomeUnknown
            unknown = isinstance(exc, OutcomeUnknown)
            latency_ms = _elapsed_ms(started)
            logger.warning("ToolExecutor handler %s raised: %s", tool, exc)
            return self._record(
                tool=tool,
                ctx=ctx,
                started_at=started_at,
                arguments_hash=arguments_hash,
                policy_decision=policy_decision,
                approval_id=approval_id,
                status="outcome_unknown" if unknown else "error",
                executed=not unknown,
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
                bounded, _ = _bound_payload(raw, tool=tool)
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
                bounded, _ = _bound_payload(raw, tool=tool)
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
                bounded, _ = _bound_payload(raw, tool=tool)
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
            bounded, _ = _bound_payload(raw, tool=tool)
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
            guard_content = {"title": raw.get("title"), "content": raw.get("content")} if tool == "research.read" else payload
            guarded = sanitize_tool_output(tool, guard_content)
            if tool in {"research.read", "research.verify", "evidence.read"}:
                # Keep server receipts typed; only source content is untrusted model text.
                receipt_keys = ("ok", "source", "requested_url", "source_ref", "source_hash", "retrieved_at", "validation", "artifact_ref", "result_ref", "format", "filename", "evidence_ref", "content_hash", "offset", "next_offset", "total_chars", "query_found", "passage_id", "excerpt_start", "excerpt_end")
                payload = {key: raw[key] for key in receipt_keys if key in raw}
                payload["untrusted_content"] = guarded
            else:
                payload = guarded
        full_payload = payload
        payload, _truncated = _bound_payload(payload, tool=tool)
        if _truncated and ctx.tenant_id and ctx.user_id != "anonymous":
            from forgeflow.runtime.professional_tools import store_evidence

            try:
                evidence = store_evidence(full_payload, ctx)
                if isinstance(payload, dict):
                    payload = {**payload, **evidence}
                    for key in ("passage_id", "excerpt_start", "excerpt_end", "source", "requested_url", "source_ref", "source_hash", "retrieved_at", "validation", "artifact_ref", "format", "filename", "skill_id", "version", "skill_content_hash", "resource_hash", "ref", "script_executed"):
                        if isinstance(full_payload, dict) and key in full_payload:
                            payload[key] = full_payload[key]
            except (OSError, ValueError, PermissionError) as exc:
                logger.warning("Tool output could not be externalized: %s", exc)

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
        if payload is None and status == "error" and invoked:
            payload = {"ok": False, "status": status, "reason": str(error or summary)[:1000]}
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


async def _persist_best_effort(invocation: ToolInvocation, ctx: ToolCallContext) -> None:
    """Best-effort materialisation of one invocation into ``run_steps`` (INC46 T01).

    The trace is an enhancement, never a gate. Any failure here — no active
    store, an import/runtime error, a database blip — is swallowed and logged
    verbatim, and the caller still receives the byte-identical
    ``ToolInvocation`` (:func:`persist_invocation` also swallows/targets
    fail-closed internally; this outer guard is defence in depth so even an
    unexpected error can never disturb a run).
    """
    try:
        await persist_invocation(
            invocation.to_dict(),
            args=dict(ctx.args or {}),
            run_id=ctx.run_id,
            tenant_id=ctx.tenant_id,
            step_id=ctx.step_id,
        )
    except Exception:  # noqa: BLE001 — the trace must never break a run
        logger.warning("INC46 trace persistence skipped (best-effort)", exc_info=True)
