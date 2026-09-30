"""OpenHands / runner event → ForgeFlow standard event (INC25 W2, design §6).

The adapter is a **whitelist**: it maps the event kinds the design registers onto
a :class:`CodeEvent` and normalises the ``status`` onto the one shared step-status
vocabulary (:data:`forgeflow.codeplane.protocol.STEP_STATUSES`). Any unregistered
kind becomes ``{phase: "observation", kind: "other", status: "ok"}`` with a
neutral label — it is **never** given an invented cause (AC-22 tail).

Two honesty rules are load-bearing:

  * raw Tool / Action / Observation / model text goes **only** into ``detail`` (the
    collapsed "详细 Trace" block) — never into the visible ``label`` (AC-13);
  * ``latency_ms`` is set only when the runner really measured it; otherwise it is
    ``None`` so the UI renders "—" and never a fabricated ``0`` (AC-14).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forgeflow.codeplane.protocol import STEP_STATUSES, normalize_status
from forgeflow.codeplane.tests_verdict import evaluate_test_output

__all__ = ["CodeEvent", "adapt_openhands_event", "summarize_timeline"]

#: OpenHands ``namespace openhands.sdk.event`` class name → (phase, kind, status).
_OPENHANDS_TYPE_MAP: dict[str, tuple[str, str, str]] = {
    "ConversationStateUpdateEvent": ("action", "state", "running"),
    "ActionEvent": ("action", "action", "running"),
    "ObservationEvent": ("observation", "observation", "ok"),
    "MessageEvent": ("plan", "message", "ok"),
    "HookExecutionEvent": ("observation", "hook", "ok"),
    # INC29 T04 (§2.1) — the SDK's own terminal error events, registered here so a
    # truncation/failure mapping is a first-class value instead of falling through
    # to the unregistered "observation/other/ok" default (which would read as
    # success). This is the ONE extension point: the mapping lives only here; the
    # agent server carries no phase/kind/status vocabulary.
    # ``ConversationErrorEvent`` (e.g. ``code="MaxIterationsReached"``) and
    # ``AgentErrorEvent`` (an ``ObservationBaseEvent`` describing an agent/scaffold
    # error) are the same class of engine-originated error, so they map identically.
    "ConversationErrorEvent": ("error", "engine", "error"),
    "AgentErrorEvent": ("error", "engine", "error"),
}

#: Fallback phase for a kind that carries no phase.
_PHASE_BY_KIND: dict[str, str] = {
    "engine": "engine_ready",
    "state": "action",
    "action": "action",
    "observation": "observation",
    "message": "plan",
    "hook": "observation",
    "test": "test",
    "result": "done",
    "timeout": "done",
    "approval": "done",
    "diff": "diff",
    "other": "observation",
}

#: Business label per (kind, status). The visible line only ever shows one of these.
_LABELS: dict[str, dict[str, str]] = {
    "engine": {
        "ok": "代码执行引擎已就绪",
        "unavailable": "代码执行引擎不可用",
        # INC29 T04 — a registered engine error (``ConversationErrorEvent`` /
        # ``AgentErrorEvent``) must not fall back to the "ok" label ("ready").
        "error": "引擎报告执行错误",
    },
    "state": {"running": "正在执行", "ok": "正在执行"},
    "action": {"running": "正在执行", "ok": "正在执行", "error": "步骤执行失败"},
    "observation": {"ok": "步骤已完成", "error": "步骤产出异常"},
    "message": {"ok": "生成修改方案"},
    "hook": {"ok": "引擎钩子已执行"},
    "test": {"ok": "测试已执行", "error": "测试未通过", "unavailable": "测试未执行"},
    "result": {"ok": "执行完成", "error": "执行异常退出"},
    "timeout": {"error": "执行超时已中止"},
    "approval": {"refused": "已拒绝本次代码变更", "awaiting_approval": "等待人工审批"},
    "diff": {"ok": "已生成代码变更", "not_applicable": "本次无代码变更"},
    "other": {"ok": "（未登记的引擎事件）"},
}

#: Business label for a tool step (visible line; the tool *name* never shows here).
_TOOL_LABELS: dict[str, str] = {
    "terminal": "正在运行命令",
    "bash": "正在运行命令",
    "file_editor": "正在修改文件",
    "str_replace_editor": "正在修改文件",
    "task_tracker": "正在更新任务清单",
    "pytest": "正在运行测试",
}

#: Raw-text keys that belong in ``detail`` (the Trace), never in the visible line.
_DETAIL_KEYS: tuple[str, ...] = (
    "detail",
    "tool_args",
    "tool_input",
    "observation",
    "model_text",
    "content",
    "stdout",
    "stderr",
)


@dataclass
class CodeEvent:
    """One standardised step of a code task's timeline."""

    seq: int = 0
    ts: str = ""
    phase: str = ""
    kind: str = ""
    status: str = "ok"
    tool: str = ""
    label: str = ""
    detail: str = ""
    latency_ms: float | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "phase": self.phase,
            "kind": self.kind,
            "status": self.status,
            "tool": self.tool,
            "label": self.label,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
            "data": dict(self.data),
        }


