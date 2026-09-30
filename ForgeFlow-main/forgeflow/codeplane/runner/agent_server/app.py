"""Starlette ASGI application for the thin agent server (INC29 §4).

Route paths and request/response field names mirror the official
``openhands-agent-server`` (``api.py::_add_api_routes`` mounts the sub-routers
under ``/api`` and the socket router at ``/sockets``). Endpoints this thin server
does not implement answer ``501 Not Implemented`` — an honest default, never a
fabricated success. The client only ever calls the small implemented subset.

Implemented (v0): ``POST /api/conversations`` (C1), ``GET/DELETE
/api/conversations/{id}`` (C2/C7), ``POST /api/conversations/{id}/run`` (C4),
``GET /api/conversations/{id}/events/search`` (C8), ``GET /api/conversations/count``
(health), ``GET /api/git/diff`` (C15), ``POST /api/bash/execute_bash_command``
(C18), and the ``WS /sockets/events/{id}`` stream (C21).

Wire discipline: every event frame is the §2.1 raw shape — ``type`` + ``seq`` +
``ts`` + passthrough raw fields. This module computes no ``label`` / ``phase`` /
``kind`` and no test pass/fail.

No ``openhands`` import and no ``forgeflow`` import (the package must import in
both virtualenvs and must never import the control plane).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .conversation import ConversationRuntime, new_conversation_id
from .models import (
    BashOutput,
    ConversationPage,
    EventPage,
    ExecuteBashRequest,
    GitDiff,
    StartConversationRequest,
    Success,
)

#: Environment variable the launcher sets with the per-process session key.
_SESSION_API_KEY_ENV = "SESSION_API_KEY"
#: Optional environment variable pinning the workspace root for the cwd boundary.
_WORKSPACE_ENV = "AGENT_SERVER_WORKSPACE"
#: Maximum concurrent WebSocket subscribers per conversation (official 1013 close).
_MAX_WS_SUBSCRIBERS = 8
#: Official page-size cap for the event search (``event_router.py::
#: search_conversation_events`` declares ``limit`` with ``gt=0, le=100``).
_MAX_SEARCH_LIMIT = 100
#: Poll cadence (seconds) for the WebSocket stream loop.
_WS_POLL_INTERVAL = 0.05

_JSON = "application/json"


# --------------------------------------------------------------------------- #
# Small helpers                                                                #
# --------------------------------------------------------------------------- #
def _error(detail: Any, status: int) -> JSONResponse:
    """The unified error body: 4xx/5xx ⇒ ``{"detail": ...}``."""
    return JSONResponse({"detail": detail}, status_code=status)


def _validation_error(exc: Exception) -> JSONResponse:
    """A 422 with sanitized error details (no raw request input echoed back)."""
    details: list[dict[str, Any]] = []
    raw = getattr(exc, "errors", None)
    if callable(raw):
        try:
            for item in raw():
                details.append(
                    {
                        "loc": [str(p) for p in item.get("loc", [])],
                        "msg": str(item.get("msg", "")),
                        "type": str(item.get("type", "")),
                    }
                )
        except Exception:  # noqa: BLE001 — a malformed error still yields a 422
            details = []
    if not details:
        details = [{"loc": [], "msg": str(exc), "type": "value_error"}]
    return _error(details, 422)


def _validate(model_cls: Any, data: Any) -> tuple[Any, JSONResponse | None]:
    if not isinstance(data, dict):
        return None, _error("Request body must be a JSON object", 422)
    try:
        return model_cls.model_validate(data), None
    except Exception as exc:  # noqa: BLE001 — pydantic ValidationError
        return None, _validation_error(exc)


async def _read_json(request: Request) -> Any:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001 — a non-JSON body is a client error
        return None


def _int_query(request: Request, name: str, default: int) -> int:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _to_int(raw: Any, default: int) -> int:
    """Parse ``raw`` as an int, falling back to ``default`` on any bad input."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _inside(path: str, root: str) -> bool:
    try:
        target = Path(path).resolve()
        base = Path(root).resolve()
    except Exception:  # noqa: BLE001 — an unresolvable path is not inside
        return False
    return target == base or base in target.parents


