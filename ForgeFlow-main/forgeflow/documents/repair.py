"""INC46 T24 — 修复循环与失败路径（Repair & Escalation）.

The contract (task book §「修复循环与失败路径」)
------------------------------------------------
A produced document that **fails validation** is never a deliverable. This module
closes the loop between the T23 five-layer validation stack and the T25 escalation
surface:

  1. a failing verdict is **diagnosed** per failing layer;
  2. a **bounded** repair is applied to the candidate bytes (≤ :data:`MAX_ROUNDS`);
  3. the candidate is **re-validated**;
  4. the per-round **failure-item count must be non-increasing** (monotonic) — the
     moment a round makes things worse the loop **stops early** (防振荡);
  5. still failing after the budget ⇒ an **honest failure report** (verbatim
     evidence + failing layer + attempts + suggested user action) and a run status
     of :data:`forgeflow.documents.status.FAILED_VALIDATION` — **never** a
     "successful" artifact.

Repairable classes (spec §1)
----------------------------
The default :func:`default_editor` really edits the bytes (python-docx / the
``docx_edit`` writer primitives), driven by the diagnosis:

* **格式丢失（恢复 run 样式）** — a paragraph edited *inside* the range lost its
  leading-run formatting ⇒ copy the original run's ``rPr`` back onto the edited
  run (``restore_run_style``);
* **不变量被改（回填）** — an explicit invariant (amount / date / …) that had to
  survive inside the range was dropped ⇒ restore the paragraph's original text
  (``backfill_paragraph``);
* **区间外改动（回滚）** — a paragraph *outside* the located range changed ⇒
  restore it wholesale from the original (``rollback_paragraph``).

Unrepairable (spec §2): a **structural** failure (L1) — the candidate is not a
readable / integral OOXML package — is **not** auto-repairable; the loop refuses
to spin and reports the failure immediately (``rounds_used == 0``).

Honesty (red line 15 / 4, red line 18)
--------------------------------------
* An unmeasured layer (``status is None``) is **never** folded into a ``pass``:
  the loop's success predicate follows the stack's aggregate ``overall`` only, and
  a ``None`` layer never becomes a success claim (it stays surfaced in
  ``unmeasured``).
* A failed document is **never** returned as a success: :class:`RepairOutcome`
  carries **no** document bytes, so a failure can never hand back an artifact.
* The loop is an Agent loop with a **budget and a termination condition** (red
  line 18): ``max_rounds`` (default :data:`MAX_ROUNDS` = 3) plus the monotonic
  guard.

「每轮失败项数」的定义（明确定义，见 :func:`failure_count`）
------------------------------------------------------------
``failure_count(verdict)`` = 对每个 ``status == "fail"`` 的层，累加其 ``detail`` 中
**可数**的失败条目：

* **L2** = ``len(lost) + len(out_of_range_changes) + (1 if heading_delta else 0)
  + len(new_numbers) + len(table_changes)``；
* **L3** = ``len(out_of_range_changes) + len(style_inheritance_failures)``；
* **L1** = ``len(detail["problems"])``（缺省 ``1``）；
* **L4 / L5** = ``1``（像素差 / 低分是一个整体失败，不细分）。

未测量层（``None``）与 ``needs_review`` 层**不计入**失败项数（它们不是 ``fail``；
前者是「没测到」，后者是「建议」）。
"""

from __future__ import annotations

import copy
import io
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from docx.oxml.ns import qn

from forgeflow.documents.docx_inspect import DocxInspectionError, open_docx
from forgeflow.documents.status import FAILED_VALIDATION
from forgeflow.documents.validation.verdict import (
    FAIL,
    LAYER_FORMAT,
    LAYER_INVARIANTS,
    LAYER_ORDER,
    LAYER_RENDER,
    LAYER_STRUCTURAL,
    PASS,
    ValidationVerdict,
)

__all__ = [
    "MAX_ROUNDS",
    "RepairAction",
    "RepairDiagnosis",
    "RepairAttempt",
    "RepairOutcome",
    "RepairEditor",
    "RepairVerifier",
    "TraceSink",
    "failure_count",
    "diagnose",
    "default_editor",
    "make_default_verifier",
    "repair_document",
]

