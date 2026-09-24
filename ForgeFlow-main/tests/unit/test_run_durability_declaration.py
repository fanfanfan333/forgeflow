"""INC9 B4 — hub-run durability declaration (drift-proofing nails).

``RunRecord`` (the hub run) is **process-lifetime**: ``get_run_store()`` ignores
``STORAGE_BACKEND`` and always returns the in-process ``MemoryRunStore``. Before
INC9 a comment in ``runtime/orchestrator.py`` claimed the loop-breaker trail was
"provable after the fact", which the mechanism did not deliver. These tests pin
the three seams so the promise and the reality cannot drift apart again
(docs/sop/12-INC9-DESIGN.md §2.4.4):

1. **mechanism** — the run store is in-process regardless of ``STORAGE_BACKEND``;
2. **honesty** — a reset loses runs (``get`` → ``None``), and the eval source is
   labelled ``source="hub_runs"`` / ``durable=False`` with genuinely-empty
   metrics reported as ``value=None`` / ``has_data=False`` (never a fake ``0``);
3. **comment ↔ mechanism alignment** — the source no longer contains the
   over-promise, and does contain the honest phrases. Fix the source of truth,
   never weaken the test.
"""

from __future__ import annotations

import pathlib

from forgeflow.evaluation.eval_source import get_agent_eval_source, reset_agent_eval_source
from forgeflow.runtime.orchestrator import (
    MemoryRunStore,
    RunRecord,
    get_run_store,
    reset_run_store,
)

_REPO = pathlib.Path(__file__).resolve().parents[2]
_ORCHESTRATOR = _REPO / "forgeflow" / "runtime" / "orchestrator.py"
_LOOP_BREAKER = _REPO / "forgeflow" / "validation" / "loop_breaker.py"
_FORGEFLOW_ROOT = _REPO / "forgeflow"

#: The exact over-promise that was removed in INC9 B4.
_OVER_PROMISE = "provable after the fact"
#: The honest replacements that MUST be present in every declaration-bearing file.
_HONEST_PHRASES = ("process-lifetime", "not persisted")

#: Files whose ``forgeflow/**/*.py`` path is allowed to contain the over-promise
#: string. Deliberately EMPTY: no source module may repeat the claim. (The
#: over-promise is *named* only in this test and in docs/sop/12-INC9-DESIGN.md,
#: neither of which lives under ``forgeflow/``.)
_OVER_PROMISE_ALLOWLIST: frozenset[str] = frozenset()


def _forgeflow_modules() -> list[pathlib.Path]:
    """Every Python module under ``forgeflow/`` — recursive (INC10 A)."""
    return sorted(p for p in _FORGEFLOW_ROOT.rglob("*.py") if p.is_file())


def _record(run_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        thread_id="t-1",
        tenant_id="default",
        agent_id=None,
        intent="test",
        status="failed",
        outcome="failure",
        steps=[],
        errors=[],
        created_at="2026-01-01T00:00:00+00:00",
    )


# --------------------------------------------------------------------------- #
# 1. mechanism — in-process regardless of backend                               #
# --------------------------------------------------------------------------- #

def test_run_store_is_in_process_regardless_of_storage_backend(monkeypatch):
    from forgeflow.config import get_settings

    # Even under the postgres profile, the run store never becomes durable.
    monkeypatch.setattr(get_settings(), "storage_backend", "postgres")
    assert isinstance(get_run_store(), MemoryRunStore)


def test_reset_run_store_drops_the_run(monkeypatch):
    from forgeflow.config import get_settings

    monkeypatch.setattr(get_settings(), "storage_backend", "postgres")
    reset_run_store()
    get_run_store().save(_record("loop-risk-1"))
    assert get_run_store().get("loop-risk-1") is not None

    reset_run_store()  # simulates a process restart
    assert get_run_store().get("loop-risk-1") is None


# --------------------------------------------------------------------------- #
# 2. honesty — memory eval source is durable=False, empty ⇒ has_data=False      #
# --------------------------------------------------------------------------- #

