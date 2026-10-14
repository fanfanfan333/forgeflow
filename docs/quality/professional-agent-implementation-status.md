# Professional Agent upgrade status

Status: partial implementation, not accepted as a complete capability upgrade.

## Implemented

- ReAct server-owned model/tool/delegation/time budgets shared with delegated runs.
- Requirement receipt with satisfied/unsatisfied/unverified states and additive UI details.
- Separate return-placement evidence; source reading alone does not verify research claims.
- Read-only specialist roles, nested delegation rejection, scoped resource authorization.
- Authenticated actor/tenant-scoped large output storage and hash-checked paged reads.
- Selected skill version readback; explicit selected skills and draft trials remain compatible.
- Code editor read-version conflict rejection and in-process edit snapshots shared by transports.
- Real-model search without a configured provider no longer produces development stub results.

## Verification on 2026-10-13

- Unit suite: 3232 passed, 7 skipped. Final small delegated input-scope adjustment additionally tested with 36 targeted cases.
- Frontend TypeScript build, changed-file ESLint and Vite build passed.
- Full suite blocked on five PostgreSQL integration cases: configured runtime database role does not satisfy existing non-superuser/NOBYPASSRLS and forced-RLS requirements. The guard was retained.
- Ollama model listing reachable. No inference benchmark performed; availability is not evidence of improved task completion.

## Required before final acceptance

- General semantic requirement decomposition beyond the current limited rule recognition.
- Unified acceptance enforcement across legacy/deterministic and ReAct paths. Generic edits with no measured requirement proof remain unverified; execution completion must not be interpreted as full acceptance.
- Code baseline checks, durable edit snapshots and verified stop/restart recovery without side-effect replay. Current snapshots are process-local and file-version checks are not a filesystem transaction lock.
- Full source/citation/claim validation and research report delivery. Research source reads currently remain unverified without citation validation.
- Actual delegated cancellation/resource isolation lifecycle integration tests, artifact projection and aggregate usage accounting.
- Agent capability status derived from live dependency checks; acceptance-qualified skill candidate promotion.
- Repeated identical failure handling that accounts for changed file/environment versions.
- 10 genuine cases per domain, three real repetitions, separate baseline comparison and offline regressions; document data/formula preservation and download/open verification.
- Frontend acceptance and delegation browser end-to-end cases; complete PostgreSQL checks in the correct runtime role.

No external framework performance figures were substituted for ForgeFlow measurements.


## 2026-10-14 execution update

Implemented the acceptance and automatic-candidate admission slice:

- Empty acceptance lists no longer count as verified completion.
- Later edit receipts invalidate earlier acceptance evidence. Return placement no longer proves an additional optimization request; a return-only request is not duplicated.
- The shared validator reports incomplete acceptance as partial delivery. Unverified skill trial runs cannot be reused as passing publication evidence.
- Automatic suggestions use verified execution receipts, authenticated tenant records, scrubbed experiences, matching tool sequences and the existing similarity/minimum-source thresholds. Receipt projections are persisted with run headers for restart readback.
- Automatic suggestions do not invoke another model, are bounded to five seconds, retain provenance and never automatically publish.
- Contract/structural evaluations are identified explicitly. Learned candidates require measured functional evidence; the engineering revision path preserves that gate and pending-approval staging cannot bypass it.
- Missing recorded tools now produces insufficient evidence instead of invented default tools/steps.
- The chat completion disclosure and candidate list distinguish automatic drafts from enabled skills.

Verification:

- Unit suite before the last publication/compiler adjustments: 3241 passed, 7 skipped.
- Isolated-database full regression: 3920 passed, 29 skipped, 1 xfailed, 1 failed. The failed case assumed experiences without any recorded tool steps should compile. Its positive fixture now supplies recorded steps; the corresponding isolated integration and changed subsystem checks passed (113 cases).
- Additional pending-approval bypass test passed; changed-file lint, frontend TypeScript/ESLint, production build and three existing skill-library browser tests passed. Browser fixtures are UI checks, not real-model capability evidence.
- Application runtime database was directly verified as NOSUPERUSER/NOBYPASSRLS. The earlier raw pytest PostgreSQL error came from the test profile's default connection, rather than proving the application's runtime configuration was invalid. Use scripts/run_tests.py for isolated database regression.

Still not complete:

- Universal semantic requirement decomposition and receipt production for all legacy/deterministic paths.
- Durable code snapshots, test baselines, stop/restart side-effect recovery and complete delegated lifecycle/usage accounting.
- Actual functional skill evaluation, research citation/claim validation, automatic skill revision and regression-controlled publishing. New automatic candidates stay drafts until functional evidence exists; legacy manually authored skill behavior remains separate.
- The 30-case, three-repeat baseline versus upgraded real-model benchmark. No improvement percentage is claimed.


