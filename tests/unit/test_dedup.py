"""INC2-16 — experience de-duplication & conflict resolution (docs §2.11).

Pins the algorithm's three guarantees and the hard INC2 constraint that the LLM
is invoked **at most once per batch**:
  * s >= dedup_merge_threshold        → merge, no new row, ``merged_from`` grows
  * 0.60 <= s < threshold + conflict  → ``conflict_with`` cross-link, both rows
  * otherwise                         → a normal new row

Embeddings are crafted by hand (length-3 vectors) so the cosine similarity is
exact and the outcomes are deterministic — no reliance on the hash embedder.
"""

from __future__ import annotations

import pytest

from forgeflow.experience.dedup import (
    ExperienceDeduplicator,
    clear_arbitration_cache,
    dedup_key,
)
from forgeflow.experience.extractor import ExperienceExtractor
from forgeflow.experience.models import ExperienceRecord
from forgeflow.repositories.memory.experience_repo import (
    MemoryExperienceRepository,
    clear_memory_store,
)

pytestmark = pytest.mark.asyncio

TENANT = "t-dedup"


class _SpyLLM:
    """Counts ``ainvoke`` calls and returns an empty JSON verdict map."""

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, prompt: str):
        self.calls += 1

        class _Response:
            content = "{}"

        return _Response()


@pytest.fixture(autouse=True)
def _clean():
    clear_memory_store()
    clear_arbitration_cache()
    yield
    clear_memory_store()
    clear_arbitration_cache()


def _rec(outcome: str, embedding: list[float], summary: str = "任务") -> ExperienceRecord:
    return ExperienceRecord(
        tenant_id=TENANT,
        run_id="r",
        summary=summary,
        outcome=outcome,
        tags=["数据分析"],
        embedding=embedding,
    )


# --------------------------------------------------------------------------- #
# merge path                                                                   #
# --------------------------------------------------------------------------- #

async def test_merge_does_not_add_row_and_accumulates_merged_from():
    repo = MemoryExperienceRepository()
    base = _rec("success", [1.0, 0.0, 0.0], summary="同一任务")
    await repo.save(base)
    assert await repo.count(TENANT) == 1

    dedup = ExperienceDeduplicator(repo=repo, llm=_SpyLLM())

    dup1 = _rec("success", [1.0, 0.0, 0.0], summary="同一任务")
    out1 = await dedup.resolve(dup1, tenant_id=TENANT)
    assert out1.action == "merged"
    assert out1.record.id == base.id          # survivor keeps the original id
    assert await repo.count(TENANT) == 1      # NO new row
    assert out1.record.merged_from == [dup1.id]

    dup2 = _rec("success", [1.0, 0.0, 0.0], summary="同一任务")
    out2 = await dedup.resolve(dup2, tenant_id=TENANT)
    assert out2.action == "merged"
    assert await repo.count(TENANT) == 1
    assert out2.record.merged_from == [dup1.id, dup2.id]   # accumulates across merges
    assert out2.record.dedup_key == dedup_key("同一任务")

    # Round-trip via a *fresh* read (not the returned object) proves persistence.
    stored = await repo.get(TENANT, base.id)
    assert stored is not None
    assert stored.merged_from == [dup1.id, dup2.id]


async def test_unrelated_record_is_created_normally():
    repo = MemoryExperienceRepository()
    base = _rec("success", [1.0, 0.0, 0.0])
    await repo.save(base)

    dedup = ExperienceDeduplicator(repo=repo, llm=_SpyLLM())
    fresh = _rec("success", [0.0, 1.0, 0.0])   # cosine 0 → unrelated
    out = await dedup.resolve(fresh, tenant_id=TENANT)

    assert out.action == "created"
    assert await repo.count(TENANT) == 2


# --------------------------------------------------------------------------- #
# conflict path                                                                #
# --------------------------------------------------------------------------- #

async def test_conflict_band_cross_links_and_keeps_both_rows():
    repo = MemoryExperienceRepository()
    existing = _rec("success", [1.0, 0.0, 0.0], summary="部署策略 A")
    await repo.save(existing)

    spy = _SpyLLM()
    dedup = ExperienceDeduplicator(repo=repo, llm=spy)

    # cosine([0.8, 0.6, 0], [1, 0, 0]) == 0.8 → inside the conflict band, and the
    # outcome differs (failure vs success) → conflict, not merge.
    conflicting = _rec("failure", [0.8, 0.6, 0.0], summary="部署策略 B")
    out = await dedup.resolve(conflicting, tenant_id=TENANT)

    assert out.action == "conflict"
    assert await repo.count(TENANT) == 2          # both rows kept
    # Read both back as fresh instances — the cross-link must be persisted.
    stored_existing = await repo.get(TENANT, existing.id)
    stored_conflicting = await repo.get(TENANT, conflicting.id)
    assert stored_conflicting.conflict_with == [existing.id]
    assert stored_existing.conflict_with == [conflicting.id]
    assert spy.calls == 1                          # exactly one arbitration call


