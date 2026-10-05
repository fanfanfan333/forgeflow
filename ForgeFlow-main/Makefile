# ForgeFlow developer command entry layer.
#
# Make is a *command orchestrator*, not a Python runtime dependency of
# ForgeFlow. Nothing in the Makefile holds business logic: every recipe shells
# out to the pinned toolchain (`python -m ruff|mypy|pytest`) or to a project
# script under scripts/.
#
# Cross-platform by construction: the recipes below avoid cmd.exe / PowerShell
# / bash-only constructs (no `export VAR=1`, no `VAR=1 cmd` prefixes, no
# `rm -rf`, no shell tricks inside `$(shell ...)`), so the same file runs under
# a native Windows make and under sh on Linux CI.
#
# How to read the targets:
#   * `make check` is the STANDARD PRE-COMMIT CHECK. It must be green before you
#     commit or open a PR. It is exactly: lint -> format-check -> typecheck ->
#     test, and it fails non-zero if any step fails.
#   * `make lint` is the MANAGED-surface ruff GATE: `ruff check $(MANAGED)`. It
#     must be green. `make lint-gate` is a compatibility alias for it.
#   * `make lint-all` runs `ruff check .` over the WHOLE repository. It is
#     currently RED (pre-existing legacy debt) and is informational, NOT a gate.
#     Do not mass-refactor old business code to make it pass.
#   * `make lint-ratchet` is the NEW-DEBT RATCHET (scripts/ruff_debt.py). It
#     counts the tracked-file lint debt under forgeflow/ dashboard/ tests/ and
#     fails if the total rose above the baseline frozen in
#     docs/quality/ruff-baseline.json. It is green today and stays green as long
#     as nobody adds debt. It is a *standalone* target: it is NOT part of
#     `make check`.
#   * `make typecheck` is the MANAGED-surface mypy GATE: it must be green.
#     `make typecheck-gate` is a compatibility alias for it.
#   * `make typecheck-all` runs mypy over the WHOLE repository source roots. It is
#     currently RED (legacy debt) and is informational, NOT a gate.
#
# The analyser versions are pinned in pyproject.toml ([project.optional-
# dependencies].dev) so a local run, CI and the agent all use the same ruff /
# mypy builds. Install them with `make install`; inspect them with `make env`.

.DEFAULT_GOAL := help

# Interpreter used for every recipe. Defaults to the `python` on PATH (works on
# Windows and on Linux CI); override per environment, e.g. `make PY=... check`.
PY ?= python

# Managed surface: the code owned by the current work. The *gate* targets scan
# this set only, and it is expected to be genuinely green.
MANAGED := forgeflow/bootstrap forgeflow/api/main.py scripts/ tests/realstack

# Whole-repository source roots for the informational `typecheck-all` target.
# Note: never `mypy .` here — scratch directories under the tree (qa_tmp/, etc.)
# degrade a full-repo run into a misleading near-empty result.
SRC_ROOTS := forgeflow dashboard scripts tests

.PHONY: help install \
        lint lint-all lint-gate lint-ratchet format format-check format-all \
        typecheck typecheck-all typecheck-gate \
        test test-unit test-integration test-fast test-all \
        check realstack backend backend-reload env clean

# ── Help ─────────────────────────────────────────────────────────────
help:
	@echo ForgeFlow developer commands:
	@echo   install         - pip install -e .[dev]  (pinned ruff, mypy, pytest)
	@echo   check           - standard pre-commit check: lint, format-check, typecheck, test
	@echo   lint            - ruff check on the managed surface (green gate)
	@echo   lint-all        - ruff check .  (whole repo; currently RED = pre-existing debt, NOT a gate)
	@echo   lint-gate       - alias of `lint` (managed-surface ruff gate; kept for compatibility)
	@echo   lint-ratchet    - ruff new-debt ratchet (tracked files; green = no new debt)
	@echo   format          - ruff format the managed surface (rewrites files)
	@echo   format-check    - ruff format --check on the managed surface (never rewrites)
	@echo   format-all      - ruff format .  (whole repo; informational)
	@echo   typecheck       - mypy on the managed surface (green gate)
	@echo   typecheck-all   - mypy over the whole repo source roots (currently RED, NOT a gate)
	@echo   typecheck-gate  - alias of `typecheck` (managed-surface mypy gate; kept for compatibility)
	@echo   test            - pytest unit + integration + realstack
	@echo   test-unit       - pytest tests/unit
	@echo   test-integration - pytest tests/integration
	@echo   test-fast       - pytest tests/unit -q
	@echo   test-all        - pytest (whole tree, incl. non-managed suites; informational)
	@echo   realstack       - real PostgreSQL + Ollama integration suite (gated, honest skip handling)
	@echo   backend         - start the API via the single forgeflow.bootstrap entry point
	@echo   backend-reload  - same entry point with hot-reload enabled
	@echo   env             - report whether this machine has the declared dev toolchain
	@echo   clean           - delete build/test caches only (__pycache__, .pytest_cache, .mypy_cache, .ruff_cache)