## 2026-10-14 research and delegated execution follow-up

Implemented and checked:

- Original-source reads now retain tenant/actor-scoped, hash-addressed receipts. The new research.verify tool resolves exact excerpts before an independent, typed model review of claim support and task relevance. Unsupported or unmeasured claims do not produce an accepted report. Accepted Markdown reports use existing artifact/download projection. This is model-reviewed evidence, not a deterministic guarantee of truth.
- Source references and metadata survive bounded tool-output projection; specialist research can return report artifacts and source references.
- Delegated role tools are intersected with parent restrictions. Recorded evidence requires actual execution. Non-object tool payloads no longer crash projection. Child usage survives cancellation and is included in parent accounting; delegation relationships remain separate from history continuation.
- Failed or concurrently changing file reads cannot authorize edits to an unseen file version.
- Evaluation model failures propagate instead of creating fabricated neutral scores.
- A real local inference failure was reproduced: ambient system proxy resolution caused HTTPX/Ollama requests to return 502 while direct requests succeeded. Shared model construction bypasses system proxies only for localhost, 127.0.0.1 and ::1; remote endpoints retain their existing proxy behavior.

Verification and limits:

- Isolated PostgreSQL full regression completed: 3929 passed, 29 skipped, 1 xfailed (279 seconds). This run predates the final delegate cancellation/read-race and local-proxy additions; those additions received focused regression checks.
- Focused acceptance, learning, source-review, delegation and file-policy regression: 54 passed. Local proxy and provider regression: 17 passed. Changed-module lint passed.
- Actual configured qwen3:8b reviewed one retained official Claude Code source and produced a supported, relevant claim verdict with a Markdown artifact (546 input tokens, 81 output tokens). An earlier review returned an invalid typed verdict and was rejected. This single successful case is not a 30-case capability benchmark or evidence of a completion-rate improvement.
- Sources checked: https://code.claude.com/docs/en/how-claude-code-works ; https://github.com/bytedance/deer-flow ; https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/ .

Still pending: durable file snapshots and safe process-restart recovery; universal requirement decomposition across executors; measured skill functional/holdout evaluation and revision; unchanged-failure retry control and measured capability profiles; the three-domain, ten-case, three-repeat baseline comparison and new browser end-to-end cases. These are not marked complete.


## Frontend design follow-up (awesome-design-md)

Applied the locally bundled Claude design analysis to existing frontend components: warm neutral surfaces, fine borders, editorial page headings, consistent spacing, compact candidate rows, responsive resource controls and visible keyboard focus. File selection now has an explicit cancel action; skill category buttons expose their selected state. No external font download, brand logo, new UI framework or server behavior change was introduced.

Design deviations: primary buttons remain neutral to preserve the product's existing action hierarchy; proprietary Copernicus/Styrene fonts are replaced by local Georgia/Songti/SimSun headings and the existing sans-serif body stack. The bundled reference is third-party analysis, not an official Claude specification.

Verification: TypeScript build and touched-file ESLint passed; five existing file/skill browser scenarios passed, including search, pagination, previews, deletion, skill usage and narrow-screen layout. Added explicit deselection coverage. Final resource-layout adjustment passed both file browser scenarios. Desktop screenshots were inspected using synthetic fixtures; these do not prove backend/model task completion. Production build still reports existing large JavaScript chunks. The broader professional-agent implementation milestones remain pending as listed above.


## Shared budget and pinned skill resources follow-up

- Task admission now initializes a shared execution budget for ReAct, legacy planning and deterministic runtime paths. Legacy model calls consume this budget at the actual invocation boundary; budget exhaustion cannot be swallowed as a provider fallback.
- Budget snapshots include measured elapsed time, call/delegation counts and ceilings. Restore validates all values, preserves spent budget and cannot raise current server ceilings. Legacy snapshots without measured duration are rejected. This is the budget restoration primitive, not an implemented task resume endpoint.
- Root deadlines report explicit budget exhaustion. Interrupted async runs retain budget metadata before dispatcher finalization; ContextVars are reset even when persistence fails.
- Newly submitted task context cannot supply tool execution receipts, model usage or runtime acceptance metadata. Those fields are rebuilt by the server; admitted resources are preserved.
- Published skill selection records a content hash. skills.read checks the selected version/hash and subsequent reads; an explicit caller version cannot override the original selected version.
- Explicit skill support-resource reads reuse ProgressiveLoader. Resource contents are returned with hashes and script_executed=false; reading a bundled script never executes it. Large-output evidence envelopes retain skill/version/resource metadata without conflating the skill hash and evidence-blob hash.

