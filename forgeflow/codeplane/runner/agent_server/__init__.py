"""Thin OpenHands agent server (INC29 §4) — standalone, OpenHands venv only.

This package implements a *minimal* REST/WebSocket surface that mirrors the
official ``openhands-agent-server`` routes and models, so the ForgeFlow control
plane can drive OpenHands over HTTP instead of a stdio subprocess. It lives under
``codeplane/runner/**`` (the runner red-line domain):

  * it must **never** ``import forgeflow`` — the AST nail
    ``tests/unit/test_inc28_codeplane_import_boundary.py`` scans this directory
    recursively; ``openhands`` / ``starlette`` / ``uvicorn`` / ``pydantic`` /
    stdlib are the only imports it may use;
  * the whole package must import in BOTH virtualenvs, so ``openhands`` is
    imported lazily *inside* functions (``conversation.py``) and ``uvicorn``
    inside ``__main__:main`` — never at module scope.

Wire discipline (single source of truth): the server emits only *raw* event
frames — ``type`` (the SDK class name) + ``seq`` + ``ts`` + passthrough raw
fields. Any semantic mapping (``label`` / ``phase`` / ``kind`` / business
``status``) happens exclusively in
``forgeflow/codeplane/events.py::adapt_openhands_event`` — never here.

Only the endpoints the ForgeFlow client truly calls are implemented; every other
official endpoint answers ``501 Not Implemented`` (an honest default, never a
fabricated success).
"""

from __future__ import annotations

__all__: list[str] = []