async def test_agent_eval_source_is_honest_when_hub_runs_are_empty(force_memory_backend):
    reset_agent_eval_source()
    reset_run_store()
    snapshot = await get_agent_eval_source().summary("empty-tenant-xyz")

    assert snapshot["source"] == "hub_runs"
    assert snapshot["durable"] is False
    assert snapshot["sample_runs"] == 0
    # A metric with no data is None / has_data=False — NOT a fabricated 0.
    success = snapshot["quality"]["metrics"]["task_success_rate"]
    assert success["value"] is None
    assert success["has_data"] is False


# --------------------------------------------------------------------------- #
# 3. comment ↔ mechanism alignment (drift-proof)                                #
# --------------------------------------------------------------------------- #

def test_orchestrator_comment_matches_the_mechanism():
    source = _ORCHESTRATOR.read_text(encoding="utf-8")
    assert _OVER_PROMISE not in source, (
        "orchestrator.py must not claim the loop trail is 'provable after the "
        "fact' — hub runs are in-process only (INC9 B4)"
    )
    for phrase in _HONEST_PHRASES:
        assert phrase in source, f"orchestrator.py must state that hub runs are {phrase!r}"


def test_loop_breaker_comment_matches_the_mechanism():
    """INC10 A — the *second* file that carried the same over-promise.

    INC9 B4 only cleaned ``orchestrator.py``; ``validation/loop_breaker.py``
    kept the identical "surfaced on the run ... provable after the fact" claim,
    so the drift survived while the single-file nail stayed green. Pin it here.
    """
    source = _LOOP_BREAKER.read_text(encoding="utf-8")
    assert _OVER_PROMISE not in source, (
        "loop_breaker.py must not claim the loop trail is 'provable after the "
        "fact' — hub runs are in-process only (INC9 B4)"
    )
    for phrase in _HONEST_PHRASES:
        assert phrase in source, f"loop_breaker.py must state that hub runs are {phrase!r}"


def test_no_forgeflow_module_repeats_the_over_promise():
    """INC10 A — the nail widened from one file to the whole package.

    INC9's nail read a single path (``orchestrator.py``); a second copy of the
    same over-promise therefore revived in ``validation/loop_breaker.py`` with
    the test still green. This scans **every** ``forgeflow/**/*.py``, so a copy
    written into ANY module (not just the two we know about) turns it red.
    """
    offenders = sorted(
        p.relative_to(_REPO).as_posix()
        for p in _forgeflow_modules()
        if _OVER_PROMISE in p.read_text(encoding="utf-8")
        and p.relative_to(_REPO).as_posix() not in _OVER_PROMISE_ALLOWLIST
    )
    assert offenders == [], (
        "these forgeflow modules claim the loop trail is 'provable after the "
        f"fact' — hub runs are in-process only (INC9 B4 / INC10 A): {offenders}"
    )


def test_loop_breaker_breadcrumb_is_wired_to_the_audit_sink():
    """The O1 breadcrumb must be a real call site, not just a docstring claim."""
    source = _ORCHESTRATOR.read_text(encoding="utf-8")
    assert 'action": "run.loop.breaker"' in source
    assert "_persist_loop_breaker_breadcrumb(" in source
    assert "write_audit_entry" in source


async def test_loop_breaker_breadcrumb_writes_a_durable_audit_entry(force_memory_backend):
    """The breadcrumb really lands on the audit sink (offline ring buffer)."""
    from forgeflow.api.routers.audit import _RING, clear_audit_ring
    from forgeflow.runtime.orchestrator import (
        RequestContext,
        _persist_loop_breaker_breadcrumb,
    )
    from forgeflow.validation.loop_breaker import BudgetBreach, LoopBreaker

    clear_audit_ring()
    ctx = RequestContext(tenant_id="t-breadcrumb", user_id="user-1", role="viewer")
    breach = BudgetBreach(dimension="tokens", used=100.0, limit=0.0, message="stop")
    breaker = LoopBreaker()

    await _persist_loop_breaker_breadcrumb(ctx, "run-breadcrumb-1", breach, breaker)

    rows = [r for r in _RING if r.get("action") == "run.loop.breaker"]
    assert len(rows) == 1
    row = rows[0]
    assert row["resource"] == "runs"
    assert row["resource_id"] == "run-breadcrumb-1"
    assert row["metadata"]["dimension"] == "tokens"
    assert row["metadata"]["breach"]["dimension"] == "tokens"
    assert set(row["metadata"]["ceilings"]) == {"max_tokens", "max_seconds"}
    clear_audit_ring()