def _detail_from(raw: dict[str, Any]) -> str:
    """Collect the raw Tool / Action / Observation text into the Trace string."""
    parts: list[str] = []
    for key in _DETAIL_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
        elif isinstance(value, (list, tuple)) and value:
            parts.append("\n".join(str(v) for v in value))
    return "\n".join(parts)


def _latency_from(raw: dict[str, Any]) -> float | None:
    """A real measured latency, or ``None`` — never a fabricated ``0`` (AC-14)."""
    value = raw.get("latency_ms")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _label_for(kind: str, status: str, tool: str) -> str:
    if kind in ("action", "state") and tool in _TOOL_LABELS:
        return _TOOL_LABELS[tool]
    table = _LABELS.get(kind, {})
    if status in table:
        return table[status]
    if "ok" in table:
        return table["ok"]
    return "步骤已更新"


def adapt_openhands_event(raw: dict[str, Any], *, seq: int | None = None) -> CodeEvent:
    """Map one raw runner / OpenHands event onto a :class:`CodeEvent`.

    Explicit ``phase`` / ``kind`` / ``status`` fields win (they are our own
    contract); otherwise the OpenHands ``type`` class name is mapped. The status
    is always normalised onto the shared vocabulary — an unrecognised status
    degrades to ``"ok"`` rather than leaking a foreign token.
    """
    r = dict(raw or {})

    seq_value = seq
    if seq_value is None:
        raw_seq = r.get("seq")
        seq_value = int(raw_seq) if isinstance(raw_seq, (int, float)) and not isinstance(raw_seq, bool) else 0

    phase = str(r.get("phase") or "").strip()
    kind = str(r.get("kind") or "").strip()
    status_in = r.get("status")

    mapped = _OPENHANDS_TYPE_MAP.get(str(r.get("type") or "").strip())
    if mapped is not None:
        phase = phase or mapped[0]
        kind = kind or mapped[1]
        if status_in is None:
            status_in = mapped[2]

    if not kind:
        kind = "other"
    if not phase:
        phase = _PHASE_BY_KIND.get(kind, "observation")

    status = normalize_status(status_in if status_in is not None else "ok")
    if status not in STEP_STATUSES:
        status = "ok"

    tool = str(r.get("tool") or r.get("tool_name") or "").strip()
    label = str(r.get("label") or "").strip() or _label_for(kind, status, tool)
    data = r.get("data") if isinstance(r.get("data"), dict) else {}

    return CodeEvent(
        seq=seq_value,
        ts=str(r.get("ts") or ""),
        phase=phase,
        kind=kind,
        status=status,
        tool=tool,
        label=label,
        detail=_detail_from(r),
        latency_ms=_latency_from(r),
        data=dict(data),
    )


#: The keys the progress summary always carries. ``None`` means **not measured**
#: (never a fabricated ``0``).
SUMMARY_KEYS: tuple[str, ...] = (
    "files_changed",
    "test_command",
    "passed",
    "failed",
    "repair_rounds",
)

#: ``data`` keys an event may use to name the file a file-edit action touched.
_FILE_PATH_KEYS: tuple[str, ...] = ("path", "file_path", "file", "filename")

#: Tools whose action edits a file — the fallback ``files_changed`` source when
#: the run produced no unified diff to count from.
_FILE_EDIT_TOOLS: frozenset[str] = frozenset(
    {"file_editor", "str_replace_editor", "edit_file", "write_file", "apply_patch"}
)

#: Test-event statuses that mean "the test command really executed" — drawn from
#: the ONE shared vocabulary (:data:`forgeflow.codeplane.protocol.STEP_STATUSES`),
#: never a second one (design §6).
_EXECUTED_TEST_STATUSES: frozenset[str] = frozenset(
    status for status in ("ok", "error") if status in STEP_STATUSES
)


