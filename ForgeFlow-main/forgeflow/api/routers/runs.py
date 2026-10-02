"""Run routes — detail, SSE event stream, and replan (docs §4.1)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.hub_schemas import (
    ReplanRequest,
    RunAbortResponse,
    RunDetailResponse,
    RunHandleResponse,
    RunListResponse,
    RunSummaryResponse,
)
from forgeflow.rbac.models import UserContext
from forgeflow.runtime.dispatcher import ensure_tenant_history
from forgeflow.runtime.events import get_event_bus
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    get_run_store,
    run_task,
)
from forgeflow.runtime.token_stream import get_token_registry

logger = logging.getLogger(__name__)
router = APIRouter()


async def _load_run(run_id: str, tenant: str):
    # INC40 — read-side, per-tenant backfill so a restart's persisted history is
    # available for THIS tenant (never pushed out by other tenants' newer rows).
    await ensure_tenant_history(tenant)
    record = get_run_store().get(run_id)
    # Tenant-scoped: a cross-tenant lookup behaves like "not found" (no leak).
    if record is None or (record.tenant_id not in (tenant, None)):
        raise HTTPException(status_code=404, detail="Run not found")
    return record


def _opt_int(value) -> int | None:
    """Pass a numeric field through honestly (INC20 / T01).

    ``None`` (the attribute is absent, or the producer wrote ``None``) stays
    ``None`` — "not measured". A real ``0`` stays ``0``. The previous
    ``int(getattr(record, "total_tokens", 0) or 0)`` collapsed BOTH cases onto
    ``0``, which is exactly the "未测量伪装成 0" lie this increment removes.

    ⚠️ Residual indivisibility (recorded, do not delete): the producer
    (``orchestrator.py::run_task`` 中 ``total_tokens=int(cost_summary["total_tokens"])`` 处) still writes ``int(...)``, so a deterministic run
    carries a genuine ``0``. This helper can therefore only distinguish "attribute
    missing" from "0", never "producer back-filled 0" from "really measured 0".
    Consumers must judge "did a model run?" by model-driven evidence, not by the
    token value itself.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value) -> float | None:
    """Float twin of :func:`_opt_int` (same honesty contract)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@router.get("", response_model=RunListResponse)
async def list_runs(
    limit: int = Query(20, ge=1, le=100),
    tenant: str = Depends(resolve_tenant),
):
    """List the tenant's recent runs (newest first) for the home page."""
    await ensure_tenant_history(tenant)
    store = get_run_store()
    rows = store.list(tenant, limit=limit)
    items = [
        RunSummaryResponse(
            run_id=r.run_id,
            thread_id=r.thread_id,
            status=r.status,
            outcome=r.outcome,
            intent=r.intent,
            title=r.intent[:60],
            created_at=r.created_at,
            completed_at=r.completed_at,
            experience_id=r.experience_id,
            step_count=len(r.steps),
            # INC32 (additive): the workspace relationships, via getattr so a
            # pre-INC32 record degrades honestly to "" rather than crashing.
            session_id=str(getattr(r, "session_id", "") or ""),
            parent_run_id=str(getattr(r, "parent_run_id", "") or ""),
        )
        for r in rows
    ]
    # INC-AUDIT — ``total`` is the tenant's **whole** run count. It used to be
    # ``len(items)``, i.e. the page size (the store slices to ``limit``), so the
    # home KPI read "0" (or ``limit``) while the list beside it showed real runs.
    return RunListResponse(total=store.count(tenant), items=items)


