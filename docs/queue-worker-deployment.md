# Queue worker deployment (A5 slice 4)

The durable queue (`POST /queue/tasks`, cron schedules, webhooks)
is consumed by the **worker**: a separate process that claims rows
(`SKIP LOCKED` + lease) and runs each through the same
`orchestrator.run_task` entry `POST /tasks` uses. The API server
only enqueues; it never runs queued work itself (unless the
in-process worker is explicitly enabled — see below).

## Running it

### Standalone (the normal deployment)

```bash
python scripts/run_worker.py            # loop until Ctrl-C
python scripts/run_worker.py --once     # one pass, then exit (smoke test)
python scripts/run_worker.py --poll-seconds 5 --lease-seconds 120 --batch-size 5
```

### docker-compose

```bash
docker compose up -d postgres            # + migrate profile once
docker compose --profile migration run --rm migrate   # alembic upgrade head
docker compose up -d worker
```

The `worker` service in `docker-compose.yml` reuses the API image
(`Dockerfile` target `api`, which ships `scripts/run_worker.py`)
and the API's environment, minus the HTTP-only settings. It needs
the `task_queue` / `schedules` / `webhooks` / `worker_heartbeats`
tables (migrations 046–049) — apply migrations before starting it.

### Helm / plain Kubernetes

- Helm: `worker.enabled` (default `true`), `worker.replicas`,
  `worker.resources` in `helm/forgeflow/values.yaml`;
  `helm/forgeflow/templates/worker.yaml` is a Deployment only
  (no Service — the worker serves no traffic). It uses the API
  image with command `python scripts/run_worker.py`.
- Plain YAML: `k8s/35-worker-deployment.yaml`
  (`kubectl apply -f 35-worker-deployment.yaml`).

### In-process (single-process dev only)

`queue_worker_enabled=true` starts a `QueueWorker` inside the API
lifespan. Default is `false`. **Never run the in-process worker and
a standalone worker at the same time against the same queue** —
both would claim (harmless for correctness — claiming is leased —
but they would split the work and double the heartbeat noise).

## Environment (exactly what the worker reads)

| Variable | Required | What reads it |
|---|---|---|
| `POSTGRES_SYNC_URL` | yes | Queue, schedule, heartbeat, checkpoint, run-lease and processed-event stores (all sync `psycopg`; `POSTGRES_DSN` is the fallback name) |
| `POSTGRES_URL` | yes | The run's trace store (async `asyncpg` pool, via `Settings.postgres_url`) |
| `LLM_PROVIDER`, `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `OLLAMA_MODEL_STRONG` | yes | The model for queued runs (same as the API) |
| `MCP_SERVER_HOST`, `MCP_SERVER_PORT`, `MCP_SERVICE_SECRET` | if tools are used | Tool calls inside queued runs |
| `TAVILY_API_KEY` | optional | Web-search tools |
| `BUDGET_LIMIT_USD` | optional | Run budget guard |
| `RESOURCE_STORE_ROOT` | optional | Where run artifacts land (default in the image) |
| `QUEUE_WORKER_ENABLED` | no (default `false`) | In-process worker switch (API lifespan only) |
| `QUEUE_WORKER_POLL_SECONDS` | no (default `5`) | Seconds between passes |
| `QUEUE_WORKER_LEASE_SECONDS` | no (default `120`) | Claim lease; a crashed worker's rows are reaped after this |
| `QUEUE_WORKER_BATCH_SIZE` | no (default `5`) | Rows claimed per pass |

No other API settings are needed: the worker serves no HTTP, so
`API_SECRET_KEY`, `DEV_LOGIN_*`, `DOCS_ENABLED` and `CORS_*` do
not apply.

## Liveness: `GET /queue/workers`

Each worker upserts a heartbeat row once per poll interval (and
once at startup). The API exposes it read-only:

```
GET /queue/workers            # read:workflows grant; list is platform-wide
```

Response (newest beat first):

```json
[
  {
    "worker_id": "worker-9f3ac2d1e04b",
    "hostname": "forgeflow-worker-7d9c6",
    "started_at": "2026-11-17T09:00:00Z",
    "last_beat_at": "2026-11-17T09:00:05Z",
    "last_report": {"claimed": 2, "done": 2, "failed": 0, "poll_seconds": 5},
    "stale": false
  }
]
```

**Staleness rule:** a worker is down when `now - last_beat_at`
exceeds 3x its poll interval (`poll_seconds` in `last_report`,
default 5s → 15s horizon). `last_report` carries the last pass's
counts (`claimed` / `done` / `failed` / `dead` / `skipped` /
`reaped`, plus `fired` / `schedules` from the scheduler pass).

The heartbeat write is best-effort: a failed beat logs a warning
and never fails a worker pass.

## Scaling

Multiple workers are safe: claiming is a single atomic
`UPDATE ... FOR UPDATE SKIP LOCKED` statement, and a row can only
be completed/failed by the worker holding a live lease. Scale by
raising `worker.replicas` (Helm), `replicas` in
`k8s/35-worker-deployment.yaml`, or `docker compose up -d
--scale worker=N`. There is no leader election and no shared
in-process state — the queue table is the coordination point.