def _event_mapping(item: Any) -> dict[str, Any]:
    """Coerce a timeline entry (a ``CodeEvent`` or a mapping) into a plain dict."""
    if isinstance(item, dict):
        return item
    to_dict = getattr(item, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        return value if isinstance(value, dict) else {}
    return {}


def _event_data(ev: dict[str, Any]) -> dict[str, Any]:
    """The event's ``data`` sub-dict (``{}`` when absent / not a mapping)."""
    data = ev.get("data")
    return data if isinstance(data, dict) else {}


def _int_or_none(value: Any) -> int | None:
    """An ``int`` from a real number, else ``None`` (bools are not counts)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _files_from_diff(diff_text: str) -> list[str]:
    """Distinct file paths named by a unified diff's ``diff --git`` / ``+++`` headers."""
    paths: list[str] = []
    for line in (diff_text or "").splitlines():
        path = ""
        if line.startswith("diff --git "):
            rest = line[len("diff --git "):]
            marker = " b/"
            index = rest.rfind(marker)
            path = rest[index + len(marker):] if index != -1 else rest
        elif line.startswith("+++ b/"):
            path = line[len("+++ b/"):]
        path = path.strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def _files_from_timeline(events: list[dict[str, Any]]) -> list[str]:
    """Distinct file paths named by the timeline's file-edit events."""
    paths: list[str] = []
    for ev in events:
        if str(ev.get("tool") or "") not in _FILE_EDIT_TOOLS:
            continue
        data = _event_data(ev)
        for key in _FILE_PATH_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                candidate = value.strip()
                if candidate not in paths:
                    paths.append(candidate)
                break
    return paths


def _diff_from_timeline(events: list[dict[str, Any]]) -> str:
    """The last real diff text carried by a ``diff`` event (``""`` when none)."""
    found = ""
    for ev in events:
        if str(ev.get("kind") or "") != "diff":
            continue
        candidate = _event_data(ev).get("diff")
        if isinstance(candidate, str) and candidate:
            found = candidate
    return found


def summarize_timeline(
    timeline: Any,
    *,
    diff: str | None = None,
    tests: Any = None,
) -> dict[str, Any]:
    """Summarise a code task's timeline into the §6 progress vocabulary.

    Every value comes from **real evidence** — the unified diff and the reviewed
    test verdict. Anything not measured is ``None``, **never** a fabricated ``0``
    (the same honesty rule ``_latency_from`` applies). Test-event statuses are
    read through the one shared vocabulary
    (:data:`forgeflow.codeplane.protocol.STEP_STATUSES`) — no second one is
    introduced.

    Args:
        timeline: the run's standardised events (``CodeEvent`` or mappings).
        diff: the unified diff, when the caller already holds it; otherwise the
            diff is read from the timeline's ``diff`` event.
        tests: the reviewed ``TestResult`` (or its ``to_dict``); otherwise the
            counts are derived from the timeline's ``test`` event through
            :func:`forgeflow.codeplane.tests_verdict.evaluate_test_output`.

    Returns:
        ``{"files_changed": int|None, "test_command": str|None, "passed":
        int|None, "failed": int|None, "repair_rounds": int|None}``.
    """
    events = [_event_mapping(item) for item in (timeline or [])]

    # --- files changed: the diff is authoritative, the timeline its fallback. -- #
    diff_text = diff if isinstance(diff, str) and diff.strip() else _diff_from_timeline(events)
    changed_files = _files_from_diff(diff_text) or _files_from_timeline(events)
    files_changed = len(changed_files) if changed_files else None

    # --- the reviewed verdict, when the caller supplied one. ------------------- #
    tests_map = _event_mapping(tests) if tests is not None else {}
    test_command = str(tests_map.get("command") or "").strip() or None
    passed: int | None = None
    failed: int | None = None
    if bool(tests_map.get("measured")):
        passed = _int_or_none(tests_map.get("passed"))
        failed = _int_or_none(tests_map.get("failed"))

    # --- the timeline's own test evidence: real executions + repair rounds. ---- #
    executed_statuses: list[str] = []
    tl_stdout = ""
    tl_exit: int | None = None
    for ev in events:
        if str(ev.get("kind") or "") != "test":
            continue
        status = normalize_status(str(ev.get("status") or ""))
        if status in _EXECUTED_TEST_STATUSES:
            executed_statuses.append(status)
        data = _event_data(ev)
        if isinstance(data.get("raw_stdout"), str):
            tl_stdout = data["raw_stdout"]
        if data.get("exit_code") is not None:
            tl_exit = _int_or_none(data.get("exit_code"))
        detail = str(ev.get("detail") or "").strip()
        if test_command is None and detail:
            test_command = detail

    # No reviewed verdict, but raw test output on the timeline ⇒ review it here so
    # a caller that passes only a timeline still gets honest counts.
    if passed is None and failed is None and (tl_stdout or tl_exit is not None):
        reviewed = evaluate_test_output(tl_stdout, tl_exit, None, command=test_command or "")
        if reviewed.measured:
            passed = reviewed.passed
            failed = reviewed.failed
            if test_command is None:
                test_command = reviewed.command or None

    # --- repair rounds: a test re-run that follows a failing test. ------------- #
    if executed_statuses:
        repair_rounds = 0
        previous_failed = False
        for index, status in enumerate(executed_statuses):
            if index >= 1 and previous_failed:
                repair_rounds += 1
            previous_failed = status == "error"
    else:
        repair_rounds = None

    return {
        "files_changed": files_changed,
        "test_command": test_command,
        "passed": passed,
        "failed": failed,
        "repair_rounds": repair_rounds,
    }
