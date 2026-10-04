# Ruff Debt

```
Baseline: 2026-10-04
Initial: 484
Current: 462
Resolved: 22
Remaining: 462
```

Tracked-file lint debt in the official scope (`forgeflow/ dashboard/ tests/`).
"Resolved" and "Remaining" refer to the same 22-finding Phase-1 cleanup: the
baseline was re-frozen from 484 to 462, so every remaining finding is now the
locked floor. The ratchet (`make lint` → `scripts/ruff_debt.py check`) compares
against this floor and may only go down.

Initial anchors (2026-10-04): **484** tracked / **504** raw (`ruff check
forgeflow dashboard tests`) / **515** whole-repo (`ruff check .`). These live in
`initial_totals` in [`ruff-baseline.json`](ruff-baseline.json), while its top-level
`total` / `raw_command_total` / `whole_repo_raw_total` are the **live, post-cleanup**
measurements (462 / 482 / 493).

> All counts are **lint debt (code-quality debt), not business Bugs and not
> functional defects**. See
> [`ruff-baseline-2026-10-04.md`](ruff-baseline-2026-10-04.md) for the full
> baseline, the 504 raw / 515 whole-repo context, and the out-of-scope notes.

## Phases

| Phase | Scope | Tracked total | Δ |
|---|---|---:|---:|
| **Phase 0** — baseline frozen | `forgeflow/ dashboard/ tests/`, tracked | 484 | — |
| **Phase 1** — zero-risk mechanical (this increment) | `forgeflow/agent`, `forgeflow/runtime`, `forgeflow/metrics` | **462** | −22 |
| **Phase 2** — remaining mechanical | `UP037`/`UP017`/`UP035`/`I001`/`UP012`/`E702`/`SIM*` across the rest of the tree | _pending_ | — |
| **Phase 3** — reviewed fixes | `F401`/`B905`/`B007`/`B023`/`B904`/`F841`/`F402`/`F822`/`E741` (human-judged) | _pending_ | — |
| **Phase 4** — typing | `ANN001`/`ANN204` | _pending_ | — |
| **Target** | — | **0** | −462 |

## Rules (current — 462)

| Rule | Count | Note |
|---|---:|---|
| UP037 | 75 | quoted annotation — mechanical |
| UP017 | 61 | `datetime.timezone.utc` → `datetime.UTC` — mechanical |
| F401 | 52 | **preserved** — may change runtime behaviour |
| UP035 | 48 | deprecated import (`typing` → `collections.abc`) — mechanical |
| I001 | 47 | unsorted imports — mechanical |
| B905 | 33 | **preserved** — `zip(strict=)` needs a per-site decision |
| E702 | 24 | semicolon statements — mechanical |
| ANN001 | 21 | **preserved** — missing arg annotation |
| UP012 | 20 | redundant `encode()` — mechanical |
| SIM300 | 16 | yoda condition — mechanical |
| UP031 | 13 | printf format → f-string — mechanical |
| SIM117 | 11 | nested `with` — mechanical |
| B007 | 4 | **preserved** — unused loop var |
| E741 | 4 | **preserved** — ambiguous name |
| SIM102 | 4 | collapsible `if` — mechanical |
| UP042 | 4 | `str`-Enum — mechanical |
| B023 | 3 | **preserved** — loop-var closure |
| F841 | 3 | **preserved** — unused local |
| SIM103 | 3 | return-condition — mechanical |
| UP041 | 2 | `TimeoutError` alias — mechanical |
| B009 | 2 | `getattr` constant — mechanical |
| B904 | 2 | **preserved** — `raise ... from` |
| ANN204 | 1 | **preserved** — missing `__init__` return |
| F402 | 1 | **preserved** — import shadow |
| F541 | 1 | f-string without placeholders — mechanical |
| F822 | 1 | **preserved** — `__all__` name |
| SIM109 | 1 | redundant comparison — mechanical |
| SIM110 | 1 | loop → `any()` — mechanical |
| SIM114 | 1 | combine `if` branches — mechanical |
| SIM118 | 1 | `key in dict.keys()` — mechanical |
| UP015 | 1 | redundant `open` mode — mechanical |
| UP045 | 1 | `Optional` → `X | None` — mechanical |

## Files (current — 462 across 224 files; top 20)