# --------------------------------------------------------------------------- #
# Authentication (pure-ASGI so 401 precedes routing / 501)                     #
# --------------------------------------------------------------------------- #
class SessionKeyMiddleware:
    """Reject an unauthenticated ``/api/*`` request with ``401`` before routing.

    The official server applies ``dependencies.check_session_api_key`` to every
    ``/api/*`` route; a thin pure-ASGI guard reproduces that precedence so an
    unimplemented endpoint still answers ``401`` (not ``501``) when the key is
    missing. WebSocket auth lives in the socket endpoint (it uses the
    ``session_api_key`` query parameter, like the official server).
    """

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token or ""

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") == "http" and self.token:
            path = scope.get("path") or ""
            if path.startswith("/api/"):
                headers = Headers(scope=scope)
                if headers.get("x-session-api-key") != self.token:
                    response = _error("Unauthorized", 401)
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


# --------------------------------------------------------------------------- #
# Conversation endpoints                                                       #
# --------------------------------------------------------------------------- #
async def conversations_collection(request: Request) -> Response:
    """``POST /api/conversations`` (C1); any other method ⇒ 501."""
    if request.method != "POST":
        return _error("Not Implemented", 501)
    data = await _read_json(request)
    model, err = _validate(StartConversationRequest, data)
    if err is not None:
        return err
    runtime = ConversationRuntime(new_conversation_id(), model)
    request.app.state.runtimes[runtime.conversation_id] = runtime
    return JSONResponse(runtime.info().model_dump(), status_code=201)


async def conversation_item(request: Request) -> Response:
    """``GET`` (C2) / ``DELETE`` (C7) ``/api/conversations/{id}``."""
    conversation_id = request.path_params["conversation_id"]
    runtime = request.app.state.runtimes.get(conversation_id)
    if request.method == "GET":
        if runtime is None:
            return _error("Conversation not found", 404)
        return JSONResponse(runtime.info().model_dump())
    if request.method == "DELETE":
        if runtime is None:
            return _error("Conversation not found", 404)
        runtime.shutdown()
        request.app.state.runtimes.pop(conversation_id, None)
        return JSONResponse(Success().model_dump())
    return _error("Not Implemented", 501)


async def conversation_run(request: Request) -> Response:
    """``POST /api/conversations/{id}/run`` (C4)."""
    if request.method != "POST":
        return _error("Not Implemented", 501)
    runtime = request.app.state.runtimes.get(request.path_params["conversation_id"])
    if runtime is None:
        return _error("Conversation not found", 404)
    if runtime.execution_status != "idle":
        return _error(
            "Conversation already running. Wait for completion or pause first.", 409
        )
    if not runtime.start():
        return _error(
            "Conversation already running. Wait for completion or pause first.", 409
        )
    return JSONResponse(Success().model_dump())


async def search_events(request: Request) -> Response:
    """``GET /api/conversations/{id}/events/search`` (C8).

    Mirrors the official page contract (``event_router.py::
    search_conversation_events``): an opaque ``page_id`` resume cursor (the
    ``next_page_id`` from the previous page) and ``limit`` (default 100, official
    cap 100). ``after_seq`` is kept as an **explicitly declared extension** — the
    official REST API has **no** such parameter (it lives only on the
    ``/sockets/session/{id}`` socket) — and is consulted only when ``page_id`` is
    absent, so the bundled client's ``seq``-style resume still works.

    The server still computes no business vocabulary: it returns the raw buffered
    frames verbatim.
    """
    if request.method != "GET":
        return _error("Not Implemented", 501)
    runtime = request.app.state.runtimes.get(request.path_params["conversation_id"])
    if runtime is None:
        return _error("Conversation not found", 404)

    limit = _int_query(request, "limit", _MAX_SEARCH_LIMIT)
    limit = max(1, min(limit, _MAX_SEARCH_LIMIT))  # official: gt=0, le=100

    page_id = request.query_params.get("page_id")
    if page_id not in (None, ""):
        # Official path: opaque cursor, inclusive resume position.
        start = _to_int(page_id, 0)
    else:
        # Declared extension: strictly-after ``seq`` (thin-server resume).
        start = _int_query(request, "after_seq", -1) + 1

    items, next_page_id = runtime.frames_from(start, limit)
    return JSONResponse(EventPage(items=items, next_page_id=next_page_id).model_dump())


