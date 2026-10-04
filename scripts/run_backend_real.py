"""Real-stack backend launcher — ``scripts/run_backend_real.py``.

Boots the ForgeFlow API on the **real local stack**: ``ForgeFlow-main/.env``
(real PostgreSQL on ``:5433`` + real Ollama on ``:11434``). It is the
version-controlled successor of the former root-level ``_run_backend_real.py``
(that file lived outside Git because of the ``/_*`` ignore rule, so the launch
behaviour could not be reviewed, diffed or reproduced on another machine).

This script owns **only** environment preparation and argument parsing. Every
byte of startup logic lives in :func:`forgeflow.bootstrap.run_api_server` — the
single managed, Windows-safe entry point. In particular this file must never
call ``uvicorn.run`` / ``uvicorn.Server``, never pick an event loop itself, and
never touch ``psycopg``: a second startup strategy is exactly the drift INC48
removed.

Why the bootstrap layer matters here (INC48 root cause): on Windows uvicorn
0.53's own loop factory hands back an ``asyncio.ProactorEventLoop`` whenever
``reload`` is off and ``workers`` is unset, and psycopg3 refuses to run on it —
the lifespan dies with ``psycopg.InterfaceError``. ``run_api_server`` pins a
Selector loop, so ``reload=False`` (the production-shaped mode) now works
without the old "reload is REQUIRED on Windows" workaround.

Two traps this script defends against (both silent failures, see
``运行说明.md``):

1. The sandbox HTTP proxy — with it set, even ``http://127.0.0.1:11434`` goes
   through the proxy, returns 502, and the engine **silently degrades** to the
   mock provider. We pop the proxy variables and set ``NO_PROXY``.
2. Running under an interpreter without the project dependencies (e.g. a bare
   system python) raises ``ModuleNotFoundError: uvicorn``. We detect that and
   hand off to the project venv.

Usage::

    python scripts/run_backend_real.py            # 127.0.0.1:8010, reload off
    python scripts/run_backend_real.py --reload   # hot-reload (development)

Log -> ``<repo>/qa_tmp/_run_backend_real.log`` (path kept stable: the local
health check ``verify_forgeflow.py`` reads that exact file).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import pathlib
import subprocess
import sys

# Derive the repo root from this file's own location instead of hardcoding it.
# This file now lives at ``<repo>/ForgeFlow-main/scripts/run_backend_real.py``,
# so ``parents[2]`` is the repo root. (The project has already been moved once;
# a hardcoded root silently breaks every future move, which is why the old
# launcher derived its root from ``__file__`` too.)
ROOT = pathlib.Path(__file__).resolve().parents[2]
FF = ROOT / "ForgeFlow-main"
LOG = ROOT / "qa_tmp" / "_run_backend_real.log"

# Interpreter to hand off to when the current one lacks the project deps.
# Overridable for other machines/CI images; the default is this box's venv.
VENV_PY = pathlib.Path(
    os.environ.get(
        "FORGEFLOW_VENV_PY",
        r"C:\Users\18769\.workbuddy\binaries\python\envs\agentflow\Scripts\python.exe",
    )
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8010


def _require_layout() -> None:
    """Fail loudly if the repository layout is not what we expect."""
    if not (FF / "forgeflow" / "bootstrap").is_dir():
        raise SystemExit(f"cannot find forgeflow/bootstrap under {FF} — is this the repo root?")


def _strip_proxy() -> None:
    """Drop the sandbox proxy so loopback calls stay direct (see docstring)."""
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1"
    os.environ["PYTHONUTF8"] = "1"


def _handoff_to_venv_if_needed() -> None:
    """If uvicorn isn't importable here, re-run this script under the project venv."""
    if importlib.util.find_spec("uvicorn") is not None:
        return
    if not VENV_PY.is_file():
        sys.stderr.write(
            f"uvicorn missing AND no interpreter at {VENV_PY}\n"
            "set FORGEFLOW_VENV_PY to the venv that has the project deps.\n"
        )
        raise SystemExit(2)
    sys.stderr.write(f"[boot] handing off to {VENV_PY}\n")
    # subprocess (not os.execv) — os.execv on Windows joins argv WITHOUT quoting,
    # which truncates any argument containing a space. The project's previous
    # location had a space in its path, so execv was unreliable there; subprocess
    # keeps this correct regardless of the current path.
    proc = subprocess.Popen([str(VENV_PY), os.path.abspath(__file__), *sys.argv[1:]])
    raise SystemExit(proc.wait())


def _redirect_logging_to_file() -> None:
    """Make startup observability unconditional (INC48 §7).

    Two traps motivated this block:

    1. Anything logged before uvicorn installs its own ``dictConfig`` has no
       handler attached, so INFO records are dropped — including the
       ``[BOOT]`` lines the bootstrap emits.
    2. Under a WMI-created (console-less) process the interpreter-level
       ``sys.stdout`` / ``sys.stderr`` objects are not guaranteed to follow a
       bare ``os.dup2``, so later writes vanish instead of reaching the file.

    A real ``FileHandler`` fixes (1); rebinding the stream objects fixes (2).
    """
    import io
    import logging

    LOG.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(LOG, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s")
    )
    logging.basicConfig(level=logging.INFO, handlers=[file_handler], force=True)
    sys.stdout = io.TextIOWrapper(
        os.fdopen(1, "wb", buffering=0),
        encoding="utf-8",
        errors="replace",
        write_through=True,
    )
    sys.stderr = io.TextIOWrapper(
        os.fdopen(2, "wb", buffering=0),
        encoding="utf-8",
        errors="replace",
        write_through=True,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python scripts/run_backend_real.py",
        description=(
            "Start the ForgeFlow API on the real local stack through the managed "
            "entry point (forgeflow.bootstrap.run_api_server)."
        ),
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"bind host (default: {DEFAULT_HOST})")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"bind port (default: {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--reload", action="store_true", help="enable uvicorn hot-reload (development only)"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Prepare the environment, then hand off to the managed startup entry point."""
    args = _parse_args(argv)

    _require_layout()
    _strip_proxy()
    _handoff_to_venv_if_needed()

    os.chdir(FF)
    sys.path.insert(0, str(FF))

    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(
            f"\n===== boot backend REAL pid={os.getpid()} cwd={FF} py={sys.executable} "
            f"reload={args.reload} via=forgeflow.bootstrap =====\n"
        )
        fh.flush()
        os.dup2(fh.fileno(), 1)
        os.dup2(fh.fileno(), 2)

        _redirect_logging_to_file()

        # The single startup strategy (INC48). reload_dirs is narrowed to the
        # package when reloading: the repo root holds thousands of scratch files
        # (and node_modules) that would thrash the watcher.
        from forgeflow.bootstrap import run_api_server

        run_api_server(
            "forgeflow.api.main:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            log_level="info",
            reload_dirs=[str(FF / "forgeflow")] if args.reload else None,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
