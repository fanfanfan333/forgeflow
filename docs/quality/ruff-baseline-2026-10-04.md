# Ruff Lint-Debt Baseline — 2026-10-04

Machine-readable twin: [`ruff-baseline.json`](ruff-baseline.json) (schema v1).
Cleanup ledger: [`ruff-debt.md`](ruff-debt.md).
Ratchet script: [`../../scripts/ruff_debt.py`](../../scripts/ruff_debt.py).

> **This is lint debt, not bugs.** The 484 tracked / 504 raw / 515 whole-repo
> counts below are **lint debt — code-quality debt — not business Bugs, and not
> functional defects.** They are style / typing / modernization findings that
> Ruff reports; they say nothing about whether a feature works. (The spec's "503"
> was the figure at the time of writing; the spec explicitly says not to assume it
> is exact. The measured tracked baseline is **484** — 504 raw / 515 whole-repo.)

## 1. Baseline metadata

| Field | Value |
|---|---|
| ① Baseline date | **2026-10-04** |
| Measured at (first baseline run) | `2026-10-05T00:35:06+08:00` |
| ② Ruff version | **0.16.10** (pinned in `pyproject.toml [project.optional-dependencies].dev`) |
| ③ Python version | **3.13.14** (venv `agentflow`) |
| Ratchet threshold (`total`, live) | **462** — re-frozen after Phase 1; was 484 at the baseline date |

> The `Measured at` above is the **first** baseline run — the one that produced
> 484 tracked / 504 raw / 515 whole-repo — and it matches
> `initial_totals.measured_at` in [`ruff-baseline.json`](ruff-baseline.json). The
> JSON's top-level `measured_at` is the most recent `--update` and is inherently
> later; the two timestamps are different by design and are **not** the same event.

## 2. ④ Scan scope and the three totals

| Scan | Command | Count |
|---|---|---|
| Official scope, **tracked only** (the ratchet) | `ruff check forgeflow/ dashboard/ tests/` ∩ `git ls-files` | **484** |
| Official scope, **raw** (incl. untracked scratch) | `ruff check forgeflow/ dashboard/ tests/` | **504** |
| Whole repo, raw | `ruff check .` | **515** |

The official debt domain (spec §一) is `forgeflow/ ` + `dashboard/ ` + `tests/`
restricted to **git-tracked** files: **484 findings across 231 tracked files**.
`dashboard/` contributes **0** findings today.

### Why the three numbers differ

* **504 − 484 = 20** — untracked scratch under `tests/qa_independent/`. These six
  files are deliberately *not* committed; a raw directory scan still sees them:

  | Untracked file | Findings |
  |---|---:|
  | `tests/qa_independent/test_qa_t20_independent.py` | 13 |
  | `tests/qa_independent/test_qa_t13_independent.py` | 2 |
  | `tests/qa_independent/test_qa_t23_independent.py` | 2 |
  | `tests/qa_independent/test_qa_t08_independent.py` | 1 |
  | `tests/qa_independent/test_qa_t13_d1_fix.py` | 1 |
  | `tests/qa_independent/test_qa_t15_interlock_probe.py` | 1 |
  | **total** | **20** |

  The ratchet intersects the scan with `git ls-files` precisely so scratch files
  can neither trip it nor mask a regression.

* **515 − 504 = 11** — files *outside* the three scope roots:

  | Out-of-scope file | Findings | Committed? |
  |---|---:|---|
  | `qa_audit_recon.py` (repo-root scratch) | 6 | no |
  | `frontend/scripts/e2e_verify.py` | 4 | **yes** |
  | `qa_t15_p8a_repro.py` (repo-root scratch) | 1 | no |

### Out-of-scope notice (explicit)

`frontend/scripts/e2e_verify.py` carries **4** findings. It is **not** inside the
`forgeflow/ dashboard/ tests/` domain declared in spec §一, so it is **recorded
here but not counted toward the ratchet** (the script never scans `frontend/`).
The repo-root scratch files (`qa_audit_recon.py`, `qa_t15_p8a_repro.py`) are
**not counted** either — they are untracked and out of scope.

## 3. ⑥ Per-rule breakdown (complete)

Tracked baseline = 484; the "Raw scan" column includes the 20 untracked findings.

