# INC46 代码地图（Code Map）

- 仓库根：`D:/Agentxm/Multi-Agent`；应用代码：`D:/Agentxm/Multi-Agent/ForgeFlow-main`
- 迁移 head：`alembic/versions/022_inc46_publish_interlock.py`
- 已落地任务：T01 / T02 / T03 / T04 / T05 / T07 / T15 / T18 / T26（提交 41bd5c1…4e44d64）

---

## 1. 顶层布局

`ForgeFlow-main/` 顶层目录：`alembic/` `forgeflow/` `tests/` `frontend/` `dashboard/` `docs/` `scripts/` `templates/`
顶层文件：`README.md` `Makefile` `pyproject.toml` `alembic.ini` `Dockerfile` `docker-compose.yml` `docker-compose.override.yml` `docker-compose.prod.yml` `requirements.txt` `requirements-dev.txt` `.env` `.env.example` `CHANGELOG.md` `ROADMAP.md` `SECURITY_AUDIT.md`
（根目录与 `frontend/` 下另有大量 `_*.txt|log|xml|py` 临时 recon 产物，如 `_runtests.sh` `_pgprobe.py` `_probe_e2e.py` `_toplevel.txt` 等，非交付代码。）

`forgeflow/` 子包（34）：`a2a` `agents` `api` `auth` `codeplane` `connectors` `cost` `documents` `evaluation` `events` `experience` `governance` `graph` `jobs` `marketplace` `mcp` `memory` `middleware` `models` `multimodal` `notifications` `observability` `rbac` `repositories` `resilience` `resources` `runtime` `security` `skills` `state` `telemetry` `validation` `workflows` `workspace`

`tests/` 子目录：`unit/` `integration/` `realstack/` `qa_independent/` `corpus/` `fixtures/`（`fixtures/` 含 `inc26/` `inc30/` `docx_corpus/`）

`frontend/src` 目录骨架：`api/` `assets/` `auth/` `components/` `docs/` `home/` `hooks/` `i18n/` `styles/` `theme/` `views/`（`views/runs/`、`views/skills/`）

---

## 2. 已落地的 INC46 代码（按任务分组）

### T01 — 真执行轨迹加性物化（run_steps）+ 迁移 019
- `alembic/versions/019_inc46_trace.py`（`upgrade()`/`downgrade()`；ALTER `run_steps`）
- `forgeflow/runtime/trace_store.py`：`TraceStep` `TraceStore(Protocol)` `PgTraceStore` `to_row()` `parse_step_index()` `persist_invocation()` `list_for_run()` `list_for_tenant()` `set_trace_sink()` `reset_trace_sink()` `trace_persistence_enabled()`
- 修改 `forgeflow/runtime/tool_executor.py`（`ToolExecutor`、`_persist_best_effort()` 调用 `persist_invocation`）
- 测试：`tests/unit/test_inc46_trace_persistence.py`、`tests/integration/test_inc46_trace_pg.py`

### T02 — Pattern Miner + Rule 资产 + 迁移 020
- `alembic/versions/020_inc46_rules.py`
- `forgeflow/skills/pattern_miner.py`：`PatternMetrics` `ExperiencePattern` `PATTERN_WEIGHTS` `PATTERN_SCORE_THRESHOLD` `FAILURE_STATUSES` `mine_patterns()` `mine_tenant_patterns()` `qualifies()` `score_components()` `step_tool()` `step_status()` `derive_run_outcome()`
- `forgeflow/skills/rule_assets.py`：`RuleAsset` `RuleStore(Protocol)` `RULE_KINDS` `RULE_MIN_SUPPORT` `RULE_MIN_CONFIDENCE` `extract_rules()` `persist_rules()` `list_rules()` `clear_rule_store()` `_MemoryRuleStore` `_PgRuleStore`
- 修改 `forgeflow/skills/candidate_compiler.py`
- 测试：`tests/unit/test_inc46_pattern_miner.py`、`tests/unit/test_inc46_rule_assets.py`

### T03 — Skill Runtime 真执行
- `forgeflow/skills/runtime.py`：`SkillRuntime` `SkillStep` `SkillNotExecutable` `load_procedure()` `to_plan_candidates()` `UNDECLARED_PROCEDURE_REASON`
- `forgeflow/skills/eligibility.py`：`Eligibility` `is_eligible()` `filter_eligible()` `ROLE_TAG_PREFIX` `CAPABILITY_TAG_PREFIX` `EXECUTABLE_STATUS`
- 修改 `forgeflow/runtime/orchestrator.py`
- 测试：`tests/unit/test_inc46_skill_runtime.py`、`tests/integration/test_inc46_skill_execution.py`

### T04 — 轻量沙箱 + 四级权限
- `forgeflow/skills/tool_permissions.py`：`READ` `WRITE` `EXTERNAL` `DANGEROUS` `TOOL_CLASSES` `CLASS_ORDER` `classify_tool()` `class_of_skill()` `risk_level_from_classes()` `DANGEROUS_TOOLS()`
- 修改 `forgeflow/runtime/tool_registry.py`、`forgeflow/skills/critic.py`、`forgeflow/skills/tester.py`
- 测试：`tests/unit/test_inc46_sandbox.py`、`tests/unit/test_inc46_tool_permissions.py`

### T05 — Evolution 闭环 + 迁移 021
- `alembic/versions/021_inc46_evolution.py`
- `forgeflow/skills/evolution_loop.py`：`EvolutionOutcome` `maybe_evolve()` `collect_skill_failures()` `record_generation()` `record_evolution_time()` `reset_evolution_state()` `EVOLVE_TRIGGER_MIN_FAILURES` `EVOLVE_COOLDOWN_HOURS` `MAX_EVOLVE_GENERATIONS` `EVOLVE_WINDOW_DAYS` `PROVENANCE_MARKER`
- 修改 `forgeflow/skills/versioning.py`
- 测试：`tests/unit/test_inc46_evolution_loop.py`、`tests/integration/test_inc46_evolution_closed_loop.py`

### T07 — Skill 七段契约（Pydantic v2）
- `forgeflow/skills/schemas.py`：`SkillContractDocument` `ManifestSegment` `KnowledgeSegment` `KnowledgeReference` `ProcedureSegment` `PoliciesSegment` `ToolBindingsSegment` `EvaluationSegment` `ExamplesSegment` `ContractValidationReport` `SkillContractValidationError` `validate_contract_document()` `require_valid_contract()` `contract_json_schema()` `seven_segments_from_draft_spec()` `seven_segments_from_skill_contract()`
- `forgeflow/skills/segments.py`：`normalise_segment_key()` `segment_title()` `missing_segments()` `estimate_tokens()` `body_volume_findings()` `normalize_reference()` `is_reference_too_deep()` `reference_depth_findings()` `contract_body_text()`（含 `REQUIRED_SEGMENTS`）
- `forgeflow/skills/contract_completion.py`：`CompletionResult` `ProvenanceEntry` `complete_contract()` `missing_required_segments()`
- `docs/inc46/skill_contract_schema.json`
- 测试：`tests/unit/test_inc46_skill_schemas.py`

