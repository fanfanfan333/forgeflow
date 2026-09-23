"""Shared pytest fixtures for unit and integration tests."""

from __future__ import annotations

import ipaddress
import os
import socket
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage

# Captured before the autouse DNS stub is installed so loopback / IP-literal
# lookups can still reach the real resolver (see ``_stub_dns`` below).
_REAL_GETADDRINFO = socket.getaddrinfo

# Ensure test env vars are set before any imports
os.environ.setdefault("OPENAI_API_KEY", "sk-test-fake-key-for-testing")
# Dev Postgres runs on 5433 (5432 is taken by an unrelated container on the
# local box). The offline profile (STORAGE_BACKEND=memory) never dials it; the
# postgres profile connects here. Export POSTGRES_URL to override.
os.environ.setdefault(
    "POSTGRES_URL", "postgresql+asyncpg://forgeflow:forgeflow@localhost:5433/forgeflow"
)
os.environ.setdefault(
    "POSTGRES_SYNC_URL", "postgresql+psycopg://forgeflow:forgeflow@localhost:5433/forgeflow"
)
os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
os.environ.setdefault("API_SECRET_KEY", "test-secret")
os.environ.setdefault("BUDGET_LIMIT_USD", "10.0")
# AgentFlow hubs (docs/sop/02-ARCHITECTURE.md): the offline profile. Keeps the
# new hub repositories/runtime/embeddings on in-memory + deterministic fallbacks
# so `pytest tests/` needs no PostgreSQL, Ollama, or API keys.
os.environ.setdefault("STORAGE_BACKEND", "memory")
os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("EMBEDDING_PROVIDER", "mock")
os.environ.setdefault("DEV_LOGIN_ENABLED", "true")
os.environ.setdefault("DEV_LOGIN_PASSWORD", "forgeflow-dev")
# support_ops + finance_recon are template scaffolds — their .run() raises in
# production unless dry_run=True. Tests bypass the guard via this opt-in flag
# (also documented in the pipelines' module docstrings).
os.environ.setdefault("FORGEFLOW_ALLOW_TEMPLATE_WORKFLOWS", "1")

# The TestRelic reporter (testrelic-pytest) reads TESTRELIC_API_KEY from the OS
# environment, but pytest doesn't auto-load .env where the rest of our config
# lives. Surface the TestRelic keys here so a bare `pytest` reports runs without
# a manual `export`. We never override an already-set value (CI secrets win),
# and the plugin silently no-ops when the key is absent.
from dotenv import dotenv_values, find_dotenv  # noqa: E402

_dotenv = dotenv_values(find_dotenv())
for _key in ("TESTRELIC_API_KEY", "TESTRELIC_PROJECT_NAME", "TESTRELIC_UPLOAD_STRATEGY"):
    _val = _dotenv.get(_key)
    if _val and not os.environ.get(_key):
        os.environ[_key] = _val


@pytest.fixture(autouse=True)
def _stub_dns(monkeypatch):
    """Make the test suite hermetic by stubbing DNS resolution.

    The SSRF guard (forgeflow.security.ssrf_guard.check_url) calls
    socket.getaddrinfo on every outbound connector request. Unit tests mock the
    HTTP transport but not DNS, so on a network-less runner the guard raised
    SSRFBlocked before the mocked client was ever reached. We resolve external
    hostnames to a fixed public IP (example.com's address) so the guard's real
    logic — scheme, userinfo, IP-literal and private-range checks — still runs,
    while no test touches the network.

    Loopback / private / IP-literal hosts are passed through to the **real**
    resolver: the postgres-profile tests dial ``localhost:5433`` for the dev
    database, and the guard's private-range tests pass IP literals. Rewriting
    those to a public IP (as an earlier version did) broke every real asyncpg
    connection with a bogus-address / WinError.
    """
    public_ip = "93.184.216.34"  # example.com — globally routable

    def _is_local(host: object) -> bool:
        if not host:
            return True
        name = str(host).strip("[]").lower()
        if name in ("localhost", "ip6-localhost", "ip6-loopback"):
            return True
        try:
            ip = ipaddress.ip_address(name)
        except ValueError:
            return False
        return ip.is_loopback or ip.is_private or ip.is_link_local

    def _fake_getaddrinfo(host, port, *args, **kwargs):
        if _is_local(host):
            return _REAL_GETADDRINFO(host, port, *args, **kwargs)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (public_ip, port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)