#: The hard bound on repair rounds (task book §3: 「轮数 ≤ 3」). Red line 18: the
#: Agent loop must have a budget — this is it.
MAX_ROUNDS: int = 3

_RE_PARA_TOKEN = re.compile(r"paragraph\[([^\]]+)\]")


# --------------------------------------------------------------------------- #
# injected protocols                                                           #
# --------------------------------------------------------------------------- #
@dataclass
class RepairAction:
    """One concrete repair step: an ``op`` on a paragraph ``target`` token.

    ``target`` is the **verbatim** token the diagnosis read off the verdict, e.g.
    ``"paragraph[2]"`` or ``"paragraph[tail-1]"`` — the editor resolves it against
    the real paragraph lists so the diagnosis never guesses an index.
    """

    op: str
    target: str

    def label(self) -> str:
        """A human-readable label recorded in :attr:`RepairAttempt.actions`."""
        return f"{self.op}({self.target})"


@dataclass
class RepairDiagnosis:
    """The structured outcome of diagnosing a failing verdict.

    ``repairable`` is ``False`` when no concrete, locatable action exists (a
    structural failure, an unlocatable defect, a render-only pixel diff) — the
    loop then stops **immediately** rather than spinning (spec §2).
    """

    layer: str | None
    kind: str
    summary: str
    actions: list[RepairAction] = field(default_factory=list)
    repairable: bool = False


@runtime_checkable
class RepairEditor(Protocol):
    """``(before, after, diagnosis) -> repaired_bytes | None``.

    ``None`` means "could not apply any repair" (treated as unrepairable).
    The default is :func:`default_editor` (real python-docx editing).
    """

    def __call__(
        self, before: bytes, after: bytes, diagnosis: RepairDiagnosis
    ) -> bytes | None: ...


@runtime_checkable
class RepairVerifier(Protocol):
    """``(before, after) -> ValidationVerdict`` (re-runs the real stack)."""

    def __call__(self, before: bytes, after: bytes) -> ValidationVerdict: ...


#: A trace sink records **one row per repair round** (run_steps-shaped dict).
#: The default is ``None`` ⇒ an honest no-op (see :func:`repair_document`).
TraceSink = Callable[[dict[str, Any]], None]


# --------------------------------------------------------------------------- #
# result data model                                                            #
# --------------------------------------------------------------------------- #
@dataclass
class RepairAttempt:
    """One executed repair round (a measured fact — never hidden).

    ``failure_count`` is the number of measured failure items **after** this
    round's repair + re-validation (see :func:`failure_count`); ``round`` is
    1-based; ``diagnosis`` is the human-readable summary; ``actions`` are the
    concrete repairs applied this round.
    """

    round: int
    failure_count: int
    actions: list[str]
    diagnosis: str


@dataclass
class RepairOutcome:
    """The loop's result. **Carries no document bytes** — a failure can never hand
    back an artifact (red line 15).

    ``status`` ∈ ``{"pass", "failed_validation"}`` (``failed_validation`` ==
    :data:`forgeflow.documents.status.FAILED_VALIDATION`). ``failure_report`` is the
    honest, human-readable escalation text (``None`` when ``status == "pass"``).
    ``failed_layer`` is the first hard-failing layer (``None`` when none / advisory).
    """

    status: str
    attempts: list[RepairAttempt] = field(default_factory=list)
    failure_report: str | None = None
    failed_layer: str | None = None
    rounds_used: int = 0


# --------------------------------------------------------------------------- #
# failure counting (the load-bearing definition)                               #
# --------------------------------------------------------------------------- #
def _layer_failure_items(layer: str, detail: dict[str, Any]) -> int:
    """The number of **measured** failure items inside one failing layer."""
    if layer == LAYER_INVARIANTS:
        return (
            len(detail.get("lost", []) or [])
            + len(detail.get("out_of_range_changes", []) or [])
            + (1 if detail.get("heading_delta") else 0)
            + len(detail.get("new_numbers", []) or [])
            + len(detail.get("table_changes", []) or [])
        )
    if layer == LAYER_FORMAT:
        return len(detail.get("out_of_range_changes", []) or []) + len(
            detail.get("style_inheritance_failures", []) or []
        )
    if layer == LAYER_STRUCTURAL:
        problems = detail.get("problems")
        if isinstance(problems, list) and problems:
            return len(problems)
        return 1
    # L4 (pixel diff) / L5 (semantic) are whole-layer failures; never subdivided.
    return 1


