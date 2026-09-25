"""Central configuration — all environment variables loaded here via Pydantic Settings."""

import json
import logging
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

logger = logging.getLogger(__name__)

#: Canonical environment labels (INC12 A3). ``app_env`` accepts the aliases on
#: the left; anything unrecognised is normalised fail-closed to ``prod``.
_ENV_ALIASES: dict[str, str] = {
    "dev": "dev",
    "development": "dev",
    "staging": "staging",
    "stage": "staging",
    "prod": "prod",
    "production": "prod",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- LLM provider ---
    llm_provider: str = Field(
        "openai",
        description="Which LLM provider to use: openai | ollama | anthropic | mock",
    )

    # OpenAI
    openai_api_key: SecretStr = Field(
        SecretStr(""),
        description="OpenAI API key (required when llm_provider=openai)",
    )
    openai_model: str = Field("gpt-4o-mini", description="Default (cheap) model for agents")
    openai_model_strong: str = Field("gpt-4o", description="Strong model for supervisor + judge")

    # Ollama (local)
    ollama_base_url: str = Field(
        "http://localhost:11434",
        description="Ollama daemon URL",
    )
    ollama_model: str = Field("qwen2.5vl:3b", description="Default Ollama model")
    ollama_model_strong: str = Field("qwen2.5vl:3b", description="Strong Ollama model")

    # Anthropic
    anthropic_api_key: SecretStr = Field(
        SecretStr(""),
        description="Anthropic API key (required when llm_provider=anthropic)",
    )
    anthropic_model: str = Field("claude-haiku-4-5", description="Default Anthropic model")
    anthropic_model_strong: str = Field(
        "claude-sonnet-4-5", description="Strong Anthropic model"
    )

    # --- Database ---
    postgres_url: str = Field(
        "postgresql+asyncpg://forgeflow:forgeflow@localhost:5432/forgeflow",
        description="asyncpg DSN for application queries",
    )
    postgres_sync_url: str = Field(
        "postgresql+psycopg://forgeflow:forgeflow@localhost:5432/forgeflow",
        description="psycopg3 DSN for LangGraph checkpointer",
    )

    # --- LangSmith ---
    langchain_tracing_v2: bool = Field(True, description="Enable LangSmith tracing")
    langchain_endpoint: str = Field("https://api.smith.langchain.com")
    langchain_api_key: SecretStr = Field(
        SecretStr(""), description="LangSmith API key (optional)"
    )
    langchain_project: str = Field("forgeflow", description="LangSmith project name")

    # --- MCP Server ---
    mcp_server_host: str = Field("0.0.0.0")
    mcp_server_port: int = Field(8001)

    # --- FastAPI ---
    api_host: str = Field("0.0.0.0")
    api_port: int = Field(8000)
    api_secret_key: SecretStr = Field(
        SecretStr("change-me-in-production"),
        description="Secret key for signing JWTs. MUST be replaced in prod.",
    )

    # --- Auth — dev / OSS preview only ---
    # Set DEV_LOGIN_ENABLED=false in production. When true, /auth/login is the
    # demo-user issuer behind a shared password. When false, the route 404s
    # and you must integrate an OIDC IdP (see SECURITY_AUDIT.md C-3).
    dev_login_enabled: bool = Field(
        True,
        description="Expose /auth/login dev path. Set false once OIDC is wired.",
    )
    dev_login_password: SecretStr = Field(
        SecretStr(""),
        description=(
            "Shared dev password gating /auth/login. Required when "
            "dev_login_enabled=true; the route refuses to mint tokens "
            "without it set so accidental prod exposure fails closed."
        ),
    )

    # --- Tokens (Increment 2 auth) ---
    access_token_ttl_hours: int = Field(
        1, ge=1, le=24, description="Access-token lifetime (short-lived JWT)."
    )
    refresh_token_ttl_days: int = Field(
        30, ge=1, le=365, description="Refresh-token lifetime (rotating, revocable)."
    )

    # --- OIDC (enterprise SSO — verify an external IdP's id_token) ---
    oidc_enabled: bool = Field(
        False, description="Enable POST /auth/oidc/exchange (external IdP login)."
    )
    oidc_issuer: str = Field("", description="Expected 'iss' of the IdP id_token.")
    oidc_audience: str = Field("", description="Expected 'aud' (this app's client id).")
    oidc_jwks_url: str = Field("", description="IdP JWKS endpoint for RS256 verification.")
    oidc_default_role: str = Field(
        "viewer", description="Role assigned to auto-provisioned OIDC users."
    )

    # --- API surface toggles ---
    docs_enabled: bool = Field(
        True,
        description="Serve /docs and /redoc. Disable in production.",
    )

    # --- Deployment environment (INC12 A3) ---
    # The environment label that gates development-only *tool* stubs
    # (``runtime.tool_registry`` bindings of kind "development"). It is separate
    # from ``otel_environment`` (which is only a tracing label) on purpose so a
    # deployment can set the tool gate without touching tracing. Default "dev"
    # keeps the offline profile byte-for-byte unchanged. An unrecognised value is
    # normalised fail-closed to "prod" (see :meth:`environment`).
    app_env: str = Field(
        "dev",
        description="Deployment environment gate for dev-only tools: dev | staging | prod.",
    )

    # --- CORS allowlist ---
    cors_allow_origins: str = Field(
        "http://localhost:5173,http://localhost:8501",
        description="Comma-separated origins allowed by CORS. Use exact origins, never '*'.",
    )

    # --- Trust boundary for proxy headers ---
    trusted_proxy_count: int = Field(
        0,
        ge=0,
        le=10,
        description=(
            "Number of trusted reverse-proxy hops in front of the API. "
            "Determines how many X-Forwarded-For entries to trust; 0 means "
            "we ignore the header and use request.client.host."
        ),
    )

    # --- Resilience ---
    max_retries: int = Field(3, ge=1, le=10)
    circuit_breaker_threshold: int = Field(5, ge=1)
    budget_limit_usd: float = Field(5.0, gt=0, description="Max USD spend per workflow run")
    workflow_run_timeout_seconds: int = Field(
        180,
        ge=5,
        le=1800,
        description=(
            "Hard ceiling on a synchronous /workflows/run. Exceeding it returns "
            "504 and frees the worker, so a hung LLM/tool can't pin a request "
            "indefinitely. (Long jobs should use the async path — see ROADMAP.)"
        ),
    )

    # --- A2A protocol ---
    a2a_dispatch_enabled: bool = Field(
        True,
        description=(
            "Route LangGraph node invocations through the A2A registry. "
            "Required if agents are deployed out-of-process via HTTPTransport."
        ),
    )

    # --- Approval escalation ---
    approval_escalation_interval_seconds: int = Field(
        300,
        ge=10,
        description="How often the escalation background task runs (>=10s)",
    )
    approval_first_escalation_minutes: int = Field(
        30, ge=1, description="Pending approval older than this -> level 1 (manager)"
    )
    approval_second_escalation_minutes: int = Field(
        120, ge=1, description="Pending approval older than this -> level 2 (director)"
    )
    approval_auto_reject_minutes: int = Field(
        1440, ge=1, description="Pending approval older than this -> auto-rejected"
    )

    # --- Search ---
    tavily_api_key: SecretStr = Field(SecretStr(""), description="Tavily search API key")

    # --- TestRelic (test-analytics reporter — testrelic-pytest plugin) ---
    testrelic_api_key: SecretStr = Field(
        SecretStr(""),
        description="TestRelic API key — authenticates the pytest reporter upload",
    )
    testrelic_project_name: str = Field(
        "ForgeFlow",
        description="Project name shown in the TestRelic dashboard",
    )
    testrelic_upload_strategy: str = Field(
        "batch",
        description="When the reporter uploads results: batch (end of run) | realtime",
    )

    # --- OpenTelemetry (optional — for Phoenix/Langfuse/Jaeger/Datadog APM) ---
    otel_enabled: bool = Field(False, description="Toggle OpenTelemetry tracing")
    otel_service_name: str = Field("forgeflow-api")
    otel_environment: str = Field("development", description="prod | staging | development")
    otel_exporter_endpoint: str = Field(
        "http://localhost:4318/v1/traces",
        description="OTLP-HTTP endpoint (Phoenix/Langfuse/Tempo/etc.)",
    )
    otel_exporter_headers: str = Field(
        "",
        description="Comma-separated 'k=v' headers for the OTLP exporter (e.g. auth)",
    )

    # --- Tracing provider switch (high-level — sets OTel endpoint accordingly) ---
    tracing_provider: str = Field(
        "langsmith",
        description="langsmith | phoenix | langfuse | none",
    )

    # --- HubSpot connector ---
    hubspot_access_token: SecretStr = Field(
        SecretStr(""),
        description="HubSpot Private App access token",
    )
    hubspot_base_url: str = Field(
        "https://api.hubapi.com",
        description="HubSpot REST API base",
    )

    # --- ServiceNow connector ---
    servicenow_instance_url: str = Field(
        "",
        description="ServiceNow tenant URL, e.g. https://acme.service-now.com",
    )
    servicenow_username: str = Field(
        "", description="Service account username for Basic auth"
    )
    servicenow_password: SecretStr = Field(
        SecretStr(""), description="Service account password"
    )

    # --- SAP S/4HANA connector ---
    sap_base_url: str = Field(
        "",
        description="S/4HANA host, e.g. https://my300000-api.s4hana.cloud.sap",
    )
    sap_username: str = Field("", description="SAP technical user")
    sap_password: SecretStr = Field(
        SecretStr(""), description="SAP password / client secret"
    )
    sap_client: str = Field("100", description="SAP client number")

    # --- QuickBooks Online connector ---
    quickbooks_access_token: SecretStr = Field(
        SecretStr(""),
        description="Intuit OAuth access token (refreshed externally)",
    )
    quickbooks_realm_id: str = Field(
        "", description="QuickBooks Company ID (realmId from OAuth callback)"
    )
    quickbooks_environment: str = Field(
        "production", description="sandbox | production"
    )
    quickbooks_minor_version: int = Field(
        65, description="Intuit API minor version"
    )

    # --- Microsoft Graph connector ---
    msgraph_access_token: SecretStr = Field(
        SecretStr(""),
        description="Azure AD OAuth bearer token (refreshed externally via MSAL)",
    )
    msgraph_tenant_id: str = Field(
        "", description="Azure AD tenant ID — used by external token refresh"
    )
    msgraph_base_url: str = Field(
        "https://graph.microsoft.com/v1.0",
        description="Microsoft Graph API base URL",
    )

    # --- Anonymous telemetry (opt-in) ---
    telemetry_enabled: bool = Field(
        False,
        description="Send anonymous event counts to telemetry_webhook_url. OFF by default.",
    )
    telemetry_webhook_url: str = Field(
        "",
        description="HTTP endpoint receiving JSON events (PostHog, Mixpanel, custom).",
    )
    telemetry_install_id: str = Field(
        "",
        description="Anonymous installation UUID. Empty means generate one at first emit.",
    )
    telemetry_version: str = Field(
        "0.1.0", description="Reported ForgeFlow version in every event"
    )

    # --- Event-driven mode ---
    events_provider: str = Field(
        "none",
        description="Event consumer to start at boot: none | redis | kafka",
    )
    events_redis_url: str = Field(
        "redis://localhost:6379/0",
        description="Redis connection URL (when events_provider=redis)",
    )
    events_redis_stream: str = Field(
        "forgeflow:workflows", description="Stream name to consume from"
    )
    events_redis_group: str = Field(
        "forgeflow", description="Consumer group name"
    )
    events_redis_consumer: str = Field(
        "forgeflow-api", description="Consumer name within the group"
    )
    events_kafka_bootstrap_servers: str = Field(
        "localhost:9092", description="Comma-separated Kafka bootstrap servers"
    )
    events_kafka_topic: str = Field(
        "forgeflow.workflows", description="Topic to consume from"
    )
    events_kafka_group_id: str = Field(
        "forgeflow", description="Kafka consumer group id"
    )

    # --- Salesforce connector ---
    salesforce_instance_url: str = Field(
        "",
        description="Per-tenant instance URL, e.g. https://acme.my.salesforce.com",
    )
    salesforce_access_token: SecretStr = Field(
        SecretStr(""),
        description="OAuth bearer token (acquire via sf CLI or JWT bearer flow)",
    )
    salesforce_api_version: str = Field(
        "v59.0", description="Salesforce REST API version"
    )

    # --- Jira connector ---
    jira_base_url: str = Field(
        "",
        description="Jira tenant URL, e.g. https://acme.atlassian.net",
    )
    jira_email: str = Field(
        "",
        description="Atlassian account email (Basic auth username)",
    )
    jira_api_token: SecretStr = Field(
        SecretStr(""),
        description="Jira API token from id.atlassian.com",
    )

    # --- GitHub connector ---
    github_token: SecretStr = Field(
        SecretStr(""),
        description="GitHub PAT or installation token (repo scope)",
    )
    github_base_url: str = Field(
        "https://api.github.com",
        description="GitHub REST API base — override for GHES",
    )
    github_default_owner: str = Field(
        "",
        description="Default repo owner so tool calls can omit it",
    )

    # --- Slack (for HITL approval notifications) ---
    slack_bot_token: SecretStr = Field(
        SecretStr(""),
        description="Slack bot user OAuth token (xoxb-...)",
    )
    slack_default_channel: str = Field(
        "",
        description="Default Slack channel for posts (e.g. #forgeflow or C0123456)",
    )
    api_public_url: str = Field(
        "http://localhost:8000",
        description="Externally-reachable API base URL — used for approval deep-links in Slack",
    )

    # --- Dashboard ---
    api_url: str = Field("http://localhost:8000", description="Used by Streamlit to call the API")

    # ------------------------------------------------------------------ #
    # AgentFlow hub extensions (docs/sop/02-ARCHITECTURE.md §10.3)        #
    # ------------------------------------------------------------------ #
    # Data-access backend for the new Repository layer. `memory` runs the
    # whole closed loop (Experience / Skill / Policy) with no PostgreSQL
    # connection, which is what the offline demo + unit tests rely on.
    storage_backend: str = Field(
        "postgres",
        description="Repository backend: postgres | memory",
    )

    # Embedding provider for experiences / memory vectors. `mock` returns a
    # deterministic 1536-dim vector so semantic search works without OpenAI.
    embedding_provider: str = Field(
        "openai",
        description="Embedding backend: openai | mock",
    )
    embedding_dimension: int = Field(
        1536, ge=1, description="Vector dimension for embeddings (pgvector column width)"
    )

    # Closed-loop jump ③ (Skill Candidate Compiler) thresholds.
    skill_candidate_min_experiences: int = Field(
        3,
        ge=1,
        description="Minimum number of similar experiences required to compile a skill candidate (N)",
    )
    skill_candidate_similarity_threshold: float = Field(
        0.85,
        ge=0.0,
        le=1.0,
        description="Cosine-similarity floor for experiences to count as 'similar' (τ)",
    )
    skill_featured_limit: int = Field(
        4, ge=1, le=20, description="Number of featured skill cards shown on the home page"
    )
    # Skill CI/CD release gate (architecture review #2/#6). A *passing* evaluation
    # is an absolute bar, so a new version that scores just above the threshold
    # still promotes even when it is worse than the version it supersedes. This
    # switch enables the relative check in skills/release_gate.py.
    skill_release_gate_enabled: bool = Field(
        True,
        description=(
            "On promotion of an *upgrade*, require the candidate's recorded "
            "evaluation metrics not to regress beyond tolerance versus the "
            "version being superseded (403 otherwise)."
        ),
    )

    # ------------------------------------------------------------------ #
    # INC9 B1 — Skill canary release (docs/sop/12-INC9-DESIGN.md §2.1)    #
    # All default to the historical behaviour (canary off, 0% exposure).  #
    # ------------------------------------------------------------------ #
    skill_canary_enabled: bool = Field(
        False,
        description=(
            "INC9 B1: when True, a promoted upgrade is released as a *canary* "
            "version (release_state='canary', current_version stays the "
            "incumbent) instead of switching over at once. Default False = the "
            "existing all-at-once promote, byte-for-byte unchanged."
        ),
    )
    skill_canary_min_samples: int = Field(
        10,
        ge=1,
        description=(
            "INC9 B1: minimum A/B sample count before resolve_canary may "
            "promote/rollback; below it the decision is 'hold' (never conclude "
            "on noise)."
        ),
    )
    skill_canary_traffic_pct: int = Field(
        0,
        ge=0,
        le=100,
        description=(
            "INC9 B1: controlled exposure in the *selection* layer "
            "(SkillRegistry.select) — a deterministic sha1(seed)%100 < pct "
            "fraction of requests see the canary version. Default 0 = no "
            "exposure, selection behaviour unchanged. NOTE: the platform has no "
            "per-request serving/router layer for skills; this is selection-layer "
            "exposure, not a per-request outcome split."
        ),
    )

    # ------------------------------------------------------------------ #
    # INC9 B2 — Memory lifecycle Score / Decay / Archive                  #
    # (docs/sop/12-INC9-DESIGN.md §2.2). All default to no-op.            #
    # ------------------------------------------------------------------ #
    memory_decay_enabled: bool = Field(
        False,
        description=(
            "INC9 B2: master switch for the memory-lifecycle sweep "
            "(GET /memory/lifecycle + POST /memory/lifecycle/sweep). Default "
            "False = sweep is a no-op, decay/scores are not applied."
        ),
    )
    memory_decay_half_life_days: float = Field(
        30.0,
        gt=0,
        description="INC9 B2: freshness half-life (days) for the decay term.",
    )
    memory_archive_min_score: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description=(
            "INC9 B2: archive entries scoring below this. **0 = never archive "
            "(default ⇒ behaviour unchanged).** Archive is a marker, never a "
            "delete."
        ),
    )
    memory_archive_max_age_days: float = Field(
        0.0,
        ge=0.0,
        description=(
            "INC9 B2: also archive entries older than this many days. 0 = no "
            "age rule (default). Companion to memory_archive_min_score so both "
            "terms of lifecycle.should_archive are wired, not dead."
        ),
    )
    memory_score_w_reuse: float = Field(
        0.5,
        ge=0.0,
        le=1.0,
        description="INC9 B2: weight of the reuse term in compute_score.",
    )
    memory_score_w_fresh: float = Field(
        0.3,
        ge=0.0,
        le=1.0,
        description="INC9 B2: weight of the freshness (decay) term in compute_score.",
    )
    memory_score_w_promote: float = Field(
        0.2,
        ge=0.0,
        le=1.0,
        description="INC9 B2: weight of the promoted term in compute_score.",
    )

    # Security hub.
    tenant_isolation_level: str = Field(
        "row",
        description=(
            "Tenant isolation strategy, chosen from {row, schema, physical}. "
            "INC9 B3 (docs/sop/12-INC9-DESIGN.md §2.3) adjudicated this to "
            "'row' — application-layer row filtering (tenant_id-first "
            "repositories) over Postgres RLS (fragile with a shared asyncpg "
            "pool + not reproducible offline) or schema-per-tenant (fan-out "
            "migrations break the single-head constraint)."
        ),
    )
    default_tenant_id: str = Field(
        "default",
        description="Fallback tenant when the request carries no workspace claim",
    )
    dlp_enabled: bool = Field(
        True,
        description="Master switch for the DLP gate (PII/outbound scanning)",
    )

    # Validation / replan loop.
    max_replan_attempts: int = Field(
        2,
        ge=0,
        le=10,
        description="Replan retry ceiling; exceeding it escalates to HITL",
    )
    # Loop *budget* ceilings (architecture review #10). max_replan_attempts caps
    # the loop by count only, which a single expensive attempt can defeat. These
    # two are consumed by validation.loop_breaker.LoopBreaker: crossing either one
    # stops replanning and escalates to HITL. Defaults are deliberately generous
    # so the offline profile's behaviour is unchanged.
    max_run_tokens: int = Field(
        200_000,
        ge=1,
        description=(
            "Cumulative token ceiling for one run's replan loop; crossing it "
            "trips the loop breaker and escalates to HITL."
        ),
    )
    max_run_seconds: float = Field(
        600.0,
        gt=0,
        description=(
            "Wall-clock ceiling (seconds) for one run's replan loop; crossing it "
            "trips the loop breaker and escalates to HITL."
        ),
    )

    # ------------------------------------------------------------------ #
    # Increment 2 (INC2) extensions                                      #
    # docs/sop/05-ARCHITECTURE-INC2.md §2 / §7                            #
    # ------------------------------------------------------------------ #
    # --- A1 Cost optimization ---
    cost_currency: str = Field(
        "CNY", description="Currency for cost accounting (INC2 A1)"
    )
    cost_warn_ratio: float = Field(
        0.80,
        ge=0.0,
        le=1.0,
        description="Budget warning ratio; ratio >= this (and < 1.0) => 'warn'",
    )
    cost_exceed_actions: str = Field(
        "swap_model,trim_context,pause_noncritical",
        description=(
            "Comma-separated degrade actions applied, in order, when a budget is "
            "exceeded (ratio >= 1.0). See Settings.cost_exceed_action_list()."
        ),
    )
    cost_savings_baseline_multiplier: float = Field(
        1.0,
        gt=0.0,
        description=(
            "Savings baseline = previous-period actual cost × this multiplier "
            "(architecture §7.1). Configurable so the baseline can be tuned "
            "without a code change."
        ),
    )

    # --- A2 SLO tiers (frozen values, §7.2) ---
    slo_critical_availability: float = Field(
        0.995, ge=0.0, le=1.0, description="SLO target: critical tier availability"
    )
    slo_critical_p95_ms: int = Field(
        1500, ge=1, description="SLO target: critical tier p95 latency (ms)"
    )
    slo_important_availability: float = Field(
        0.990, ge=0.0, le=1.0, description="SLO target: important tier availability"
    )
    slo_important_p95_ms: int = Field(
        2000, ge=1, description="SLO target: important tier p95 latency (ms)"
    )
    slo_edge_availability: float = Field(
        0.970, ge=0.0, le=1.0, description="SLO target: edge tier availability"
    )
    slo_edge_p95_ms: int = Field(
        5000, ge=1, description="SLO target: edge tier p95 latency (ms)"
    )

    # --- HITL risk policy (review finding ⑤) ---
    # The PolicyEngine's HITL trigger conditions are **configurable policy**, not
    # hard-coded Python constants (review ⑤: "触发条件由策略统一决策、可审计").
    # The defaults are byte-identical to the historical hard-coded sets, so the
    # default classification is unchanged.
    high_risk_actions: str = Field(
        "delete,drop,destroy,truncate,purge,revoke",
        description=(
            "Comma-separated actions the PolicyEngine classifies as HIGH risk "
            "(⇒ HITL). Defaults equal the historical hard-coded set."
        ),
    )
    high_risk_resources: str = Field(
        "transfer,payment,pay,funds,wire,payout",
        description=(
            "Comma-separated resources the PolicyEngine classifies as HIGH risk "
            "(⇒ HITL). Defaults equal the historical hard-coded set."
        ),
    )

    # --- A5 Context builder ---
    context_budget_tokens: int = Field(
        2000,
        ge=50,
        le=200_000,
        description="Default token budget for build_context() (architecture §2.5)",
    )
    context_per_item_ratio: float = Field(
        0.4,
        gt=0.0,
        le=1.0,
        description="Per-item token cap as a fraction of the total budget (budget*0.4)",
    )

    # --- B4 dedup / conflict resolution ---
    dedup_merge_threshold: float = Field(
        0.95,
        ge=0.0,
        le=1.0,
        description=(
            "Cosine-similarity floor at which two experiences are merged into "
            "one instead of kept as a conflict pair (architecture §2.11)"
        ),
    )

    # --- C1 multi-model fallback routing ---
    # NOTE: 'mock' is deliberately NOT part of the default chain. It is a
    # development-only stub, appended implicitly by get_model() **only outside
    # production**; in production an exhausted chain raises ModelUnavailableError
    # instead of silently degrading to canned output (T1).
    model_fallback_chain: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["ollama"],
        description=(
            "Ordered provider fallback chain used by get_model(). Accepts a JSON "
            "array or a comma-separated string. Outside production the "
            "deterministic 'mock' provider is appended automatically; in "
            "production 'mock' is never a fallback and an exhausted chain raises."
        ),
    )
    ollama_think: bool = Field(
        False,
        description=(
            "Enable Ollama thinking mode. MUST stay false: qwen3 thinking models "
            "can consume the whole num_predict budget and return an empty string. "
            "Wired into the Ollama provider's 'reasoning' flag (T2) and into the "
            "C1 thinking-model guard; production flags a true value as a fatal "
            "misconfiguration."
        ),
    )

    # --- Agent runtime path (INC4 §A / T0) ---
    # Consumed by runtime.orchestrator.resolve_agent_runtime_mode(). 'auto'
    # resolves from llm_provider so the offline profile (LLM_PROVIDER=mock) keeps
    # the pre-INC4 deterministic platform graph byte-for-byte, while a real
    # provider drives the LLM planning/reflection path.
    agent_runtime_mode: Literal["auto", "llm", "deterministic"] = Field(
        "auto",
        description=(
            "Agent execution path: auto | llm | deterministic. 'auto' resolves "
            "from llm_provider — a real provider (not 'mock') ⇒ 'llm' (real LLM "
            "planning/reflection), otherwise ⇒ 'deterministic' (the original "
            "platform graph, so the offline suite's behaviour and timing are "
            "unchanged). 'llm' / 'deterministic' pin the path explicitly."
        ),
    )

    # --- A4 skill marketplace ---
    marketplace_cross_tenant: bool = Field(
        False,
        description=(
            "Allow cross-tenant skill sharing in the marketplace. Default FALSE "
            "(tenant-only); shared=True requires an explicit approve:skills policy "
            "(architecture §7.3)."
        ),
    )

    # --- C4 multimodal attachments ---
    multimodal_max_bytes: int = Field(
        5 * 1024 * 1024,
        gt=0,
        description="Maximum accepted attachment body size for POST /tasks (5 MB)",
    )

    # --- B1 DLP configurable rules ---
    dlp_rules_file: str = Field(
        "",
        description=(
            "Path to a JSON file overriding/adding DLP rule categories. Empty "
            "means use the built-in default rule set (which includes cn_id)."
        ),
    )

    @field_validator("model_fallback_chain", mode="before")
    @classmethod
    def _parse_model_fallback_chain(cls, value: object) -> list[str]:
        """Accept a JSON array, a comma-separated string, or a list.

        ``NoDecode`` stops pydantic-settings from JSON-decoding the env value
        before this runs, so a plain ``ollama,mock`` string is valid too.

        Empty / missing falls back to ``["ollama"]`` — the deterministic
        ``mock`` provider is never part of the configured chain (it is appended
        implicitly, and only outside production — see provider._fallback_candidates).
        """
        if value is None or value == "":
            return ["ollama"]
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return ["ollama"]
            if text.startswith("["):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = [p.strip() for p in text.split(",")]
                return [str(p) for p in parsed if str(p)]
            return [p.strip() for p in text.split(",") if p.strip()]
        if isinstance(value, (list, tuple)):
            return [str(p) for p in value]
        return ["ollama"]

    def cost_exceed_action_list(self) -> list[str]:
        """Parsed degrade-action list applied when a budget is exceeded."""
        return [a.strip() for a in self.cost_exceed_actions.split(",") if a.strip()]

    def high_risk_action_set(self) -> frozenset[str]:
        """Parsed set of actions classified HIGH risk (HITL trigger policy)."""
        return frozenset(
            a.strip().lower() for a in self.high_risk_actions.split(",") if a.strip()
        )

    def high_risk_resource_set(self) -> frozenset[str]:
        """Parsed set of resources classified HIGH risk (HITL trigger policy)."""
        return frozenset(
            r.strip().lower() for r in self.high_risk_resources.split(",") if r.strip()
        )

    def cors_origins(self) -> list[str]:
        """Parsed CORS allowlist. Empty list ⇒ no cross-origin requests allowed."""
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    def is_langsmith_enabled(self) -> bool:
        key = self.langchain_api_key.get_secret_value()
        return self.langchain_tracing_v2 and bool(key and key != "")

    def is_tavily_enabled(self) -> bool:
        key = self.tavily_api_key.get_secret_value()
        return bool(key and key != "")

    def is_slack_enabled(self) -> bool:
        key = self.slack_bot_token.get_secret_value()
        return bool(key and key.startswith("xoxb-"))

    def is_testrelic_enabled(self) -> bool:
        key = self.testrelic_api_key.get_secret_value()
        return bool(key and key.startswith("tr_"))

    # ------------------------------------------------------------------ #
    # Production posture + startup validation                            #
    # ------------------------------------------------------------------ #
    _INSECURE_SECRETS = frozenset(
        {
            "",
            "change-me-in-production",
            "change-me-in-production-use-secrets-manager",
        }
    )

    @staticmethod
    def _normalize_env_label(value: object) -> str | None:
        """Canonicalise an environment label, or ``None`` when unrecognised."""
        raw = str(value or "").strip().lower()
        return _ENV_ALIASES.get(raw)

    def environment(self) -> str:
        """The normalised deployment environment from ``app_env`` (INC12 A3).

        Returns one of ``"dev" | "staging" | "prod"``. An unrecognised value
        (or a missing attribute on an older ``Settings``) is normalised
        **fail-closed** to ``"prod"`` and logged, so a typo can never quietly
        enable development-only tools.
        """
        normalized = self._normalize_env_label(getattr(self, "app_env", "dev"))
        if normalized is None:
            raw = getattr(self, "app_env", "dev")
            logger.warning(
                "invalid APP_ENV=%r; treating environment as 'prod' (fail-closed)", raw
            )
            return "prod"
        return normalized

    def allows_development_tools(self) -> bool:
        """Whether development-only tool stubs may run (INC12 A3).

        Only ``dev`` permits them. ``staging`` / ``prod`` (and any invalid value,
        which normalises to ``prod``) return ``False`` so
        :class:`forgeflow.runtime.tool_executor.ToolExecutor` refuses them.
        """
        return self.environment() == "dev"

    def is_production(self) -> bool:
        """True for prod-shaped deployments. We key off the explicit
        environment label; dev_login_enabled is a secondary signal.

        Unchanged contract: true iff ``otel_environment`` normalises to
        ``prod`` or ``staging`` (i.e. the historical
        ``in {"prod", "production", "staging"}`` check). It reuses the same
        label normaliser as :meth:`environment` but keeps its own source field,
        so an unrecognised label still yields ``False`` exactly as before.
        """
        return self._normalize_env_label(self.otel_environment) in {"prod", "staging"}

    def validate_runtime(self) -> list[str]:
        """Return a list of fatal misconfigurations. Empty ⇒ safe to boot.

        The caller (API lifespan) hard-fails on any problem in production and
        logs warnings in dev — so an insecure prod deploy fails closed at
        startup instead of silently serving with default secrets.
        """
        problems: list[str] = []
        prod = self.is_production()

        if self.api_secret_key.get_secret_value() in self._INSECURE_SECRETS:
            problems.append("API_SECRET_KEY is unset or a known default")

        if prod and self.dev_login_enabled:
            problems.append("DEV_LOGIN_ENABLED must be false in production (use OIDC)")

        if self.dev_login_enabled and not self.dev_login_password.get_secret_value():
            problems.append("DEV_LOGIN_PASSWORD is required when DEV_LOGIN_ENABLED=true")

        if "*" in self.cors_origins():
            problems.append("CORS_ALLOW_ORIGINS must be an explicit allowlist, never '*'")

        if prod and self.docs_enabled:
            problems.append("DOCS_ENABLED should be false in production")

        if prod:
            # T1 — 'mock' is a development-only stub. get_model() already refuses
            # it at runtime; surfacing it here makes the misconfiguration fail
            # closed at startup (same spirit as the DEV_LOGIN_ENABLED check above)
            # instead of silently degrading to canned output.
            chain_providers = {
                str(p).strip().lower()
                for p in [self.llm_provider, *(self.model_fallback_chain or [])]
            }
            if "mock" in chain_providers:
                problems.append(
                    "MODEL_FALLBACK_CHAIN / LLM_PROVIDER includes 'mock' in "
                    "production — mock is a development-only stub and must never "
                    "serve prod traffic"
                )
            # T2 — OLLAMA_THINK is documented as MUST-stay-false; enforce it.
            if self.ollama_think:
                problems.append(
                    "OLLAMA_THINK must stay false in production — thinking models "
                    "can consume the whole num_predict budget and return an empty "
                    "response"
                )

        # INC12 A3 — the environment gate for development-only tools is
        # ``app_env`` (``allows_development_tools``). A deployment that declares a
        # non-dev environment but still ships the development-only 'mock' stub in
        # its LLM chain is internally inconsistent: the tool gate would refuse
        # dev tools while the model layer silently serves canned output. This is
        # the same failure family as the T1 check above, keyed on the *new* gate
        # so setting ``APP_ENV=prod`` without also moving ``OTEL_ENVIRONMENT``
        # cannot leave the misconfiguration unflagged. It fires only when
        # ``app_env`` is explicitly non-dev, so the default (dev) profile is
        # unaffected.
        if self.environment() != "dev":
            env_chain = {
                str(p).strip().lower()
                for p in [self.llm_provider, *(self.model_fallback_chain or [])]
            }
            if "mock" in env_chain:
                problems.append(
                    f"APP_ENV={self.environment()!r} is not 'dev' but a "
                    "development-only stub ('mock') is configured in "
                    "LLM_PROVIDER / MODEL_FALLBACK_CHAIN — development stubs must "
                    "not serve a non-dev environment"
                )

        if self.llm_provider == "openai" and not self.openai_api_key.get_secret_value():
            problems.append("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        if self.llm_provider == "anthropic" and not self.anthropic_api_key.get_secret_value():
            problems.append("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")

        if self.trusted_proxy_count == 0 and prod:
            problems.append(
                "TRUSTED_PROXY_COUNT=0 in production — client IPs (and login "
                "rate-limiting) will trust the socket peer only; set it to your "
                "real proxy hop count"
            )
        return problems


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