### T15 — 自动发布联锁（Publish Interlock）+ 迁移 022
- `alembic/versions/022_inc46_publish_interlock.py`（建表 `publish_approvals`）
- `forgeflow/skills/publish_interlock.py`：`CapabilityAnchor` `RequirementStatus` `InterlockStatus` `PublishApprovalRecord` `evaluate_interlock()` `auto_publish_permitted()` `record_decision()` `list_decisions()` `latest_decision()` `publish_state_of()` `stage_pending_version()` `approve_publish()` `reset_interlock_state()`
- `forgeflow/api/routers/evolution.py`：`router`，`GET /interlock`
- 修改 `forgeflow/api/main.py`、`forgeflow/api/hub_schemas.py`、`forgeflow/config.py`、`forgeflow/rbac/policies.py`、`forgeflow/skills/evolution_loop.py`、`forgeflow/skills/versioning.py`
- 测试：`tests/unit/test_inc46_publish_interlock.py`、`tests/qa_independent/test_qa_t15_interlock_probe.py`

### T18 — SKILL.md 官方规范对齐
- `forgeflow/skills/spec_mapping.py`：`SEVEN_SECTIONS` `SECTION_MAPPING` `MappingEntry` `SPEC_URL` `SPEC_MARKDOWN_URL` `SNAPSHOT_FETCHED_AT` `SNAPSHOT_SHA256` `suggest_slug()` `render_skill_md()` `sha256_text()` `spec_drift_alert()`
- `forgeflow/skills/spec_validator.py`：`SkillValidationReport` `validate_skill_name()` `validate_description()` `validate_frontmatter()` `parse_skill_md()` `validate_skill_md()` `skills_ref_path()` `validate_with_skills_ref()`
- `docs/inc46/skill_md_spec_snapshot.md`、`docs/inc46/skill_md_mapping_sample.md`
- 测试：`tests/unit/test_inc46_skill_md_spec.py`

### T26 — 保真语料库（Fidelity Corpus）
- `tests/corpus/__init__.py`、`tests/corpus/build_corpus.py`、`tests/corpus/manifest.yaml`、`tests/corpus/package_compare.py`、`tests/corpus/pass_matrix.md`
- `tests/fixtures/docx_corpus/README.md` + 38 个 `.docx`（comments/ content_controls/ equations/ fields/ floating_images/ footnotes/ headers_footers/ hyperlinks/ large_document/ merged_cells/ nested_tables/ numbering/ revisions/ styles/ textboxes，各 1–3 份）
- 测试：`tests/integration/test_inc46_fidelity_corpus.py`

---

## 3. 相邻既有模块

### Skill 工程域（INC43）
- `forgeflow/skills/engineering.py`：`LIFECYCLE_STATES`（六态 `DRAFT` `CANDIDATE` `TESTING` `REVIEW` `PUBLISHED` `DEPRECATED`）`LifecycleEdge` `SkillEngineeringResult` `can_transition()` `assert_transition()` `transition_requires_approval()` `derive_lifecycle()` `synthesize()` `sandbox_evaluate()` `version_and_publish()` `run_engineering_loop()`
- `forgeflow/skills/registry.py::SkillRegistry`（`select()` 等）
- `forgeflow/skills/models.py`：`SkillRecord` `SkillVersionRecord` `SkillCandidateRecord` `SkillEvaluationRecord` `SKILL_STATUSES` `CANDIDATE_STATUSES`
- `forgeflow/skills/evaluator.py::evaluate_candidate`、`critic.py`、`tester.py`、`governance_gate.py::promote_candidate`、`release_gate.py`、`canary.py`、`trust_baseline.py`、`draft_spec.py::DraftSpec`、`versioning.py`（`bump_semver` `create_version` `diff_specs` `parse_semver` `rollback`）、`marketplace_bridge.py`、`tenant_scope.py`、`contracts.py`、`revision.py`、`errors.py`
- `forgeflow/api/hub_schemas.py`：`SkillResponse` `SkillVersionResponse` `SkillEngineeringResponse` `SkillLifecycleResponse` `ApprovePublishRequest` 等
- `forgeflow/api/routers/skill_engineering.py`：`router` `candidates_router`（`GET /{skill_id}/engineering`、`GET /{skill_id}/lifecycle`）
- `forgeflow/api/routers/skills.py`：`router` `candidates_router`（含 `POST /{skill_id}/versions/{semver}/approve-publish`）

### Evolution 闭环（T05）
`forgeflow/skills/evolution_loop.py`（见 §2 T05）

### Pattern Miner（T02） / Rule 资产（T02）
`forgeflow/skills/pattern_miner.py`、`forgeflow/skills/rule_assets.py`（见 §2 T02）

### Skill Runtime 真执行（T03）
`forgeflow/skills/runtime.py`。**`_default_executor` 在 `forgeflow/runtime/orchestrator.py::_default_executor`（约 :2136）**；`forgeflow/runtime/react_executor.py` 调用它。

### 轻量沙箱 + 四级权限（T04）
`forgeflow/skills/tool_permissions.py`（`READ`/`WRITE`/`EXTERNAL`/`DANGEROUS`）

### Experience
- 构建：`forgeflow/experience/context_builder.py::build_context`
- 抽取：`forgeflow/experience/extractor.py::ExperienceExtractor`（`extract()` / `extract_terminal()`）、模块函数 `extract_experience()`
- 去重/写库：`forgeflow/experience/dedup.py::ExperienceDeduplicator`、`forgeflow/experience/memory_store.py::search`、`promotion.py`、`scopes.py`、`lifecycle.py`、`memory_types.py`、`token_budget.py`、`embedding.py`、`models.py`

### 文档编辑平面
- 包 `forgeflow/documents/`：`docx_inspect.py::inspect_docx/open_docx`、`docx_edit.py`、`pptx_inspect.py`、`pptx_edit.py`、`sheet_inspect.py`、`sheet_edit.py`、`textfile_inspect.py`、`textfile_edit.py`、`textdiff.py`、`validation.py::verify_docx`、`store.py::DocArtifactStore`、`pdf_inspect.py`、`pdf_generate.py`
- `forgeflow/resources/storage.py::FileBlobStore`（:69）；`forgeflow/resources/summaries.py` `summarize_bytes()` 等
- 资源承接键（`document_paths` / `text_paths` / `sheet_paths` / `pdf_paths`）在 `forgeflow/resources/service.py::ResourceService.resolve()`（:466+）与 `forgeflow/runtime/orchestrator.py`（:1129+）、消费在 `forgeflow/runtime/tool_handlers.py`

