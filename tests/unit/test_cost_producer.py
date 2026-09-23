"""INC2 A1 — the cost producer end-to-end (pricing policy → ledger → /cost/*).

The gap this closes: the Cost Optimization skeleton **never powered on**. There
was no producer feeding ``RunRecord.total_cost_usd``, so ``/cost/board`` always
showed ``0`` and ``/cost/savings`` always showed 「—」. These tests prove, with
an injected priced model + fake token usage (no network, no paid key):

  1. the pricing policy — paid models cost money, self-hosted/unknown models
     do **not** (no fabricated cloud spend);
  2. ``run_task`` records real tokens/cost on the run record;
  3. ``MemoryMetricsSource.has_cost`` is derived from real data;
  4. ``/cost/board`` + ``/cost/savings`` reflect the three scenarios —
     no data / mock-or-ollama / paid model;
  5. the degrade chain actually fires from real ledger spend;
  6. the PostgreSQL ledger branch reads ``workflow_runs`` (fake pool — no DB).

Honesty rule: a mock/self-hosted run must stay ``has_cost=False`` with 「—」 —
never a fabricated amount.

Profile independence: the hub run store is the ledger for the AgentFlow hub path
in *every* ``STORAGE_BACKEND`` (there is no PG run store for hub runs), so the
run-store-backed tests force the ledger onto its memory branch. That keeps them
deterministic whether the suite runs ``STORAGE_BACKEND=memory`` or ``postgres``.
"""

from __future__ import annotations

import pytest
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

from forgeflow.api.routers.cost import build_board_payload, build_savings_payload
from forgeflow.config import get_settings
from forgeflow.cost.budget_service import BudgetService
from forgeflow.observability.cost_tracker import calculate_cost
from forgeflow.observability.metrics_source import MemoryMetricsSource
from forgeflow.repositories.factory import reset_repositories
from forgeflow.repositories.memory.cost_repo import clear_cost_budget_store
from forgeflow.runtime.orchestrator import (
    RequestContext,
    RunRecord,
    TaskCreate,
    _resolve_context_budget,
    get_run_store,
    reset_run_store,
    run_task,
)

_MEMORY_SETTINGS = SimpleNamespace(storage_backend="memory")