@router.get("/{run_id}", response_model=RunDetailResponse)
async def get_run(run_id: str, tenant: str = Depends(resolve_tenant)):
    """Fetch a run's detail (tenant-scoped)."""
    record = await _load_run(run_id, tenant)
    return RunDetailResponse(
        run_id=record.run_id,
        thread_id=record.thread_id,
        status=record.status,
        outcome=record.outcome,
        intent=record.intent,
        steps=record.steps,
        errors=record.errors,
        created_at=record.created_at,
        completed_at=record.completed_at,
        experience_id=record.experience_id,
        # Runtime truth for this run — the executor that produced it and the
        # real metered usage (INC4 §A). INC20 / T01: ``None`` (未测量) is now
        # passed through verbatim instead of being coerced to ``0`` by ``or 0``.
        # A real ``0`` is still preserved (distinct from ``None``).
        total_tokens=_opt_int(getattr(record, "total_tokens", None)),
        total_cost_usd=_opt_float(getattr(record, "total_cost_usd", None)),
        # INC42 — pass the recorded runtime mode through **verbatim** (no
        # ``or "deterministic"`` coercion). Before this, a record whose mode was
        # never recorded (every pre-017 row — the ``runtime_mode`` column did not
        # exist) was silently relabelled ``deterministic``, so the result page
        # falsely claimed 「未连接模型服务」 for a genuinely react run (defect ④).
        # ``""`` is the honest "not recorded"; a real ``deterministic`` / ``react``
        # / ``llm`` is preserved. ``getattr`` keeps a pre-INC4 record valid.
        runtime_mode=str(getattr(record, "runtime_mode", "") or ""),
        llm=dict(getattr(record, "llm", None) or {}),
        # Loop-breaker audit trail (INC5): persisted on the run by ``run_task``.
        # getattr keeps pre-INC5 records — which have no ``loop`` attribute —
        # from breaking the endpoint (they degrade to an empty ``{}``).
        loop=dict(getattr(record, "loop", None) or {}),
        # INC12 A1 — the run's real tool invocations. ``getattr`` keeps a
        # pre-INC12 record (no attribute) from breaking the endpoint; it
        # degrades honestly to ``[]`` rather than inventing anything.
        tool_invocations=list(getattr(record, "tool_invocations", None) or []),
        # INC12 A5 — who initiated the run. ``getattr`` keeps a pre-A5 record
        # (no attribute) from breaking the endpoint; it degrades honestly to the
        # documented defaults rather than inventing an actor.
        actor_user_id=str(getattr(record, "actor_user_id", "anonymous") or "anonymous"),
        actor_role=str(getattr(record, "actor_role", "viewer") or "viewer"),
        # INC12 A5b — the tenant that owns the run (Q1's third leg). ``getattr``
        # keeps a record without the attribute from breaking the endpoint; it
        # degrades honestly to ``None`` rather than inventing an owner.
        tenant_id=getattr(record, "tenant_id", None),
        # INC14 — the run's real deliverables (任务产物). ``getattr`` keeps a
        # pre-INC14 record (no attribute) from breaking the endpoint; it
        # degrades honestly to ``[]`` rather than inventing a result.
        artifacts=list(getattr(record, "artifacts", None) or []),
        # INC15 L1 — the run's **Task Plan** (the dynamic per-task step list the
        # planner produced). ``getattr`` keeps a pre-INC15 record (no attribute)
        # from breaking the endpoint; it degrades honestly to ``{}`` rather than
        # inventing a plan the run never had.
        plan=dict(getattr(record, "plan", None) or {}),
        # INC15 L3 — the run's **Observations**: only the steps that were
        # *truly executed* (``executed is True``). ``getattr`` keeps a pre-INC15
        # record (no attribute) from breaking the endpoint; it degrades honestly
        # to ``[]`` rather than padding with fake observations.
        observations=list(getattr(record, "observations", None) or []),
        # INC22 W1 — the run's original declaration. ``getattr`` keeps a pre-INC22
        # record (no attribute) from breaking the endpoint; it degrades honestly
        # to the producer's own defaults (``"generic"`` / ``{}``) rather than
        # inventing a domain or an input the run never had.
        workflow_type=str(getattr(record, "workflow_type", "generic") or "generic"),
        declared_inputs=dict(getattr(record, "declared_inputs", None) or {}),
        # INC25 W2 — the code-execution plane summary. ``getattr`` keeps a
        # pre-INC25 record (no attribute) from breaking the endpoint; it degrades
        # honestly to ``{}`` rather than inventing an engine status.
        codeplane=dict(getattr(record, "codeplane", None) or {}),
        # INC32 ADR-02 (additive) — the workspace relationships. ``getattr``
        # keeps a pre-INC32 record from breaking the endpoint; it degrades
        # honestly to ``""`` rather than inventing a session / parent.
        session_id=str(getattr(record, "session_id", "") or ""),
        parent_run_id=str(getattr(record, "parent_run_id", "") or ""),
        # INC33 (additive, default-safe) — whether this run's execution detail
        # survived into this process. ``getattr`` keeps every pre-INC33 record
        # valid (defaults to ``True`` — "the detail is here"). A record hydrated
        # at startup carries ``False`` so the UI tells the truth instead of
        # rendering the (in-memory-only) missing steps as a confident empty.
        detail_retained=bool(getattr(record, "detail_retained", True)),
    )