### tool_registry / tools / 权限图
- `forgeflow/runtime/tool_registry.py`：`ToolBinding` `register()` `resolve()` `known_ids()` `reset_registry()` `load_default_bindings()` `register_mock_bindings()` `snapshot_bindings()` `restore_bindings()`
- `TOOL_PERMISSION_MAP` 在 `forgeflow/runtime/gate.py`（:47）；同文件 `required_permission()` `check_tool_permission()` `describe_denial()` `required_code_permission()` `check_code_permission()`
- tools 目录：`forgeflow/mcp/server/tools/`（`crm_tools.py` `email_tools.py` `github_tools.py` `hubspot_tools.py` `jira_tools.py` `msgraph_tools.py` `quickbooks_tools.py` `salesforce_tools.py` `sap_tools.py` `servicenow_tools.py` `slack_tools.py` `platform_tools.py` `search_tools.py` `data_tools.py` `multimodal_tools.py`）
- tool 处理实现：`forgeflow/runtime/tool_handlers.py`

### run_steps 物化（T01）与 workspace_runs 表
- `run_steps` 表：migration `010_agentflow_hubs.py`（019 加列 `status` `artifact_ref` `verification` `actor_user_id` `attempt`；`latency_ms` 放宽）
- 写入函数：`forgeflow/runtime/trace_store.py::persist_invocation()`（由 `forgeflow/runtime/tool_executor.py::_persist_best_effort()` :740 调用）
- `workspace_runs` 表：migration `016_workspace_runs.py`；ORM 投影 `forgeflow/workspace/models.py::WorkspaceRunRecord`；仓储 `forgeflow/workspace/store.py::WorkspaceStore(Protocol)` `MemoryWorkspaceStore` `PgWorkspaceStore` `get_workspace_store()` `reset_workspace_store()`

---

## 4. 数据库与迁移约定

- 迁移文件 `alembic/versions/NNN_*.py`；`revision` / `down_revision` 为数字字符串（`"019"`→`"022"`）；`upgrade()` / `downgrade()`。
- **手写 `op.execute("CREATE TABLE IF NOT EXISTS ...")`，不使用 `op.create_table`**；索引 `CREATE INDEX IF NOT EXISTS`；`downgrade()` 用 `DROP TABLE IF EXISTS` / `DROP COLUMN IF EXISTS`；全程 `IF NOT EXISTS`/`IF EXISTS` 保证 `alembic upgrade head` 幂等（连跑两次第二次 no-op）。
- **tenant 约定：`tenant_id TEXT NOT NULL`**（不透明 TEXT，应用层 fail-closed：无 tenant 不写不读）。INC46 迁移头注释明确「migration-020 单一 yardstick」。
- 未测量值一律 `NULL`（不默认 `0`/`''`）。
- ORM：**不使用 SQLAlchemy ORM**；采用 Repository 模式 + asyncpg 原生 SQL。`forgeflow/repositories/` 为唯一存储边界：`base.py`（`TenantScopedRepository` + `ExperienceRepository`/`SkillRepository`/`SkillCandidateRepository`/`PolicyRepository`/`CostBudgetRepository`/`ResourceRepository` Protocol）、`factory.py::get_*_repository()`、`memory/` 与 `postgres/` 双实现。
- PG 连接：`Settings.postgres_url`（asyncpg DSN）、`Settings.postgres_sync_url`（psycopg3，alembic/checkpointer）。`.env` dev 库 `localhost:5433`。
- 测试取库：`tests/conftest.py` 用 `setdefault` 注入 `POSTGRES_URL=postgresql+asyncpg://forgeflow:forgeflow@localhost:5433/forgeflow`、`POSTGRES_SYNC_URL=postgresql+psycopg://...`。realstack 走 `realstack`/`pg_conn` fixture。临时探针 `_pgprobe.py` 读 `FF_TEST_DSN`。

---

## 5. 测试基础设施

`tests/conftest.py` 环境默认：`STORAGE_BACKEND=memory`、`LLM_PROVIDER=mock`、`TAVILY_API_KEY=""`（无条件覆盖）、`DEV_LOGIN_ENABLED=true`、`FORGEFLOW_ALLOW_TEMPLATE_WORKFLOWS=1`。

fixture（见 §9 全清单）：`_stub_dns`(autouse) `_isolate_asyncpg_pool`(autouse) `_isolate_degrade_state`(autouse) `force_memory_backend` `pg_purge` `mock_llm` `mock_pool` `sample_workflow_state`

`tests/realstack/conftest.py`：`realstack` `realstack_env` `pg_conn`；门控 `GATE_ENV_VAR = "FORGEFLOW_REAL_STACK"`（须 ==`"1"`），并设 `NO_PROXY=127.0.0.1,localhost,::1` 且清 `*_PROXY`；会话末清理 `workspace_runs`/`experiences`。

pytest 配置（仅在 `pyproject.toml`）：
```
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "-v --tb=short"
```
无 `pytest.ini`，无自定义 `markers`。

INC46 测试命名：`tests/unit/test_inc46_*.py`、`tests/integration/test_inc46_*.py`、`tests/qa_independent/test_qa_t15_interlock_probe.py`。

骨架示例（`tests/unit/test_inc46_skill_schemas.py`）：模块 docstring 列 positive / negative / findings / completion / counterfactual 覆盖 → import `forgeflow.skills.{segments,contract_completion,schemas,spec_mapping,spec_validator}` 符号 → helper `_valid_contract(**overrides)` 生成七段 dict → 断言 `pytest.raises(SkillContractValidationError)` 与 `ContractValidationReport.messages`；schema 快照 `Path(__file__).resolve().parents[2] / "docs" / "inc46" / "skill_contract_schema.json"`。

---

## 6. API 层

`forgeflow/api/routers/` 现有 router：`approvals.py` `workspaces.py` `auth.py` `marketplace.py` `workflows.py` `agents.py` `memory.py` `metrics.py` `cost.py` `audit.py` `tasks.py` `runs.py` `experiences.py` `skills.py` `skill_engineering.py` `evolution.py` `policies.py` `approvals_hub.py` `security.py` `context.py` `resources.py` `codeplane.py` `workspace.py`

`forgeflow/api/main.py` 注册方式（全部）：`app.include_router(<module>.router, prefix="/...", tags=[...])`（skills 与 skill_engineering 各再挂 `candidates_router`）。**无手写 `Route()`，无 `StaticFiles`/MCP `mount`**；MCP 经 `forgeflow/mcp/client/adapter.py::get_mcp_tools()` 载入 LangGraph（`compile_graph(mcp_tools=...)`）。`api/routers/__init__.py` 为空。

依赖注入：`forgeflow/api/dependencies.py`：`get_pool()` `get_graph()` `get_graphs()` `get_current_user() -> UserContext` `get_workspace_id() -> str | None`；`forgeflow/api/hub_deps.py::resolve_tenant()`（依赖 `get_workspace_id`）。RBAC 由 `forgeflow/middleware/auth.py::RBACMiddleware` + `forgeflow/runtime/gate.py` 联合实施。

---

## 7. 前端