| Rule | Tracked (484) | Raw scan (504) |
|------|----:|----:|
| UP037 | 79 | 79 |
| UP017 | 71 | 71 |
| UP035 | 53 | 53 |
| F401 | 52 | 53 |
| I001 | 49 | 52 |
| B905 | 33 | 33 |
| E702 | 24 | 36 |
| ANN001 | 21 | 21 |
| UP012 | 20 | 20 |
| SIM300 | 16 | 17 |
| UP031 | 13 | 13 |
| SIM117 | 11 | 11 |
| B007 | 4 | 5 |
| E741 | 4 | 4 |
| SIM102 | 4 | 4 |
| UP042 | 4 | 4 |
| B023 | 3 | 3 |
| F841 | 3 | 3 |
| SIM103 | 3 | 3 |
| UP041 | 3 | 4 |
| B009 | 2 | 2 |
| B904 | 2 | 2 |
| ANN204 | 1 | 1 |
| F402 | 1 | 1 |
| F541 | 1 | 2 |
| F822 | 1 | 1 |
| SIM109 | 1 | 1 |
| SIM110 | 1 | 1 |
| SIM114 | 1 | 1 |
| SIM118 | 1 | 1 |
| UP015 | 1 | 1 |
| UP045 | 1 | 1 |
| **total** | **484** | **504** |

## 4. Per-area breakdown (tracked, 484)

| Area | Findings |
|---|---:|
| tests/unit | 138 |
| forgeflow/skills | 45 |
| tests/integration | 37 |
| forgeflow/documents | 32 |
| forgeflow/codeplane | 29 |
| tests/corpus | 28 |
| forgeflow/lifecycle | 23 |
| forgeflow/runtime | 15 |
| forgeflow/api | 12 |
| forgeflow/rollout | 12 |
| forgeflow/agent | 11 |
| forgeflow/metrics | 11 |
| forgeflow/evaluation | 9 |
| forgeflow/governance | 9 |
| forgeflow/context | 8 |
| forgeflow/experience | 8 |
| forgeflow/outcomes | 8 |
| forgeflow/repositories | 7 |
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
| **total** | **484** |

## 5. ⑦ Current policy

`make lint` is a **new-debt ratchet**, not a whole-repo scan:

* `make lint` → `python scripts/ruff_debt.py check` — counts the tracked findings
  in the official scope and **fails only if the total grew above 462** (the current
  locked floor; 484 at the 2026-10-04 baseline). It is
  **green today** and stays green as long as nobody adds debt.
* `make lint-gate` → `ruff check $(MANAGED)` — the **absolute** gate over the
  managed surface; must be green (it is).
* `make lint-all` → `ruff check .` — whole-repo scan; **RED, informational, not a
  gate**.
* `make check` → `lint-gate lint format-check typecheck-gate test` — the standard
  pre-commit check (CI runs exactly this).

Hard constraints carried into every future increment:

* **No `# noqa` mass-suppression.**
* **No widening of `pyproject.toml` `ignore` / `extend-ignore`.**
* Behaviour-sensitive rules are **not** auto-fixed (see §6 and the Preserved list
  in [`ruff-debt.md`](ruff-debt.md)).
* The ratchet baseline may only **go down**; `scripts/ruff_debt.py --update`
  refuses an increase unless `--force --reason "..."` is given (which warns).

## 6. ⑧ Debt-reduction plan (subsequent increments)

Debt is paid down **mechanically first, then deliberately**:

1. **Zero-risk mechanical fixes** (ruff *safe* fixes only, no `--unsafe-fixes`):
   `UP037`, `UP017`, `UP035`, `UP041`, `I001`, `UP012`, `UP031`, `UP015`, `E702`,
   `F541`, `SIM103`, `SIM110`, `SIM114`, `SIM118`, `SIM109`, `SIM102`, `SIM300`
   — applied area by area, always re-running the unit suite and re-freezing the
   (lower) baseline with `--update`.
2. **Behaviour-sensitive rules — human judgement required, never blind-fixed:**
   `F401`, `F841`, `B905`, `B023`, `B007`, `B904`, `F402`, `F822`, `E741`,
   `ANN001`, `ANN204`. Each file must be read and the fix justified
   (e.g. `B905` → explicit `strict=`; `F401` → confirm the import is not a
   side-effect / re-export).
3. **Typing debt** (`ANN001` / `ANN204`) is lowest priority and is fixed with the
   surrounding module, not in isolation.

## 7. Post-baseline progress (Phase 1)

The first P1 batch (spec D7) cleared the zero-risk mechanical findings under
`forgeflow/agent ` + `forgeflow/runtime ` + `forgeflow/metrics`:

| Rule | Baseline | After Phase 1 |
|------|----:|----:|
| UP037 | 79 | 75 |
| UP017 | 71 | 61 |
| UP035 | 53 | 48 |
| I001 | 49 | 47 |
| UP041 | 3 | 2 |
| **tracked total** | **484** | **462** |

22 findings resolved; the baseline was re-frozen to **462** with
`scripts/ruff_debt.py --update` (a lowering). Full detail — including the
**Preserved** list — is in [`ruff-debt.md`](ruff-debt.md).
