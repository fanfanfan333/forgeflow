"""Launcher for the thin agent server (INC29 §4).

Run by the ForgeFlow engine as a **bare script**::

    python <abs>/forgeflow/codeplane/runner/agent_server/__main__.py \
        --host 127.0.0.1 --port <p>

``uvicorn`` is imported lazily inside :func:`main` (the package must import in
both virtualenvs and ``uvicorn`` lives only in the OpenHands venv). When the file
is executed as a bare script there is no package context, so :func:`_load_app`
puts the package's parent directory on ``sys.path`` and imports the package as a
top-level ``agent_server`` instead of a relative import.

Binding policy mirrors the official ``__main__.py``: loopback by default; a
wildcard bind without a session API key emits a warning (an unauthenticated
server must never be exposed to the network by accident).

Intentional exemption from the managed bootstrap entry point (INC48 §17-2)
--------------------------------------------------------------------------
The bare ``uvicorn.run(...)`` call in :func:`main` is a **deliberate, reviewed
exemption** from ``forgeflow.bootstrap.run_api_server`` (the single managed
ForgeFlow startup entry point). It is safe to leave as-is for four reasons:

1. This module runs inside an **isolated OpenHands virtualenv**
   (``envs/openhands``), which does not contain ``forgeflow`` at all — it could
   not import the managed entry point even if it wanted to.
2. It does **not** depend on ``psycopg`` / ``asyncpg``, so it never exercises the
   "Windows ``ProactorEventLoop`` + psycopg3" root cause that
   ``forgeflow.bootstrap`` exists to fix.
3. It is the thin agent server of the Codeplane **execution plane**, not the
   ForgeFlow **control-plane** API server.
4. If it ever gains a ``psycopg`` dependency, or is ever started bare on Windows,
   it must be re-routed through ``forgeflow.bootstrap.run_api_server`` (tracked as
   a separate change) — the bare ``uvicorn.run`` below must not be extended.

(These mentions of ``forgeflow`` / ``openhands`` are plain prose, not imports;
the runner import-boundary gate ``tests/unit/test_inc28_codeplane_import_boundary.py``
scans AST imports only, so this is safe.)
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Callable

#: Hosts that bind every interface.
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "[::]"})
_LOOPBACK_HOST = "127.0.0.1"
_SESSION_API_KEY_ENV = "SESSION_API_KEY"


def _load_app() -> Callable[..., object]:
    """Return ``create_app`` from ``agent_server.app`` (works bare or as ``-m``)."""
    if __package__:
        from .app import create_app

        return create_app
    # Bare-script execution: ``__package__`` is empty, so a relative import would
    # fail. Make this package importable by its own name and import it absolutely.
    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(here)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    from agent_server.app import create_app  # noqa: E402 — path bootstrapped above

    return create_app


def main() -> int:
    import uvicorn  # lazy: keeps this package importable in the ForgeFlow venv

    create_app = _load_app()
    parser = argparse.ArgumentParser(description="Thin OpenHands agent server")
    parser.add_argument("--host", default=None, help="Bind host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000).")
    args = parser.parse_args()

    token = os.environ.get(_SESSION_API_KEY_ENV, "")
    host = args.host
    if host is None:
        host = _LOOPBACK_HOST
    elif host in _WILDCARD_HOSTS and not token:
        print(
            "WARNING: binding to all interfaces without SESSION_API_KEY — this "
            "agent server would be reachable from the network without auth.",
            file=sys.stderr,
            flush=True,
        )

    app = create_app(token=token or None)
    print(f"Starting thin agent server on {host}:{args.port}", flush=True)
    # Intentional exemption from forgeflow.bootstrap.run_api_server — see the
    # module docstring ("Intentional exemption ...") for the four reasons this
    # bare uvicorn.run is safe here. Do not extend it; re-route through the
    # managed entry point if this server ever needs psycopg or bare-Windows runs.
    uvicorn.run(app, host=host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