`frontend/src/views/skills/`：`skillAssets.ts`、`SkillLibrary.tsx`、`SkillInspector.tsx`、`SkillEngineering.tsx`

data-testid 约定（kebab-case）：`skill-library` `skill-library-search` `skill-library-scope` `skill-library-group` `skill-library-item` `skill-inspector` `skill-inspector-basic` `skill-inspector-capabilities` `skill-inspector-tools` `skill-inspector-eval` `skill-engineering` `skill-card-header` `skill-card-goal` `skill-exec-flow` `skill-exec-node` `skill-card-counts` `skill-eng-panel` `skill-eng-loading` `skill-eng-error` `skill-eng-degraded` `skill-eng-run` `skill-eng-lifecycle` `skill-eng-critique` `skill-eng-tests` `skill-eng-test-cat` `skill-eng-eval`

`frontend/package.json` scripts：`dev`(vite) `build`(`tsc -b && vite build`) `lint`(eslint) `preview`(vite preview) `test:e2e`(`playwright test`)

E2E：`frontend/e2e/*.spec.ts`（含 `inc43_skill_engineering.spec.ts` `inc43_skill_asset_center.spec.ts` `inc43_honesty_and_regression.spec.ts` 等）。`frontend/playwright.config.ts`：`testDir:'./e2e'`、`webServer.command = 'npm run build && npm run preview -- --port 4173 --strictPort'`、`reuseExistingServer = !process.env.CI`、`baseURL=http://localhost:4173`、chromium 项目。`frontend/dist/` 已预构建；另有 `frontend/playwright.inc26.config.ts`。

---

## 8. 执行方式

- 解释器（项目 venv）：`C:/Users/18769/.workbuddy/binaries/python/envs/agentflow/Scripts/python.exe`（Python 3.13.14）。openhands venv（`...\envs\openhands\Scripts\python.exe`）仅供 `codeplane` runner——见 `.env` 的 `CODEPLANE_INTERPRETER` / `CODEPLANE_TEST_COMMAND`。
- 跑测试（离线默认）：`python -m pytest tests/unit tests/integration -v`（conftest 自动切 memory/mock；`Makefile: make test` 等价）。realstack：需 `FORGEFLOW_REAL_STACK=1` + 本机 Ollama(:11434) + PostgreSQL(:5433)。
- alembic：`alembic upgrade head`（连跑两次验幂等），即 `python -m alembic upgrade head`。
- 代理：**必须** `NO_PROXY=127.0.0.1,localhost,::1` 且清 `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY`（realstack conftest 与 codeplane runner 均如此），否则本机 Ollama 走代理 → 502 → 静默降级 mock。

---

## 9. 后续任务接缝点（精确定位）

- `forgeflow/experience/context_builder.py::build_context(tenant_id, intent, *, user_id=None, team_id=None, budget_tokens=2000, k_memory=5, k_skill=3, k_exp=3, min_similarity=0.6, per_item_ratio=None) -> ContextBundle`；上下文来源键 = `ContextSection.source ∈ {memory, skill, experience}`：memory←`experience/memory_store.py::search`、skill←`skills/registry.py::SkillRegistry().select`、experience←`repositories::get_experience_repository().find_similar`。返回 `ContextBundle(sections, tokens_used, tokens_raw, compression_ratio, hit_rate, dropped, recalled)`。
- Experience 写入口：`forgeflow/experience/extractor.py::ExperienceExtractor.extract(run, verdict=None, memories=None, *, tenant_id=None, team_id=None) -> ExperienceRecord`（持久化由 `forgeflow/experience/dedup.py::ExperienceDeduplicator.resolve` 拥有）；底层落库 `forgeflow/repositories/postgres/experience_repo.py::PgExperienceRepository.save()`（`INSERT INTO experiences`）。调用点：`forgeflow/runtime/orchestrator.py::run_task`（约 :3043）。
- `forgeflow/skills/` 全部文件（33）：`__init__.py` `canary.py` `candidate_compiler.py` `contract_completion.py` `contracts.py` `critic.py` `draft_spec.py` `eligibility.py` `engineering.py` `errors.py` `evaluator.py` `evolution.py` `evolution_loop.py` `governance_gate.py` `marketplace_bridge.py` `models.py` `pattern_miner.py` `publish_interlock.py` `registry.py` `release_gate.py` `revision.py` `rule_assets.py` `runtime.py` `schemas.py` `segments.py` `spec_mapping.py` `spec_validation.py` `spec_validator.py` `tenant_scope.py` `tester.py` `tool_permissions.py` `trust_baseline.py` `versioning.py`
- `forgeflow/repositories/` 仓储类 → 表：
  - `base.py::TenantScopedRepository`（基类）；Protocol `ExperienceRepository` `SkillRepository` `SkillCandidateRepository` `PolicyRepository` `CostBudgetRepository` `ResourceRepository`
  - `memory/experience_repo.py::MemoryExperienceRepository` → `experiences`（+`experience_memory`）
  - `postgres/experience_repo.py::PgExperienceRepository` → `experiences` / `experience_memory`
  - `memory/skill_repo.py::MemorySkillRepository` / `MemorySkillCandidateRepository` → `skills` `skill_versions` `skill_candidates` `candidate_experience` `skill_evaluations`
  - `postgres/skill_repo.py::PgSkillRepository` / `PgSkillCandidateRepository` → 同上
  - `memory/policy_repo.py::MemoryPolicyRepository` / `postgres/policy_repo.py::PgPolicyRepository` → `policies` `agent_approvals`
  - `memory/cost_repo.py::MemoryCostBudgetRepository` / `postgres/cost_repo.py::PgCostBudgetRepository` → `cost_budgets`
  - `memory/resource_repo.py::MemoryResourceRepository` / `postgres/resource_repo.py::PgResourceRepository` → `resources`
  - `eval_sample_repo.py::MemoryEvalSampleRepo` / `postgres/eval_sample_repo.py::PgEvalSampleRepository` → `agent_eval_samples`
  - 工厂：`factory.py::get_experience_repository()` `get_skill_repository()` `get_skill_candidate_repository()` `get_policy_repository()` `get_cost_repository()` `get_eval_sample_repository()` `get_resource_repository()` `reset_repositories()`