| File | Count |
|---|---:|
| tests/corpus/build_corpus.py | 26 |
| forgeflow/codeplane/runner/run_code_task.py | 22 |
| tests/unit/test_inc44_textfile_edit.py | 14 |
| tests/integration/test_inc46_doc_intent_api.py | 12 |
| tests/unit/test_inc46_doc_intent.py | 12 |
| forgeflow/lifecycle/retirement.py | 7 |
| tests/unit/test_inc22_citation_drift.py | 7 |
| forgeflow/lifecycle/similarity.py | 6 |
| forgeflow/lifecycle/state_machine.py | 6 |
| forgeflow/rollout/rollback.py | 6 |
| forgeflow/skills/evolution_loop.py | 6 |
| forgeflow/agent/loop.py | 5 |
| forgeflow/governance/dlp_rules.py | 5 |
| forgeflow/metrics/definitions.py | 5 |
| forgeflow/outcomes/labeler.py | 5 |
| forgeflow/api/routers/audit.py | 4 |
| forgeflow/auth/store.py | 4 |
| forgeflow/documents/validation/__init__.py | 4 |
| forgeflow/evaluation/agent_metrics.py | 4 |
| forgeflow/rollout/store.py | 4 |

## Modules (current — 462; by area)

| Module | Count |
|---|---:|
| tests/unit | 138 |
| forgeflow/skills | 45 |
| tests/integration | 37 |
| forgeflow/documents | 32 |
| forgeflow/codeplane | 29 |
| tests/corpus | 28 |
| forgeflow/lifecycle | 23 |
| forgeflow/api | 12 |
| forgeflow/rollout | 12 |
| forgeflow/evaluation | 9 |
| forgeflow/governance | 9 |
| forgeflow/context | 8 |
| forgeflow/experience | 8 |
| forgeflow/outcomes | 8 |
| forgeflow/metrics | 7 |
| forgeflow/repositories | 7 |
| forgeflow/agent | 6 |
| forgeflow/auth | 5 |
| forgeflow/cost | 5 |
| forgeflow/privacy | 5 |
| forgeflow/hitl | 4 |
| forgeflow/security | 4 |
| forgeflow/workspace | 4 |
| forgeflow/benchmark | 3 |
| forgeflow/resources | 3 |
| forgeflow/sandbox | 3 |
| forgeflow/mcp | 2 |
| forgeflow/memory | 2 |
| forgeflow/middleware | 2 |
| forgeflow/runtime | 2 |

(`forgeflow/agent`, `forgeflow/runtime` and `forgeflow/metrics` dropped from
11/15/11 to 6/2/7 in Phase 1.)

## Preserved — not changed, needs a human decision

These findings were **deliberately left untouched** (spec §十一: preserve and
report; do **not** blind-fix code whose behaviour could change). Each needs a
human to read the site and decide, then pay it down one file at a time.

Rationale per rule:

* **F401** (unused import, 52) — removing an import can silently drop a
  side-effect/registration import or a re-export; must verify per site.
* **F841** (unused local, 3) — the assignment may be an intentional
  placeholder or carry a side effect in the RHS.
* **B905** (33) — `zip()` without `strict=`; the correct value
  (`True`/`False`) depends on intent and can change runtime behaviour.
* **B023** (3) — loop-variable in a closure; fixing changes what the closure
  captures.
* **B007** (4) — unused loop variable; renaming may be intentional documentation.
* **B904** (2) — `raise` without `from`; adding `from err` changes the traceback
  chain.
* **F402** (1) — import shadowed by loop var; fix reorders imports.
* **F822** (1) — name in `__all__` not defined; may be a dynamic re-export.
* **E741** (4) — ambiguous `l`/`I`/`O` name; a public rename.
* **ANN001 / ANN204** (21 / 1) — missing annotations; typing work, done with the
  surrounding module.

<!-- BEGIN PRESERVED LIST (generated) -->

