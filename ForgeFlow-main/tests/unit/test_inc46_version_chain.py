"""INC46 T25 —— 多轮迭代与版本链（TABLE 31）。主理人自跑（成员 429 配额窗口内）。

覆盖任务书 DoD：
  * 阳性：v1 → v2（approved）→「再正式一点」基于 v2 生成 v3；revert 到 v1 ⇒ v4 内容 == v1，
    且 v1/v2/v3 仍可读（红线 6：不删除、不改写历史）。
  * 阴性：基于 rejected / pending 版本继续改 ⇒ 拒绝并提示；并发基于旧 head ⇒ 409；
    跨租户 ⇒ 403；原件（v0）字节不变。
  * 乐观锁、edge 语义（refine / revert 的 parent 与 source）、T16 feedback（revise / revert）、
    追问指令解析。

反事实在 ``_t33``/``_t25`` 脚本里真跑：摘下 parent 链接 ⇒ refine 基线用例转红；
把 revert 实现为覆盖 ⇒ 「历史仍可读」用例转红。
"""

from __future__ import annotations

import io

import pytest

from forgeflow.documents import followup as fu
from forgeflow.documents import review_store as rs
from forgeflow.documents import version_chain as vc
from forgeflow.outcomes.store import InMemoryOutcomeStore, reset_outcome_store, set_outcome_store

TENANT_A = "tenant-vc-a"
TENANT_B = "tenant-vc-b"
ART = "art-vc-1"


# --------------------------------------------------------------------------- #
# fixtures / helpers                                                           #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _memory_outcome_store():
    """Pin T16's feedback stream to the in-memory store (hermetic unit run)."""
    set_outcome_store(InMemoryOutcomeStore())
    yield
    reset_outcome_store()


def _docx(paragraphs: list[str]) -> bytes:
    """Real .docx bytes (the diff engine must run on a real package)."""
    import docx  # noqa: PLC0415 — test-only dependency

    doc = docx.Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _stores() -> tuple[rs.InMemoryArtifactReviewStore, vc.InMemoryVersionChainStore]:
    return rs.InMemoryArtifactReviewStore(), vc.InMemoryVersionChainStore()


def _seed_committed(
    store: rs.ArtifactReviewStore,
    content: bytes,
    *,
    tenant: str = TENANT_A,
    artifact_id: str = ART,
    fmt: str = "docx",
    run_id: str | None = None,
) -> rs.ArtifactVersion:
    """Mint a committed version through the real T22 state machine."""
    version, review = store.create_pending_version(
        tenant, artifact_id=artifact_id, content=content, fmt=fmt, run_id=run_id
    )
    committed, _ = store.approve(tenant, artifact_id, version.version, actor="human-1")
    return committed


def _seed_pending(
    store: rs.ArtifactReviewStore,
    content: bytes,
    *,
    tenant: str = TENANT_A,
    artifact_id: str = ART,
    fmt: str = "docx",
    run_id: str | None = None,
) -> rs.ArtifactVersion:
    version, _ = store.create_pending_version(
        tenant, artifact_id=artifact_id, content=content, fmt=fmt, run_id=run_id
    )
    return version


# --------------------------------------------------------------------------- #
# 1. 阳性：refine 基于最新 committed 版本                                        #
# --------------------------------------------------------------------------- #
def test_refine_builds_on_the_committed_head():
    review, chain = _stores()
    v1 = _seed_committed(review, _docx(["第一段", "第二段"]))

    v2, _ = vc.refine(
        TENANT_A, artifact_id=ART, content=_docx(["第一段", "第二段（更正式）"]),
        base_version=v1.version, review_store=review, chain_store=chain,
    )

    assert v2.version == 2
    assert v2.base_version == v1.version  # 基线正确 —— refine 的关键不变量
    assert v2.state == rs.PENDING  # 仍须人工 approve（红线 11）
    edge = chain.get_edge(TENANT_A, ART, 2)
    assert edge is not None
    assert edge.edge_kind == vc.EDGE_REFINE
    assert edge.parent_version == 1
    assert edge.source_version is None  # 非 revert ⇒ 未测量（红线 4）


def test_refine_then_approve_advances_the_head():
    review, chain = _stores()
    v1 = _seed_committed(review, _docx(["a"]))
    v2, _ = vc.refine(
        TENANT_A, artifact_id=ART, content=_docx(["a2"]),
        base_version=v1.version, review_store=review, chain_store=chain,
    )
    review.approve(TENANT_A, ART, v2.version, actor="h")
    head = vc.resolve_head(review, TENANT_A, ART)
    assert head is not None and head.version == 2

    v3, _ = vc.refine(
        TENANT_A, artifact_id=ART, content=_docx(["a3"]),
        base_version=2, review_store=review, chain_store=chain,
    )
    assert v3.version == 3 and v3.base_version == 2