- tool_registry 精确位置：`forgeflow/runtime/tool_registry.py::known_ids()`（:93）、`::load_default_bindings()`（:103）；`TOOL_PERMISSION_MAP` 在 `forgeflow/runtime/gate.py`（:47）。
- 租户/RBAC 依赖注入（FastAPI `Depends` 用）：`forgeflow/api/dependencies.py::get_current_user` / `get_workspace_id`；`forgeflow/api/hub_deps.py::resolve_tenant`。
- `tests/conftest.py` 全部 fixture：`_stub_dns`(autouse) `_isolate_asyncpg_pool`(autouse) `_isolate_degrade_state`(autouse) `force_memory_backend` `pg_purge` `mock_llm` `mock_pool` `sample_workflow_state`。（realstack 另有 `realstack` `realstack_env` `pg_conn`。）
- run_steps 物化写入函数（T01）：`forgeflow/runtime/trace_store.py::persist_invocation(invocation, *, args, run_id, tenant_id, step_id=None)`（经 `PgTraceStore.insert_step`；由 `runtime/tool_executor.py::_persist_best_effort` 调用）。
- 文档编辑平面：DOCX 读 `forgeflow/documents/docx_inspect.py::inspect_docx()/open_docx()`；DOCX 写 `forgeflow/documents/docx_edit.py`；校验 `forgeflow/documents/validation.py::verify_docx()`；Artifact 存储 `forgeflow/documents/store.py::DocArtifactStore.put(data, suffix='.docx')`；Blob 存储 `forgeflow/resources/storage.py::FileBlobStore`；Tool 层 `forgeflow/runtime/tool_handlers.py::_edit_docx()`。
- `forgeflow/documents/` 全部文件（14）：`__init__.py` `docx_edit.py` `docx_inspect.py` `pdf_generate.py` `pdf_inspect.py` `pptx_edit.py` `pptx_inspect.py` `sheet_edit.py` `sheet_inspect.py` `store.py` `textdiff.py` `textfile_edit.py` `textfile_inspect.py` `validation.py`

---

## 10. INC46 测试文件清单（路径 + `def test_` 用例数）

| 文件 | 用例数 |
|---|---|
| `tests/unit/test_inc46_trace_persistence.py` | 12 |
| `tests/integration/test_inc46_trace_pg.py` | 3 |
| `tests/unit/test_inc46_pattern_miner.py` | 18 |
| `tests/unit/test_inc46_rule_assets.py` | 12 |
| `tests/unit/test_inc46_skill_runtime.py` | 22 |
| `tests/integration/test_inc46_skill_execution.py` | 3 |
| `tests/unit/test_inc46_sandbox.py` | 18 |
| `tests/unit/test_inc46_tool_permissions.py` | 20 |
| `tests/unit/test_inc46_evolution_loop.py` | 10 |
| `tests/integration/test_inc46_evolution_closed_loop.py` | 4 |
| `tests/unit/test_inc46_skill_schemas.py` | 32 |
| `tests/unit/test_inc46_skill_md_spec.py` | 27 |
| `tests/unit/test_inc46_publish_interlock.py` | 31 |
| `tests/unit/test_inc46_docx_run_fidelity.py` | 8 |
| `tests/integration/test_inc46_fidelity_corpus.py` | 13 |
| `tests/qa_independent/test_qa_t15_interlock_probe.py` | 24 |

---

## 11. NOT_FOUND 汇总

- `forgeflow/evolution/`（目录）— NOT_FOUND（Evolution 逻辑在 `forgeflow/skills/evolution_loop.py` + `forgeflow/api/routers/evolution.py`）
- `forgeflow/api/routers/skill_insights.py` — NOT_FOUND（域路由为 `skill_engineering.py` + `skills.py` + `evolution.py`）
- `pytest.ini` / `tox.ini` / `setup.cfg` — NOT_FOUND（pytest 配置在 `pyproject.toml`）
- `forgeflow/evolution/publish_interlock.py` — NOT_FOUND（实际 `forgeflow/skills/publish_interlock.py`）
- 无 SQLAlchemy ORM models 模块 — NOT_FOUND（Repository + asyncpg）

---

## DEVIATIONS（任务簿建议路径 ≠ 真实路径）

1. **publish_interlock**：建议 `evolution/publish_interlock.py` → 实际 `forgeflow/skills/publish_interlock.py`；**无 `forgeflow/evolution/` 目录**（`evolution` 仅为 `api/routers/evolution.py` + `skills/evolution_loop.py`）。
2. **`api/routers/skill_insights.py`** — **NOT_FOUND**；实际域路由为 `skill_engineering.py` + `skills.py` + `evolution.py`。
3. **`tests/corpus/manifest.yaml`** — 存在，但位于 `tests/corpus/`，**不在** `tests/fixtures/` 下（`tests/fixtures/docx_corpus/` 仅含 `.docx`）。
4. **run_steps 表** — 在 migration `010` 已建；T01（`019`）**只加列**（`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`），非新建表。
5. **文档编辑平面** — 在 `forgeflow/documents/`（**非** `runtime/`）；`FileBlobStore` 在 `forgeflow/resources/storage.py`（**非** documents）。
6. **`skills/spec_validator.py` 与 `skills/spec_mapping.py`** — 是**两个独立文件**（验证器未并入 mapping）；另有 `skills/spec_validation.py`（IO schema 校验，与 SKILL.md 校验器不同）。

---

## AI_GAP_SLOTS

任务簿若要新建下列目录/文件，仓库现状如下：

| 槽位 | 状态 |
|---|---|
| `forgeflow/hitl/` | **不存在** |
| `forgeflow/privacy/` | **不存在** |
| `forgeflow/rollout/` | **不存在** |
| `forgeflow/lifecycle/` | **不存在**（生命周期逻辑在 `skills/engineering.py::LIFECYCLE_STATES`；`workspace/` 有 history-lifecycle） |
| `forgeflow/metrics/` | **不存在**（指标在 `forgeflow/observability/`，含 `prometheus.py` `metrics_source.py`；API 层 `api/routers/metrics.py`） |
| `forgeflow/benchmark/` | **不存在**（评估基线在 `tests/eval_baseline.json`、`forgeflow/evaluation/`） |
| `forgeflow/outcomes/` | **不存在**（结果/轨迹在 `runtime/trace_store.py`、`runtime/artifacts.py`） |
| `forgeflow/evaluation/` | **已存在**（`dataset.py` `metrics.py` `runner.py`） |
| `forgeflow/security/` | **已存在**（`email_allowlist.py` `ssrf_guard.py`；另有 `middleware/security*.py`、`api/routers/security.py`、`rbac/`） |
| `forgeflow/agent/` | **不存在**（单数 agent 目录无；复数 `forgeflow/agents/` 存在，含 `base.py` `analyzer.py` `researcher.py` `supervisor.py`） |
| `forgeflow/context/` | **不存在**（上下文在 `forgeflow/experience/context_builder.py` + `api/routers/context.py`） |
| `forgeflow/memory/` | **已存在**（`memory_manager.py` `relational_store.py`；另有 `experience/memory_store.py`） |
| `forgeflow/artifacts/` | **不存在**（Artifact 逻辑在 `forgeflow/runtime/artifacts.py` 与 `forgeflow/documents/store.py`） |
| `forgeflow/jobs/` | **已存在**（`escalation.py`） |

---

## 12. 补充接缝点（第二轮）

> 只读侦察，未运行 pytest。凡命中均给「文件 + 符号」；未命中显式写 `NOT_FOUND`。

### A. T08（Candidate Gate / Critic 增强）