### F401 (52)
- `forgeflow/agent/budget.py:38:F401`
- `forgeflow/agent/loop.py:75:F401`
- `forgeflow/agent/loop.py:77:F401`
- `forgeflow/agent/loop.py:82:F401`
- `forgeflow/agent/loop.py:88:F401`
- `forgeflow/api/routers/eval.py:30:F401`
- `forgeflow/auth/memory_store.py:34:F401`
- `forgeflow/codeplane/engine.py:40:F401`
- `forgeflow/codeplane/runner/agent_server/app.py:42:F401`
- `forgeflow/documents/sheet_edit.py:33:F401`
- `forgeflow/documents/textdiff.py:27:F401`
- `forgeflow/documents/validation/__init__.py:65:F401`
- `forgeflow/documents/validation/__init__.py:67:F401`
- `forgeflow/documents/validation/__init__.py:68:F401`
- `forgeflow/documents/validation/__init__.py:69:F401`
- `forgeflow/documents/validation/render.py:40:F401`
- `forgeflow/documents/validation/semantic.py:30:F401`
- `forgeflow/experience/dedup.py:34:F401`
- `forgeflow/hitl/pending.py:53:F401`
- `forgeflow/lifecycle/retirement.py:24:F401`
- `forgeflow/memory/preferences.py:34:F401`
- `forgeflow/metrics/definitions.py:26:F401`
- `forgeflow/metrics/definitions.py:31:F401`
- `forgeflow/metrics/definitions.py:32:F401`
- `forgeflow/metrics/definitions.py:33:F401`
- `forgeflow/metrics/definitions.py:35:F401`
- `forgeflow/repositories/memory/experience_repo.py:25:F401`
- `forgeflow/repositories/memory/policy_repo.py:6:F401`
- `forgeflow/repositories/memory/skill_repo.py:6:F401`
- `forgeflow/runtime/react_executor.py:299:F401`
- `forgeflow/runtime/react_executor.py:52:F401`
- `forgeflow/sandbox/docker_isolated.py:53:F401`
- `forgeflow/sandbox/docker_isolated.py:56:F401`
- `forgeflow/sandbox/docker_isolated.py:57:F401`
- `forgeflow/skills/evolution.py:26:F401`
- `forgeflow/skills/skill_subgraph.py:99:F401`
- `tests/integration/test_inc14_result_first.py:41:F401`
- `tests/integration/test_inc15_four_layer_contract.py:44:F401`
- `tests/integration/test_inc43_tenant_isolation.py:338:F401`
- `tests/unit/test_inc46_agent_loop.py:19:F401`
- `tests/unit/test_inc46_context_budget.py:25:F401`
- `tests/unit/test_inc46_progressive_loader.py:30:F401`
- `tests/unit/test_inc46_skill_md.py:23:F401`
- `tests/unit/test_inc46_skill_retrieval.py:20:F401`
- `tests/unit/test_inc46_trace_persistence.py:26:F401`
- `tests/unit/test_inc46_trace_persistence.py:27:F401`
- `tests/unit/test_inc46_validation_stack.py:37:F401`
- `tests/unit/test_runtime_rbac.py:10:F401`
- `tests/unit/test_runtime_tool_whitelist.py:21:F401`
- `tests/unit/test_skill_evolution.py:13:F401`
- `tests/unit/test_token_stream.py:29:F401`
- `tests/unit/test_trust_baseline.py:13:F401`

### B905 (33)
- `forgeflow/documents/docx_edit.py:251:B905`
- `forgeflow/documents/review_store.py:715:B905`
- `forgeflow/documents/review_store.py:729:B905`
- `forgeflow/documents/validation/invariants.py:295:B905`
- `forgeflow/documents/version_chain.py:334:B905`
- `forgeflow/documents/xlsx_edit.py:133:B905`
- `forgeflow/hitl/pending.py:754:B905`
- `forgeflow/lifecycle/store.py:262:B905`
- `forgeflow/lifecycle/store.py:332:B905`
- `forgeflow/memory/preferences.py:429:B905`
- `forgeflow/metrics/store.py:201:B905`
- `forgeflow/metrics/store.py:272:B905`
- `forgeflow/privacy/audit.py:244:B905`
- `forgeflow/rollout/store.py:297:B905`
- `forgeflow/rollout/store.py:385:B905`
- `forgeflow/rollout/store.py:455:B905`
- `forgeflow/security/quarantine.py:380:B905`
- `forgeflow/security/quarantine.py:401:B905`
- `forgeflow/skills/candidate_compiler.py:468:B905`
- `forgeflow/skills/engineering.py:319:B905`
- `forgeflow/skills/registry.py:206:B905`
- `tests/corpus/package_compare.py:206:B905`
- `tests/integration/test_inc46_fidelity_corpus.py:196:B905`
- `tests/integration/test_inc46_fidelity_corpus.py:278:B905`
- `tests/unit/test_audit_sink_alignment.py:143:B905`
- `tests/unit/test_audit_sink_alignment.py:172:B905`
- `tests/unit/test_inc46_pattern_miner.py:70:B905`
- `tests/unit/test_inc46_repair_loop.py:273:B905`
- `tests/unit/test_inc46_rule_assets.py:39:B905`
- `tests/unit/test_inc46_seed_skills.py:366:B905`
- `tests/unit/test_memory_lifecycle.py:33:B905`
- `tests/unit/test_middleware_vs_gate_boundary.py:147:B905`
- `tests/unit/test_tool_output_guard_wiring.py:122:B905`

