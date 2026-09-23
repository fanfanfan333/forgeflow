import asyncio
import sys

import uvicorn

# On Windows uvicorn's asyncio loop factory hard-codes ProactorEventLoop, which
# psycopg (the LangGraph Postgres checkpointer) refuses to run under. Override
# the loop factory so uvicorn uses a Selector loop instead. See
# uvicorn/loops/asyncio.py::asyncio_loop_factory.
config = uvicorn.Config(
    "forgeflow.api.main:app",
    host="127.0.0.1",
    port=8010,
    log_level="info",
    reload=False,
)
if sys.platform == "win32":
    config.get_loop_factory = lambda: asyncio.SelectorEventLoop

if __name__ == "__main__":
    uvicorn.Server(config).run()