**A1. `forgeflow/skills/critic.py` 公开符号**
- `__all__ = ["critique", "SEVERITY_ORDER"]`；常量 `SEVERITY_ORDER = {"none":0,"low":1,"medium":2,"high":3}`、`MAX_PROCEDURE_STEPS = 16`、`_FAILURE_PATH_TOKENS`、`_SIDE_EFFECT_CLASSES`
- 函数：`critique(contract: SkillContract) -> SkillCritique`、`_finding(code, severity, message, field_name) -> dict`、`_max_severity(findings) -> str`、`_privilege_findings(contract) -> list[dict]`
- **critique 结果对象**：`forgeflow/skills/contracts.py::SkillCritique`（dataclass），字段逐个：`findings: list[dict[str, Any]]`、`severity: str`、`must_fix: list[str]`（+ `to_dict()`）
- **finding 结构**（`critic._finding`）：`{"code": str, "severity": str, "message": str, "field": str}` —— code 字段名就叫 `code`；字段名 key 为 `field`
- **severity 取值**：`"none" | "low" | "medium" | "high"`（`contracts.CRITIQUE_SEVERITIES = ("none","low","medium","high")`）
- 现有 code 全集：`goal_missing` `procedure_missing` `tools_missing` `tools_not_whitelisted`（high）、`inputs_missing` `outputs_missing` `verification_missing`（medium）、`policies_missing`（low）、`risk_invalid` `high_risk_unverified`（high）、T04 新增 `dangerous_operation`(high) `missing_failure_path`(medium) `insufficient_boundary`(medium) `no_termination`(medium)；`must_fix` = 所有 severity=="high" 的 code

**A2. `forgeflow/skills/engineering.py::run_engineering_loop` 调用 critic 的位置**
- 前置（:456-457）：`contract = SkillContract.from_draft_spec(candidate.draft_spec)` → `_reconcile_contract_tools(contract)`
- 调用点（:464）：`crit = critique(contract)`；随后 while 循环 :465-470：`while crit.must_fix and rounds < max_repair:` → `contract, revision = repair(contract, crit, None, rounds)` → `crit = critique(contract)`
- 后置（:472-483）：`if crit.must_fix: return SkillEngineeringResult(lifecycle="DRAFT", passed=False, degraded_reason=f"critique 阻断项未清除：{', '.join(crit.must_fix)}")`
- TESTING 阶段再次调用（:499）：`crit = critique(contract)`

**A3. `candidate_gates.py` / `risk_escalation.py`**
- `forgeflow/skills/candidate_gates.py` — **NOT_FOUND**
- `forgeflow/skills/risk_escalation.py` — **NOT_FOUND**

**A4. `tool_permissions` 两函数**
- `DANGEROUS_TOOLS() -> frozenset[str]`（:82）：返回 `frozenset(TOOL_PERMISSION_MAP) | {"code.commit"}`（懒加载 `runtime.gate.TOOL_PERMISSION_MAP` 派生，单一真源）
- `risk_level_from_classes(classes: Any) -> str`（:138）：接受 `{tool: class}` 映射或裸 class 可迭代；`DANGEROUS ⇒ "high"`，含 `WRITE|EXTERNAL ⇒ "medium"`，否则 `"low"`；未知值忽略

### B. T16（成功信号）

**B5. run 状态常量/枚举的权威定义处（多处，作用不同）**
- **run 级（运行时生产者）**：`forgeflow/runtime/orchestrator.py::RunRecord.status`（dataclass :1397）。实际写入值：`"completed"` / `"failed"`（:2994 `status = "completed" if verdict.success else "failed"`）、`"awaiting_approval"`（:3005）。另有 dispatcher `"aborted"`（`runtime/dispatcher.py::_ABORTED_STATUS = "aborted"`）。
- **run 级（持久化/终态权威集合）**：`forgeflow/workspace/store.py::TERMINAL_STATUSES = frozenset({"completed","failed","aborted","interrupted","rejected"})`（:47-48）+ `INTERRUPTED_STATUS = "interrupted"`（:52）；列默认值 `forgeflow/workspace/models.py::WorkspaceRunRecord.status = "running"`。
- **validator 侧**：`forgeflow/validation/validator.py` `_SUCCESS_STATUSES = {"completed","success","done","ok"}`、`_FAILURE_STATUSES = {"failed","failure","error"}`、`_ABORTED_STATUSES = {"aborted","cancelled","canceled"}`、`_UNRUN_STATUSES`（含 `awaiting_approval`）。
- **step 级（**不是** run 级）**：`forgeflow/codeplane/protocol.py::STEP_STATUSES = ("ok","error","unavailable","refused","blocked","not_applicable","running","awaiting_approval","pending_approval","paused")`；`forgeflow/runtime/planning.py::FAILED_STATUSES={"error","unavailable","refused"}`（+`SUCCEEDED_STATUS="ok"`,`BLOCKED_STATUS`,`NOT_APPLICABLE_STATUS`,`TERMINAL_STATUSES`）；`forgeflow/skills/pattern_miner.py::FAILURE_STATUSES={"error","unavailable","refused"}`。
- **观测侧**：`forgeflow/observability/metrics_source.py::_TERMINAL_STATUSES=("completed","failed")`，`forgeflow/evaluation/agent_metrics.py::_TERMINAL_STATUSES` 同值。
- **结论**：**没有单一 run-status 枚举**；`"failed_validation"` 全仓 `NOT_FOUND`。T24 若要新增，run 级最贴近的「权威词汇表」是 `workspace/store.py::TERMINAL_STATUSES`，生产赋值点是 `orchestrator.RunRecord.status`。

**B6. run 创建/结束写库函数**（`workspace_runs`）
- `forgeflow/workspace/store.py::WorkspaceStore(Protocol).save(record)`（insert/upsert）；实现 `MemoryWorkspaceStore.save()`（:180）、`PgWorkspaceStore.save()`（:326，`INSERT INTO workspace_runs ... ON CONFLICT DO UPDATE`）
- 状态更新：`mark_interrupted(tenant_id, run_id)`、`interrupt_stale_running()`、`soft_delete_session(...)`（Pg :428/:443/:462）
- 调用点：`forgeflow/runtime/orchestrator.py` 的 `store.save(record)`（:1657、:1681、:3147）

**B7. `pattern_miner.derive_run_outcome()`**
- 签名 `derive_run_outcome(steps: list[Any]) -> str`（:160）；读每个 step 的 `step_status(step)`（由 `step_tool/step_status` 解 step 对象的 `.status`，或 dict 的 `status`/`verdict`）；任何 step ∈ `FAILURE_STATUSES`(`{"error","unavailable","refused"}`) ⇒ 返回 `"failure"`，否则 `"success"`
- 即：**基于 step 的硬失败状态**判定，不看 run 的验收/`outcome` 字段；全仓 `ACCEPTED_EXPLICIT` **NOT_FOUND**（T16 需新增概念）