@pytest.fixture(autouse=True)
def _isolate_asyncpg_pool():
    """Give every test an asyncpg pool bound to *its own* event loop.

    ``forgeflow.database`` keeps a single module-global pool, pinned to the
    event loop that created it. pytest-asyncio (mode=auto) hands each test a
    fresh loop and ``TestClient`` runs the ASGI app on its own anyio loop, so a
    pool created during an earlier test gets reused from a different / closed
    loop — producing ``RuntimeError: Event loop is closed`` and
    ``asyncpg...InterfaceError: cannot perform operation: another operation is
    in progress`` in the postgres profile. Dropping the reference before each
    test makes the next ``get_pool()`` build a fresh pool on the current loop.
    """
    import forgeflow.database as _db

    _db._pool = None
    yield
    _db._pool = None


@pytest.fixture
def force_memory_backend(monkeypatch):
    """Pin the active storage/metrics backend to ``memory`` for one test.

    Several suites are *memory-profile by construction*: they assert on the
    in-process hub run store, the scoped memory store and the memory
    repositories. The code under test resolves ``get_settings().storage_backend``
    at call time, so when the whole run is launched with
    ``STORAGE_BACKEND=postgres`` those assumptions silently break (the memory
    assertions end up reading PostgreSQL). A test must therefore *declare* the
    backend it means to exercise instead of inheriting it from the environment —
    this fixture makes each such test deterministic under any ``STORAGE_BACKEND``.

    It patches the live settings instance (not the env var, which is already
    frozen at import) and drops the backend-keyed caches so the next lookup
    rebuilds against ``memory``. ``monkeypatch`` restores the original value and
    the fixture re-drops the caches on teardown.
    """
    from forgeflow.config import get_settings
    from forgeflow.observability.metrics_source import reset_metrics_source
    from forgeflow.repositories.factory import reset_repositories

    settings = get_settings()
    monkeypatch.setattr(settings, "storage_backend", "memory")
    reset_metrics_source()
    reset_repositories()
    yield settings
    reset_metrics_source()
    reset_repositories()


@pytest.fixture
def pg_purge():
    """Return a ``purge(*tables)`` helper that deletes rows from PostgreSQL tables.

    A no-op under the offline (memory) profile. It opens its **own** short-lived
    psycopg connection — never the module-global asyncpg pool — so it is safe for
    both sync and async tests and can never cross event loops. Suites that share
    a live dev database use this to guarantee row-counting assertions start from
    a clean slate, instead of inheriting rows left by an earlier test or a
    previous run (a shared, unclean ``skill_listings`` / ``cost_budgets`` table
    is the real defect behind those "Left contains N more items" failures).
    """

    def _purge(*tables: str) -> None:
        from forgeflow.config import get_settings

        settings = get_settings()
        if settings.storage_backend.lower() != "postgres":
            return
        if not tables:
            return
        import psycopg

        dsn = settings.postgres_sync_url.replace(
            "postgresql+psycopg://", "postgresql://"
        )
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                for table in tables:
                    cur.execute(f"DELETE FROM {table}")
            conn.commit()

    return _purge


@pytest.fixture
def mock_llm():
    """Returns a deterministic ChatOpenAI mock."""
    llm = MagicMock()
    llm.ainvoke = AsyncMock(
        return_value=AIMessage(
            content='{"next": "researcher", "reasoning": "test routing"}',
            name="mock",
        )
    )
    llm.with_structured_output = MagicMock(return_value=llm)
    llm.bind_tools = MagicMock(return_value=llm)
    return llm


@pytest.fixture
def mock_pool():
    """Mock asyncpg pool for unit tests."""
    pool = MagicMock()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="OK")
    pool.acquire = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=conn),
        __aexit__=AsyncMock(return_value=None),
    ))
    return pool


@pytest.fixture
def sample_workflow_state():
    """Returns a minimal valid WorkflowState for testing."""
    return {
        "messages": [],
        "research_results": [],
        "analysis_scores": [],
        "executed_actions": [],
        "errors": [],
        "workflow_id": "test-workflow-123",
        "thread_id": "test-thread-456",
        "current_stage": "qualify",
        "next_agent": None,
        "lead_id": None,
        "lead_data": {"company_name": "Acme Corp", "industry": "saas"},
        "proposal": None,
        "approval_status": None,
        "approval_token": None,
        "total_tokens": 0,
        "total_cost_usd": 0.0,
        "dry_run": False,
        "run_metadata": {"user_id": "test-user", "role": "sales_rep"},
    }