def test_refine_root_when_the_chain_is_empty():
    review, chain = _stores()
    v1, _ = vc.refine(
        TENANT_A, artifact_id=ART, content=_docx(["root"]),
        base_version=None, review_store=review, chain_store=chain,
    )
    assert v1.version == 1
    edge = chain.get_edge(TENANT_A, ART, 1)
    assert edge is not None and edge.edge_kind == vc.EDGE_INITIAL
    assert edge.parent_version is None


# --------------------------------------------------------------------------- #
# 2. 阴性：基线必须是「已批准的 head」                                            #
# --------------------------------------------------------------------------- #
def test_refine_from_a_stale_base_conflicts():
    review, chain = _stores()
    v1 = _seed_committed(review, _docx(["v1"]))
    v2, _ = vc.refine(
        TENANT_A, artifact_id=ART, content=_docx(["v2"]),
        base_version=v1.version, review_store=review, chain_store=chain,
    )
    review.approve(TENANT_A, ART, v2.version, actor="h")

    with pytest.raises(vc.VersionConflict) as exc:
        vc.refine(
            TENANT_A, artifact_id=ART, content=_docx(["stale"]),
            base_version=1, review_store=review, chain_store=chain,
        )
    assert exc.value.head_version == 2


def test_refine_from_a_pending_base_is_refused():
    review, chain = _stores()
    _seed_committed(review, _docx(["v1"]))
    pending = _seed_pending(review, _docx(["v2-pending"]))

    with pytest.raises(vc.NotCommittedBase) as exc:
        vc.refine(
            TENANT_A, artifact_id=ART, content=_docx(["x"]),
            base_version=pending.version, review_store=review, chain_store=chain,
        )
    assert exc.value.state == rs.PENDING


def test_refine_from_a_rejected_base_is_refused():
    review, chain = _stores()
    _seed_committed(review, _docx(["v1"]))
    pending = _seed_pending(review, _docx(["v2"]))
    review.reject(TENANT_A, ART, pending.version, actor="h", reason="no")

    with pytest.raises(vc.NotCommittedBase) as exc:
        vc.refine(
            TENANT_A, artifact_id=ART, content=_docx(["x"]),
            base_version=pending.version, review_store=review, chain_store=chain,
        )
    assert exc.value.state == rs.REJECTED


def test_refine_without_any_committed_base_is_refused():
    review, chain = _stores()
    _seed_pending(review, _docx(["only-pending"]))

    with pytest.raises(vc.NoCommittedBase):
        vc.refine(
            TENANT_A, artifact_id=ART, content=_docx(["x"]),
            base_version=None, review_store=review, chain_store=chain,
        )


def test_refine_naming_no_base_while_a_head_exists_conflicts():
    review, chain = _stores()
    _seed_committed(review, _docx(["v1"]))
    with pytest.raises(vc.VersionConflict):
        vc.refine(
            TENANT_A, artifact_id=ART, content=_docx(["x"]),
            base_version=None, review_store=review, chain_store=chain,
        )


# --------------------------------------------------------------------------- #
# 3. 阳性：revert 生成新版本且历史原样保留（红线 6）                              #
# --------------------------------------------------------------------------- #
def test_revert_mints_a_new_version_and_preserves_history():
    review, chain = _stores()
    v1_bytes = _docx(["原始第一段", "原始第二段"])
    v1 = _seed_committed(review, v1_bytes)
    v2, _ = vc.refine(
        TENANT_A, artifact_id=ART, content=_docx(["改写后的第一段", "原始第二段"]),
        base_version=v1.version, review_store=review, chain_store=chain,
    )
    review.approve(TENANT_A, ART, v2.version, actor="h")

    v4, _ = vc.revert(
        TENANT_A, artifact_id=ART, to_version=1, review_store=review, chain_store=chain
    )

    assert v4.version == 3  # 追加新版本，不是覆盖
    assert v4.content_sha256 == v1.content_sha256  # 内容 == 目标版本
    # 历史仍可读、未被改写：
    assert review.read_content(TENANT_A, ART, 1) == v1_bytes
    assert {v.version for v in review.list_versions(TENANT_A, ART)} == {1, 2, 3}
    assert review.get_version(TENANT_A, ART, 1).state == rs.COMMITTED


def test_revert_records_source_and_parent_edges():
    review, chain = _stores()
    v1 = _seed_committed(review, _docx(["v1"]))
    v2, _ = vc.refine(
        TENANT_A, artifact_id=ART, content=_docx(["v2"]),
        base_version=v1.version, review_store=review, chain_store=chain,
    )
    review.approve(TENANT_A, ART, v2.version, actor="h")
    v3, _ = vc.revert(
        TENANT_A, artifact_id=ART, to_version=1, review_store=review, chain_store=chain
    )

    edge = chain.get_edge(TENANT_A, ART, v3.version)
    assert edge is not None
    assert edge.edge_kind == vc.EDGE_REVERT
    assert edge.parent_version == 2  # parent = 当时的 head
    assert edge.source_version == 1  # source = 被回退到的版本


