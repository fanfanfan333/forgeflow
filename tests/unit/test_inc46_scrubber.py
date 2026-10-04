"""INC46 T32 — PII scrubber + tenant-scoped sinking (unit / offline profile).

Covers the task book's probes with **real assertions** (no stdout estimation):

阳性
  1. a text carrying a name / phone / email has all three replaced (before/after
     strings are asserted literally);
  2. tenant A never observes tenant B's experiences (empty result);
  3. the backfill job is idempotent — a second run reports ``changed == 0`` and
     its counts are recomputable;
  4. an **effective** re-scrub: ``scrub_status`` *and* ``scrub_version`` both
     change on a legacy row, and the new-version pattern miner then runs.

阴性
  5. ``scrub_status`` ``NULL`` ⇒ the pattern miner does **not** read the
     experience (measured assertion);
  6. cross-tenant access ⇒ empty / refused;
  7. a structure-breaking scrub (a reference token would be destroyed) ⇒ the
     engine **refuses** (raises) — never a silent pass.

反事实 (真跑)
  8. turn the scrub into a no-op ⇒ the PII assertion goes red;
  9. drop the tenant condition ⇒ the cross-tenant assertion goes red.
"""

from __future__ import annotations

import pytest

from forgeflow.experience.models import ExperienceRecord
from forgeflow.jobs.backfill_scrub import backfill_scrub
from forgeflow.privacy.audit import InMemoryScrubAuditStore
from forgeflow.privacy.scrubber import (
    CATEGORY_NAME,
    CATEGORY_PHONE,
    SCRUB_STATUS_SCRUBBED,
    SCRUB_VERSION,
    ScrubAvailabilityError,
    Scrubber,
    ScrubResult,
    ScrubRule,
    check_availability,
    filter_miner_eligible,
    mine_eligible_experiences,
    miner_eligible,
    reference_tokens,
    scrub_experience_record,
)
from forgeflow.repositories.memory import experience_repo as _er
from forgeflow.repositories.memory.experience_repo import (
    MemoryExperienceRepository,
    clear_memory_store,
)

pytestmark = pytest.mark.asyncio

_TENANT_A = "t-t32-a"
_TENANT_B = "t-t32-b"

_PII_SAMPLE = "客户姓名：张伟，电话：13800138000，邮箱：zhangwei@example.com"
_NAME = "张伟"
_PHONE = "13800138000"
_EMAIL = "zhangwei@example.com"


@pytest.fixture(autouse=True)
def _clean():
    clear_memory_store()
    yield
    clear_memory_store()


def _inject_raw(repo: MemoryExperienceRepository, record: ExperienceRecord) -> None:
    """Persist ``record`` bypassing the scrub-on-save path (a *legacy* row).

    Used to synthesise a row that predates T32 — ``scrub_status`` stays ``NULL``.
    """
    key = repo.scope_key(record.tenant_id)
    _er._STORE.setdefault(key, {})[record.id] = record  # noqa: SLF001 — test seam


def _seed(repo: MemoryExperienceRepository, tenant: str, n: int) -> list[ExperienceRecord]:
    records: list[ExperienceRecord] = []
    for i in range(n):
        record = ExperienceRecord(
            tenant_id=tenant,
            run_id=f"r-{i}",
            outcome="success",
            summary=f"任务 {i}",
            reusable_steps=[{"tool": "a"}, {"tool": "b"}],
        )
        _inject_raw(repo, record)
        records.append(record)
    return records


# --------------------------------------------------------------------------- #
# 阳性 1 — the three categories are all replaced                              #
# --------------------------------------------------------------------------- #
async def test_name_phone_email_are_all_redacted():
    scrubber = Scrubber()
    result = scrubber.scrub(_PII_SAMPLE, tenant_id=_TENANT_A)

    # before / after, literally
    assert _PII_SAMPLE == "客户姓名：张伟，电话：13800138000，邮箱：zhangwei@example.com"
    assert result.text == (
        "客户姓名：[REDACTED:name]，电话：[REDACTED:phone]，邮箱：[REDACTED:email]"
    )
    assert _NAME not in result.text
    assert _PHONE not in result.text
    assert _EMAIL not in result.text
    assert set(result.categories) >= {CATEGORY_NAME, CATEGORY_PHONE, "email"}


