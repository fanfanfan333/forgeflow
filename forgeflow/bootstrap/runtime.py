"""ForgeFlow runtime bootstrap — the single Windows-safe server entry point.

Why this module exists
----------------------
On Windows, ``psycopg`` (psycopg3), which backs the LangGraph checkpointer
(``forgeflow/graph/checkpointer.py``), refuses to run on a ``ProactorEventLoop``.
uvicorn 0.53.0 hard-codes that loop::

    # uvicorn/loops/asyncio.py
    def asyncio_loop_factory(use_subprocess: bool = False):
        if sys.platform == "win32" and not use_subprocess:
            return asyncio.ProactorEventLoop   # <-- reload/workers OFF
        return asyncio.SelectorEventLoop

and ``uvicorn.server.Server.run`` consumes it via
``asyncio_run(self.serve(...), loop_factory=self.config.get_loop_factory())`` —
i.e. uvicorn pins the loop through its **own factory** and never consults
``asyncio.set_event_loop_policy``. That is why, on this host, a bare
``uvicorn forgeflow.api.main:app`` (no ``--reload``) dies inside the lifespan
when ``STORAGE_BACKEND=postgres``, while ``--reload``/``--workers`` works by
accident (``use_subprocess=True`` happens to select a Selector loop).

The fix is therefore two-layered, and both layers live here — nowhere else:

1. ``install_windows_selector_policy()`` pins ``WindowsSelectorEventLoopPolicy``
   so *any* other asyncio entry point (ad-hoc scripts, pytest, tooling) that
   asks the policy for a loop gets a Selector loop.
2. ``ForgeFlowConfig`` overrides ``Config.get_loop_factory`` to hand uvicorn a
   Selector loop unconditionally. This is the layer that actually fixes
   ``reload=False``; policy alone is measurably insufficient (see INC48 report).

Both are required; neither is a monkeypatch of a private symbol — ``uvicorn``
documents ``get_loop_factory`` as the loop hook (it replaced ``setup_event_loop``
in 0.36.0), and ``Config`` is designed to be subclassed and pickled into the
reload/workers child process.

Scope
-----
Runtime / bootstrap only: platform detection, event-loop selection, and launch
diagnostics. No business logic, no DB schema, no config mutation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import sys
from collections.abc import Callable, Sequence
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

import uvicorn

logger = logging.getLogger("forgeflow.bootstrap")

#: Backend defaults. ``:8010`` is ForgeFlow's local port (``:8000`` is another
#: project on this host, ``:5173``/``:4173`` are the frontend dev/preview ports).
DEFAULT_APP = "forgeflow.api.main:app"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8010


# --------------------------------------------------------------------------- #
# Layer 1 — event loop policy (covers non-uvicorn asyncio entry points)
# --------------------------------------------------------------------------- #
def install_windows_selector_policy() -> str:
    """Idempotently pin the Windows event-loop policy to the Selector variant.

    Returns a short status token describing what happened, so callers/tests can
    assert the effect without re-deriving it: ``"noop-non-win32"`` (POSIX — the
    default policy is already selector-backed), ``"unavailable"`` (no such
    policy class on this interpreter), ``"already"`` (nothing to do) or
    ``"set"``.
    """
    if sys.platform != "win32":
        return "noop-non-win32"
    policy_cls = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy_cls is None:  # pragma: no cover — every CPython on Windows has it
        return "unavailable"
    if isinstance(asyncio.get_event_loop_policy(), policy_cls):
        return "already"
    asyncio.set_event_loop_policy(policy_cls())
    return "set"


# --------------------------------------------------------------------------- #
# Layer 2 — uvicorn loop factory (covers reload=False, the actual bug)
# --------------------------------------------------------------------------- #
def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """Return a fresh ``SelectorEventLoop``.

    Kept as a named module-level function (not a lambda) so it can also be used
    as uvicorn's ``loop=`` import string
    (``loop="forgeflow.bootstrap.runtime:selector_loop_factory"``) and so its
    ``__name__`` is a readable diagnostic.
    """
    return asyncio.SelectorEventLoop()


def _make_loop_factory() -> Callable[[], asyncio.AbstractEventLoop]:
    """Loop factory for the Windows profile. Split out for the tests to target
    the exact object uvicorn receives."""
    return selector_loop_factory


class ForgeFlowConfig(uvicorn.Config):
    """uvicorn ``Config`` whose loop is pinned to Selector on Windows.

    Overriding ``get_loop_factory`` (rather than only setting the event-loop
    policy) is the load-bearing part: ``Server.run`` takes the loop from here and
    ignores the policy entirely. On POSIX the base implementation is untouched.

    Defined at module level on purpose — ``reload=True`` / ``workers>1`` pickle
    the Config across the supervisor→child process boundary, and a *locally*
    defined class cannot be pickled (``PicklingError: attribute lookup
    ... failed``). Keeping it importable at
    ``forgeflow.bootstrap.runtime.ForgeFlowConfig`` is what makes the reload
    path work.
    """

    def get_loop_factory(self) -> Callable[[], asyncio.AbstractEventLoop] | None:
        if sys.platform == "win32":
            return _make_loop_factory()
        return super().get_loop_factory()


def get_config_class() -> type:
    """Return the config class uvicorn must be launched with."""
    return ForgeFlowConfig


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #
def _pkg_version(dist: str) -> str:
    try:
        return _dist_version(dist)
    except PackageNotFoundError:  # pragma: no cover
        return "not-installed"
    except Exception:  # noqa: BLE001 — diagnostics must never raise
        return "unknown"


def current_loop_name() -> str:
    """Name of the *actually running* event loop, or ``no-running-loop``.

    Called from inside the app lifespan this is the authoritative answer to
    "which loop is the server really on".
    """
    try:
        return type(asyncio.get_running_loop()).__name__
    except RuntimeError:
        return "no-running-loop"


def _safe_settings():
    try:
        from forgeflow.config import get_settings

        return get_settings()
    except Exception:  # noqa: BLE001 — diagnostics must never raise
        return None


def describe_runtime(*, event_loop: str | None = None) -> dict[str, str]:
    """Collect the runtime facts the startup block reports."""
    settings = _safe_settings()
    return {
        "platform": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "uvicorn": _pkg_version("uvicorn"),
        "psycopg": _pkg_version("psycopg"),
        "event_loop": event_loop or current_loop_name(),
        "storage_backend": str(getattr(settings, "storage_backend", "unknown")),
        "llm_provider": str(getattr(settings, "llm_provider", "unknown")),
    }


def log_boot_diagnostics(
    *, event_loop: str | None = None, prefix: str = "[BOOT]"
) -> dict[str, str]:
    """Emit the ``[BOOT] ...`` startup block and return the collected facts.

    Call this from the running app (inside the lifespan) so ``event_loop`` is
    the real loop class. It also raises a loud ``[BOOT] MISCONFIG`` line when
    the postgres profile is found on a non-Selector loop — that combination is
    exactly what psycopg rejects, and it must never pass silently.
    """
    info = describe_runtime(event_loop=event_loop)
    logger.info("%s ForgeFlow backend starting", prefix)
    logger.info("%s platform=%s", prefix, info["platform"])
    logger.info("%s python=%s", prefix, info["python"])
    logger.info("%s uvicorn=%s", prefix, info["uvicorn"])
    logger.info("%s psycopg=%s", prefix, info["psycopg"])
    logger.info("%s event_loop=%s", prefix, info["event_loop"])
    logger.info("%s storage_backend=%s", prefix, info["storage_backend"])
    logger.info("%s llm_provider=%s", prefix, info["llm_provider"])

    if (
        info["storage_backend"].lower() == "postgres"
        and sys.platform == "win32"
        and "Selector" not in info["event_loop"]
        and info["event_loop"] != "no-running-loop"
    ):
        logger.error(
            "%s MISCONFIG: storage_backend=postgres on loop=%s — psycopg's async "
            "connector requires SelectorEventLoop on Windows. The checkpointer "
            "will fail. Start the server via forgeflow.bootstrap.run_api_server().",
            prefix,
            info["event_loop"],
        )
    return info


def _describe_loop_factory(config: uvicorn.Config) -> str:
    """Human-readable description of the loop factory uvicorn will use."""
    try:
        factory = config.get_loop_factory()
    except Exception as exc:  # noqa: BLE001
        return f"<error {type(exc).__name__}: {exc}>"
    if factory is None:
        policy = type(asyncio.get_event_loop_policy()).__name__
        return f"None (uvicorn falls back to event-loop policy {policy})"
    name = getattr(factory, "__name__", repr(factory))
    if factory is selector_loop_factory:
        return "selector_loop_factory -> SelectorEventLoop"
    return name


# --------------------------------------------------------------------------- #
# The single entry point
# --------------------------------------------------------------------------- #
def run_api_server(
    app: str = DEFAULT_APP,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    reload: bool = False,
    workers: int | None = None,
    log_level: str = "info",
    reload_dirs: Sequence[str] | None = None,
    env_file: str | os.PathLike[str] | None = None,
    access_log: bool = True,
) -> None:
    """Start the ForgeFlow API with a Selector event loop on Windows.

    This mirrors uvicorn's own dispatch (``uvicorn.main.run``) so that
    ``reload``/``workers`` keep working, but builds a ``ForgeFlowConfig`` instead
    of a plain ``uvicorn.Config`` — the plain one would silently hand back a
    Proactor loop on ``reload=False``.

    ``reload=False`` is the default: it is closer to production, has a direct
    startup chain and clean logs. Pass ``reload=True`` (or ``workers>1``) for
    hot-reload development; both paths run through *this* function, so there is
    exactly one startup strategy.
    """
    import uvicorn.supervisors

    policy_state = install_windows_selector_policy()

    should_reload = bool(reload) or (workers or 1) > 1
    if should_reload and not isinstance(app, str):
        raise RuntimeError(
            "'reload'/'workers' require the application as an import string, "
            f"got {type(app).__name__}; pass e.g. {DEFAULT_APP!r}."
        )

    config_cls = get_config_class()
    config = config_cls(
        app,
        host=host,
        port=port,
        reload=reload,
        workers=workers,
        log_level=log_level,
        reload_dirs=list(reload_dirs) if reload_dirs else None,
        env_file=env_file,
        access_log=access_log,
    )

    logger.info(
        "%s launching uvicorn app=%s host=%s port=%s reload=%s workers=%s "
        "pid=%s policy=%s factory=%s",
        "[BOOT]",
        config.app,
        config.host,
        config.port,
        config.reload,
        config.workers,
        os.getpid(),
        type(asyncio.get_event_loop_policy()).__name__,
        _describe_loop_factory(config),
    )
    logger.debug("[BOOT] selector policy state=%s", policy_state)

    server = uvicorn.Server(config)

    if config.should_reload:
        sock = config.bind_socket()
        uvicorn.supervisors.ChangeReload(config, target=server.run, sockets=[sock]).run()
    elif (config.workers or 1) > 1:
        sock = config.bind_socket()
        uvicorn.supervisors.Multiprocess(config, sockets=[sock]).run()
    else:
        # Fail fast on an unimportable app string rather than deep inside
        # startup, matching uvicorn's own non-reload branch.
        config.load_app()
        server.run()


__all__ = [
    "DEFAULT_APP",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "ForgeFlowConfig",
    "current_loop_name",
    "describe_runtime",
    "get_config_class",
    "install_windows_selector_policy",
    "log_boot_diagnostics",
    "run_api_server",
    "selector_loop_factory",
]
