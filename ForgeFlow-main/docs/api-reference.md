# ForgeFlow API Reference

> The **always-current** source of truth is the interactive OpenAPI UI at
> **`http://localhost:8000/docs`** and the raw schema at
> **`/openapi.json`**. This document is a hand-maintained companion that adds the
> auth model, role requirements, and error semantics the raw schema doesn't
> convey. It was cross-checked against `/openapi.json`.

## Base URL & versioning

| Environment | Base URL |
|---|---|
| Local (Docker) | `http://localhost:8000` |
| Behind the console proxy | `http://localhost:8501/api` |

The API is `v0.1.0` (pre-1.0; routes may change — see [CHANGELOG.md](../CHANGELOG.md)).

## Authentication

Every route **except** the public ones below requires a bearer JWT:

```
Authorization: Bearer <access_token>
```

There is **no** header-based (`X-Role`) fallback — unauthenticated requests
fail closed with `401`. Get a token from `/auth/login` (dev) or
`/auth/oidc/exchange` (production).

**Public routes** (no token): `GET /`, `GET /health`, `GET /openapi.json`,
`/docs`, `/redoc`, `POST /auth/login`, `POST /auth/refresh`,
`POST /auth/logout`, `POST /auth/introspect`, `POST /auth/oidc/exchange`,
`GET /marketplace/templates*` (discovery is intentionally open).

### Roles & permissions

Access is role-based ([`forgeflow/rbac/policies.py`](../forgeflow/rbac/policies.py)):

