# Skill usefulness and release criteria

Date: 2026-10-17. This is a local, single-document regression assessment, not a general capability or production benchmark. The user approved 20 review cases; each ran with and without the revised Skill using the same local `qwen3:8b` model and permissions. Original business files, answers and execution transcripts remain local and are not included in the public repository.

| Variant | Correct / attempts | Recorded tokens | Summed trial duration |
|---|---:|---:|---:|
| Without Skill | 20 / 20 | 61,500 | 822.52 s |
| Revised Skill | 20 / 20 | 68,404 | 836.12 s |

**No incremental Skill benefit was demonstrated.** Both paths now work on these known cases. The candidate adds tokens and did not save time; it remains a draft. Do not attribute the retrieval repair to Skill learning. Concurrent machine load affects the timing and these are single attempts, not statistical confidence intervals.

The actual tool repair adds paired `task_id` / `field` selectors to `document.inspect`, isolates matching table fields, reports zero/ambiguous matches, and preserves character pagination for long fields. It does not broaden permissions, execute document instructions, or disable injection protection. Generic retrieval and multiple-question queries retain their existing behavior.

The subsequent benefit policy records correctness separately from usefulness. For paired measured cases, no successful baseline case may regress. A candidate must improve the correct-case count, or preserve complete correctness while reducing recorded tokens by at least 10%. Token savings below that threshold or an unmeasured baseline are not accepted as demonstrated benefit; latency alone is not enough. This conservative threshold is a release policy, not a claim of statistical significance. Overall functional acceptance still requires every candidate case to pass. All new candidate promotions require measured functional evidence bound to the current draft and positive benefit; a structural pass cannot authorize release or erase a functional failure. Historical manually authored instructions are not silently removed or represented as newly benefit-verified.

The original quality-only pass remains immutable. The user's later requirement for incremental benefit is recorded as a separate failed assessment under `incremental-benefit-v1`, not used to rewrite the original experiment. No Skill was published. A change to a Skill still requires evaluation of its new fixed version and explicit publication.

Validation: the final broad run exercised 4,010 pass/fail cases: 4,008 passed, while two historical tests still expected structural-only publication and failed under the intentionally stricter policy. Both expectations were migrated and the failure-only rerun passed 2/2 (0.96 s). There were also 16 skips and 1 expected failure. Runtime code did not change between that broad run and the failure-only rerun. The earlier complete run before strict promotion passed 4,009 cases. Focused evaluator/interlock and release/canary/audit tests passed separately; their counts overlap, and their mock evidence is not a real business benefit result. Frontend types, lint and production build passed; 10 relevant browser cases passed, followed by 4 focused engineering cases including the new no-benefit status (overlapping coverage). Managed Ruff, formatting and mypy checks passed.

Known limits: these cases were used to diagnose the reader and are not an unseen holdout. They do not prove cross-document generalization or that other Skills are useful. This change does not migrate or retire historical user-authored Skills automatically.
