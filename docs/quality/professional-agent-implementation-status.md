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


## 2026-10-15: durable files, isolated code execution and real composite repair

This is a verified implementation increment, not completion of the full extension plan.

Implemented:

- Initial workspace files are stored in content-addressed, fsynced snapshots outside the editable workspace. Manifest hashes bind the tenant and workspace. Bounded capture excludes dependency/build caches, symlinks and private source files; original source files are preserved.
- Authenticated checkpoint listing, paged current-vs-stored file hashes and explicit single-file restore are available. Restore requires a stopped workspace, expected checkpoint version and current file hash; current content is backed up first. Concurrent admission and crash-left locks fail closed. New unrelated user files remain intact. Restore invalidates old code approval evidence.
- Run headers retain actual code-plane metadata through the existing JSON persistence field. Restart history can locate checkpoints without inventing lost step details. Approve/reject decisions are also persisted; old records without evidence remain empty. Root admission rejects submitted code-plane execution evidence.
- A Docker transport reuses the existing OpenHands JSONL runner, with no host-process fallback. The verified image is selected by immutable ID, the process is unprivileged, root and runner are read-only, host .git is read-only, platform secrets and Docker socket are absent, and resource limits are applied. Git execution uses private container metadata and a filtered environment. Cleanup uncertainty marks the workspace recovery_required. Network mode is ordinary Docker bridge; this is not a microVM or an egress policy.
- The code image includes Python, the pinned OpenHands SDK/tools, C/C++ compilers, Node, Playwright and Chromium. Before/after test results are separate; a preexisting failing test is not confused with a failed final repair. Large trace data is summarized while full successful payloads use existing scoped evidence storage.
- Cancellation reaps the managed subprocess tree on Windows/POSIX. Windows supervisor retains root creation identity after short-lived processes exit, preventing stale parent-PID links from importing older unrelated processes.
- Explicit literal edit clauses now receive actual before/after checks, independently of correct return placement. A missing requested literal change fails before producing a success artifact. The authoritative original intent takes precedence over rewritten tool arguments. This is not a universal semantic verifier.
- Frontend code results provide a folded checkpoint panel, version selection, pagination and confirmed restore with current hash/version. Skills/files retain their compact layout.

Final verification:

- Fresh isolated PostgreSQL full suite: **4018 passed, 31 skipped, 1 xfailed**, 303.79 seconds. Log: `D:/Temp/forgeflow-20261015-release-regression.log`. Skips are not passes.
- Changed backend modules passed Ruff. Frontend TypeScript, ESLint and production build passed; existing large-chunk warning remains.
- Chromium files/skills/code-entry/checkpoint regression: **11 passed**. These are synthetic authenticated HTTP fixtures, not proof that every production interaction works.
- Explicit real Docker isolation and real Chromium click/screenshot checks: **2 passed**. The browser scenario is a small isolated local page, not the whole ForgeFlow application.
- Actual local qwen3:8b composite C-file task satisfied both literal correction and return-in-main requirements (6854 reported tokens). Actual Docker/OpenHands Python repair reproduced one failing test and produced a passing test and real diff, preserving the original source. These are two measured examples, not completion-rate benchmarks.
- Local `.env` now selects `CODEPLANE_TRANSPORT=docker`; preflight verified the built image. The API at :8010 and frontend at :5173 started and returned HTTP 200. Health checks do not substitute for end-to-end task acceptance.

Outstanding full-plan acceptance work:

1. Universal model-assisted requirement decomposition and per-requirement semantic evidence across every executor; generic optimization is still unverified when no matching validator exists.
2. Durable per-edit history, explicit task pause/resume and crash-safe side-effect journal. Current file restore is not task recovery, database rollback or remote-operation rollback.
3. SDK-internal model/tool usage integrated with the shared root call budget, and measured capability profiles. Existing SDK iteration/wall limits do not prove exact shared-call accounting.
4. Repository language-service navigation, project-derived check selection and full-application behavior validation. Available container/browser dependencies alone do not implement these features.
5. Real functional skill replay, no-skill/current/candidate comparison on independent holdouts, candidate revision, and four measured built-in skills. Structural checks still cannot publish learned candidates as functionally verified.
6. Complete multi-file document batch and research comparison acceptance, specialist restart/lifecycle coverage, and code/document/research 10 cases each x 3 repeats for both baseline and upgraded versions.

Do not announce full-plan completion or capability uplift percentages until these gates have measured evidence.


## 2026-10-15 follow-up: verify actual behavior before declaring success

Changes and verified behavior:

- Test review rejects a structured list with no recognized executed cases, a nonzero process exit and a missing execution exit code. Node TAP/spec and Vitest summaries are recognized without counting passing files as passing cases. Changed code with unmeasured tests has outcome `unverified`, not `succeeded`.
- Node repositories use declared package.json test/typecheck/build scripts when no explicit job command is supplied. Scripts are selected from fixed names, package size and structure are checked, and the configuration hash is retained. Unsupported repositories retain their configured checks. This is not language-service navigation or automatic dependency installation.
- Code calls reserve the parent's remaining model/tool budget before workspace creation. Docker/subprocess and the bundled AgentServer enforce shared limits before SDK provider/tool calls. Provider retries inside LiteLLM are disabled; SDK retries pass through the same counter. Per-thread guards keep different conversations independent. Measured usage settles reservations; missing usage retains the reservation, including after budget restoration. Provider-reported token usage joins the existing cost contract when available. This does not account for arbitrary HTTP calls made by repository code; Docker bridge is not an egress firewall.
- The actual Docker/Ollama repair used five provider attempts and five SDK tool calls, reproduced a failing test, produced a passing test and real diff, and preserved source files. It reported 14651 input and 195 output tokens. A one-model/one-tool allocation stopped at exactly those measured counts with an error. The actual bundled AgentServer also repaired the repository and passed its test (seven model attempts, six tools). Its local health and API requests now bypass ambient proxies; configured remote servers keep their proxy behavior.
- A real authenticated PostgreSQL/API task revealed that a literal correction plus return was incorrectly treated as return-only, while `代码中的` was incorrectly included in the old literal. Both shared editor branches are fixed, including quoted and contextual Chinese forms. The original request then completed with both evidence receipts; the actual downloaded file contained the requested correction and return before main's closing brace.
- The actual result page revealed another defect: it read the first file's empty inline content instead of the report. The page and edit view now share primary-report selection. Referenced files with unloaded content show unknown size/rows instead of invented zeroes. A real authenticated browser verified the report, absence of the false empty-delivery warning, file metadata and the conversation view. Synthetic QA accounts were disabled after testing; user history was not modified.