def failure_count(verdict: ValidationVerdict) -> int:
    """Total measured failure items across every ``fail`` layer (see module doc)."""
    total = 0
    for name in LAYER_ORDER:
        layer_verdict = verdict.layers[name]
        if layer_verdict.status != FAIL:
            continue
        total += _layer_failure_items(name, layer_verdict.detail or {})
    return total


def _first_failed_layer(verdict: ValidationVerdict) -> str | None:
    """The first hard-failing layer in evaluation order (``None`` if none)."""
    for name in LAYER_ORDER:
        if verdict.layers[name].status == FAIL:
            return name
    return None


def is_pass(verdict: ValidationVerdict) -> bool:
    """Success predicate — follows the stack's aggregate ``overall`` only.

    An unmeasured layer (``status is None``) is **never** treated as a pass and
    never removed from ``unmeasured``; the stack's own aggregation already refuses
    to upgrade a mechanical ``fail`` (or an L5 ``needs_review``) into a ``pass``
    (red line 15). The repair loop therefore never rewrites a ``None`` into
    success.
    """
    return verdict.overall == PASS


# --------------------------------------------------------------------------- #
# diagnosis                                                                    #
# --------------------------------------------------------------------------- #
def _paragraph_token(text: str) -> str | None:
    """Extract the ``paragraph[…]`` token from a failure item (e.g. L2 ``lost``)."""
    match = _RE_PARA_TOKEN.search(text)
    return match.group(0) if match else None


def _dedupe_actions(actions: list[RepairAction]) -> list[RepairAction]:
    """One strongest op per target: ``rollback`` > ``backfill`` > ``restore``."""
    priority = {"rollback_paragraph": 0, "backfill_paragraph": 1, "restore_run_style": 2}
    best: dict[str, RepairAction] = {}
    for action in actions:
        current = best.get(action.target)
        if current is None or priority.get(action.op, 9) < priority.get(current.op, 9):
            best[action.target] = action
    return list(best.values())


def diagnose(verdict: ValidationVerdict) -> RepairDiagnosis:
    """Diagnose a failing verdict into concrete repair actions (or «unrepairable»).

    A structural (L1) failure is **unrepairable** — the candidate is not a
    readable / integral package, so there is no locatable target to fix (spec §2).
    L3 / L2 failures map to :func:`default_editor`'s three repairable ops.
    """
    failing = [name for name in LAYER_ORDER if verdict.layers[name].status == FAIL]

    if LAYER_STRUCTURAL in failing:
        evidence = verdict.layers[LAYER_STRUCTURAL].evidence_ref
        return RepairDiagnosis(
            LAYER_STRUCTURAL,
            "structural-unrepairable",
            f"结构层失败，无法定位可修复目标：{evidence}",
            [],
            False,
        )

    actions: list[RepairAction] = []
    if LAYER_FORMAT in failing:
        detail = verdict.layers[LAYER_FORMAT].detail or {}
        for token in detail.get("style_inheritance_failures", []) or []:
            actions.append(RepairAction("restore_run_style", token))
        for token in detail.get("out_of_range_changes", []) or []:
            actions.append(RepairAction("rollback_paragraph", token))

    if LAYER_INVARIANTS in failing:
        detail = verdict.layers[LAYER_INVARIANTS].detail or {}
        for item in detail.get("lost", []) or []:
            token = _paragraph_token(item)
            if token:
                actions.append(RepairAction("backfill_paragraph", token))
        for token in detail.get("out_of_range_changes", []) or []:
            actions.append(RepairAction("rollback_paragraph", token))

    if LAYER_RENDER in failing and not actions:
        return RepairDiagnosis(
            LAYER_RENDER,
            "render-unrepairable",
            "渲染像素差异无法自动定位到可修复目标",
            [],
            False,
        )

    actions = _dedupe_actions(actions)
    if not actions:
        return RepairDiagnosis(
            failing[0] if failing else None,
            "no-repairable-action",
            "无匹配的可修复动作（无法定位缺陷）",
            [],
            False,
        )

    summary = "；".join(action.label() for action in actions)
    return RepairDiagnosis(
        _first_failed_layer(verdict),
        "repair",
        summary,
        actions,
        True,
    )


