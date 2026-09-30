"""Graph checkpointer — persists graph state after every node execution.

Two backends, selected by ``STORAGE_BACKEND``:
  * ``postgres`` — AsyncPostgresSaver (psycopg3) so any API worker can resume
    any thread_id across processes.
  * ``memory``   — process-local InMemorySaver (offline / zero-dependency
    profile); state and interrupts stay resumable within one process only.

Call get_checkpointer() once at startup; pass the result to compile_graph().
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from forgeflow.config import get_settings

logger = logging.getLogger(__name__)

# The saver is protocol-compatible across backends: the postgres profile uses
# AsyncPostgresSaver, the offline (memory) profile uses an in-process
# InMemorySaver. Both expose the same async checkpoint API consumed by
# compile_graph(), so callers never branch on the backend.
_checkpointer: Any = None
_cm = None  # keeps the from_conn_string() context manager alive for the process lifetime


def _warn_if_proactor_loop() -> None:
    """Warn when the postgres profile is about to run on a Windows Proactor loop.

    psycopg3's async connector refuses ``ProactorEventLoop`` on Windows. This is a
    *warning with the exact remedy* — never a self-repair and never a crash. The
    loop cannot be switched from inside the app: uvicorn pins it through its own
    loop factory (``uvicorn.loops.asyncio``), which is independent of
    ``asyncio.set_event_loop_policy``, so the operator must choose a Selector loop
    at launch time.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # no running loop — nothing to inspect
        return
    if type(loop).__name__ != "ProactorEventLoop":
        return
    logger.warning(
        "B 档（storage_backend=postgres）必须运行在 Selector 事件循环上："
        "请用 `--reload` 或 `--workers N` 启动，或自定义 `Config.get_loop_factory` "
        "返回 `asyncio.SelectorEventLoop`；裸 `uvicorn <app>` 在 Windows 上会得到 "
        "Proactor，psycopg 异步连接器不可用。"
    )


async def get_checkpointer() -> Any:
    global _checkpointer, _cm
    if _checkpointer is not None:
        return _checkpointer

    settings = get_settings()

    # Offline (memory) profile — zero external dependency (§E / K5). An
    # InMemorySaver keeps graph state and human-in-the-loop interrupts
    # resumable for the process lifetime without dialing PostgreSQL, so the
    # "all-offline" profile boots with no reachable database.
    if settings.storage_backend.lower() != "postgres":
        from langgraph.checkpoint.memory import InMemorySaver

        logger.info("Initialising in-memory checkpointer (offline profile)...")
        _checkpointer = InMemorySaver()
        logger.info("In-memory checkpointer ready")
        return _checkpointer

    logger.info("Initialising PostgreSQL checkpointer...")
    _warn_if_proactor_loop()

    # from_conn_string is @asynccontextmanager — must __aenter__ to get the saver,
    # and the CM must outlive the saver or the underlying connection closes.
    # psycopg needs a plain libpq URL; SQLAlchemy's "+psycopg" driver suffix is rejected.
    conn_url = settings.postgres_sync_url.replace("postgresql+psycopg://", "postgresql://", 1)
    _cm = AsyncPostgresSaver.from_conn_string(conn_url)
    _checkpointer = await _cm.__aenter__()
    # Creates langgraph_checkpoints and langgraph_writes tables if they don't exist
    await _checkpointer.setup()
    logger.info("Checkpointer ready")
    return _checkpointer
