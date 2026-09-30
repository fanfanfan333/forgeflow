# ForgeFlow Code-Task Runner (`codeplane/runner`)

A **standalone** program executed by the OpenHands virtualenv's interpreter. It is
spawned by the ForgeFlow code-execution engine
(`forgeflow/codeplane/engine.py::SubprocessOpenHandsEngine`) over a **process
boundary**; the two halves never share an address space.

## Isolation rules (do not break)

| Rule | Why |
|---|---|
| `runner/**` **must not** `import forgeflow` | It runs in the OpenHands venv, which does not have ForgeFlow installed. |
| `forgeflow/**` (outside `runner/`) **must not** `import openhands` | The OpenHands SDK requires `openai<3` and `litellm`, which conflict with the ForgeFlow venv (`openai==3.19.0`, no `litellm`). |
| The runner writes **only** inside its `workspace_path` | The target repository and the ForgeFlow checkout must be untouched (AC-11). |

Both import rules are pinned by a drift test
(`tests/unit/test_inc28_codeplane_import_boundary.py`), which scans the AST (not
text) and is falsified by a counterfactual injection.

## Contract (one in, one out)

**In** — one JSON object on stdin:

```json
{"run_id": "…", "task_intent": "…", "workspace_path": "/abs/…",
 "model": "ollama_chat/qwen3:8b", "base_url": "http://127.0.0.1:11434",
 "api_key": "", "max_rounds": 30, "wall_timeout_s": 180,
 "test_command": "python -m pytest -q", "language_hint": ""}
```

**Out** — JSONL on stdout, one event per line:

```json
{"seq": 0, "ts": "2026-10-02T00:00:00+00:00", "phase": "engine_ready",
 "kind": "engine", "status": "ok", "label": "代码执行引擎已就绪",
 "tool": "…", "detail": "…", "data": {…}, "latency_ms": 12.3}
```

* `phase` ∈ `engine_ready | workspace | plan | action | observation | test | diff | done | error`
* The **last** line is always `{"kind": "result", "data": {"exit_code": …}}`.
* The process exit code is `0` on normal completion (including test failures — a
  failing test is a *result*, not a runner crash) and non-`0` on an abnormal end.

## Degradation (never a fake success)

| Situation | Event emitted |
|---|---|
| OpenHands SDK not importable | `engine_ready` / `engine` / `unavailable`, `data.degraded = "engine_unavailable"` |
| Model / Ollama unreachable | `engine_ready` / `engine` / `unavailable`, `data.degraded = "model_unavailable"` |
| Test command failed to run | `test` / `test` / `error` with a verbatim reason |
| No workspace changes | `diff` / `diff` / `not_applicable` |

The runner only reports **raw** output; ForgeFlow
(`forgeflow/codeplane/tests_verdict.py`) owns the pass/fail verdict.

## Proxy hygiene

Every `*_PROXY` variable is removed by the parent engine before the child starts,
and `NO_PROXY=127.0.0.1,localhost,::1` is set, so the child's httpx/LiteLLM call
to local Ollama is never sent through the sandbox proxy.

## Manual smoke test

```bash
echo '{"run_id":"r1","task_intent":"say hi","workspace_path":"/tmp/ws","model":"ollama_chat/qwen3:8b"}' \
  | "<openhands venv python>" /abs/path/forgeflow/codeplane/runner/run_code_task.py
```