# ── Install ──────────────────────────────────────────────────────────
install:
	$(PY) -m pip install -e ".[dev]"

# ── Linting ──────────────────────────────────────────────────────────
# Managed-surface gate — must be green.
lint:
	$(PY) -m ruff check $(MANAGED)

# Compatibility alias for `lint` (managed-surface ruff gate). Kept so older
# docs/scripts calling `make lint-gate` keep working.
lint-gate:
	$(PY) -m ruff check $(MANAGED)

# New-debt ratchet: counts lint findings under forgeflow/ dashboard/ tests/ over
# git-tracked files only and fails if the total rose above the baseline frozen in
# docs/quality/ruff-baseline.json. Must stay green; lower the baseline with
# `python scripts/ruff_debt.py --update` as debt is paid down. Standalone target:
# it is deliberately NOT a prerequisite of `make check`.
lint-ratchet:
	$(PY) scripts/ruff_debt.py check

# Whole-repository scan. Expected RED until the pre-existing lint debt is paid
# down; informational, not a gate. See the header notes.
lint-all:
	$(PY) -m ruff check .

# ── Formatting ───────────────────────────────────────────────────────
format:
	$(PY) -m ruff format $(MANAGED)

# Check-only: never rewrites a file. Safe to run in CI.
format-check:
	$(PY) -m ruff format --check $(MANAGED)

# Whole-repository formatting; informational.
format-all:
	$(PY) -m ruff format .

# ── Type checking ────────────────────────────────────────────────────
# Managed-surface gate — must be green. --follow-imports=silent keeps mypy from
# re-reporting debt inside the historical package when it follows imports out of
# the managed scope.
typecheck:
	$(PY) -m mypy $(MANAGED) --ignore-missing-imports --follow-imports=silent

# Compatibility alias for `typecheck` (managed-surface mypy gate). Kept so older
# docs/scripts calling `make typecheck-gate` keep working.
typecheck-gate:
	$(PY) -m mypy $(MANAGED) --ignore-missing-imports --follow-imports=silent

# Whole-repo source roots. Expected RED (legacy debt); informational, not a
# gate. Do not use `mypy .` — scratch dirs degrade the result.
typecheck-all:
	$(PY) -m mypy $(SRC_ROOTS) --ignore-missing-imports

# ── Tests ────────────────────────────────────────────────────────────
# Managed test tree. No external service is started: the realstack package
# double-gates on FORGEFLOW_REAL_STACK=1 + Ollama/PG liveness, so without those
# it skips with an explicit reason instead of failing.
test:
	$(PY) -m pytest tests/unit tests/integration tests/realstack

test-unit:
	$(PY) -m pytest tests/unit

test-integration:
	$(PY) -m pytest tests/integration

test-fast:
	$(PY) -m pytest tests/unit -q

# Whole tree, including suites not yet managed (e.g. tests/qa_independent).
# Informational.
test-all:
	$(PY) -m pytest

# ── Standard pre-commit check ────────────────────────────────────────
# Order matters and is preserved by GNU Make for sequentially-run
# prerequisites; a failure in any prerequisite aborts the build with a non-zero
# exit. There is deliberately no `|| true` and no forced `exit 0` anywhere.
# Exactly the managed-surface gates in this order: lint -> format-check ->
# typecheck -> test.
check: lint format-check typecheck test

# ── Real stack (real PostgreSQL + Ollama) ────────────────────────────
# Uses a Python launcher so it stays cross-platform; the launcher refuses to
# report success when the environment is unavailable (all-skip is NOT a pass).
realstack:
	$(PY) scripts/run_realstack.py

# ── Backend ──────────────────────────────────────────────────────────
# Both go through the single forgeflow.bootstrap entry point; there is exactly
# one startup strategy (normal -> reload=False, reload -> reload=True).
backend:
	$(PY) -m forgeflow.bootstrap

backend-reload:
	$(PY) -m forgeflow.bootstrap --reload

# ── Environment / caches ─────────────────────────────────────────────
env:
	$(PY) scripts/check_dev_env.py

clean:
	$(PY) scripts/clean_caches.py