| Role | Can |
|---|---|
| `admin` | everything (`*:*`) |
| `manager` | **execute** workflows, read workflows/metrics/audit/proposals/leads/agents/memory/workspaces, **approve** proposals, **send** agents, read/**write** skills/policies/marketplace, `manage:self` (MFA self-service) |
| `sales_rep` | **execute** workflows, read workflows/metrics/agents/marketplace/skills, read/**write** memory, `manage:self` (MFA self-service) |
| `viewer` | read metrics/workflows/marketplace/skills/policies, `manage:self` (MFA self-service) |
| `service` | read/execute workflows, read metrics/skills (service-to-service JWTs) |

Object-level rule: on `GET /workflows/{id}` and `/trace`, non-elevated roles
(e.g. `sales_rep`) may only read **their own** runs; `manager`/`admin`/`viewer`
may read any run in their workspace.

### Error semantics

| Status | Meaning |
|---|---|
| `400` | Malformed input (e.g. non-UUID path id) |
| `401` | Missing/invalid/expired token, or bad credentials |
| `403` | Authenticated but role lacks the permission |
| `404` | Resource not found **or** hidden by object-level auth |
| `409` | Conflict (e.g. approval already resolved) |
| `410` | Gone (e.g. approval expired) |
| `422` | Schema validation failure |
| `429` | Rate limited (login: 5/min/IP; global limiter otherwise) |
| `504` | Workflow exceeded `WORKFLOW_RUN_TIMEOUT_SECONDS` |
| `5xx` | Upstream/LLM failure surfaced after retries + circuit breaker |

---

## Auth

### `POST /auth/login` — password (+ optional MFA) login
Public. Gated by `DEV_LOGIN_ENABLED`. Rate limited 5/min/IP.

Request:
```json
{ "user_id": "rep-1", "password": "…", "mfa_code": "123456", "workspace_id": null, "ttl_hours": 1 }
```
`mfa_code` is required only when the user has MFA enabled (else `401 {"detail":"mfa_required"}`).

Response `200`:
```json
{ "access_token": "eyJ…", "refresh_token": "…", "token_type": "bearer",
  "expires_in": 3600, "role": "sales_rep", "workspace_id": null }
```

### `POST /auth/refresh` — rotate tokens
Public. Body `{ "refresh_token": "…" }` → new access + refresh pair. Reusing a
rotated token revokes the whole family (`401 refresh token reuse detected`).

### `POST /auth/logout`
Public. Body `{ "token": "<access>", "refresh_token": "<refresh>" }` (both optional).
Revokes the access `jti` and the refresh-token family.

### `POST /auth/introspect`
Public. Body `{ "token": "<access>" }` → `{ "active": true, "claims": {…} }` or `401`.

### `POST /auth/mfa/enroll` · `POST /auth/mfa/verify`
Authenticated (`manage:self`). Enroll returns `{ secret, otpauth_uri }`; verify
takes `{ "code": "123456" }` and enables MFA.

### `POST /auth/oidc/exchange`
Public. Requires `OIDC_ENABLED=true`. Body `{ "id_token": "<idp jwt>" }` →
verifies against the IdP JWKS and returns local tokens. `404` when OIDC is off.

---

## Workflows

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/workflows/run` | `execute:workflows` | Run to completion or first interrupt (sync) |
| POST | `/workflows/stream` | `execute:workflows` | Same, as an SSE event stream |
| GET | `/workflows/{run_id}` | `read:workflows` (+owner) | Run status + state |
| GET | `/workflows/{run_id}/trace` | `read:workflows` (+owner) | Per-agent traces (prompts + output) |

`POST /workflows/run` request:
```json
{ "workflow_type": "sales_ops", "lead_data": { "company_name": "Stripe" }, "dry_run": false }
```
`workflow_type` ∈ `sales_ops | support_ops | finance_recon`. `dry_run` skips
side effects (CRM writes, email, Slack) but **still calls the LLM**.
Response `200`: `{ "run_id", "thread_id", "status", "message" }`.

---

## Approvals (human-in-the-loop)

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/approvals/pending` | `read:proposals` | List proposals awaiting review |
| GET | `/approvals/{token}` | `read:proposals` | One approval request |
| POST | `/approvals/{token}/approve` | `approve:proposals` | Resume approved. Body `{ "note": "…" }` |
| POST | `/approvals/{token}/reject` | `approve:proposals` | Resume rejected. Body `{ "reason": "…" }` |

---

## Agents (A2A) · Memory · Metrics · Audit · Workspaces · Marketplace

| Method | Path | Role |
|---|---|---|
| GET | `/agents/` | `read:agents` |
| GET | `/agents/dispatch` | `read:agents` |
| GET | `/agents/{agent_id}/status` | `read:agents` |
| POST | `/agents/{agent_id}/message` | `send:agents` |
| POST | `/memory/store` | `write:memory` |
| GET | `/memory/search?q=&limit=` | `read:memory` |
| DELETE | `/memory/{memory_id}` | `write:memory` |
| GET | `/metrics/` · `/metrics/cost*` · `/metrics/runs` · `/metrics/evaluation` | `read:metrics` |
| GET | `/metrics/prometheus` | open (scrape endpoint; not in OpenAPI) |
| GET | `/audit/search` · `/audit/stats` | `read:audit` |
| GET | `/workspaces/` · `/workspaces/{slug}` | `read:workspaces` |
| POST | `/workspaces/` | `write:workspaces` |
| GET | `/marketplace/templates` · `/marketplace/templates/{name}` | open |
| POST | `/marketplace/templates/refresh` | `write:marketplace` |

---

## AgentFlow Hub

The AgentFlow hubs (docs/sop/02-ARCHITECTURE.md §4.1) expose the runtime,
memory, skill and governance surfaces. Each row's permission is the exact
`ROUTE_PERMISSION_MAP` entry the RBAC middleware enforces (longest-prefix match);
`GET /context` reports the context-builder build stats.

| Method | Path | Permission | Purpose |
|---|---|---|---|
| POST | `/tasks` | `execute:workflows` | Submit a task intent; starts a hub run |
| GET | `/runs` | `read:workflows` | List recent hub runs |
| GET | `/runs/{run_id}` | `read:workflows` | One run's detail (steps, tokens, loop) |
| GET | `/runs/{run_id}/events` | `read:workflows` | SSE event stream for a run |
| POST | `/runs/{run_id}/replan` | `execute:workflows` | Force a replan with a reason |
| GET | `/experiences` | `read:memory` | List distilled experiences |
| POST | `/experiences` | `write:memory` | Record an experience |
| GET | `/experiences/{id}/lineage` | `read:memory` | Experience → run → memories lineage |
| GET | `/skills` | `read:skills` | List skills |
| POST | `/skills` | `write:skills` | Create a skill |
| GET | `/skills/evolution-advice` | `read:skills` | Skill-evolution suggestions |
| GET | `/skills/{id}/versions` | `read:skills` | Skill version history |
| POST | `/skills/{id}/versions` | `write:skills` | Add a skill version |
| POST | `/skills/{id}/rollback` | `write:skills` | Roll a skill back |
| GET | `/skill-candidates` | `read:skills` | List candidate skills |
| POST | `/skill-candidates` | `write:skills` | Draft a candidate from experiences |
| POST | `/skill-candidates/{id}/evaluate` | `write:skills` | Evaluate a candidate |
| POST | `/skill-candidates/{id}/promote` | `write:skills` (+`approve:skills` in handler) | Promote a candidate to a skill version |
| GET | `/policies` | `read:policies` | List policies |
| POST | `/policies` | `write:policies` | Create a policy |
| POST | `/policies/evaluate` | `read:policies` | Evaluate a decision |
| GET | `/approvals` | `read:proposals` | Hub approval list |
| POST | `/approvals/{id}/decision` | `approve:proposals` | Approve/reject an approval |
| GET | `/security/overview` | `read:audit` | Home-page security summary |
| GET | `/cost/board` | `read:metrics` | Three-tier budget board |
| GET | `/cost/savings` | `read:metrics` | Savings vs. the previous period |
| GET | `/context` | `read:memory` | Context-builder build stats (`source` / `degraded`) |
| GET | `/metrics/slo` | `read:metrics` | Three-tier SLO attainment |

---

## Health

`GET /health` (public) → `{ "status": "healthy", "database": "connected", "graph": "compiled" }`.
Used by container/orchestrator liveness probes.