Verification: earlier isolated full regression reached 3948 passed, 29 skipped, 1 xfailed. Subsequent selected-skill and dispatcher checks passed 55 cases; final deadline/trusted-evidence/learning checks passed 39 cases. A fresh isolated full regression is running for the final changes. Changed-file lint passed. Existing deterministic no-model assertions now verify zero model calls while permitting truthful shared budget metadata.

Still pending: persistent file snapshots, explicit task recovery, semantic requirement decomposition across producers, duplicate-failure control, actual skill functional/holdout evaluation, capability profiles and the three-domain real-model comparison. No overall plan-completion claim is made.


### Final verification of this slice

- Fresh isolated full suite: 3954 passed, 29 skipped, 1 xfailed (274.27 seconds), recorded in D:/Temp/forgeflow-harness-validated-full.log.
- Latest dispatcher error-retention adjustment: 30 focused tests passed; touched-module lint passed. Background failure now retains the actual error on the live run detail.
- Cost/token-breaker fixtures now inject simulated usage at the executor boundary after admission, instead of relying on client-submitted runtime metadata. Their 66-case combined regression passed before the final full run. These are offline accounting tests, not measurements of a paid or local model.
- New tasks reset per-task skill-read pins; ReAct prefers the active root budget over a stale reused request context. Child agents retain version/hash metadata without inheriting parent skill instructions or sharing the mutable pin map.
- Large skill-resource receipt metadata was exercised through the actual ToolExecutor evidence offload path: skill and evidence hashes remain separate, the version survives, and reading scripts does not execute them.

The task-resume API and persistent code snapshots are still not implemented. Budget.restore is only a validated restoration primitive, not proof of safe recovery. Functional skill evaluation and the requested real-model capability benchmark remain outstanding.


## 2026-10-14 verified publication checkpoint

Implemented in this follow-up:

- Code workspace metadata now uses atomic, fsynced files outside the editable workspace. Released workspaces can be read after restart; previously active/destroying workspaces become recovery_required rather than falsely active. This does not replay operations or implement task recovery.
- Both OpenHands transports and test/diff subprocesses use an environment allowlist instead of inheriting platform database/auth/API secrets. AgentServer terminal configuration blanks its internal session nonce and refuses an incompatible legacy tool configuration. Local execution still lacks an operating-system sandbox; environment filtering cannot prevent reading host files.
- The shared ToolExecutor stops a third identical failure when tool arguments, error and actual evidence have not changed. Re-reading identical evidence does not unlock retries; new evidence and other runs remain independent. Retry receipts are in-process and are not recovery checkpoints.
- SKILL.md and root-layout ZIP packages can be imported as drafts through the authenticated skills route. UTF-8 references, scripts and evaluation resources round-trip through existing exports and ProgressiveLoader. Resource counts, sizes, archive entries, paths, content hashes and duplicate entries are checked. Scripts are data and never execute on import/read. Binary resources and enclosing ZIP directories are not supported by this initial importer.
- The skills page adds a compact import control and authenticated full-package export. Drafts are not usable; editing them through the instruction-only endpoint is refused to prevent silently dropping resources. Existing instruction skills retain their entry points.

Validation:

- Full isolated PostgreSQL regression: 3992 passed, 29 skipped, 1 xfailed, 292.55 seconds; D:/Temp/forgeflow-final-upload-regression.log. Final package route and invalid-input changes additionally received focused checks.
- Browser file/skill regression: 6 passed, including draft package import, source/resource notice, no activation and mobile layout. These use synthetic HTTP fixtures, not real-model capability benchmarks.
- Frontend TypeScript, ESLint and production build passed. Existing large JavaScript chunk warnings remain. Touched backend modules passed Ruff.
- Package tests include actual FastAPI routes with RBAC, tenant isolation, duplicate imports, ZIP export and prevention of resource-losing instruction edits. Offline package tests exercise path traversal, archive size limits, invalid text, duplicate names and hash conflicts.
- Publication excludes local environment files, runtime data, caches and the temporary test_zzz_probe_debug.py diagnostic. Credential-pattern inspection found only documented synthetic security-test examples.

Remaining plan acceptance gaps (not completed): universal semantic requirement decomposition across all executors; durable per-edit snapshots and explicit safe task recovery; OS-isolated code execution; functional skill/holdout comparison and candidate revision; measured local model capability profiles; repository language-service/application behavioral validation; complete batch/research comparison coverage; the requested three-domain 10-case x 3-repeat baseline comparison. Passing offline regression does not close these gaps. No overall capability improvement percentage or complete-plan claim is made.