async def count_conversations(request: Request) -> Response:
    """``GET /api/conversations/count`` — the liveness/health probe."""
    return JSONResponse(len(request.app.state.runtimes))


async def conversations_search(request: Request) -> Response:
    """``GET /api/conversations/search`` (C3) — not implemented in v0."""
    return _error("Not Implemented", 501)


# --------------------------------------------------------------------------- #
# Git / bash endpoints                                                         #
# --------------------------------------------------------------------------- #
def _run_git(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30, check=False
    )
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _git_diff_for_path(path: str, ref: str | None, commit: str | None) -> GitDiff:
    """A single-file ``GitDiff{original, modified}`` against ``ref``/``commit``."""
    target = Path(path)
    parent = str(target.parent if target.suffix else target)
    code, top, err = _run_git(["rev-parse", "--show-toplevel"], cwd=parent)
    if code != 0:
        raise RuntimeError(f"not a git repository: {parent} ({err.strip()})")
    top = top.strip()
    try:
        rel = target.resolve().relative_to(Path(top).resolve()).as_posix()
    except ValueError:
        rel = target.name
    baseline = commit or ref or "HEAD"
    code, original, err = _run_git(["show", f"{baseline}:{rel}"], cwd=top)
    if code != 0:
        raise RuntimeError(f"git show failed for {baseline}:{rel} ({err.strip()})")
    try:
        modified = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        modified = None
    return GitDiff(modified=modified, original=original)


async def git_diff(request: Request) -> Response:
    """``GET /api/git/diff`` (C15)."""
    if request.method != "GET":
        return _error("Not Implemented", 501)
    path = request.query_params.get("path") or ""
    if not path:
        return _error("'path' is required", 400)
    ref = request.query_params.get("ref")
    commit = request.query_params.get("commit")
    if ref and commit:
        return _error("'ref' and 'commit' are mutually exclusive", 400)
    try:
        return JSONResponse(_git_diff_for_path(path, ref, commit).model_dump())
    except Exception as exc:  # noqa: BLE001 — a git failure is a 400, not a 500
        return _error(str(exc), 400)


