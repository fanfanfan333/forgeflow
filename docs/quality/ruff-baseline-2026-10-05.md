# Ruff Lint-Debt Baseline — 2026-10-05 (whole-repository view)

Machine-readable twin: [`ruff-baseline.json`](ruff-baseline.json) (schema v1).
Cleanup ledger: [`ruff-debt.md`](ruff-debt.md).
Tracked-only perspective (2026-10-04): [`ruff-baseline-2026-10-04.md`](ruff-baseline-2026-10-04.md).
Ratchet script: [`../../scripts/ruff_debt.py`](../../scripts/ruff_debt.py).

> **This is lint debt, not bugs.** The **493** whole-repository findings below are
> **lint debt — code-quality debt — not business Bugs, and not functional
> defects.** They are style / typing / modernization findings that Ruff reports;
> they say nothing about whether a feature works. "Fixing" a finding can even
> introduce a regression, which is why behaviour-sensitive rules are gated behind
> human review (see §6 and the Preserved list in [`ruff-debt.md`](ruff-debt.md)).

This document records the **whole-repository** domain — `python -m ruff check .`
— which is a different (wider) lens than the tracked-only ratchet described in
[`ruff-baseline-2026-10-04.md`](ruff-baseline-2026-10-04.md). Both are kept on
purpose; the two views must stay mutually consistent.

## 1. Baseline metadata

| Field | Value |
|---|---|
| ① Baseline date | **2026-10-05** |
| Measured at (this run) | `2026-10-05T10:38:41+08:00` |
| ② Ruff version | **0.16.10** (pinned in `pyproject.toml [project.optional-dependencies].dev`) |
| ③ Python version | **3.13.14** (venv `agentflow`) |
| ④ Scan command | `python -m ruff check .` |
| ⑤ Total errors | **493** |
| Initial anchor (2026-10-04, pre-cleanup) | **515** |
| Resolved since the anchor | **22** |
| Remaining (live) | **493** |

## 2. ④ Scan scope and the totals