class _RecordingBus:
    """Minimal stand-in for the run event bus (no global SSE side effects)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    # Force the ledger onto its memory branch so these run-store assertions are
    # deterministic regardless of the suite's STORAGE_BACKEND.
    from forgeflow.cost import ledger as ledger_mod

    monkeypatch.setattr(ledger_mod, "get_settings", lambda: _MEMORY_SETTINGS)

    reset_run_store()
    reset_repositories()
    clear_cost_budget_store()
    yield
    clear_cost_budget_store()
    reset_repositories()
    reset_run_store()


def _save_run(
    run_id: str,
    tenant_id: str,
    *,
    cost: float,
    tokens: int = 0,
    age_days: float = 0.0,
) -> None:
    """Persist a terminal run directly into the hub run store."""
    created = datetime.now(timezone.utc) - timedelta(days=age_days)
    get_run_store().save(
        RunRecord(
            run_id=run_id,
            thread_id=f"t-{run_id}",
            tenant_id=tenant_id,
            agent_id=None,
            intent="分析销售数据",
            status="completed",
            outcome="success",
            steps=[{"tool": "research.search"}],
            errors=[],
            created_at=created.isoformat(),
            completed_at=created.isoformat(),
            experience_id=f"exp-{run_id}",
            total_tokens=tokens,
            total_cost_usd=cost,
        )
    )


# --------------------------------------------------------------------------- #
# 1. Pricing policy — no fabricated money                                      #
# --------------------------------------------------------------------------- #

class TestPricingPolicy:
    def test_paid_model_is_priced(self):
        # gpt-4o-mini: input $0.00015/1k, output $0.0006/1k
        cost = calculate_cost("gpt-4o-mini", 1000, 500)
        assert cost == pytest.approx(0.00045)

    @pytest.mark.parametrize(
        "model",
        ["ollama", "ollama:latest", "qwen3:8b", "qwen2.5:7b", "llama3.2:3b", "mistral", "mock"],
    )
    def test_free_and_self_hosted_models_cost_zero(self, model):
        assert calculate_cost(model, 100_000, 100_000) == 0.0

    def test_unknown_model_is_not_billed_at_a_cloud_guess(self):
        # The old code fell back to a gpt-4o-shaped price (0.01/0.03). A brand
        # new model we cannot price must be 0.0 (under-report, never fabricate).
        assert calculate_cost("some-unreleased-model-xyz", 1_000_000, 1_000_000) == 0.0


# --------------------------------------------------------------------------- #
# 2. Producer — run_task records real usage on the run record                  #
# --------------------------------------------------------------------------- #

class TestProducer:
    async def test_paid_usage_lands_on_the_run_record(self):
        tenant = "t-producer-paid"
        ctx = RequestContext(tenant_id=tenant, user_id="admin", role="admin")
        task = TaskCreate(
            intent="分析华东销售数据",
            context={
                "llm_usage": [
                    {"agent": "researcher", "model": "gpt-4o-mini", "input_tokens": 1000, "output_tokens": 500}
                ]
            },
        )

        handle = await run_task(task, ctx, bus=_RecordingBus())

        record = get_run_store().get(handle.run_id)
        assert record is not None
        assert record.total_tokens == 1500
        assert record.total_cost_usd == pytest.approx(0.00045)
        assert record.cost_by_agent["researcher"]["calls"] == 1

    async def test_mock_usage_stays_cost_free(self):
        tenant = "t-producer-mock"
        ctx = RequestContext(tenant_id=tenant, user_id="admin", role="admin")
        task = TaskCreate(
            intent="本地模型任务",
            context={
                "llm_usage": [
                    {"agent": "researcher", "model": "ollama", "input_tokens": 9999, "output_tokens": 9999}
                ]
            },
        )

        handle = await run_task(task, ctx, bus=_RecordingBus())

        record = get_run_store().get(handle.run_id)
        assert record is not None
        assert record.total_tokens == 19998  # tokens are counted...
        assert record.total_cost_usd == 0.0  # ...but a free model is not billed


# --------------------------------------------------------------------------- #
# 3. has_cost is derived from real data                                        #
# --------------------------------------------------------------------------- #

class TestHasCostDerivation:
    async def test_has_cost_false_without_billable_runs(self):
        _save_run("free-1", "t-hascost-free", cost=0.0, tokens=1234)
        payload = await MemoryMetricsSource().summary("t-hascost-free")
        assert payload["has_data"] is True
        assert payload["has_cost"] is False
        assert payload["total_cost_usd"] == 0.0
        assert payload["avg_cost_usd"] == 0.0

    async def test_has_cost_true_with_a_priced_run(self):
        _save_run("paid-1", "t-hascost-paid", cost=0.00045, tokens=1500)
        payload = await MemoryMetricsSource().summary("t-hascost-paid")
        assert payload["has_cost"] is True
        assert payload["total_cost_usd"] == pytest.approx(0.00045)

    async def test_recent_runs_exposes_real_tokens_and_cost(self):
        _save_run("paid-2", "t-recent-paid", cost=0.002, tokens=5000)
        runs = await MemoryMetricsSource().recent_runs("t-recent-paid", limit=5)
        assert runs[0]["total_cost_usd"] == pytest.approx(0.002)
        assert runs[0]["total_tokens"] == 5000


# --------------------------------------------------------------------------- #
# 4. /cost/board + /cost/savings — the three scenarios                         #
# --------------------------------------------------------------------------- #

class TestCostEndpointsScenarios:
    async def test_no_data(self):
        tenant = "t-scenario-empty"
        board = await build_board_payload(tenant)
        savings = await build_savings_payload(tenant)

        assert board["has_data"] is False
        assert board["total_spent"] == 0.0
        assert savings["has_data"] is False
        assert savings["amount"] is None  # not 0
        assert savings["baseline"] is None

    async def test_mock_or_ollama_run(self):
        tenant = "t-scenario-mock"
        _save_run("m-1", tenant, cost=0.0, tokens=4000)

        board = await build_board_payload(tenant)
        savings = await build_savings_payload(tenant)
        metrics = await MemoryMetricsSource().summary(tenant)

        assert metrics["has_cost"] is False
        assert board["has_data"] is False  # free spend is not "data"
        assert board["total_spent"] == 0.0
        assert savings["has_data"] is False
        assert savings["amount"] is None  # 「—」, never a fake number

    async def test_paid_model_run_current_window_only(self):
        tenant = "t-scenario-paid"
        _save_run("p-1", tenant, cost=0.00045, tokens=1500)

        board = await build_board_payload(tenant)
        savings = await build_savings_payload(tenant)
        metrics = await MemoryMetricsSource().summary(tenant)

        assert metrics["has_cost"] is True
        assert metrics["total_cost_usd"] == pytest.approx(0.00045)
        assert board["has_data"] is True
        assert board["total_spent"] == pytest.approx(0.00045)
        # No *previous* window → no baseline → 「—」 (honest, not 0).
        assert savings["has_data"] is False
        assert savings["amount"] is None
        assert savings["actual"] == pytest.approx(0.00045)

    async def test_paid_runs_in_both_windows_flip_savings_to_a_real_value(self):
        tenant = "t-scenario-two-windows"
        # Previous window (45 days ago) cost 0.002; current window cost 0.001.
        _save_run("prev", tenant, cost=0.002, tokens=2000, age_days=45)
        _save_run("cur", tenant, cost=0.001, tokens=1000, age_days=1)

        savings = await build_savings_payload(tenant)

        assert savings["has_data"] is True
        assert savings["baseline"] == pytest.approx(0.002)
        assert savings["actual"] == pytest.approx(0.001)
        assert savings["amount"] == pytest.approx(0.001)  # 0.002 - 0.001

    async def test_free_history_does_not_forge_a_baseline(self):
        # A previous window full of free runs must NOT produce "0 saved".
        tenant = "t-scenario-free-history"
        _save_run("prev-free", tenant, cost=0.0, tokens=3000, age_days=45)
        _save_run("cur-paid", tenant, cost=0.001, tokens=1000, age_days=1)

        savings = await build_savings_payload(tenant)

        assert savings["has_data"] is False
        assert savings["amount"] is None


# --------------------------------------------------------------------------- #
# 5. The degrade chain fires from real ledger spend                            #
# --------------------------------------------------------------------------- #

class TestDegradeChain:
    async def test_real_spend_over_budget_trims_the_context(self):
        tenant = "t-degrade-ledger"
        # A tiny budget the tenant already blew (0.5 spent >> 0.0001 limit).
        await BudgetService().set_budget(tenant, "tenant", 0.0001)
        _save_run("blowout", tenant, cost=0.5, tokens=1000)

        ctx = RequestContext(tenant_id=tenant, user_id="admin", role="admin")
        budget = await _resolve_context_budget(TaskCreate(intent="x"), ctx)

        # exceeded ⇒ on_exceed actions include trim_context ⇒ budget halved.
        assert budget == get_settings().context_budget_tokens // 2

    async def test_no_spend_leaves_the_budget_untouched(self):
        tenant = "t-degrade-free"
        await BudgetService().set_budget(tenant, "tenant", 100.0)
        _save_run("free", tenant, cost=0.0, tokens=1000)

        ctx = RequestContext(tenant_id=tenant, user_id="admin", role="admin")
        budget = await _resolve_context_budget(TaskCreate(intent="x"), ctx)

        assert budget == get_settings().context_budget_tokens


# --------------------------------------------------------------------------- #
# 6. PostgreSQL ledger branch (fake pool — no DB, profile-independent)         #
# --------------------------------------------------------------------------- #

class _FakeConn:
    def __init__(self, row: dict) -> None:
        self._row = row
        self.calls: list[tuple] = []

    async def fetchrow(self, query: str, *args):
        self.calls.append((query, args))
        return self._row


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakePool:
    def __init__(self, row: dict) -> None:
        self.conn = _FakeConn(row)

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self.conn)


class TestPostgresLedgerBranch:
    async def _run(self, monkeypatch, row: dict):
        from forgeflow.cost import ledger as ledger_mod

        monkeypatch.setattr(
            ledger_mod, "get_settings", lambda: SimpleNamespace(storage_backend="postgres")
        )
        pool = _FakePool(row)
        return await ledger_mod.period_totals("t-pg", window_days=30, pool=pool), pool

    async def test_reads_current_and_previous_from_workflow_runs(self, monkeypatch):
        (previous, current), pool = await self._run(
            monkeypatch,
            {"current_cost": 1.5, "previous_cost": 2.0, "previous_billable_runs": 1},
        )
        assert current == pytest.approx(1.5)
        assert previous == pytest.approx(2.0)
        # The query targets workflow_runs over both windows.
        assert "workflow_runs" in pool.conn.calls[0][0]

    async def test_no_billable_previous_window_yields_no_baseline(self, monkeypatch):
        (previous, current), _pool = await self._run(
            monkeypatch,
            {"current_cost": 1.5, "previous_cost": 0.0, "previous_billable_runs": 0},
        )
        assert current == pytest.approx(1.5)
        assert previous is None  # ⇒ savings renders 「—」, never 0