async def test_same_outcome_in_band_is_not_a_conflict():
    repo = MemoryExperienceRepository()
    existing = _rec("success", [1.0, 0.0, 0.0])
    await repo.save(existing)

    spy = _SpyLLM()
    dedup = ExperienceDeduplicator(repo=repo, llm=spy)
    similar_same_outcome = _rec("success", [0.8, 0.6, 0.0])
    out = await dedup.resolve(similar_same_outcome, tenant_id=TENANT)

    assert out.action == "created"                 # no outcome clash → new row
    assert spy.calls == 0


# --------------------------------------------------------------------------- #
# LLM call budget (hard constraint)                                            #
# --------------------------------------------------------------------------- #

async def test_llm_called_at_most_once_for_a_whole_batch():
    repo = MemoryExperienceRepository()
    await repo.save(_rec("success", [1.0, 0.0, 0.0], summary="A"))
    await repo.save(_rec("success", [0.0, 1.0, 0.0], summary="B"))

    spy = _SpyLLM()
    dedup = ExperienceDeduplicator(repo=repo, llm=spy)

    # Both incoming records conflict with one of the two stored rows.
    c1 = _rec("failure", [0.8, 0.6, 0.0], summary="A'")          # vs A → 0.8
    c2 = _rec("failure", [0.0, 0.7, 0.7141], summary="B'")       # vs B → 0.7
    outcomes = await dedup.resolve_batch([c1, c2], tenant_id=TENANT)

    assert [o.action for o in outcomes] == ["conflict", "conflict"]
    assert spy.calls == 1                          # ONE call for BOTH pairs


async def test_arbitration_result_is_cached_by_pair_hash():
    repo = MemoryExperienceRepository()
    existing = _rec("success", [1.0, 0.0, 0.0], summary="A")
    await repo.save(existing)

    spy = _SpyLLM()
    dedup = ExperienceDeduplicator(repo=repo, llm=spy)

    incoming = _rec("failure", [0.8, 0.6, 0.0], summary="A'")
    await dedup.resolve(incoming, tenant_id=TENANT)
    assert spy.calls == 1

    # Re-resolving the same pair must hit the cache — no second LLM call.
    await dedup.resolve(incoming, tenant_id=TENANT)
    assert spy.calls == 1


async def test_no_conflict_means_no_llm_call():
    repo = MemoryExperienceRepository()
    spy = _SpyLLM()
    dedup = ExperienceDeduplicator(repo=repo, llm=spy)

    await dedup.resolve(_rec("success", [1.0, 0.0, 0.0]), tenant_id=TENANT)
    await dedup.resolve(_rec("success", [0.0, 1.0, 0.0]), tenant_id=TENANT)

    assert spy.calls == 0


# --------------------------------------------------------------------------- #
# extractor integration                                                        #
# --------------------------------------------------------------------------- #

async def test_extractor_merges_duplicate_runs_without_adding_rows():
    repo = MemoryExperienceRepository()
    # A shared tenant keeps both runs in the same partition so de-dup applies.
    run = {
        "run_id": "r-dup",
        "tenant_id": TENANT,
        "intent": "分析华东地区销售数据并生成报告",
        "status": "completed",
        "tags": ["数据分析"],
        "steps": [{"tool": "data.query"}],
    }

    first = await ExperienceExtractor(repo=repo).extract(run, None, tenant_id=TENANT)
    second = await ExperienceExtractor(repo=repo).extract(
        {**run, "run_id": "r-dup-2"}, None, tenant_id=TENANT
    )

    assert await repo.count(TENANT) == 1           # identical runs collapse to one
    assert second.id == first.id                   # the original row survives
    # The persisted row (fresh read) records exactly one merged-away lineage id.
    stored = await repo.get(TENANT, first.id)
    assert stored is not None
    assert len(stored.merged_from) == 1
    assert stored.merged_from[0] != first.id
