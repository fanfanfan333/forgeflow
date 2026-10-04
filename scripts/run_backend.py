"""Managed backend startup entry point — ``scripts/run_backend.py``.

This is the **only supported way to start the ForgeFlow API server** from a
checked-out source tree. It is a thin wrapper around
:func:`forgeflow.bootstrap.run_api_server`, the single managed startup entry
point. Do **not** call ``uvicorn.run`` / ``uvicorn.Server`` directly from here,
and do **not** re-implement event-loop selection in this file.

Why not launch bare uvicorn?
----------------------------
On Windows, uvicorn 0.53.0 pins the event loop through its own config factory:
``Config.get_loop_factory()`` returns ``asyncio.ProactorEventLoop`` whenever
``sys.platform == "win32"`` and neither ``reload`` nor ``workers`` is enabled.
psycopg3 (the LangGraph checkpointer backend) refuses to run on a Proactor
loop, so a bare ``uvicorn forgeflow.api.main:app`` dies inside the lifespan
whenever ``STORAGE_BACKEND=postgres``. ``forgeflow.bootstrap.run_api_server``
fixes this by installing the Windows Selector policy *and* launching uvicorn
with a ``ForgeFlowConfig`` whose ``get_loop_factory`` always yields a Selector
loop. Routing through it here keeps that the one and only startup strategy, so
there is a single place to reason about the event loop.

Usage::

    python scripts/run_backend.py                     # 127.0.0.1:8010, no reload
    python scripts/run_backend.py --reload            # hot-reload (development)
    python scripts/run_backend.py --port 9000
    python scripts/run_backend.py --host 0.0.0.0 --workers 4
"""

from __future__ import annotations

import argparse
import os
import sys

# The project root must be importable when this file is run directly
# (``python scripts/run_backend.py``): ``forgeflow`` is not pip-installed into
# the venv, so ``sys.path[0]`` would otherwise be ``scripts/`` and the import
# below would fail. Mirrors scripts/validate_hubspot.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forgeflow.bootstrap import (  # noqa: E402 — path bootstrapped above
    DEFAULT_HOST,
    DEFAULT_PORT,
    run_api_server,
)


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and hand off to the managed startup entry point.

    Args:
        argv: Argument list to parse; defaults to ``sys.argv[1:]`` when ``None``.

    Returns:
        Process exit code (``0`` on a clean run; the call blocks until the
        server stops).
    """
    parser = argparse.ArgumentParser(
        prog="python scripts/run_backend.py",
        description=(
            "Start the ForgeFlow API through the managed Windows-safe entry "
            "point (forgeflow.bootstrap.run_api_server)."
        ),
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"Bind host (default: {DEFAULT_HOST}).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Bind port (default: {DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="enable uvicorn hot-reload (development only)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="number of worker processes (enables supervisor mode)",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        help="uvicorn log level (default: info)",
    )
    args = parser.parse_args(argv)

    run_api_server(
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