async def test_categories_are_configurable():
    """Disabling ``name`` leaves names intact (categories are configurable)."""
    scrubber = Scrubber(enabled={CATEGORY_PHONE})
    result = scrubber.scrub(_PII_SAMPLE, tenant_id=_TENANT_A)
    assert _NAME in result.text  # not an enabled category ⇒ untouched
    assert _PHONE not in result.text


async def test_hash_strategy_is_per_tenant_and_deterministic():
    """HMAC strategy: same value ⇒ same token within a tenant, different across."""
    scrubber = Scrubber(strategies={CATEGORY_PHONE: "hash"})
    a1 = scrubber.scrub(f"电话 {_PHONE}", tenant_id=_TENANT_A).text
    a2 = scrubber.scrub(f"电话 {_PHONE}", tenant_id=_TENANT_A).text
    b1 = scrubber.scrub(f"电话 {_PHONE}", tenant_id=_TENANT_B).text
    assert a1 == a2                       # deterministic within a tenant
    assert a1 != b1                       # unlinkable across tenants (租户盐)
    assert "[HASH:phone:" in a1 and _PHONE not in a1


async def test_drop_strategy_removes_the_value():
    scrubber = Scrubber(strategies={"email": "drop"})
    result = scrubber.scrub(f"邮箱 {_EMAIL} 结束", tenant_id=_TENANT_A).text
    assert _EMAIL not in result
    assert result == "邮箱  结束"


# --------------------------------------------------------------------------- #
# 可用性 — structure / numbering / references survive (or the scrub refuses)  #
# --------------------------------------------------------------------------- #
async def test_structure_and_references_survive():
    doc = "参考文献[12] 与 [3]，见 (5)。\n1. 第一步\n2. 第二步\n电话 13800138000"
    scrubber = Scrubber()
    result = scrubber.scrub_document(doc, tenant_id=_TENANT_A)

    assert reference_tokens(result.text) == reference_tokens(doc)
    assert check_availability(doc, result.text) == []
    assert _PHONE not in result.text          # PII still masked
    assert "[12]" in result.text and "1. 第一步" in result.text


# --------------------------------------------------------------------------- #
# 阴性 7 — a structure-breaking scrub REFUSES (never a silent pass)           #
# --------------------------------------------------------------------------- #
async def test_structure_break_is_refused_not_silently_passed():
    # An over-broad rule that would eat the '[12]' reference token.
    broken = Scrubber(
        rules=[ScrubRule(CATEGORY_PHONE, r"\[\s*\d+\s*\]", 0, "over-broad")],
        enabled={CATEGORY_PHONE},
    )
    with pytest.raises(ScrubAvailabilityError) as excinfo:
        broken.scrub_document("参考文献[12]，电话 13800138000")
    assert excinfo.value.missing == ["[12]"]
    assert "structure" in str(excinfo.value)


async def test_scrub_experience_record_refuses_and_does_not_stamp():
    """A refusing record is left untouched and NOT stamped as scrubbed."""
    record = ExperienceRecord(
        tenant_id=_TENANT_A,
        run_id="r-refuse",
        summary="参考文献[12]",
    )
    broken = Scrubber(
        rules=[ScrubRule(CATEGORY_PHONE, r"\[\s*\d+\s*\]", 0, "over-broad")],
        enabled={CATEGORY_PHONE},
    )
    outcome = scrub_experience_record(record, tenant_id=_TENANT_A, scrubber=broken)
    assert outcome.refused
    assert not outcome.ok
    assert record.scrub_status is None            # nothing stamped
    assert record.summary == "参考文献[12]"        # untouched


