.PHONY: up down migrate test lint lint-ruff lint-mypy lint-all fmt build logs demo env

# ── Local development ──────────────────────────────────────────────
up:
	docker compose up

up-d:
	docker compose up -d

down:
	docker compose down

migrate:
	docker compose --profile migration run --rm migrate

build:
	docker compose build

logs:
	docker compose logs -f

# ── Database ───────────────────────────────────────────────────────
seed:
	docker compose exec api python scripts/seed_db.py

# ── Testing ────────────────────────────────────────────────────────
test:
	pytest tests/unit tests/integration -v --cov=forgeflow --cov-report=term-missing

test-unit:
	pytest tests/unit -v

test-integration:
	pytest tests/integration -v

# ── Code quality ───────────────────────────────────────────────────
# The analyser versions are pinned in pyproject.toml
# ([project.optional-dependencies].dev: ruff==0.16.10, mypy==2.4.0) so `make lint`,
# CI and the local agent all run the *same* builds. Install them with:
#     python -m pip install -e '.[dev]'      # or: pip install -r requirements-dev.txt
# Check what is present with:  make env
LINT_SCOPE      := forgeflow/bootstrap forgeflow/api/main.py scripts/ tests/realstack
MYPY_EXTRA_ARGS := --ignore-missing-imports --follow-imports=silent

# `make lint` gates the managed INC48 surface (bootstrap / API entry / scripts /
# Windows real-stack tests). It is expected to be green.
lint: lint-ruff lint-mypy

lint-ruff:
	ruff check $(LINT_SCOPE)

# --follow-imports=silent keeps mypy from re-reporting debt inside the whole
# historical package when it follows imports out of the managed scope.
lint-mypy:
	mypy $(LINT_SCOPE) $(MYPY_EXTRA_ARGS)

# Whole-repository scan. This is expected to be RED until the pre-existing
# lint/type debt is worked down; it is informational, not a gate, and it is
# deliberately not run by CI. Do not mass-refactor old business code to make it
# pass — see docs/winboot-2026-10-04.md ("BASELINE / EXISTING DEBT").
lint-all:
	ruff check .
	mypy forgeflow/ --ignore-missing-imports

fmt:
	ruff format forgeflow/ dashboard/ tests/

# ── Environment ────────────────────────────────────────────────────
env:
	python scripts/check_dev_env.py

# ── Demo ───────────────────────────────────────────────────────────
demo:
	python scripts/run_demo.py

eval:
	python scripts/generate_eval_dataset.py

# ── Help ───────────────────────────────────────────────────────────
help:
	@echo "ForgeFlow Makefile targets:"
	@echo "  up         Start all services"
	@echo "  down       Stop all services"
	@echo "  migrate    Run Alembic migrations"
	@echo "  test       Run all tests with coverage"
	@echo "  lint       Run ruff + mypy on the managed surface (green gate)"
	@echo "  lint-all   Run ruff + mypy on the whole repo (known pre-existing debt)"
	@echo "  env        Report whether this machine has the declared dev toolchain"
	@echo "  fmt        Auto-format with ruff"
	@echo "  demo       Run demo workflow"
	@echo "  seed       Seed demo data"