@router.get("/{run_id}/events")
async def run_events(run_id: str, tenant: str = Depends(resolve_tenant)):
    """Server-Sent Events stream for a run's execution.

    Reconnecting replays the run's history (so a finished run still yields its
    steps and a ``[DONE]`` terminator). ``X-Accel-Buffering: no`` disables
    proxy buffering so steps arrive live.

    The body is the **merged** stream (步骤事件 + token 旁路)；route 声明、租户
    校验与 headers 与改动前逐字一致，仅把出口换成 :func:`_merged_run_stream`。
    """
    await _load_run(run_id, tenant)
    return StreamingResponse(
        _merged_run_stream(run_id),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


#: bus.stream 自身的 ``[DONE]`` 终止帧（逐字）。
_DONE_FRAME: str = "data: [DONE]\n\n"
#: token 子流排空的兜底上限（收尾确定性：最终答案 token 帧必须先于 ``[DONE]``）。
_TOKEN_DRAIN_TIMEOUT_SECONDS: float = 2.0
#: 等待 executor 打开 token 通道的轮询间隔（仅真 provider 会真正打开）。
_TOKEN_OPEN_POLL_SECONDS: float = 0.01


async def _merged_run_stream(run_id: str) -> AsyncGenerator[str, None]:
    """并行消费「步骤事件流」+「token 通道」，各自保持内部顺序；bus 终态后收尾。

    * 两条子流各自**内部有序**（bus 单队列 / token 单队列）⇒ 既有步骤帧的格式与
      相对顺序**逐字不变**（P3）；
    * 两条子流的**交织先后**不被任何 AC 约束（前端按 ``event.type`` 分派）；
    * ``bus.stream`` 自身的 ``[DONE]`` / 30s keep-alive / 历史重放**完全保留**：
      这里**截获** bus 的 ``[DONE]``，先等 token 子流排空（≤2s 兜底）再放行它；
    * 无 token 通道（mock 档）时输出与改动前**逐字节相同**（C9）：token 子流在
      bus 结束的瞬间即返回，不放行任何多余字节。
    * 收尾顺序：bus 终态 → 等待 token 子流排空（≤2s 兜底）→ 放行 ``[DONE]`` → 结束；
    * 断开安全（RD-1）：客户端**中途断开**时**不** release 通道（仅 run 终态才
      release），使重连仍能消费其后 token 帧（避免「空洞等待」）；
    * 终止帧**精确**匹配（RD-4）：只认逐字的 ``data: [DONE]``，不做子串匹配。
    """
    bus = get_event_bus()
    registry = get_token_registry()
    out: asyncio.Queue[str | None] = asyncio.Queue()
    bus_finished = asyncio.Event()
    #: 出口正在被关闭（客户端断开 / ``aclose``）。用于区分 bus 的**真**终帧与
    #: ``events.py::RunEventBus.stream`` 在 **finally 里 yield** 的 ``[DONE]``：
    #: 取消时该 finally 会**补吐一帧** DONE（作为值返回，而非异常）——若据它判定
    #: 「自然终态」就会在断开时误 release（RD-1）。
    disconnecting = False

    async def _pump_tokens() -> None:
        """转发 token 帧；通道可能尚未打开 ⇒ 有界等待，bus 结束即不再等待。"""
        token = registry.get(run_id)
        while token is None and not bus_finished.is_set():
            # 真 provider 的通道由 executor 在 run() 内打开；mock 档永不打开，
            # 故 bus 一结束立即返回（不拖慢 mock SSE，字节级不变）。
            await asyncio.sleep(_TOKEN_OPEN_POLL_SECONDS)
            token = registry.get(run_id)
        if token is None:
            return
        async for frame in token.frames():
            await out.put(frame)

    async def _pump_bus() -> None:
        """转发步骤帧（原样，含 keep-alive / 历史重放），截获并延后 ``[DONE]``。"""
        natural = False  # bus 是否**自然**到达终止帧（非取消 / 非异常）
        try:
            async for frame in bus.stream(run_id):
                # RD-4: 精确匹配终止帧——勿用子串匹配，否则 payload 里出现字面
                # ``[DONE]`` 的 step 事件会被误判为终止帧（提前结束、丢掉其后全部帧）。
                if frame.rstrip("\n") == _DONE_FRAME.rstrip("\n"):
                    if disconnecting:
                        # 伪终帧：取消 bus 子流时 ``events.py`` 的 ``finally`` 补吐的
                        # ``[DONE]``（不是 run 自然终态）——不计入 natural。
                        break
                    natural = True
                    break
                await out.put(frame)
        finally:
            if natural:
                # 仅**自然**终态才置 ``bus_finished``（= run 已终态，供 release 判定）。
                bus_finished.set()
                # 收尾确定性：等 token 子流排空（≤2s 兜底）再放行 ``[DONE]``。
                try:
                    await asyncio.wait_for(
                        asyncio.shield(tokens_task), timeout=_TOKEN_DRAIN_TIMEOUT_SECONDS
                    )
                except asyncio.TimeoutError:  # pragma: no cover — 兜底，不阻塞出口
                    logger.warning("token drain timed out for run %s; releasing [DONE]", run_id)
                except Exception as exc:  # noqa: BLE001 — 排空失败不得吞掉终止帧
                    logger.warning("token drain failed for run %s: %s", run_id, exc)
            await out.put(_DONE_FRAME)
            await out.put(None)

    tokens_task = asyncio.create_task(_pump_tokens())
    bus_task = asyncio.create_task(_pump_bus())
    try:
        while True:
            item = await out.get()
            if item is None:
                break
            yield item
    finally:
        # RD-1: 先置「正在断开」再取消——取消会把 ``events.py::RunEventBus.stream``
        # 的 ``finally`` 补吐的 ``[DONE]`` 作为**值**送到 ``_pump_bus``；在断开标志
        # 下该帧被视为非自然终帧，故不会把 run 误标为终态、不会误 release 通道。
        disconnecting = True
        # 客户端断开 / 提前结束：取消两条 pump。
        for task in (bus_task, tokens_task):
            if not task.done():
                task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.gather(bus_task, tokens_task, return_exceptions=True)
        # RD-1: **只在 run 终态（bus 自然到达 ``[DONE]``）时** release。客户端中途断开
        # 时保留 token 通道——release = pop + ``close()``，而关闭后 ``publish()`` 静默
        # 全丢，且 executor 只在 ``run()`` 开头 ``open()`` 一次、永不重开 ⇒ 重连后只剩
        # step 事件（空洞等待）。提前断开时改由 executor 自己的 ``finally`` 负责
        # ``close()``、registry 的 ``_evict_if_needed`` 负责回收。
        if bus_finished.is_set():
            registry.release(run_id)


@router.post("/{run_id}/replan", response_model=RunHandleResponse)
async def replan_run(
    run_id: str,
    request: ReplanRequest,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Re-run a task's intent (manual replan trigger).

    INC22 W1 — the re-run re-declares the **original task's** inputs, not just
    its intent. Before this, a run created with ``workflow_type="sales_ops"`` +
    ``context={"table": …}`` was replayed as a bare ``intent`` (``workflow_type``
    fell back to ``"generic"`` and every explicit input was dropped), so the same
    task that had just executed came back ``blocked`` — the "Agent 做了一半"
    symptom. The declaration is read through ``getattr`` so a pre-INC22 record
    (which carries neither attribute) degrades to the producer's own defaults
    (``"generic"`` / ``{}``) instead of crashing or inventing inputs. ``intent`` /
    ``title`` behave exactly as before.
    """
    record = await _load_run(run_id, tenant)
    ctx = RequestContext(tenant_id=tenant, user_id=user.user_id, role=user.role)
    task = TaskCreate(
        intent=record.intent,
        title=record.intent[:60],
        workflow_type=str(getattr(record, "workflow_type", "generic") or "generic"),
        context=dict(getattr(record, "declared_inputs", None) or {}),
    )
    handle = await run_task(task, ctx)
    # INC25 W2 — "重新分析" is the third code-plane approval action (approve /
    # reject / reanalyze, AC-18/AC-19). It reuses THIS route, so the audit entry
    # for the action is written here — but ONLY for a code-plane run (the record
    # carries a codeplane summary), so a plain replan's behaviour is unchanged.
    if dict(getattr(record, "codeplane", None) or {}):
        from forgeflow.codeplane.approval import audit_reanalysis

        await audit_reanalysis(
            tenant_id=tenant,
            run_id=handle.run_id,
            requester=user.user_id,
            role=user.role,
            from_run_id=run_id,
            reason=request.reason,
        )
    logger.info("manual replan | from=%s to=%s reason=%s", run_id, handle.run_id, request.reason)
    return RunHandleResponse(**handle.to_dict())


# --------------------------------------------------------------------------- #
# INC32 — Stop / Abort (ADR-04)                                                #
# --------------------------------------------------------------------------- #
@router.post("/{run_id}/abort", response_model=RunAbortResponse)
async def abort_run(run_id: str, tenant: str = Depends(resolve_tenant)):
    """Stop a running run (INC32 ADR-04).

    HTTP semantics are honest and idempotent:
      * running / awaiting_approval → **200** ``{run_id, status: "aborted"}``;
      * already ``aborted`` → **200** (same value, idempotent);
      * already ``completed`` / ``failed`` → **409** (never 200, never 500);
      * unknown run / cross-tenant → **404** (``_load_run``'s no-leak口径).

    RBAC is inherited: the path prefix ``/runs`` ⇒
    ``ROUTE_PERMISSION_MAP[("POST", "/runs")] = ("execute", "workflows")``, so a
    ``viewer`` gets **403** at the middleware and the run is untouched (AC-37).
    """
    from forgeflow.runtime.dispatcher import (
        RunNotAbortableError,
        RunNotFoundError,
        get_run_dispatcher,
    )

    # Resolve the run + cross-tenant 404 through the same helper the other run
    # routes use, then let the dispatcher own the terminal-state rules.
    await _load_run(run_id, tenant)
    try:
        status = await get_run_dispatcher().abort(tenant, run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    except RunNotAbortableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RunAbortResponse(run_id=run_id, status=status)


# --------------------------------------------------------------------------- #
# INC32 — Artifact download (ADR-05)                                           #
# --------------------------------------------------------------------------- #
#: format → (media type, filename extension). Only the kinds the platform really
#: produces are served; anything else degrades to ``text/plain``. ``docx``
#: (INC43 S4) is binary — its body is fetched from ``content_ref`` rather than
#: read from ``content`` (see ``download_artifact``).
_ARTIFACT_MEDIA: dict[str, tuple[str, str]] = {
    "markdown": ("text/markdown; charset=utf-8", "md"),
    "diff": ("text/plain; charset=utf-8", "diff"),
    "text": ("text/plain; charset=utf-8", "txt"),
    "docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "docx",
    ),
}
_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


async def _find_artifact(record, tenant: str, artifact_id: str):
    """Locate an artifact by id — memory first, then the durable copy.

    Returns the artifact dict or ``None`` (never a fabricated one). The durable
    fallback reads ``workspace_runs.artifacts`` so a download still works after a
    restart even though the run body is gone (ADR-05).
    """
    for art in list(getattr(record, "artifacts", None) or []):
        if isinstance(art, dict) and str(art.get("id") or "") == artifact_id:
            return art
    try:
        from forgeflow.workspace.store import get_workspace_store

        stored = await get_workspace_store().get(tenant, record.run_id)
    except Exception as exc:  # noqa: BLE001 — a store hiccup degrades to "not found"
        logger.debug("artifact workspace fallback skipped: %s", exc)
        stored = None
    if stored is not None:
        for art in list(stored.artifacts or []):
            if isinstance(art, dict) and str(art.get("id") or "") == artifact_id:
                return art
    return None


@router.get("/{run_id}/artifacts/{artifact_id}")
async def download_artifact(
    run_id: str, artifact_id: str, tenant: str = Depends(resolve_tenant)
):
    """Download one artifact's content verbatim (INC32 ADR-05).

    Read-only: ``GET /runs`` ⇒ ``read:workflows`` (a viewer may download). The
    artifact is located by ``run_id`` + ``artifact_id``; a missing id or a run
    the tenant does not own is **404** (honest — never a fabricated file).
    """
    record = await _load_run(run_id, tenant)
    artifact = await _find_artifact(record, tenant, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")

    fmt = str(artifact.get("format") or "text")
    media_type, ext = _ARTIFACT_MEDIA.get(fmt, ("text/plain; charset=utf-8", "txt"))
    safe = _FILENAME_SAFE.sub("_", artifact_id).strip("_")[:80] or "artifact"
    filename = f"{safe}.{ext}"

    # INC43 S4 — additive binary branch: a DOCX artifact stores no body in
    # ``content`` (a base64 body would be truncated by the payload ceiling); its
    # bytes live in the DocArtifactStore behind ``content_ref``. Read-only and
    # tenant-checked (via ``_load_run`` / ``_find_artifact``) exactly as above.
    content_ref = str(artifact.get("content_ref") or "").strip()
    if content_ref:
        from forgeflow.documents.store import DocArtifactStore

        try:
            blob = DocArtifactStore().get(content_ref)
        except Exception as exc:  # noqa: BLE001 — a missing blob is an honest 404
            logger.debug("docx artifact blob read failed: %s", exc)
            blob = None
        if blob is None:
            raise HTTPException(status_code=404, detail="Artifact content unavailable")
        return Response(
            content=blob,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    content = str(artifact.get("content") or "")
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
