"""INC46 T23 — the **five-layer validation stack** (编排器).

The stack runs the five validation layers over a ``before``/``after`` document
pair and aggregates **one verdict per layer** plus an overall verdict:

======== =============================================================== ===========
Layer     Meaning                                                         Unmeasured
======== =============================================================== ===========
**L1**    structural — python-docx open + OOXML package integrity        engine note
**L2**    invariants — amounts/dates/tables/… + 3 implicit baselines      raises
**L3**    format fidelity — out-of-range zero-change + run style inherit  raises
**L4**    render comparison — PDF → raster → pixel diff in bbox           ``None``
**L5**    semantic review — LLM-as-judge (advisory only)                  ``None``
======== =============================================================== ===========

Aggregation (task book §「五层验证栈」)
--------------------------------------
* any of **L1–L3** ``fail`` ⇒ overall ``fail``;
* **L4** ``fail`` ⇒ overall ``fail`` **or** ``warn`` (``ValidationConfig.l4_fail_is_fatal``);
* **L5** below threshold ⇒ ``needs_review`` (advisory; **never** a standalone release);
* otherwise ``pass``. Precedence: ``fail`` > ``warn`` > ``needs_review`` > ``pass``.

Every layer's verdict is ``{"layer", "status", "evidence_ref"}`` where ``status`` is
``"pass"`` / ``"fail"`` / ``"needs_review"`` / ``None``. ``None`` means **could not
measure** and is surfaced verbatim in :attr:`ValidationVerdict.unmeasured` so an
aggregate ``pass`` can never hide an unmeasured layer (red line 4 / 15).

The per-layer **disable** hook (:attr:`ValidationConfig.disabled_layers`) is the
counterfactual injection point: a disabled layer yields ``status=None`` without
running, which turns the matching "this layer caught the defect" probe **red**.
"""

from __future__ import annotations

from typing import Any

from forgeflow.documents.validation import (
    format as format_layer,
    invariants as invariants_layer,
    render as render_layer,
    semantic as semantic_layer,
    structural as structural_layer,
)
from forgeflow.documents.validation.verdict import (
    FAIL,
    LAYER_FORMAT,
    LAYER_INVARIANTS,
    LAYER_ORDER,
    LAYER_RENDER,
    LAYER_SEMANTIC,
    LAYER_STRUCTURAL,
    NEEDS_REVIEW,
    PASS,
    LayerVerdict,
    ValidationConfig,
    ValidationVerdict,
)

__all__ = ["resolve_range", "validate_document"]

_FAILING_LAYERS = (LAYER_STRUCTURAL, LAYER_INVARIANTS, LAYER_FORMAT)


