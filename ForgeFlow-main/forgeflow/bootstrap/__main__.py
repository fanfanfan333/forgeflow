"""``python -m forgeflow.bootstrap`` — the single CLI startup entry point.

Default is ``reload=False`` (production-shaped, direct startup chain, clean
logs). Use ``--reload`` for hot-reload development. Both go through
:func:`forgeflow.bootstrap.runtime.run_api_server`, so there is exactly one
startup strategy.
"""

from __future__ import annotations

import argparse
import sys

from forgeflow.bootstrap.runtime import (
    DEFAULT_APP,
    DEFAULT_HOST,
    DEFAULT_PORT,
    run_api_server,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m forgeflow.bootstrap",
        description="Start the ForgeFlow API on a Windows Selector event loop.",
    )
    parser.add_argument("--app", default=DEFAULT_APP, help="ASGI app import string")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--reload",
        action="store_true",
        help="enable uvicorn hot-reload (development only)",
    )
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "--reload-dir",
        action="append",
        default=None,
        dest="reload_dirs",
        help="directory to watch (repeatable)",
    )
    args = parser.parse_args(argv)

    run_api_server(
        args.app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers,
        log_level=args.log_level,
        reload_dirs=args.reload_dirs,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