**B8. `tests/conftest.py::pg_purge` 清理的表**
- 该 fixture **不内置固定表清单**：`pg_purge(*tables)` 由调用方传表名，`DELETE FROM <table>`（非 postgres 档为 no-op）。
- 现有调用实际使用的表名：`cost_budgets`（test_cost_api / test_cost_degrade）、`skill_ratings`、`skill_listings`（test_marketplace_skills）。

### C. T13（真实隔离沙箱）

**C9. 仓库已有子进程/超时/资源能力（可复用）**
- `forgeflow/codeplane/engine.py`：`SubprocessOpenHandsEngine`（:225，`subprocess.Popen` + `wall_timeout_s` + `proc.terminate()`/`subprocess.TimeoutExpired`）、`AgentServerOpenHandsEngine`、`_AutoFallbackEngine`、`get_code_engine()`、`CodeJob`/`CodeRunResult`/`CodePlaneEngine(Protocol)`
- `forgeflow/codeplane/workspace.py`：`Workspace`、`WorkspaceManager`、`get_workspace_manager()`（`subprocess.run(["git", ...])` 限制在项目外）
- `forgeflow/codeplane/runner/run_code_task.py`：`_run_with_wall_clock(conversation, wall_timeout_s)`（守护线程 + `join(timeout)`）、`_WallClockExceeded`
- `forgeflow/mcp/server/tools/platform_tools.py::_run_git(args, cwd, timeout)`（限时 git）
- `forgeflow/resilience/circuit_breaker.py`：`CircuitBreaker`、`CBState`、`CircuitOpenError`、`get_circuit_breaker()`；`retry.py`：`with_retry()`、`retry_async()`；`budget_guard.py`：`BudgetGuard`、`BudgetExceededError`
- **缺什么**：无 OS 级配额/隔离原语 —— 全仓 `resource.setrlimit` / `multiprocessing` / cgroup / `seccomp` 均 **NOT_FOUND**；现有超时是「进程级 wall clock + terminate」，无 CPU/内存/文件句柄配额。T13 需新增子进程配额层（可复用 `subprocess`/`wall_timeout_s`/`terminate` 模式）。

**C10. T04「轻量沙箱」是否同进程**
- 是**同进程**执行。入口 `forgeflow/skills/engineering.py::sandbox_evaluate()`（:298）→ `forgeflow/skills/tester.py::run_tests()`（:357）；受限模式 `tester.py::_run_tests_restricted()`（:238）/`_simulate_restricted_plan()`（:308）用 `concurrent.futures.ThreadPoolExecutor(max_workers=1)`（:304）在**本进程线程内**跑 mock 绑定，`SandboxMode.DECLARATIVE="declarative"` / `RESTRICTED="restricted"`，`RESTRICTED_ALLOWED_CLASSES={"READ","WRITE","EXTERNAL"}`（DANGEROUS fail-closed 为 `error`）。无 OS 隔离、无子进程。

### D. T32（脱敏）

**D11. `experiences` 表与 INSERT**
- 建表迁移：`alembic/versions/010_agentflow_hubs.py`（`CREATE TABLE IF NOT EXISTS experiences`，:75）。列：`id UUID PK DEFAULT gen_random_uuid()`、`tenant_id UUID`、`team_id UUID`、`run_id UUID`、`summary TEXT NOT NULL DEFAULT ''`、`decisions JSONB NOT NULL DEFAULT '[]'`、`outcome VARCHAR(16) NOT NULL DEFAULT 'success'`、`reusable_steps JSONB NOT NULL DEFAULT '[]'`、`tags TEXT[] NOT NULL DEFAULT '{}'`、`embedding vector(1536)`、`created_at TIMESTAMPTZ NOT NULL DEFAULT now()`。
- **现行类型（后续迁移修正）**：`tenant_id`/`team_id` → **TEXT**（`013_tenant_id_text.py`）；`run_id` → **TEXT**（`014_run_id_text.py`）。`011_cost_slo_evolution.py` 追加列：`merged_from UUID[] NOT NULL DEFAULT '{}'`、`conflict_with UUID[] NOT NULL DEFAULT '{}'`、`dedup_key TEXT`、`confidence DOUBLE PRECISION`。
- `PgExperienceRepository.save()`（`forgeflow/repositories/postgres/experience_repo.py` :91）INSERT 列：`id, tenant_id, team_id, run_id, summary, decisions, outcome, reusable_steps, tags, embedding, merged_from, conflict_with, dedup_key, confidence, created_at`（`ON CONFLICT (id) DO UPDATE`；`embedding` 以 `$10::vector` 写入，`run_id or None`）。

**D12. 已有脱敏/正则掩码代码 —— 已存在**
- `forgeflow/security/pii_redactor.py`：`redact(text) -> tuple[str, list[RedactionMatch]]`、`RedactionMatch`、正则 `_EMAIL_RE/_PHONE_RE/_SSN_RE/_CN_ID_RE/_CREDIT_CARD_RE/_API_KEY_RE`、`_luhn_valid()`
- 关联消费：`forgeflow/governance/dlp_rules.py::DlpRuleSet.scan`（先跑 `pii_redactor.redact`）、`forgeflow/governance/dlp.py`、`forgeflow/middleware/security.py`（`from forgeflow.security.pii_redactor import redact`）
- 结论：T32 **不是从零**，应扩展现有 `pii_redactor`/`dlp_rules`。

### E. T33（注入防护）

**E13. `forgeflow/security/` 文件清单与公开符号**
- `__init__.py`：`__all__ = ["RedactionMatch", "redact", "RiskLevel", "RiskScore", "scan_prompt"]`
- `pii_redactor.py`：`redact()`、`RedactionMatch`
- `prompt_guard.py`：`RiskLevel`（StrEnum）、`RiskScore`、`scan_prompt(text) -> RiskScore`、`_EN_HIGH_PATTERNS`/`_ZH_HIGH_PATTERNS`/`_MEDIUM_PATTERNS`/`_HIGH_PATTERNS`
- `tool_output_guard.py`：`sanitize_tool_output(name, output) -> str`、`guard_tool_output(tool)`、`guard_tools(tools)`、`SYSTEM_HARDENING_NOTE`、`_sanitize_tool_result()`
- `ssrf_guard.py`：`check_url(url)`、`safe_get()`、`SSRFBlocked`、`_ALLOWED_SCHEMES`
- `email_allowlist.py`：`is_recipient_allowed(recipient, allowed_domains)`、`require_allowed(...)`、`EmailNotAllowed`
- 结论：T33 基础件（`prompt_guard` + `tool_output_guard`）**已存在并已接线**（`runtime/tool_executor.py::sanitize_tool_output` :642、`mcp/client/adapter.py::guard_tools`）。

### F. 通用

