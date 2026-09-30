"""ForgeFlow FastAPI application — app factory with lifespan management.

Startup:
  1. Initialize asyncpg connection pool
  2. Compile LangGraph StateGraph (with PostgreSQL checkpointer)
  3. Load MCP tools from tool server (graceful fallback if unavailable)
  4. Register agents in A2A registry

Shutdown:
  1. Close connection pool
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from forgeflow.a2a.registry import register_default_agents
from forgeflow.config import Settings, get_settings
from forgeflow.database import close_pool, init_pool
from forgeflow.events.dispatcher import EventDispatcher
from forgeflow.graph.builder import compile_graph
from forgeflow.jobs.escalation import ApprovalEscalationJob, EscalationThresholds
from forgeflow.mcp.client.adapter import get_mcp_tools
from forgeflow.middleware.audit import AuditMiddleware
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.middleware.rate_limit import RateLimitMiddleware
from forgeflow.middleware.security import SecurityMiddleware
from forgeflow.middleware.security_headers import SecurityHeadersMiddleware
from forgeflow.observability.prometheus import _build_registry
from forgeflow.observability.tracing import init_tracing
from forgeflow.observability.tracing_provider import configure as configure_tracing_provider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("ForgeFlow API starting...")

    # Fail-fast configuration validation. In production any problem aborts
    # startup (fail closed); in dev we log warnings so local work isn't blocked.
    _startup_settings = get_settings()
    _config_problems = _startup_settings.validate_runtime()
    if _config_problems:
        for _p in _config_problems:
            logger.error("CONFIG: %s", _p)
        if _startup_settings.is_production():
            raise RuntimeError(
                f"Refusing to start: {len(_config_problems)} fatal configuration "
                f"problem(s) in production — see CONFIG errors above."
            )
        logger.warning(
            "Continuing in non-production despite %d config warning(s).",
            len(_config_problems),
        )

    # Database pool. The offline (memory) profile is zero-external-dependency:
    # it must boot with **no reachable PostgreSQL** (§E / K5 "双后端不可破").
    # ``pool=None`` is a first-class state — every read site already guards with
    # ``getattr(app.state, "pool", None)`` — so we skip pool creation entirely
    # offline instead of letting ``init_pool()`` abort startup on a dead DSN.
    # The postgres profile builds the pool exactly as before.
    offline = _startup_settings.storage_backend.lower() == "memory"
    if offline:
        app.state.pool = None
        logger.info("Offline (memory) profile — PostgreSQL pool skipped")
    else:
        app.state.pool = await init_pool()
        logger.info("Database pool ready")

    # Seed the demo users into the credential store so the local/dev password
    # login keeps working after the Increment-2 auth overhaul. Prod (dev login
    # off) skips this and relies on OIDC / externally-provisioned users. The
    # credential store is PostgreSQL-backed, so an offline profile (no pool)
    # skips seeding too rather than crashing on a None pool.
    if _startup_settings.dev_login_enabled and app.state.pool is not None:
        await _seed_demo_users(app.state.pool, _startup_settings)
    elif _startup_settings.dev_login_enabled and app.state.pool is None:
        # Offline (memory) profile — seed the *same* roster into the in-process
        # credential store so ``POST /auth/login`` works with no database. Same
        # roster (``auth.demo_users``) and same Argon2id hash helper as the PG
        # path, so the two profiles cannot drift apart.
        from forgeflow.auth import memory_store

        if memory_store.memory_auth_allowed(_startup_settings):
            await memory_store.seed_demo_users(
                _startup_settings.dev_login_password.get_secret_value(),
                workspace_id=_startup_settings.default_tenant_id,
            )
        else:
            logger.warning(
                "Offline profile but in-memory auth is not permitted "
                "(production-shaped deployment) — password login will 503"
            )

    # MCP tools (optional — agents degrade gracefully without them)
    mcp_tools = await get_mcp_tools()
    logger.info("MCP tools loaded: %d", len(mcp_tools))

    # Agent graphs — one compiled graph per workflow_type, prompts differ per domain
    app.state.graphs = {
        "sales_ops": await compile_graph(mcp_tools=mcp_tools, workflow_type="sales_ops"),
        "support_ops": await compile_graph(mcp_tools=mcp_tools, workflow_type="support_ops"),
        "finance_recon": await compile_graph(mcp_tools=mcp_tools, workflow_type="finance_recon"),
    }
    # Default exposure for code paths that still expect a single graph
    app.state.graph = app.state.graphs["sales_ops"]
    logger.info("Agent graphs compiled | types=%s", list(app.state.graphs))

    # A2A registry
    register_default_agents()
    logger.info("A2A registry populated")

    # AgentFlow hubs: seed the four built-in featured skills when running the
    # offline (memory) profile, so the home page renders real data with no PG.
    if _startup_settings.storage_backend.lower() == "memory":
        try:
            from forgeflow.skills.registry import SkillRegistry

            seeded = await SkillRegistry().seed_featured(_startup_settings.default_tenant_id)
            logger.info("Seeded %d featured skills (memory backend)", len(seeded))
        except Exception as exc:  # noqa: BLE001 — never block startup on seeding
            logger.warning("featured-skill seeding skipped: %s", exc)

    # Prometheus registry — populated lazily on each /metrics/prometheus scrape
    registry, prom_metrics = _build_registry()
    app.state.prom_registry = registry
    app.state.prom_metrics = prom_metrics
    logger.info("Prometheus registry initialised")

    # Approval escalation background task — queries approval_requests in
    # PostgreSQL, so it only runs when the pool exists. The offline (memory)
    # profile has no PG approvals table to ratchet; skipping it keeps the
    # background loop from spinning error iterations against a None pool.
    settings = get_settings()
    if app.state.pool is not None:
        escalation_job = ApprovalEscalationJob(
            pool=app.state.pool,
            interval_seconds=settings.approval_escalation_interval_seconds,
            thresholds=EscalationThresholds(
                first_escalation_minutes=settings.approval_first_escalation_minutes,
                second_escalation_minutes=settings.approval_second_escalation_minutes,
                auto_reject_minutes=settings.approval_auto_reject_minutes,
            ),
        )
        escalation_job.start()
        app.state.escalation_job = escalation_job
    else:
        app.state.escalation_job = None
        logger.info("Offline (memory) profile — approval escalation job skipped")

    # Optional event-driven consumer (Redis Streams or Kafka)
    app.state.event_consumer = None
    if settings.events_provider in ("redis", "kafka"):
        dispatcher = EventDispatcher(graphs=app.state.graphs)
        try:
            app.state.event_consumer = await _build_event_consumer(settings, dispatcher)
            await app.state.event_consumer.start()
        except Exception as exc:
            logger.warning(
                "Event consumer (%s) failed to start; continuing without it: %s",
                settings.events_provider,
                exc,
            )
            app.state.event_consumer = None

    # INC32 ADR-01/ADR-02 — honest startup收尾: the in-process background-task
    # references are necessarily gone after a restart, so any run a previous
    # process left ``running`` is marked ``interrupted`` (never a fabricated
    # completion). Best-effort — a missing table / dead DB must not block startup.
    try:
        from forgeflow.runtime.dispatcher import get_run_dispatcher

        await get_run_dispatcher().reconcile_on_start()
    except Exception as exc:  # noqa: BLE001 — housekeeping must never block startup
        logger.warning("workspace startup reconciliation skipped: %s", exc)

    logger.info("ForgeFlow API ready")
    yield

    logger.info("ForgeFlow API shutting down...")
    if getattr(app.state, "event_consumer", None):
        await app.state.event_consumer.stop()
    if getattr(app.state, "escalation_job", None):
        await app.state.escalation_job.stop()
    await close_pool()


async def _seed_demo_users(pool: Any, settings: Settings) -> None:
    """Idempotently upsert the demo users with the dev password (Argon2-hashed).
    No-op with a warning when DEV_LOGIN_PASSWORD is unset."""
    from forgeflow.auth import passwords
    from forgeflow.auth import users as user_store
    from forgeflow.auth.demo_users import DEMO_USERS

    password = settings.dev_login_password.get_secret_value()
    if not password:
        logger.warning("DEV_LOGIN_PASSWORD unset — skipping demo-user seeding")
        return
    password_hash = passwords.hash_password(password)
    for username, role in DEMO_USERS.items():
        try:
            await user_store.upsert_local_user(pool, username, password_hash, role)
        except Exception as exc:  # noqa: BLE001
            logger.warning("demo-user seed failed for %s: %s", username, exc)
    logger.info("Seeded %d demo users into the credential store", len(DEMO_USERS))


async def _build_event_consumer(settings: Settings, dispatcher: EventDispatcher) -> Any:
    """Construct the configured event consumer. Kept out of lifespan body
    so the lazy imports don't fire when events_provider=none.

    Return type is Any because the concrete class depends on which optional
    extra is installed; both consumers expose the same start()/stop() shape."""
    if settings.events_provider == "redis":
        from forgeflow.events.redis_consumer import RedisStreamsConsumer

        return RedisStreamsConsumer(
            dispatcher=dispatcher,
            redis_url=settings.events_redis_url,
            stream=settings.events_redis_stream,
            group=settings.events_redis_group,
            consumer_name=settings.events_redis_consumer,
        )
    if settings.events_provider == "kafka":
        from forgeflow.events.kafka_consumer import KafkaConsumer

        return KafkaConsumer(
            dispatcher=dispatcher,
            bootstrap_servers=settings.events_kafka_bootstrap_servers,
            topic=settings.events_kafka_topic,
            group_id=settings.events_kafka_group_id,
        )
    raise ValueError(f"unknown events_provider: {settings.events_provider}")


_settings_for_app = get_settings()
app = FastAPI(
    title="ForgeFlow API",
    description="Multi-Agent Enterprise Workflow Orchestrator — LangGraph + MCP + A2A",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _settings_for_app.docs_enabled else None,
    redoc_url="/redoc" if _settings_for_app.docs_enabled else None,
    openapi_url="/openapi.json" if _settings_for_app.docs_enabled else None,
)
if not _settings_for_app.dev_login_enabled:
    logger.info("DEV_LOGIN_ENABLED=false — /auth/login will return 404")
if not _settings_for_app.docs_enabled:
    logger.info("DOCS_ENABLED=false — /docs and /redoc disabled")

# Pick the tracing backend (phoenix / langfuse / langsmith / none), then wire
# OTel instrumentation if the selected backend uses OTLP.
configure_tracing_provider()
init_tracing(app)

# CORS — explicit origin allowlist from config. NEVER use "*" with credentials.
_cors_origins = get_settings().cors_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or [],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-Id"],
    max_age=600,
)

# Middleware stack — Starlette adds each middleware to the OUTSIDE, i.e. the
# LAST one registered is the OUTERMOST on the request path and the FIRST to see
# a request (and the last to touch the response). We want this request flow:
#
#   SecurityHeaders → Audit → RBAC → RateLimit → Security → handler
#
# Rationale (SECURITY_AUDIT.md API5 — every attempt must be auditable):
#   * SecurityHeaders outermost so hardening headers land on EVERY response,
#     including the 401/403/429 produced by the inner middlewares.
#   * Audit OUTSIDE RBAC so *rejected* attempts (RBAC 401/403, RateLimit 429,
#     Security blocks) are recorded too. AuditMiddleware reads
#     request.state.user_id/role AFTER `await call_next()`, so for allowed
#     requests the identity RBAC verified is still attributed correctly.
#   * RateLimit INSIDE RBAC so its bucket key uses the already-verified user_id
#     + workspace_id (an unauthenticated caller can't earn a fresh bucket).
#
# Because the last registration is outermost, we register in reverse below.
app.add_middleware(SecurityMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(RBACMiddleware)
app.add_middleware(AuditMiddleware)
# Outermost (added last) so hardening headers land on EVERY response, including
# 401/403/429 returned by the inner middlewares.
app.add_middleware(SecurityHeadersMiddleware)

# Routers
from forgeflow.api.routers import (
    agents,
    approvals,
    approvals_hub,
    audit,
    auth,
    codeplane,
    context,
    cost,
    experiences,
    marketplace,
    memory,
    metrics,
    policies,
    resources,
    runs,
    security as security_router,
    skills,
    tasks,
    workflows,
    workspace,
    workspaces,
)

app.include_router(auth.router, prefix="/auth", tags=["Auth"])
app.include_router(marketplace.router, prefix="/marketplace", tags=["Marketplace"])
app.include_router(workspaces.router, prefix="/workspaces", tags=["Workspaces"])
app.include_router(workflows.router, prefix="/workflows", tags=["Workflows"])
app.include_router(approvals.router, prefix="/approvals", tags=["Approvals"])
app.include_router(agents.router, prefix="/agents", tags=["Agents"])
app.include_router(memory.router, prefix="/memory", tags=["Memory"])
app.include_router(metrics.router, prefix="/metrics", tags=["Metrics"])
app.include_router(cost.router, prefix="/cost", tags=["Cost"])
app.include_router(audit.router, prefix="/audit", tags=["Audit"])

# --- AgentFlow hubs (docs/sop/02-ARCHITECTURE.md §4.1) ---
app.include_router(tasks.router, prefix="/tasks", tags=["Tasks"])
app.include_router(runs.router, prefix="/runs", tags=["Runs"])
app.include_router(experiences.router, prefix="/experiences", tags=["Experiences"])
app.include_router(skills.router, prefix="/skills", tags=["Skills"])
app.include_router(skills.candidates_router, prefix="/skill-candidates", tags=["Skill Candidates"])
app.include_router(policies.router, prefix="/policies", tags=["Policies"])
app.include_router(approvals_hub.router, prefix="/approvals", tags=["Approvals · Hub"])
app.include_router(security_router.router, prefix="/security", tags=["Security"])
# Context-builder observability. The RBAC entry ("GET", "/context") already
# existed but nothing served it — this makes that promise real (INC7).
app.include_router(context.router, prefix="/context", tags=["Context"])
# INC25 W1 — Resource Center (five resource kinds: register / list / detail /
# preview). Routes are gated in rbac/policies.py::ROUTE_PERMISSION_MAP.
app.include_router(resources.router, prefix="/resources", tags=["Resources"])
# INC25 W2 — code-plane approval closure (approve / reject). "重新分析" reuses the
# existing POST /runs/{id}/replan. Routes gated in rbac/policies.py.
app.include_router(codeplane.router, prefix="/codeplane", tags=["Code Plane"])
# INC32 ADR-06 — the workspace BFF: async dispatch (POST /workspace/tasks) +
# session surface (GET /workspace/sessions[/{id}]). Gated by the two new
# ("POST"/"GET", "/workspace") entries in rbac/policies.py (no existing entry
# changed). Live in the same app so it shares the in-process event bus + run store.
app.include_router(workspace.router, prefix="/workspace", tags=["Workspace"])


@app.get("/", include_in_schema=False)
async def root():
    return {"service": "ForgeFlow", "version": "0.1.0", "docs": "/docs"}


@app.get("/health")
async def health():
    pool = getattr(app.state, "pool", None)
    graph = getattr(app.state, "graph", None)
    return JSONResponse({
        "status": "healthy",
        "database": "connected" if pool else "unavailable",
        "graph": "compiled" if graph else "not_ready",
    })