def test_revert_to_an_unknown_version_is_not_found():
    review, chain = _stores()
    _seed_committed(review, _docx(["v1"]))
    with pytest.raises(rs.ArtifactNotFound):
        vc.revert(TENANT_A, artifact_id=ART, to_version=99, review_store=review, chain_store=chain)


def test_revert_cross_tenant_target_is_forbidden():
    review, chain = _stores()
    _seed_committed(review, _docx(["v1"]), tenant=TENANT_A)
    with pytest.raises(rs.CrossTenantArtifact):
        vc.revert(TENANT_B, artifact_id=ART, to_version=1, review_store=review, chain_store=chain)


def test_the_original_bytes_never_change():
    """红线 6：一连串 refine / revert 之后，v1 的字节与 sha 逐字节不变。"""
    review, chain = _stores()
    v1_bytes = _docx(["原件"])
    v1 = _seed_committed(review, v1_bytes)
    before = v1.content_sha256

    v2, _ = vc.refine(
        TENANT_A, artifact_id=ART, content=_docx(["改1"]),
        base_version=1, review_store=review, chain_store=chain,
    )
    review.approve(TENANT_A, ART, v2.version, actor="h")
    vc.revert(TENANT_A, artifact_id=ART, to_version=1, review_store=review, chain_store=chain)

    reloaded = review.get_version(TENANT_A, ART, 1)
    assert reloaded.content_sha256 == before
    assert review.read_content(TENANT_A, ART, 1) == v1_bytes


# --------------------------------------------------------------------------- #
# 4. compare + 版本链视图                                                        #
# --------------------------------------------------------------------------- #
def test_compare_diffs_two_versions():
    review, _chain = _stores()
    v1 = _seed_committed(review, _docx(["第一段", "第二段"]))
    v2 = _seed_pending(review, _docx(["第一段", "第二段-改"]))
    out = vc.compare(TENANT_A, artifact_id=ART, a=v1.version, b=v2.version, review_store=review)
    assert out["format"] == "docx"
    assert out["counts"]["modified"] >= 1


def test_compare_cross_tenant_is_forbidden():
    review, _ = _stores()
    _seed_committed(review, _docx(["v1"]), tenant=TENANT_A)
    with pytest.raises(rs.CrossTenantArtifact):
        vc.compare(TENANT_B, artifact_id=ART, a=1, b=1, review_store=review)


def test_version_chain_view_reports_head_versions_and_edges():
    review, chain = _stores()
    # Seed the root through T25's own entry so both edges exist (initial + refine).
    v1, _ = vc.refine(
        TENANT_A, artifact_id=ART, content=_docx(["v1"]),
        base_version=None, review_store=review, chain_store=chain,
    )
    review.approve(TENANT_A, ART, v1.version, actor="h")
    v2, _ = vc.refine(
        TENANT_A, artifact_id=ART, content=_docx(["v2"]),
        base_version=v1.version, review_store=review, chain_store=chain,
    )
    review.approve(TENANT_A, ART, v2.version, actor="h")
    view = vc.version_chain(TENANT_A, ART, review_store=review, chain_store=chain)
    assert view["head_version"] == 2
    assert len(view["versions"]) == 2
    assert len(view["edges"]) == 2
    assert {e["edge_kind"] for e in view["edges"]} == {vc.EDGE_INITIAL, vc.EDGE_REFINE}


# --------------------------------------------------------------------------- #
# 5. 租户 fail-closed（红线 5）                                                  #
# --------------------------------------------------------------------------- #
def test_writes_fail_closed_without_a_tenant():
    review, chain = _stores()
    for call in (
        lambda: vc.refine("", artifact_id=ART, content=_docx(["x"]), base_version=None,
                          review_store=review, chain_store=chain),
        lambda: vc.revert("", artifact_id=ART, to_version=1,
                          review_store=review, chain_store=chain),
        lambda: vc.compare("", artifact_id=ART, a=1, b=2, review_store=review),
        lambda: chain.record_edge("", artifact_id=ART, child_version=1, edge_kind=vc.EDGE_INITIAL),
    ):
        with pytest.raises(ValueError):
            call()


def test_reads_are_empty_without_a_tenant():
    review, chain = _stores()
    _seed_committed(review, _docx(["v1"]))
    assert review.list_versions("", ART) == []
    assert chain.list_edges("", ART) == []
    assert vc.version_chain("", ART, review_store=review, chain_store=chain)["versions"] == []