### ANN001 (21)
- `forgeflow/auth/store.py:116:ANN001`
- `forgeflow/auth/store.py:50:ANN001`
- `forgeflow/auth/store.py:68:ANN001`
- `forgeflow/auth/store.py:98:ANN001`
- `forgeflow/codeplane/runner/run_code_task.py:1139:ANN001`
- `forgeflow/codeplane/runner/run_code_task.py:1139:ANN001`
- `forgeflow/codeplane/runner/run_code_task.py:1139:ANN001`
- `forgeflow/codeplane/runner/run_code_task.py:1139:ANN001`
- `forgeflow/codeplane/runner/run_code_task.py:1139:ANN001`
- `forgeflow/codeplane/runner/run_code_task.py:116:ANN001`
- `forgeflow/codeplane/runner/run_code_task.py:1181:ANN001`
- `forgeflow/codeplane/runner/run_code_task.py:577:ANN001`
- `forgeflow/codeplane/runner/run_code_task.py:580:ANN001`
- `forgeflow/codeplane/runner/run_code_task.py:602:ANN001`
- `forgeflow/codeplane/runner/run_code_task.py:602:ANN001`
- `forgeflow/codeplane/runner/run_code_task.py:657:ANN001`
- `forgeflow/codeplane/runner/run_code_task.py:671:ANN001`
- `forgeflow/codeplane/runner/run_code_task.py:700:ANN001`
- `forgeflow/mcp/server/tools/data_tools.py:17:ANN001`
- `forgeflow/mcp/server/tools/search_tools.py:17:ANN001`
- `forgeflow/skills/sandbox_isolated.py:706:ANN001`

### B007 (4)
- `forgeflow/skills/marketplace_bridge.py:420:B007`
- `forgeflow/skills/pattern_miner.py:511:B007`
- `tests/unit/test_inc46_trace_persistence.py:165:B007`
- `tests/unit/test_token_stream.py:58:B007`

### E741 (4)
- `forgeflow/api/routers/marketplace.py:60:E741`
- `forgeflow/skills/retrieval.py:342:E741`
- `tests/unit/test_marketplace_skills.py:168:E741`
- `tests/unit/test_marketplace_skills.py:89:E741`

### B023 (3)
- `forgeflow/documents/docx_inspect.py:352:B023`
- `forgeflow/documents/docx_inspect.py:352:B023`
- `forgeflow/documents/docx_inspect.py:353:B023`

### F841 (3)
- `forgeflow/skills/engineering.py:470:F841`
- `forgeflow/skills/engineering.py:471:F841`
- `tests/unit/test_inc46_scrubber.py:214:F841`

### B904 (2)
- `tests/unit/test_inc23_ui_vocabulary.py:150:B904`
- `tests/unit/test_inc42_history_lifecycle.py:510:B904`

### ANN204 (1)
- `forgeflow/codeplane/runner/run_code_task.py:602:ANN204`

### F402 (1)
- `forgeflow/skills/candidate_gates.py:189:F402`

### F822 (1)
- `forgeflow/resources/code_sources.py:41:F822`

<!-- END PRESERVED LIST -->

**Preserved total: 125** of 462 remaining (F401 52, B905 33, ANN001 21, B007 4,
E741 4, B023 3, F841 3, B904 2, ANN204 1, F402 1, F822 1).

### Deferred this round (deliberately not fixed)

- `forgeflow/agent/loop.py:576:UP017` — **not applied.** `loop.py` also carries
  four protected `F401` findings; applying UP017 (`datetime.timezone.utc` →
  `datetime.UTC`) would orphan its `from datetime import datetime, timezone`
  import into a **new `F401`**, which spec §十一 forbids removing. Deferred by
  **batch cohesion**: `loop.py`'s UP017 is left to be cleaned up in the Phase-2
  pass that resolves its own `F401`s in one change, rather than splitting out a
  single UP017 now. This one finding is
  counted in the **462 remaining**, but is **not** part of the 125-item §十一
  preserved set above (which is exactly the blind-fix-forbidden rules).

## Known frozen artifacts

`docs/winboot-2026-10-04.md` is the **INC48** historical report; per spec §十八
("do not mix in INC48") it is intentionally left unmodified, even though it still
describes the **old** `make lint` semantics (a managed-surface shortcut). It is a
frozen record of that increment, not current guidance — this file plus the
`Makefile` header are the source of truth for the live gate semantics.
