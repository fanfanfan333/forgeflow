"""QA launcher — start the ForgeFlow API on 127.0.0.1:8010.

Why not `uvicorn.run(...)`:
  * The app's LangGraph Postgres checkpointer uses psycopg async, which raises
    ``Psycopg cannot use the 'ProactorEventLoop'`` on Windows. Python 3.13's
    uvicorn ignores ``set_event_loop_policy`` and builds a Proactor loop, so we
    must drive ``Server.serve()`` inside an ``asyncio.Runner`` whose
    ``loop_factory`` returns a SelectorEventLoop.
Also chdir to the repo root (so ``.env`` loads) and prepend it to sys.path
(``forgeflow`` is not installed as a package).
"""
import asyncio
import os
import pathlib
import selectors
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]  # <repo>/qa_tmp/.. == repo root
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402


def _loop_factory():
    return asyncio.SelectorEventLoop(selectors.SelectSelector())


async def _serve() -> None:
    config = uvicorn.Config(
        "forgeflow.api.main:app",
        host="127.0.0.1",
        port=8010,
        log_level="info",
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    with asyncio.Runner(loop_factory=_loop_factory) as runner:
        runner.run(_serve())
