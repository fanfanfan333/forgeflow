# Professional Agent progress — 2026-10-16

Implemented this round:

- Durable scoped ReAct checkpoints preserve messages, pending accepted calls, usage, input versions and pinned Skill versions. Resume requires the exact version, reauthorizes inputs and creates a separate run. Unknown in-flight operations cannot replay. Process locks distinguish dead and live workers. Directory/repository execution remains explicitly unsupported by this recovery path.
- Actual Ollama/API pause/resume evidence, duplicate-resume rejection and final downloaded code: `evidence/2026-10-15/task-recovery-live.json`. This predates later source-reader changes and does not certify every recovery path.
- Functional Skill evaluation compares without-Skill, candidate and optional incumbent variants against frozen held-out artifact assertions. Trials do not recall or produce training experiences. Candidate/dataset hashes bind results; admission is atomic and interrupted evaluations retain completed rows.
- Learned Skill publication requires matching measured results. Failed feedback creates a separate revision, preserving the old candidate. Four built-in packages import as drafts and include explicitly synthetic examples; independent business evaluation remains required before publication.
- Typed source receipts, real tool-error feedback, keyword/paged source reading and platform-selected original passages address failures exposed by real model attempts. Passage selection still requires independent claim review; it does not assert that a citation supports a conclusion.
- An optional server-controlled HTTPS DNS resolver handles this machine's VPN fake addresses. Public-IP checks, TLS identity, connection pinning and redirect validation remain mandatory. An actual public fetch succeeded and loopback remained blocked. System DNS settings were not changed.

Verification boundaries:

- Current application source passed the isolated PostgreSQL suite: **4140 passed, 32 skipped, 1 xfailed**. Log: `D:/Temp/forgeflow-professional-sandbox-fixed-full-20261016.log`. This includes separate functional/structural evaluation history. Later standalone smoke-runner changes are not application-runtime changes.
- **13 Chromium UI cases passed**, including lazy built-in import and version-bound recovery. Log: `D:/Temp/forgeflow-professional-ui-verified-20261016.log`. TypeScript, lint on the touched evaluation component, and production build passed. These are fixture-driven UI tests, separate from real-service verification.
- Current real API package checks passed: four metadata-only built-in entries, draft-only import, duplicate import rejected with 409, foreign-tenant export rejected with 404, invalid evaluation kind rejected with 422. Evidence: `evidence/2026-10-16/builtin-package-api.json`. QA identities were disabled after testing.
- Published baseline commit `88bf7a45aa9433ad7b1c6859172da61ef8b92ba1` completed **90 real-model attempts**. It is not a snapshot of all earlier uncommitted work. Intermediate failed versions remain separate under `evidence/2026-10-15/professional-benchmark/`; they are not final scores. The common manifest contains synthetic regression data, not a human business Golden set.

Completed comparison: both versions finished all 90 real-model attempts. Raw upgraded completion is **24/30 code, 22/30 document, 8/30 research**. Two research false-completion flags are topic-word case mismatches (`POLICY` versus `policy`), not unsupported citations. A separately reported, hash-checked lexical calibration yields **10/30 research and zero false-completion flags**; raw files remain unchanged. See [full report](evidence/2026-10-15/professional-benchmark/REPORT.md). The baseline is the earlier published release, not this turn's starting dirty tree. The low research completion rate does not satisfy the overall professional-agent goal.

Functional and structural Skill evaluations now retain separate immutable histories. The UI requests functional results explicitly; learned-candidate revision and publication read the latest functional result, including the latest failure. A later instruction check cannot erase or replace actual functional evidence. Memory and PostgreSQL checks cover tenant isolation and append-only behavior.

Mixed-file testing exposed early model termination and invalid same-type file indices. Explicit multi-file Office whitespace-only requests now finish missing work using existing `normalize_whitespace` operations through the same permission, budget and checkpoint boundaries. Negated, compound or unauthorized requests do not use this fallback; each generated target is attempted once per executor loop. File-open/data/formula checks still determine acceptance. The validator may recognize a historical rejected file selector only when a later real edit has an artifact and verified requirements; permission denials and pending approvals remain unresolved. Prior failed runs are retained rather than rewritten.

Functional-trial exceptions/cancellation now finalize their own run header, avoiding stale “running” evaluation tasks. The underlying exception and checkpoint remain available. Focused error/cancellation and validator-boundary checks passed; final live batch and recovery records are tracked separately below.

Current live API checks passed:

- **Two-code batch:** both literal corrections and scoped main returns passed, with distinct helper returns preserved and separate per-file acceptance evidence.
- **Word + Excel batch:** both downloaded files open; Word structure and data, Excel sheets/numbers/formulas/number formats pass. Final status is `completed/success`. Evidence: `evidence/2026-10-16/batch-tasks/results.json`. Earlier failures remain in the adjacent `batch-tasks-before-*` directories; they were not rewritten as success.
- **Pause/resume:** actual checkpoint after `textfile.inspect`; resumed task completes and downloaded code passes; duplicate resume returns 409; original status remains aborted; model calls accumulate from 2 to 3. Evidence: `evidence/2026-10-16/task-recovery-live.json`. This proves this scoped ReAct file path, not all-path or multi-node recovery.

Actual paired Skill smoke evaluation finished **16 trials**: document cleaning and definition navigation each passed 2/2 with and without the Skill; compound repair and research each passed 1/2 with and without it, with different cases regressing and improving. There is no demonstrated aggregate improvement. See [paired results](evidence/2026-10-16/skill-pairs-report.md). Packages remain drafts.

Still open: independent business-holdout Skill evaluation; universal semantic decomposition; all-path recovery; durable per-edit SDK history; repository application behavior acceptance. Offline counts and individual successful examples do not establish completion of the whole expansion plan. Shutdown is cancelled by the user's latest instruction.

Resolver protocol reference: [Cloudflare HTTPS DNS JSON API](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/dns-json/).

Windows sandbox follow-up: a prior full run recorded a process-quota false positive. Supervisor identity capture now occurs before the suspended child resumes. Its 88 passing / 4 skipped targeted checks and the final 4140-passing suite retain quota enforcement; prior failed logs remain available.

## User-approved business Skill assessment

The user approved 20 real INC46 document-review cases. The fixed v5 run completed all 40 paired Ollama executions: without Skill **18/20**, candidate **15/20**. The candidate took longer and did not establish a benefit; it remains a draft. A separate revision draft references the actual failed evaluation and remains unmeasured. Cases used during diagnosis are known regression cases, not an unseen cross-document holdout. See [business report](business-skill-evaluation-2026-10-16.md).

Actual testing exposed and fixed same-source Golden split leakage, missing persisted partitions, inline-report grading, and DOCX task/field ranking. Source bytes, tenant authorization, specification and runtime/model versions are checked before execution; safe-boundary evaluation continuation preserves completed failures and rejects unconfirmed work. Local developer targets now select explicit relevant tests through the existing isolated database runner; full gates remain intact. Current verification is 4003 passing unit/integration tests, 77 passing focused checks, 54 subsequent security/document checks and 3 browser interaction cases; these overlap and must not be summed as unique coverage. The separate 16 realstack tests were gated off, not passed. Ruff, formatting, managed mypy, frontend lint/types/build passed. The broader open items listed above remain open.