def _decode(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def _run_bash(req: ExecuteBashRequest) -> BashOutput:
    command_id = str(uuid.uuid4())
    timeout = int(req.timeout or 300)
    try:
        completed = subprocess.run(
            req.command, cwd=req.cwd or None, shell=True,
            capture_output=True, text=True, timeout=timeout,
        )
        return BashOutput(
            id=str(uuid.uuid4()),
            command_id=command_id,
            order=0,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            timeout=False,
        )
    except subprocess.TimeoutExpired as exc:
        return BashOutput(
            id=str(uuid.uuid4()),
            command_id=command_id,
            order=0,
            exit_code=None,
            stdout=_decode(exc.stdout),
            stderr=_decode(exc.stderr),
            timeout=True,
        )
    except Exception as exc:  # noqa: BLE001 — a harness failure is reported raw
        return BashOutput(
            id=str(uuid.uuid4()),
            command_id=command_id,
            order=0,
            exit_code=None,
            stdout=None,
            stderr=str(exc),
            timeout=False,
        )


async def execute_bash(request: Request) -> Response:
    """``POST /api/bash/execute_bash_command`` (C18) — raw ``exit_code``/``stdout``."""
    if request.method != "POST":
        return _error("Not Implemented", 501)
    data = await _read_json(request)
    model, err = _validate(ExecuteBashRequest, data)
    if err is not None:
        return err
    root = request.app.state.workspace_root
    if root and model.cwd and not _inside(model.cwd, root):
        return _error("cwd must be inside the conversation workspace", 422)
    return JSONResponse(_run_bash(model).model_dump())


# --------------------------------------------------------------------------- #
# WebSocket (C21)                                                              #
# --------------------------------------------------------------------------- #
async def events_socket(websocket: WebSocket) -> None:
    """``WS /sockets/events/{conversation_id}?session_api_key=&after_seq=`` (C21).

    Authentication is the ``session_api_key`` query parameter (the official
    socket auth method). Frames are the §2.1 raw event JSON, ``seq`` monotonic;
    a client reconnect passes ``after_seq=<last_committed_seq>`` to resume with no
    loss and no duplicate (A6). Close codes mirror the official server:
    ``4001`` auth failure, ``4004`` conversation not found, ``1013`` over capacity.
    """
    conversation_id = websocket.path_params["conversation_id"]
    token = websocket.app.state.token
    supplied = websocket.query_params.get("session_api_key") or websocket.headers.get(
        "x-session-api-key"
    )
    if token and supplied != token:
        await websocket.close(code=4001, reason="Authentication failed")
        return

    runtime = websocket.app.state.runtimes.get(conversation_id)
    if runtime is None:
        await websocket.close(code=4004, reason="Conversation not found")
        return

    subscribers = websocket.app.state.ws_subscribers
    if subscribers.get(conversation_id, 0) >= _MAX_WS_SUBSCRIBERS:
        await websocket.close(code=1013, reason="Too many connections for this conversation")
        return
    subscribers[conversation_id] = subscribers.get(conversation_id, 0) + 1

    await websocket.accept()
    cursor = _int_query_ws(websocket, "after_seq", -1)
    try:
        while True:
            for frame in runtime.frames_after(cursor):
                await websocket.send_json(frame)
                cursor = max(cursor, int(frame.get("seq", cursor)))
            if runtime.is_terminal() and cursor >= runtime.last_seq():
                break
            await asyncio.sleep(_WS_POLL_INTERVAL)
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001 — a broken socket must not crash the server
        return
    finally:
        subscribers[conversation_id] = max(0, subscribers.get(conversation_id, 1) - 1)
    try:
        await websocket.close(code=1000)
    except Exception:  # noqa: BLE001 — already closed is fine
        pass


def _int_query_ws(websocket: WebSocket, name: str, default: int) -> int:
    raw = websocket.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


async def not_implemented(request: Request) -> Response:
    """Every unimplemented ``/api/*`` path ⇒ ``501`` (honest default)."""
    return _error("Not Implemented", 501)


async def ws_not_implemented(websocket: WebSocket) -> None:
    """Every unimplemented ``/sockets/*`` path ⇒ close cleanly."""
    await websocket.close(code=1000)


# --------------------------------------------------------------------------- #
# App factory                                                                  #
# --------------------------------------------------------------------------- #
def create_app(*, token: str | None = None, workspace_root: str | None = None) -> Starlette:
    """Create the thin ASGI app.

    Args:
        token: the ``X-Session-API-Key`` REST / ``session_api_key`` WS secret;
            ``None`` ⇒ read ``SESSION_API_KEY`` from the environment (empty ⇒ auth
            disabled, matching the official "no key configured" behaviour).
        workspace_root: optional cwd boundary for ``C18``; ``None`` ⇒ read
            ``AGENT_SERVER_WORKSPACE``.
    """
    if token is None:
        token = os.environ.get(_SESSION_API_KEY_ENV, "")
    if workspace_root is None:
        workspace_root = os.environ.get(_WORKSPACE_ENV, "")

    routes = [
        # Concrete collection paths MUST precede the dynamic ``{conversation_id}``.
        Route("/api/conversations/count", count_conversations, methods=["GET"]),
        Route("/api/conversations/search", conversations_search, methods=["GET"]),
        Route(
            "/api/conversations",
            conversations_collection,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        ),
        Route(
            "/api/conversations/{conversation_id}/run",
            conversation_run,
            methods=["GET", "POST"],
        ),
        Route(
            "/api/conversations/{conversation_id}/events/search",
            search_events,
            methods=["GET"],
        ),
        Route(
            "/api/conversations/{conversation_id}",
            conversation_item,
            methods=["GET", "DELETE", "POST", "PUT", "PATCH"],
        ),
        Route("/api/git/diff", git_diff, methods=["GET"]),
        Route("/api/bash/execute_bash_command", execute_bash, methods=["POST"]),
        # Everything else under /api is unimplemented in v0.
        Route(
            "/api/{rest:path}",
            not_implemented,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        ),
        # WebSockets.
        WebSocketRoute("/sockets/events/{conversation_id}", events_socket),
        WebSocketRoute("/sockets/{rest:path}", ws_not_implemented),
    ]

    app = Starlette(routes=routes)
    app.state.token = token or ""
    app.state.workspace_root = workspace_root or ""
    app.state.runtimes = {}
    app.state.ws_subscribers = {}
    app.add_middleware(SessionKeyMiddleware, token=token or "")
    return app