# --------------------------------------------------------------------------- #
# 阳性 2 / 阴性 6 — tenant isolation (empty / cross-tenant)                    #
# --------------------------------------------------------------------------- #
async def test_tenant_a_never_observes_tenant_b():
    repo = MemoryExperienceRepository()
    await repo.save(
        ExperienceRecord(
            tenant_id=_TENANT_A,
            run_id="r-a",
            outcome="success",
            summary="A",
            reusable_steps=[{"tool": "a"}, {"tool": "b"}],
        )
    )
    await repo.save(
        ExperienceRecord(
            tenant_id=_TENANT_B,
            run_id="r-b",
            outcome="success",
            summary="B",
            reusable_steps=[{"tool": "a"}, {"tool": "b"}],
        )
    )

    assert await repo.list(_TENANT_B) != []
    pats_a = await mine_eligible_experiences(_TENANT_A, repo=repo, min_support=1)
    pats_b = await mine_eligible_experiences(_TENANT_B, repo=repo, min_support=1)
    # A reads only A (support 1), B reads only B — never each other.
    assert pats_a and pats_a[0].metrics.support == 1
    assert not set(pats_a[0].source_run_ids) & {"r-b"}

    # A tenant that owns nothing reads the empty set (fail-closed).
    assert await mine_eligible_experiences("t-t32-vacant", repo=repo, min_support=1) == []
    # An unresolved tenant reads nothing at all.
    assert await mine_eligible_experiences(None, repo=repo, min_support=1) == []


# --------------------------------------------------------------------------- #
# 阴性 5 — a NULL scrub_status experience is invisible to the miner            #
# --------------------------------------------------------------------------- #
async def test_null_scrub_status_is_not_read_by_the_miner():
    repo = MemoryExperienceRepository()
    tenant = "t-t32-null"
    # 2 scrubbed (via the write path) + 1 legacy raw row that never went through it.
    for i in range(2):
        await repo.save(
            ExperienceRecord(
                tenant_id=tenant,
                run_id=f"scrubbed-{i}",
                outcome="success",
                summary="ok",
                reusable_steps=[{"tool": "a"}, {"tool": "b"}],
            )
        )
    legacy = ExperienceRecord(
        tenant_id=tenant,
        run_id="legacy-null",
        outcome="success",
        summary="never scrubbed",
        reusable_steps=[{"tool": "a"}, {"tool": "b"}],
    )
    _inject_raw(repo, legacy)
    assert legacy.scrub_status is None and not miner_eligible(legacy)

    all_rows = await repo.list(tenant, limit=100)
    assert len(all_rows) == 3                      # 3 rows exist …
    eligible = filter_miner_eligible(all_rows)
    assert len(eligible) == 2                      # … but only 2 are eligible

    pats = await mine_eligible_experiences(tenant, repo=repo, min_support=1)
    assert len(pats) == 1
    assert pats[0].metrics.support == 2            # the NULL row was NOT read
    assert "legacy-null" not in pats[0].source_run_ids


# --------------------------------------------------------------------------- #
# 阳性 3+4 — the backfill is idempotent … and effective when it should be      #
# --------------------------------------------------------------------------- #
async def test_backfill_is_idempotent_and_recomputable():
    repo = MemoryExperienceRepository()
    audit = InMemoryScrubAuditStore()
    tenant = "t-t32-backfill"
    _seed(repo, tenant, 3)

    first = await backfill_scrub(tenant, repo=repo, audit_store=audit, version=SCRUB_VERSION)
    assert first.scanned == 3
    assert first.changed == 3          # legacy NULL rows become scrubbed/v1
    assert first.unchanged == 0
    assert first.refused == 0
    assert first.residual == 0         # 无残留 PII
    # recomputable: every scanned row is accounted for exactly once
    assert first.scanned == first.changed + first.unchanged + first.refused
    assert first.audits == first.scanned

    second = await backfill_scrub(tenant, repo=repo, audit_store=audit, version=SCRUB_VERSION)
    assert second.changed == 0         # idempotent — nothing changed the 2nd time
    assert second.unchanged == 3
    assert second.scanned == second.changed + second.unchanged + second.refused