# --------------------------------------------------------------------------- #
# range resolution                                                             #
# --------------------------------------------------------------------------- #
def resolve_range(
    before: bytes,
    *,
    instruction: str | None = None,
    selector: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> tuple[int, int | None, str]:
    """Resolve the ``[start, end)`` range the layers are anchored to.

    Priority: explicit ``start``/``end`` > ``selector`` (via the T20 locator) >
    ``instruction`` (via the T20 deterministic intent front-end) > whole document.

    Returns ``(start, end, note)``; ``end`` may be ``None`` (EOF). An ambiguous or
    missing target is **never** guessed — it falls back to the whole document and
    the ``note`` records the honest reason.
    """
    if start is not None:
        return int(start), end, "range: explicit [start, end)"

    if selector:
        from forgeflow.documents.locator import locate

        result = locate(before, selector)
        if result.chosen is not None:
            return (
                int(result.chosen.index),
                int(result.chosen.section_end_index),
                f"range: located {selector!r}",
            )
        reason = "ambiguous" if result.ambiguity else "not_found"
        return 0, None, f"range: {selector!r} {reason} ⇒ whole document (未猜测)"

    if instruction:
        from forgeflow.documents.intent import EditIntent, resolve_intent_document

        intent = resolve_intent_document(before, instruction)
        if isinstance(intent, EditIntent) and intent.resolved and intent.target_selector:
            from forgeflow.documents.locator import locate

            result = locate(before, intent.target_selector)
            if result.chosen is not None:
                return (
                    int(result.chosen.index),
                    int(result.chosen.section_end_index),
                    f"range: instruction ⇒ {intent.target_selector!r}",
                )
        return 0, None, "range: instruction unresolved ⇒ whole document (未猜测)"

    return 0, None, "range: whole document"


# --------------------------------------------------------------------------- #
# orchestration                                                                #
# --------------------------------------------------------------------------- #
def _disabled(layer: str) -> LayerVerdict:
    return LayerVerdict(layer, None, f"{layer} disabled (counterfactual)", {"disabled": True})


def _run_layer(layer: str, thunk: Any) -> LayerVerdict:
    """Run one layer, converting an unexpected crash into an honest ``fail``.

    A layer that cannot measure its input must never be silently treated as a
    pass (red line 15): a raised exception (e.g. the ``after`` bytes are not a
    readable DOCX) becomes ``status="fail"`` with the error in the evidence — the
    stack never swallows it and never crashes the whole run.
    """
    try:
        verdict = thunk()
    except Exception as exc:  # noqa: BLE001 — honest fail, never a silent pass
        return LayerVerdict(
            layer,
            FAIL,
            f"{layer}:ERROR({type(exc).__name__}: {exc})",
            {"error": str(exc), "error_type": type(exc).__name__},
        )
    if not isinstance(verdict, LayerVerdict):
        return LayerVerdict(
            layer,
            FAIL,
            f"{layer}:BAD_VERDICT({type(verdict).__name__})",
            {"got": repr(verdict)},
        )
    return verdict


def validate_document(
    before: bytes,
    after: bytes,
    *,
    instruction: str | None = None,
    selector: str | None = None,
    start: int | None = None,
    end: int | None = None,
    config: ValidationConfig | None = None,
    judge: semantic_layer.SemanticJudge | None = None,
    render_engine: str | None = None,
    render_converter: Any = None,
    rasterizer: Any = None,
    page_counter: Any = None,
    bbox: tuple[int, int, int, int] | None = None,
) -> ValidationVerdict:
    """Run L1–L5 over ``before``/``after`` and aggregate them.

    Args:
        before: original DOCX bytes.
        after: edited DOCX bytes.
        instruction: optional raw instruction (context + range fallback).
        selector: optional target selector (located via the T20 locator).
        start / end: explicit ``[start, end)`` paragraph range.
        config: stack config (failure policy, L5 floor, disabled layers).
        judge: injectable L5 judge.
        render_engine: injectable L4 conversion-engine path.
        render_converter: injectable L4 ``(docx_bytes, engine) -> pdf_bytes | None``.
        rasterizer: injectable L4 ``(pdf, tool, dpi) -> RasterImage | None``.
        page_counter: injectable L4 ``(pdf) -> int | None`` page-count reader.
        bbox: L4 comparison box (``None`` ⇒ whole page).

    Returns:
        A :class:`ValidationVerdict` with one :class:`LayerVerdict` per layer, the
        aggregate ``overall`` (``pass`` / ``warn`` / ``needs_review`` / ``fail``)
        and the ``unmeasured`` layer list.
    """
    settings = config or ValidationConfig()
    resolved_start, resolved_end, range_note = resolve_range(
        before,
        instruction=instruction,
        selector=selector,
        start=start,
        end=end,
    )

    thunks: dict[str, Any] = {
        LAYER_STRUCTURAL: lambda: structural_layer.structural_verdict(after),
        LAYER_INVARIANTS: lambda: invariants_layer.invariants_verdict(
            before, after, start=resolved_start, end=resolved_end
        ),
        LAYER_FORMAT: lambda: format_layer.format_verdict(
            before, after, start=resolved_start, end=resolved_end
        ),
        LAYER_RENDER: lambda: render_layer.render_verdict(
            before,
            after,
            engine=render_engine,
            converter=render_converter,
            rasterizer=rasterizer,
            page_counter=page_counter,
            bbox=bbox,
        ),
        LAYER_SEMANTIC: lambda: semantic_layer.semantic_verdict(
            before,
            after,
            start=resolved_start,
            end=resolved_end,
            instruction=instruction,
            judge=judge,
            config=settings,
        ),
    }

    layer_verdicts: dict[str, LayerVerdict] = {}
    for layer_name in LAYER_ORDER:
        if settings.is_disabled(layer_name):
            layer_verdicts[layer_name] = _disabled(layer_name)
        else:
            layer_verdicts[layer_name] = _run_layer(layer_name, thunks[layer_name])

    ordered = {name: layer_verdicts[name] for name in LAYER_ORDER}
    overall, notes = _aggregate(ordered, settings)

    unmeasured = [name for name, verdict in ordered.items() if verdict.status is None]
    if unmeasured:
        notes = f"{notes}；未测量层（status=None）：{unmeasured}"
    notes = f"{notes}；{range_note}"

    return ValidationVerdict(
        layers=ordered,
        overall=overall,
        unmeasured=unmeasured,
        notes=notes,
    )


def _aggregate(
    layers: dict[str, LayerVerdict], config: ValidationConfig
) -> tuple[str, str]:
    """The aggregate verdict + an explanatory note (precedence: fail>warn>review>pass)."""
    for name in _FAILING_LAYERS:
        if layers[name].status == FAIL:
            return FAIL, f"硬失败层：{name}"

    if layers[LAYER_RENDER].status == FAIL:
        if config.l4_fail_is_fatal:
            return FAIL, f"渲染层失败（l4_fail_is_fatal=True）：{LAYER_RENDER}"
        return "warn", f"渲染层失败但按配置降级为 warn：{LAYER_RENDER}"

    # 页数变化 ⇒ 警告（规格明文「页数变化 ⇒ 警告」）。只在**两端都测得**时生效：
    # ``page_delta`` 为非零整数才 warn；``None``（未测量）绝不凭空警告（红线 15）。
    # 页数变化不折成 fail，也不改 L4 自身 status（其 status 仅由像素 diff 决定）。
    page_delta = layers[LAYER_RENDER].detail.get("page_delta")
    if isinstance(page_delta, int) and not isinstance(page_delta, bool) and page_delta != 0:
        pages_before = layers[LAYER_RENDER].detail.get("pages_before")
        pages_after = layers[LAYER_RENDER].detail.get("pages_after")
        return "warn", f"渲染层页数变化（{pages_before} → {pages_after}）：{LAYER_RENDER}"

    if layers[LAYER_SEMANTIC].status == NEEDS_REVIEW:
        return NEEDS_REVIEW, "语义层评分低于阈值（仅建议，绝不单独放行）"

    if all(verdict.status == PASS for verdict in layers.values()):
        return PASS, "五层全部 measure 且通过"

    return PASS, "机械层通过（存在未测量层，见 unmeasured）"