**F14. `ExperienceExtractor.extract()` 完整签名 + `ExperienceRecord` 字段**
- `forgeflow/experience/extractor.py::ExperienceExtractor.extract(self, run: Any, verdict: Any = None, memories: list[Any] | None = None, *, tenant_id: str | None = None, team_id: str | None = None) -> ExperienceRecord`（:159）；同文件 `extract_terminal(self, run, verdict=None, memories=None, *, tenant_id=None, team_id=None) -> ExperienceRecord | None`（:218）、模块函数 `extract_experience(...)`（:236）
- `forgeflow/experience/models.py::ExperienceRecord` 字段：`id`、`tenant_id`、`team_id`、`run_id`、`summary`、`decisions`、`outcome`、`reusable_steps`、`tags`、`embedding`、`merged_from`、`conflict_with`、`dedup_key`、`confidence`、`created_at`、`memory_ids`（`OUTCOMES = ("success","failure","aborted")`）

**F15. 测试基线（只读计数，未运行 pytest）**
- `tests/` 下 `test_*.py`：**244** 个（`tests/unit` 198、`tests/integration` 39、其余 7 = `realstack` 4 + `qa_independent` 2 + `fixtures/inc26/code_fixture/test_invoice.py` 1（夹具，非真实用例））
- `def test_` / `async def test_` 顶层与类方法总计：**2153**（含上述夹具文件；按 `^[[:space:]]*(async )?def test_` 计）

---

## 13. T20 落地接缝（补 · 2026-10-03，提交 `T20`）

> 供 **T23（五层验证栈）** 消费：T23 的 L2「不变量」层直接吃 T20 的 `InvariantSet`。

- 包新增：`forgeflow/documents/locator.py`、`invariants.py`、`intent.py`；路由新增 `forgeflow/api/routers/documents.py`（挂载 `forgeflow/api/main.py` 的 `include_router(documents.router, prefix="/documents")`）。
- **定位**：`locator.py::locate(source: bytes|DocStructure, selector, *, threshold=None) -> LocatorResult`；`LocatorResult.{chosen, ambiguity, candidates, not_found, structure, located, confidence_threshold}`；`LocatorCandidate.{index, level, label, confidence, evidence, section_end_index}`。
  - 常量：`CONFIDENCE_THRESHOLD=0.8`、`_SINGLE_SOURCE_CONFIDENCE=0.85`、`_STRUCTURAL_CONFIDENCE=0.9`、`_CONTAINS_CONFIDENCE=0.95`、`_AGREEMENT_CAP=0.97`。
  - **唯一歧义注入点**：`locator.py::_apply_ambiguity_policy(candidates, threshold) -> (selected, ambiguity)`（反事实测试换注入点即改 `_DIVISION_KINDS` / 该函数）。
  - 选择子语法：`parse_selector(selector) -> SelectorSpec{raw, mode ∈ contains|indexed|division|marker|unparsed, ordinal, kind, level, contains}`。
- **不变量**：`invariants.py::extract_invariants(data, *, start, end) -> InvariantSet`；`InvariantSet.{items, coverage, baseline_items()}`；`Invariant.{kind, value, location, evidence, explicit}`；`BASELINE_KIND="baseline"`；`_COVERAGE`（covered/uncovered）。金额/日期识别器**独立于** `docx_inspect._NUMERIC_RE`（后者是 ASCII 专用且被编辑数字守护消费，**禁改**）。
- **意图**：`intent.py::resolve_intent_document(data, instruction) -> EditIntent | NotResolved`；`EditIntent` 六字段 = `INTENT_FIELDS`；`intent_id = "intent-"+sha256(归一化指令‖选择子)[:16]`。
  - 与既有 **LLM 层** `docx_edit.py::resolve_intent(data, intent, structure)`（async，产出 `EditOp`，不写字节）**共存**：T20 是**确定性前端**（不调模型），负责"指向哪里 / 什么不能变"。
- `docx_inspect.py` **只增**符号：`chinese_number_to_int`、`text_marker`、`paragraph_numbering_ids`、`compute_numbering_labels`；`DocStructure` 追加 `table_ids`/`image_ids`，heading 项追加 `numbering_label`/`section_end_index`。既有 `to_dict()` 键**一字未改**。
- 路由：`documents.py::resolve_document_intent`（`POST /{document_id}/intent:resolve`）；`_resolve_document_bytes(tenant, document_id)` 走 `ResourceService().get(tenant, id)` → `record.kind == ResourceKind.FILE.value` → `FileLocator.storage_ref` → `service.blobs.exists/read`；请求体 `IntentResolveRequest{instruction, document_text}`，**`document_text` 永不解析**（红线 14）。
- RBAC：`rbac/policies.py` 增 `("POST","/documents"): ("read","skills")`（最长前缀命中；未放宽任何角色）。
- 测试：`tests/unit/test_inc46_doc_intent.py`（16，路由用打桩取字节）、`tests/integration/test_inc46_doc_intent_api.py`（6，**真 seam**）。QA 独立文件：`tests/qa_independent/test_qa_t20_independent.py`（28）。


## 14. T23 落地接缝（五层验证栈）

- **入口**：`forgeflow/documents/validation/stack.py::validate_document(before, after, *, instruction/selector/start/end, config, judge, render_engine, render_converter, rasterizer, bbox, page_counter) -> ValidationVerdict`
- **区间解析**：`stack.py::resolve_range` 优先级 **显式 start/end > selector（T20 `locator.locate`）> instruction（T20 `intent.resolve_intent_document`）> 整篇**；歧义/未找到**不猜**，回退整篇并写 note。
- **消费 T20 契约（禁止 fork 识别器）**：L2 调 `forgeflow/documents/invariants.py::extract_invariants`；L1/L3 调 `forgeflow/documents/docx_inspect.py::open_docx` / `heading_level` / `document_numbers`。**`docx_inspect._NUMERIC_RE` 不得修改**（其 sha256 是 T20 冻结点）。
- **层判决**：`verdict.py::LayerVerdict(layer, status, evidence_ref, detail)`，`status ∈ {pass, fail, needs_review, None}`；`ValidationVerdict(layers: dict[str, LayerVerdict], overall, unmeasured[], notes)`。
- **聚合**：`stack.py::_aggregate` 优先级 **fail(L1–L3) > fail/warn(L4，按 `l4_fail_is_fatal`) > warn(页数变化) > needs_review(L5) > pass**。
- **注入接缝（供 T24/T27/T36 复用）**：`judge`（L5，另见 `semantic.set_default_judge`）、`render_engine`/`render_converter`/`rasterizer`（L4）、`page_counter`（L4 页数，默认 `render.probe_pdf_page_count`）、`config.disabled_layers`（**测试接缝**，非生产配置）。
- **转包与导入路径**：`forgeflow.documents.validation` 现为**包**；`legacy.py` 逐字承载旧 `verify_*` 六名；**新层符号不在 `__all__`**（`__all__` 保持 legacy 六名逐字）。消费者可 `from forgeflow.documents.validation import verify_docx`（不变）或 `...validation.stack import validate_document`。
- **后续消费者**：T24 修复循环（按层 fail 定位修复）、T22 Diff 预览（区间外变化阻断）、T27 格式能力矩阵、T36 指标（L4/L5 未测量口径）、T19 冷启动。
