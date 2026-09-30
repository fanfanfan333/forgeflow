# Air-Gapped Deployment

ForgeFlow can run with zero outbound internet traffic — useful for
regulated industries (healthcare, defense, finance) and on-prem
deployments where data residency is a hard requirement.

## What "air-gapped" requires

| Concern | Default ForgeFlow behavior | Air-gapped requirement |
|---------|---------------------------|------------------------|
| LLM calls | Local Ollama on the private network | Stays on-net — no external LLM egress |
| Web search | Tavily API | Disabled (mock connector) or self-hosted SearXNG |
| Tracing | LangSmith (SaaS) | Self-hosted Phoenix or Langfuse, OR `TRACING_PROVIDER=none` |
| Slack approvals | Slack API | Disabled or self-hosted Mattermost |
| Connectors | Real APIs (Salesforce, Jira, etc.) | Disabled (`*_TOKEN=""`) or on-prem equivalents |
| Container images | `ghcr.io/joeljohnsonthomas/forgeflow/*` | Mirror to your private registry |
| pip dependencies | PyPI | Pre-baked into the image; no install at runtime |

## Settings for full offline mode

```env
# .env
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://ollama.internal:11434
OLLAMA_MODEL=qwen2.5vl:3b
OLLAMA_MODEL_STRONG=qwen2.5vl:3b

TRACING_PROVIDER=none
LANGCHAIN_TRACING_V2=false

# Empty all external integrations
TAVILY_API_KEY=
SLACK_BOT_TOKEN=
GITHUB_TOKEN=
JIRA_API_TOKEN=
HUBSPOT_ACCESS_TOKEN=
SALESFORCE_ACCESS_TOKEN=
SERVICENOW_PASSWORD=
SAP_PASSWORD=
QUICKBOOKS_ACCESS_TOKEN=
MSGRAPH_ACCESS_TOKEN=

# Embeddings are local & deterministic (forgeflow/experience/embedding.py);
# no external embedding key is required (INC16 removed the OpenAI provider).
```

Every connector built on `forgeflow/connectors/base.py` short-circuits
to a mock response when its credentials are missing — so leaving them
blank in air-gapped mode is safe and intentional.

## Offline bundle workflow

The standard delivery is a single tarball containing:

- Container images for `forgeflow-api`, `forgeflow-mcp`,
  `forgeflow-dashboard`, `pgvector/pgvector:pg16`, plus an Ollama image
  with pre-pulled models.
- The Helm chart (`helm/forgeflow/`) and rendered `k8s/` manifests.
- All required pip wheels in `wheels/` so a rebuild inside the air-gap
  doesn't need PyPI.

Build the bundle on a machine WITH internet access:

```bash
./scripts/build_offline_bundle.sh \
  --output forgeflow-offline-0.1.0.tar.gz \
  --version 0.1.0 \
  --ollama-models "qwen2.5vl:3b,qwen3:8b"
```

Inside the air-gap:

```bash
tar -xzf forgeflow-offline-0.1.0.tar.gz
cd forgeflow-offline-0.1.0/

# 1. Push images into the on-prem registry
./load-and-push.sh registry.internal.corp/forgeflow

# 2. Install via Helm pointing at the on-prem registry + Ollama
helm install ff ./helm/forgeflow -n forgeflow --create-namespace \
  --set image.registry=registry.internal.corp/forgeflow \
  --set image.tag=0.1.0 \
  --set image.pullSecrets[0].name=onprem-registry-creds \
  --set config.llmProvider=ollama \
  --set config.tracingProvider=none \
  --set-string config.ollamaModel=qwen2.5vl:3b \
  --set-string config.ollamaModelStrong=qwen2.5vl:3b \
  -f offline-values.yaml
```

## Verification

After deploy, confirm no DNS lookup for SaaS endpoints leaves the cluster:

```bash
# Tail DNS resolver logs (CoreDNS) for any external query.
# openai/anthropic were removed in INC16 but are kept in this probe list as
# defence-in-depth: any residual lookup means something still reaches out.
kubectl -n kube-system logs -l k8s-app=kube-dns -f | grep -iE \
  "openai|anthropic|langsmith|tavily|slack|atlassian|salesforce|hubspot"
# Empty output for the duration of a workflow run = full air-gap.
```

## What's NOT yet supported in air-gapped mode

These ship with caveats — track or contribute on the linked issues:

1. **Web research without Tavily**. The researcher agent expects web
   search. In air-gapped mode the workflow still runs but research_results
   will be empty unless a self-hosted search tool is registered as an
   MCP tool. SearXNG + a custom MCP tool is the recommended pattern.
2. **HITL approval notifications**. With Slack disabled, approvals still
   land in `/approvals/pending` — but reviewers need to poll the
   dashboard. Wire Microsoft Teams via `forgeflow.connectors.msgraph` if
   you have an on-prem Teams setup, or build a mattermost connector.

## See also

- [Helm chart values](../../helm/forgeflow/values.yaml) — `config.llmProvider`,
  `config.tracingProvider`, `secrets.values`
- [scripts/build_offline_bundle.sh](../../scripts/build_offline_bundle.sh) — the bundler