# --------------------------------------------------------------------------- #
# default editor (real python-docx editing)                                    #
# --------------------------------------------------------------------------- #
def _resolve_index(
    target: str, n_before: int, n_after: int
) -> int | None:
    """Resolve a ``paragraph[i]`` / ``paragraph[tail-N]`` token to an index.

    ``tail-N`` is the mirrored-from-the-end anchor both L2 and L3 use for the
    untouched tail; it resolves against ``after``'s length.
    """
    match = _RE_PARA_TOKEN.search(target)
    if match is None:
        return None
    inner = match.group(1)
    if inner.startswith("tail-"):
        try:
            return max(0, n_after - int(inner[5:]))
        except ValueError:
            return None
    try:
        return int(inner)
    except ValueError:
        return None


def _restore_run_style(before_para: Any, after_para: Any) -> bool:
    """Copy the leading run's ``rPr`` from ``before`` onto ``after`` (格式恢复)."""
    before_runs = list(before_para.runs)
    after_runs = list(after_para.runs)
    if not before_runs or not after_runs:
        return False
    before_rpr = before_runs[0]._r.find(qn("w:rPr"))
    after_run = after_runs[0]._r
    after_rpr = after_run.find(qn("w:rPr"))
    if before_rpr is not None:
        replacement = copy.deepcopy(before_rpr)
        if after_rpr is not None:
            after_run.replace(after_rpr, replacement)
        else:
            after_run.insert(0, replacement)
        return True
    if after_rpr is not None:  # before had no formatting ⇒ drop the added rPr
        after_run.remove(after_rpr)
        return True
    return False


def _rollback_paragraph(before_para: Any, after_para: Any) -> bool:
    """Replace ``after``'s paragraph element with ``before``'s (区间外回滚)."""
    element = after_para._p
    parent = element.getparent()
    if parent is None:
        return False
    parent.replace(element, copy.deepcopy(before_para._p))
    return True


def _backfill_paragraph(before_para: Any, after_para: Any) -> bool:
    """Restore ``before``'s paragraph text into ``after`` (不变量回填).

    Reuses the real ``docx_edit`` whole-paragraph writer (``_set_text_preserving``)
    so the run formatting / paragraph style are preserved exactly as the edit
    engine would — the lost invariant's text comes back without forking the writer.
    """
    if before_para.text == after_para.text:
        return False
    from forgeflow.documents.docx_edit import _set_text_preserving

    _set_text_preserving(after_para, before_para.text)
    return True