async def test_effective_rescrub_changes_status_and_version_and_miner_runs():
    repo = MemoryExperienceRepository()
    audit = InMemoryScrubAuditStore()
    tenant = "t-t32-rescrub"
    records = _seed(repo, tenant, 3)
    ids = [r.id for r in records]

    # legacy: scrub_status NULL, scrub_version NULL
    for record_id in ids:
        stored = await repo.get(tenant, record_id)
        assert stored.scrub_status is None and stored.scrub_version is None

    first = await backfill_scrub(tenant, repo=repo, audit_store=audit, version="1")
    assert first.changed == 3
    for record_id in ids:
        stored = await repo.get(tenant, record_id)
        assert stored.scrub_status == SCRUB_STATUS_SCRUBBED      # NULL → 'scrubbed'
        assert stored.scrub_version == "1"                       # NULL → '1'

    # a *new scrubber version* is an effective re-scrub (version transitions) …
    bumped = await backfill_scrub(tenant, repo=repo, audit_store=audit, version="2")
    assert bumped.changed == 3
    for record_id in ids:
        stored = await repo.get(tenant, record_id)
        assert stored.scrub_version == "2"

    # … and the new-version pattern miner then runs over the re-scrubbed rows.
    patterns = await mine_eligible_experiences(tenant, repo=repo, min_support=1)
    assert patterns and patterns[0].metrics.support == 3
    assert patterns[0].key == "a→b"

    # audit trail: tenant-scoped + minimal (no raw PII ever stored).
    audits = audit.list(tenant)
    assert len(audits) == 6            # 3 rows × 2 backfill passes
    assert audit.list(_TENANT_B) == []  # cross-tenant ⇒ empty
    for row in audits:
        assert row.tenant_id == tenant
        assert "张伟" not in str(row.to_dict())


async def test_backfill_without_tenant_is_fail_closed():
    repo = MemoryExperienceRepository()
    _seed(repo, _TENANT_A, 2)
    report = await backfill_scrub(None, repo=repo, audit_store=InMemoryScrubAuditStore())
    assert report.scanned == 0 and report.changed == 0


# --------------------------------------------------------------------------- #
# 反事实 (真跑)                                                                #
# --------------------------------------------------------------------------- #
async def test_counterfactual_noop_scrub_turns_pii_probe_red(monkeypatch):
    """摘掉脱敏（改为 no-op）⇒ 阳性 PII 断言必须转红。"""

    def _noop(self, text, *, tenant_id=None):  # noqa: ANN001, ANN202
        return ScrubResult(original=text, text=text)

    monkeypatch.setattr(Scrubber, "scrub", _noop)
    scrubber = Scrubber()
    scrubbed = scrubber.scrub(_PII_SAMPLE, tenant_id=_TENANT_A)
    # The real probe (``assert _PHONE not in scrubbed.text``) now FAILS:
    with pytest.raises(AssertionError):
        assert _PHONE not in scrubbed.text
    with pytest.raises(AssertionError):
        assert _EMAIL not in scrubbed.text


async def test_counterfactual_dropping_tenant_condition_turns_cross_tenant_red(monkeypatch):
    """去掉租户条件 ⇒ 跨租户「空结果」断言必须转红。"""
    repo = MemoryExperienceRepository()
    await repo.save(
        ExperienceRecord(
            tenant_id=_TENANT_A,
            run_id="r-a",
            outcome="success",
            summary="A",
            reusable_steps=[{"tool": "a"}, {"tool": "b"}],
        )
    )
    # real behaviour: tenant B reads nothing.
    assert await repo.list(_TENANT_B) == []

    async def _ignoring_tenant(self, tenant_id, **kwargs):  # noqa: ANN001, ANN003
        return [
            _er._clone(r)  # noqa: SLF001 — test seam
            for r in _er._STORE.get(_TENANT_A, {}).values()  # noqa: SLF001
        ]

    monkeypatch.setattr(MemoryExperienceRepository, "list", _ignoring_tenant)
    # The cross-tenant probe now FAILS:
    with pytest.raises(AssertionError):
        assert await repo.list(_TENANT_B) == []