# --------------------------------------------------------------------------- #
# 6. edge 写入的守卫                                                             #
# --------------------------------------------------------------------------- #
def test_record_edge_rejects_an_unknown_kind():
    _review, chain = _stores()
    with pytest.raises(ValueError):
        chain.record_edge(TENANT_A, artifact_id=ART, child_version=1, edge_kind="bogus")


def test_record_edge_is_idempotent_per_child():
    _review, chain = _stores()
    first = chain.record_edge(
        TENANT_A, artifact_id=ART, child_version=1, edge_kind=vc.EDGE_INITIAL
    )
    again = chain.record_edge(
        TENANT_A, artifact_id=ART, child_version=1, edge_kind=vc.EDGE_REFINE, parent_version=9
    )
    assert first.edge_kind == vc.EDGE_INITIAL
    assert again.edge_kind == vc.EDGE_INITIAL  # the original edge wins
    assert len(chain.list_edges(TENANT_A, ART)) == 1


# --------------------------------------------------------------------------- #
# 7. T16 feedback —— refine / revert 事件                                        #
# --------------------------------------------------------------------------- #
def test_refine_writes_a_revise_feedback_event():
    review, chain = _stores()
    from forgeflow.outcomes.store import get_outcome_store

    run = "run-refine-1"
    _seed_committed(review, _docx(["v1"]), run_id=run)
    v2, _ = vc.refine(
        TENANT_A, artifact_id=ART, content=_docx(["v2"]),
        base_version=1, run_id=run, actor="human-1",
        review_store=review, chain_store=chain,
    )
    events = get_outcome_store().list_feedback(TENANT_A, run)
    kinds = [e["kind"] for e in events]
    assert vc.FEEDBACK_KIND_REVISED in kinds
    label = get_outcome_store().relabel(TENANT_A, run)["outcome_label"]
    assert label == "REVISED"


def test_revert_writes_a_revert_feedback_event():
    review, chain = _stores()
    from forgeflow.outcomes.store import get_outcome_store

    run = "run-revert-1"
    v1 = _seed_committed(review, _docx(["v1"]), run_id=run)
    vc.revert(
        TENANT_A, artifact_id=ART, to_version=v1.version, run_id=run, actor="human-1",
        review_store=review, chain_store=chain,
    )
    kinds = [e["kind"] for e in get_outcome_store().list_feedback(TENANT_A, run)]
    assert vc.FEEDBACK_KIND_REVERTED in kinds
    label = get_outcome_store().relabel(TENANT_A, run)["outcome_label"]
    assert label == "REVERTED"


def test_feedback_is_skipped_without_a_run_id():
    """无 run_id ⇒ 事件无挂载点：显式跳过，绝不编造一个 run（红线 4 的精神）。"""
    review, chain = _stores()
    from forgeflow.outcomes.store import get_outcome_store

    _seed_committed(review, _docx(["v1"]))  # no run_id
    vc.refine(
        TENANT_A, artifact_id=ART, content=_docx(["v2"]),
        base_version=1, review_store=review, chain_store=chain,
    )
    assert get_outcome_store().list_feedback(TENANT_A, "run-nonexistent") == []


# --------------------------------------------------------------------------- #
# 8. 追问指令解析（documents/followup.py）                                        #
# --------------------------------------------------------------------------- #
def test_followup_parses_refine():
    out = fu.parse_followup("再正式一点")
    assert out.kind == fu.KIND_REFINE
    assert out.to_version is None and out.a is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("回到第 1 版", 1),
        ("回退到 v2", 2),
        ("撤销到版本3", 3),
        ("还原为第 2 版", 2),
    ],
)
def test_followup_parses_revert(text, expected):
    out = fu.parse_followup(text)
    assert out.kind == fu.KIND_REVERT
    assert out.to_version == expected


@pytest.mark.parametrize(
    "text,a,b",
    [
        ("对比 v1 和 v3", 1, 3),
        ("比较版本 1 与版本 2", 1, 2),
        ("v1 和 v4 有什么不同", 1, 4),
    ],
)
def test_followup_parses_compare(text, a, b):
    out = fu.parse_followup(text)
    assert out.kind == fu.KIND_COMPARE
    assert (out.a, out.b) == (a, b)


def test_followup_reports_unknown_for_unrecognised_text():
    assert fu.parse_followup("今天天气不错").kind == fu.KIND_UNKNOWN
    assert fu.parse_followup("").kind == fu.KIND_UNKNOWN


def test_followup_never_invents_a_version():
    """fail-closed：识别出意图但取不到版本号 ⇒ 抛错，绝不默认一个目标版本。"""
    with pytest.raises(fu.FollowUpParseError):
        fu.parse_followup("回退一下")
    with pytest.raises(fu.FollowUpParseError):
        fu.parse_followup("对比一下")