Evidence:

![Actual authenticated conversation](evidence/2026-10-15/chat-verified.png)

![Actual result page after fixing report selection](evidence/2026-10-15/result-verified.png)

- Actual login/upload/async task/download/browser proof: `D:/Temp/forgeflow-authenticated-live-20261015.json`.
- Actual result-view verification: `D:/Temp/forgeflow-ui-live-verify-20261015.log`.
- Explicit Docker boundary, Chromium and Node-script checks: 3 passed; together with test-verdict cases, 13 passed (`D:/Temp/forgeflow-docker-node-real-check.log`).
- Frontend TypeScript, ESLint, production build and 12 Chromium regression cases passed (`D:/Temp/forgeflow-20261015-ui-verified-final.log`). Fixture-driven cases and actual-service checks are separate evidence.
- Final fresh isolated PostgreSQL regression: **4048 passed, 32 skipped, 1 xfailed**. Log: `D:/Temp/forgeflow-20261015-accepted-regression.log`. An additional provider-token projection case passed in the 9-case focused budget/metadata suite after full-suite collection. Changed-module Ruff and whitespace checks passed. Passing regressions is not completion of the remaining full-plan gates.

Reference boundaries checked on 2026-10-15: [Claude Code lifecycle hooks](https://code.claude.com/docs/en/hooks), [Deep Agents filesystem middleware](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/filesystem.py), [DeerFlow repository](https://github.com/bytedance/deer-flow), and the locally installed OpenHands SDK **1.49.6** provider/tool boundaries. References guide mechanism selection; their published performance is not a ForgeFlow result. No third main runtime was introduced.

Remaining acceptance gates are still open: universal semantic decomposition; durable task pause/resume and side-effect recovery; per-edit durable history; actual language-service/application-level development workflows; functional skill/holdout replay with candidate revision and four measured built-ins; comprehensive batch/research/specialist recovery coverage; and the requested three-domain 10-case x 3-repeat baseline comparison. The new SDK call budget closes the earlier call-accounting gap for the supported bundled transports, not the whole plan.


## 2026-10-15 continued: evaluation identity and budget settlement

- Structural and engineering evaluations now persist a SHA-256 of the exact candidate JSON. The release gate rejects an evaluated candidate whose instructions, tool scope, resource metadata or other spec content changed. Key order alone does not change the hash. Engineering repair binds the repaired spec, not its pre-repair input. Learned candidates additionally require a bound evaluation; structural scores still do not establish functional success.
- Existing non-learning evaluations without hashes retain their legacy behavior. This compatibility exception is not functional verification and does not apply to newly recorded evaluations.
- Code-budget settlement checks the actual outstanding lease identity, validates both reported counters before mutation and clears the lease after settlement. A stale lease cannot settle a subsequent identical reservation, release quota twice or partially refund malformed usage. Restored unknown usage remains reserved.
- Focused tests cover changed-spec rejection, unchanged-spec publication, repaired-spec identity, duplicate/stale lease rejection and atomic invalid settlement. Full regression results are recorded below when available.

The remaining full-plan gates listed above remain open; this change does not add task recovery or functional Skill holdout replay.

Validation: fresh isolated PostgreSQL full regression **4054 passed, 32 skipped, 1 xfailed** in 291.32 seconds (`D:/Temp/forgeflow-20261015-skill-budget-regression.log`). The final lease-identity hardening and two additional cases landed after full-suite collection; the final 58-case focused suite passed against the final source. Changed-module Ruff and whitespace checks passed. Existing test warnings remain; skipped and expected-failure cases are not counted as passes. Frontend and real-model benchmarks were not rerun for these backend-only changes.


## 2026-10-15 compound-task live acceptance

Three actual Ollama/API upload-and-download cases now pass: literal repair plus scoped main return while preserving helper code; Word whitespace cleanup preserving identity/date/amount/table; and multi-sheet Excel cleanup preserving numbers/formulas/number formats/sheets. The initial runs revealed preserved-return intent interference, missing Office requirement receipts, and model narrowing of workbook-wide normalization to the active sheet. Fixes are in shared edit execution and actual Office-package verification, not final-response wording. Failed/intermediate records remain separate from passing runs. Final downloaded C compiled with GCC C11 -Wall -Wextra -Werror in a network-disabled container and ran with exit 0/output hello world. QA users were disabled after execution.

Details and real files: [compound-task report](complex-task-live-2026-10-15.md). Focused regression: 120 passed. Fresh isolated PostgreSQL full regression: **4065 passed, 32 skipped, 1 xfailed**, 318.10 seconds (`D:/Temp/forgeflow-complex-full-regression.log`). Changed-module Ruff and whitespace checks passed. These are three actual compound-task samples, not completion of the requested repeated three-domain baseline benchmark or the remaining recovery/Skill-learning gates.