def default_editor(
    before: bytes, after: bytes, diagnosis: RepairDiagnosis
) -> bytes | None:
    """Apply ``diagnosis`` to ``after`` with real python-docx editing.

    Returns the repaired bytes, or ``None`` when **nothing** could be applied
    (which the loop treats as unrepairable — it never spins on a no-op).
    """
    try:
        before_doc = open_docx(before)
        after_doc = open_docx(after)
    except DocxInspectionError:
        return None

    before_paras = list(before_doc.paragraphs)
    after_paras = list(after_doc.paragraphs)
    n_before = len(before_paras)
    n_after = len(after_paras)

    changed = False
    for action in diagnosis.actions:
        index = _resolve_index(action.target, n_before, n_after)
        if index is None or index < 0 or index >= n_before or index >= n_after:
            continue
        before_para = before_paras[index]
        after_para = after_paras[index]
        if action.op == "restore_run_style":
            applied = _restore_run_style(before_para, after_para)
        elif action.op == "rollback_paragraph":
            applied = _rollback_paragraph(before_para, after_para)
        elif action.op == "backfill_paragraph":
            applied = _backfill_paragraph(before_para, after_para)
        else:
            applied = False
        changed = changed or applied

    if not changed:
        return None
    buffer = io.BytesIO()
    after_doc.save(buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# default verifier (re-runs the real T23 stack, same range)                     #
# --------------------------------------------------------------------------- #
def _range_from_verdict(verdict: ValidationVerdict) -> tuple[int, int | None]:
    """Recover the validated ``[start, end)`` range from the verdict's L3 detail."""
    layer = verdict.layers.get(LAYER_FORMAT)
    if layer is not None:
        rng = (layer.detail or {}).get("range")
        if isinstance(rng, (list, tuple)) and len(rng) == 2:
            start = int(rng[0])
            end = int(rng[1]) if rng[1] is not None else None
            return start, end
    return 0, None


def make_default_verifier(initial_verdict: ValidationVerdict) -> RepairVerifier:
    """Build the default verifier: the real stack over the same ``[start, end)``.

    The range is recovered from the initial verdict's L3 ``detail["range"]`` (the
    range the caller validated against), so a re-validation compares the same
    in-range / out-of-range split. When L3 was unmeasured the whole document is
    used — never a guessed sub-range.
    """
    start, end = _range_from_verdict(initial_verdict)

    def _verify(before: bytes, after: bytes) -> ValidationVerdict:
        from forgeflow.documents.validation.stack import validate_document

        return validate_document(before, after, start=start, end=end)

    return _verify


# --------------------------------------------------------------------------- #
# counterfactual seams (mirror the T23 ``_aggregate`` hook)                     #
# --------------------------------------------------------------------------- #
def _effective_max_rounds(max_rounds: int) -> int:
    """The loop bound (the counterfactual seam for 「去掉轮数上限」)."""
    return int(max_rounds)


def _oscillation_violated(previous: int, current: int) -> bool:
    """True iff this round made things worse (the 「单调不增」 guard)."""
    return current > previous


# --------------------------------------------------------------------------- #
# the loop                                                                     #
# --------------------------------------------------------------------------- #
def _emit(
    trace_sink: TraceSink | None,
    run_id: str | None,
    round_index: int,
    diagnosis: RepairDiagnosis,
    verdict: ValidationVerdict,
    count: int,
) -> None:
    """Record one run_steps-shaped row for this round (no-op when no sink)."""
    if trace_sink is None:
        return
    trace_sink(
        {
            "step_type": "repair",
            "step_index": round_index - 1,
            "round": round_index,
            "run_id": run_id,
            "diagnosis": diagnosis.summary,
            "actions": [action.label() for action in diagnosis.actions],
            "failure_count": count,
            "status": verdict.overall,
            "overall": verdict.overall,
            "failed_layer": _first_failed_layer(verdict),
            "artifact_ref": None,
        }
    )


def repair_document(
    before_bytes: bytes,
    after_bytes: bytes,
    verdict: ValidationVerdict,
    *,
    editor: RepairEditor | None = None,
    verifier: RepairVerifier | None = None,
    max_rounds: int = MAX_ROUNDS,
    trace_sink: TraceSink | None = None,
    run_id: str | None = None,
) -> RepairOutcome:
    """Diagnose → repair → re-validate, bounded by ``max_rounds`` (spec §1–§4).

    Args:
        before_bytes: the original document bytes.
        after_bytes: the candidate bytes that produced ``verdict``.
        verdict: the initial (failing) verdict from the T23 stack.
        editor: injectable ``(before, after, diagnosis) -> bytes | None``; default
            :func:`default_editor` (real python-docx repair).
        verifier: injectable ``(before, after) -> ValidationVerdict``; default
            :func:`make_default_verifier` (real T23 stack over the same range).
        max_rounds: the hard round budget (default :data:`MAX_ROUNDS` = 3).
        trace_sink: optional callable receiving one run_steps-shaped dict **per
            round**. Default ``None`` = **honest no-op**: this is a pure function
            with no ``run_id`` / ``tenant_id`` context, so there is nothing
            attributable to persist here; the caller (which owns the run context)
            injects a sink that writes through
            :mod:`forgeflow.runtime.trace_store`. Tests inject a recording sink to
            assert one row per round.
        run_id: optional run id carried into the trace rows (never fabricated).

    Returns:
        A :class:`RepairOutcome`. ``status`` is ``"pass"`` iff the candidate (after
        ≤ ``max_rounds`` repairs) re-validated to an aggregate ``pass``; otherwise
        ``"failed_validation"`` with an honest :attr:`RepairOutcome.failure_report`.
    """
    if max_rounds < 0:
        raise ValueError(f"max_rounds 必须 ≥ 0，实得 {max_rounds}")

    resolved_editor: RepairEditor = editor if editor is not None else default_editor
    resolved_verifier: RepairVerifier = (
        verifier if verifier is not None else make_default_verifier(verdict)
    )

    current = after_bytes
    current_verdict = verdict

    if is_pass(current_verdict):
        return RepairOutcome(status=PASS, attempts=[], failure_report=None,
                             failed_layer=None, rounds_used=0)

    previous = failure_count(current_verdict)
    attempts: list[RepairAttempt] = []
    rounds_used = 0

    cap = _effective_max_rounds(max_rounds)
    for round_index in range(1, cap + 1):
        diagnosis = diagnose(current_verdict)
        if not diagnosis.repairable:
            # Unrepairable (e.g. structural damage): stop immediately, never spin.
            break

        repaired = resolved_editor(before_bytes, current, diagnosis)
        if repaired is None:
            break

        new_verdict = resolved_verifier(before_bytes, repaired)
        new_count = failure_count(new_verdict)
        rounds_used = round_index
        attempts.append(
            RepairAttempt(
                round=round_index,
                failure_count=new_count,
                actions=[action.label() for action in diagnosis.actions],
                diagnosis=diagnosis.summary,
            )
        )
        _emit(trace_sink, run_id, round_index, diagnosis, new_verdict, new_count)

        current = repaired
        current_verdict = new_verdict

        if _oscillation_violated(previous, new_count):
            # The repair made things worse ⇒ terminate early (防振荡).
            break
        previous = new_count

        if is_pass(new_verdict):
            return RepairOutcome(status=PASS, attempts=attempts, failure_report=None,
                                 failed_layer=None, rounds_used=rounds_used)

    failed_layer = _first_failed_layer(current_verdict)
    report = build_failure_report(current_verdict, attempts, rounds_used, max_rounds)
    return RepairOutcome(
        status=FAILED_VALIDATION,
        attempts=attempts,
        failure_report=report,
        failed_layer=failed_layer,
        rounds_used=rounds_used,
    )


# --------------------------------------------------------------------------- #
# honest failure report                                                        #
# --------------------------------------------------------------------------- #
def build_failure_report(
    verdict: ValidationVerdict,
    attempts: list[RepairAttempt],
    rounds_used: int,
    max_rounds: int,
) -> str:
    """Render the escalation report (verbatim evidence + attempts + next step)."""
    failing = [name for name in LAYER_ORDER if verdict.layers[name].status == FAIL]
    lines: list[str] = [
        "【修复失败报告】",
        f"run 状态：{FAILED_VALIDATION}",
        f"失败层：{_first_failed_layer(verdict) or '（无硬失败层）'}",
        f"尝试次数：{len(attempts)}；轮数：{rounds_used}；轮数上限：{max_rounds}",
        "失败信息（verbatim）：",
    ]
    if failing:
        for name in failing:
            lines.append(f"  - {name}：{verdict.layers[name].evidence_ref}")
    else:
        lines.append(f"  - （无 fail 层）overall={verdict.overall}；notes={verdict.notes}")

    lines.append("修复轨迹：")
    if attempts:
        for attempt in attempts:
            lines.append(
                f"  轮 {attempt.round}：{attempt.diagnosis} ⇒ {attempt.actions}"
                f" ⇒ 失败项数={attempt.failure_count}"
            )
    else:
        lines.append("  （无可执行的修复动作：缺陷不可定位）")

    lines.append(
        "建议用户操作：请人工核对上述失败层，确认编辑意图或放弃本次自动修复；"
        "未测量层（status=None）请勿视为通过。"
    )
    lines.append("已提交版本：无（验证未通过，未产出成功制品）。")
    return "\n".join(lines)