| Scan | Command | Count |
|---|---|---|
| **Whole repo, raw** (this document's domain) | `ruff check .` | **493** |
| Official scope, raw (ratchet scope + scratch) | `ruff check forgeflow/ dashboard/ tests/` | 482 |
| Official scope, **tracked only** (the ratchet) | `ruff check forgeflow/ dashboard/ tests/` ∩ `git ls-files` | **462** |

### Initial anchor — how 515 became 493

The **515** anchor is the **whole-repo** (`ruff check .`) value measured on
**2026-10-04**, *before* the Phase-1 cleanup. Phase 1 (2026-10-04) applied the
same batch of **22** zero-risk mechanical fixes, which dropped the whole-repo and
the three-directory raw total **in lockstep by 22**:

```
Initial (2026-10-04):  515
Current (2026-10-05):  493     (515 - 22)
Resolved:               22
Remaining:             493
```

The `initial_totals` block in [`ruff-baseline.json`](ruff-baseline.json) freezes
the whole-repo anchor (`515`) alongside the tracked (`484`) and raw (`504`)
anchors so the original debt is always distinguishable from the live numbers.

### Untracked scratch — the 493 vs 466 gap (explicit)

**27** of the 493 whole-repo findings come from **untracked scratch files** that
are deliberately *not* part of the repository. A clean clone therefore reproduces
**466**, not 493.

| Untracked scratch | Findings |
|---|---:|
| `tests/qa_independent/*.py` (6 files) | 20 |
| repo-root `qa_*.py` (2 files: `qa_audit_recon.py` 6, `qa_t15_p8a_repro.py` 1) | 7 |
| **total untracked scratch** | **27** |

`tests/qa_independent/` (20) is inside the ratchet's *directory* scope but is
excluded because it is untracked — the ratchet intersects with `git ls-files`
precisely so scratch files can neither trip nor mask a regression. The repo-root
`qa_*.py` (7) sit outside the scope roots entirely.

* **Clean-clone reproducible value = tracked-only = 466** (`493 − 27`).
* The tracked-only figure is the whole repo restricted to `git ls-files`
  (i.e. `ruff check .` ∩ `git ls-files`), independent of the three-directory
  ratchet scope.

### Out-of-scope notice (explicit)

`frontend/scripts/e2e_verify.py` carries **4** findings. It is **tracked**, but
it is **not** inside the `forgeflow/ dashboard/ tests/` ratchet domain declared
in spec §一, so it is **recorded here but not counted toward the ratchet** (the
ratchet script never scans `frontend/`). The repo-root scratch files
(`qa_audit_recon.py`, `qa_t15_p8a_repro.py`) are **not counted** either — they are
untracked and out of scope.

## 3. ⑥ Per-rule breakdown (complete — whole repo, 493)

| Rule | Count | Note |
|---|---:|---|
| UP037 | 75 | quoted annotation — mechanical |
| UP017 | 62 | `datetime.timezone.utc` → `datetime.UTC` — mechanical |
| F401 | 54 | **preserved** — may change runtime behaviour |
| I001 | 50 | unsorted imports — mechanical |
| UP035 | 48 | deprecated import (`typing` → `collections.abc`) — mechanical |
| E702 | 36 | semicolon statements — mechanical |
| B905 | 33 | **preserved** — `zip(strict=)` needs a per-site decision |
| ANN001 | 24 | **preserved** — missing arg annotation |
| UP012 | 20 | redundant `encode()` — mechanical |
| SIM300 | 17 | yoda condition — mechanical |
| UP031 | 14 | printf format → f-string — mechanical |
| SIM117 | 11 | nested `with` — mechanical |
| B007 | 9 | **preserved** — unused loop var |
| E741 | 4 | **preserved** — ambiguous name |
| SIM102 | 4 | collapsible `if` — mechanical |
| UP042 | 4 | `str`-Enum — mechanical |
| F541 | 3 | f-string without placeholders — mechanical |
| B023 | 3 | **preserved** — loop-var closure |
| SIM103 | 3 | return-condition — mechanical |
| UP041 | 3 | `TimeoutError` alias — mechanical |
| F841 | 3 | **preserved** — unused local |
| B009 | 2 | `getattr` constant — mechanical |
| B904 | 2 | **preserved** — `raise ... from` |
| SIM109 | 1 | redundant comparison — mechanical |
| SIM114 | 1 | combine `if` branches — mechanical |
| F402 | 1 | **preserved** — import shadow |
| SIM118 | 1 | `key in dict.keys()` — mechanical |
| ANN204 | 1 | **preserved** — missing `__init__` return |
| UP045 | 1 | `Optional` → `X | None` — mechanical |
| UP015 | 1 | redundant `open` mode — mechanical |
| SIM110 | 1 | loop → `any()` — mechanical |
| F822 | 1 | **preserved** — `__all__` name |
| **total** | **493** | |

## 4. ⑦ Per-directory breakdown (whole repo, 493)

| Directory | Whole-repo raw | Tracked | Untracked (scratch) |
|---|---:|---:|---:|
| `forgeflow/` | 259 | 259 | 0 |
| `tests/` | 223 | 203 | 20 |
| `dashboard/` | 0 | 0 | 0 |
| `scripts/` | 0 | 0 | 0 |
| `frontend/` | 4 | 4 | 0 |
| repo root (`*.py`) | 7 | 0 | 7 |
| **total** | **493** | **466** | **27** |

(`dashboard/` and `scripts/` contribute **0** findings today. `frontend/` is
out of the ratchet domain — see §2. The repo-root `*.py` are all untracked
scratch.)

## 5. ⑧ Current policy

`make check` enforces the **managed surface** as a **four-step** standard
pre-commit check, and the whole-repo scans are kept as separate, informational
targets:

* `make check` → `lint format-check typecheck test` — the standard pre-commit
  check (the same gate CI runs). Exactly four steps, in that order; any failure
  is non-zero.
* `make lint` → `ruff check $(MANAGED)` — the **managed-surface ruff gate**;
  must be green (it is). `make lint-gate` is a compatibility alias.
* `make lint-all` → `ruff check .` — whole-repo scan; **RED, informational, not
  a gate**.
* `make lint-ratchet` → `python scripts/ruff_debt.py check` — the **new-debt
  ratchet** on the tracked three-directory scope; **green today**. Standalone:
  it is *not* part of `make check`.
* `make typecheck` → `mypy $(MANAGED) ...` — the **managed-surface mypy gate**;
  must be green. `make typecheck-gate` is a compatibility alias.
* `make typecheck-all` → `mypy $(SRC_ROOTS) --ignore-missing-imports` —
  whole-repo scan; **RED, informational, not a gate**.

Hard constraints carried into every future increment:

* **No `# noqa` mass-suppression.**
* **No widening of `pyproject.toml` `ignore` / `extend-ignore`.**
* Behaviour-sensitive rules are **not** auto-fixed (see §6 and the Preserved list
  in [`ruff-debt.md`](ruff-debt.md)).
* The ratchet baseline may only **go down**; `scripts/ruff_debt.py --update`
  refuses an increase unless `--force --reason "..."` is given (which warns).

## 6. Debt-reduction plan (subsequent increments)

Debt is paid down **mechanically first, then deliberately**:

1. **Zero-risk mechanical fixes** (ruff *safe* fixes only, no `--unsafe-fixes`):
   `UP037`, `UP017`, `UP035`, `UP041`, `I001`, `UP012`, `UP031`, `UP015`, `E702`,
   `F541`, `SIM103`, `SIM110`, `SIM114`, `SIM118`, `SIM109`, `SIM102`, `SIM300`
   — applied area by area, always re-running the unit suite and re-freezing the
   (lower) ratchet baseline with `--update`.
2. **Behaviour-sensitive rules — human judgement required, never blind-fixed:**
   `F401`, `F841`, `B905`, `B023`, `B007`, `B904`, `F402`, `F822`, `E741`,
   `ANN001`, `ANN204`. Each file must be read and the fix justified
   (e.g. `B905` → explicit `strict=`; `F401` → confirm the import is not a
   side-effect / re-export).
3. **Typing debt** (`ANN001` / `ANN204`) is lowest priority and is fixed with the
   surrounding module, not in isolation.

> **Reminder — lint debt ≠ bugs.** None of the counts in this document is a
> business Bug or a functional defect; they are code-quality findings only.
