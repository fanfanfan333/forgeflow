"""INC46 T22 —— Diff 预览与确认（含修订模式）。

覆盖任务书 TABLE 29 的 DoD：
  * diff 粒度：段落级 + run 级 + 表格单元格级；
  * 状态机 pending → approved(committed) | rejected | expired；
  * reject ⇒ 无 committed 版本、原件字节不变；
  * 非 pending 版本 approve ⇒ 409；
  * 跨租户 ⇒ 拒绝；
  * diff 含区间外变化 ⇒ 阻断 approve（可 override 且记录）。

反事实（每个关键机制都有一条）：摘掉该机制 ⇒ 对应行为必须翻转。
"""

from __future__ import annotations

import io

import pytest

from forgeflow.documents import diff as D
from forgeflow.documents import review_store as rs

TENANT_A = "tenant-diff-a"
TENANT_B = "tenant-diff-b"


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def _docx(paragraphs: list[str]) -> bytes:
    """Build real .docx bytes (no fakes — the diff must run on a real package)."""
    import docx  # noqa: PLC0415 — test-only dependency

    doc = docx.Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _store() -> rs.InMemoryArtifactReviewStore:
    return rs.InMemoryArtifactReviewStore()


def _pending(
    store: rs.ArtifactReviewStore,
    *,
    tenant: str = TENANT_A,
    artifact_id: str = "art-1",
    content: bytes = b"v1-content",
    **kwargs,
):
    return store.create_pending_version(
        tenant, artifact_id=artifact_id, content=content, **kwargs
    )


# --------------------------------------------------------------------------- #
# 1. diff 粒度：段落 / run / 表格单元格                                          #
# --------------------------------------------------------------------------- #
def test_diff_sees_a_paragraph_edit_and_its_runs():
    old = _docx(["第一段", "第二段", "第三段"])
    new = _docx(["第一段", "第二段（已改写）", "第三段"])
    d = D.diff_documents(old, new)

    assert d.format == "docx"
    assert d.is_clean() is True  # 有变化
    assert d.modified >= 1
    kinds = {p.kind for p in d.paragraphs}
    assert D.PARA_KIND_MODIFIED in kinds
    changed = [p for p in d.paragraphs if p.kind == D.PARA_KIND_MODIFIED]
    assert changed and changed[0].old_index == 1  # 定位到原文档第 1 段


def test_identical_documents_produce_no_changes():
    same = _docx(["a", "b", "c"])
    d = D.diff_documents(same, same)
    assert d.is_clean() is False
    assert d.paragraphs == []
    assert d.tables == []


# --------------------------------------------------------------------------- #
# 2. 区间外变化（红线 4：未测量 ⇒ None，绝不冒充干净）                            #
# --------------------------------------------------------------------------- #
def test_out_of_region_is_empty_when_the_edit_stays_inside():
    old = _docx(["p0", "p1", "p2", "p3"])
    new = _docx(["p0", "p1-rewritten", "p2", "p3"])
    d = D.diff_documents(old, new)
    assert d.out_of_region(1, 2) == []  # 改动落在 [1,2) 内 ⇒ 干净


def test_out_of_region_lists_an_edit_outside_the_target():
    old = _docx(["p0", "p1", "p2", "p3"])
    new = _docx(["p0", "p1", "p2", "p3-CHANGED"])
    d = D.diff_documents(old, new)
    outside = d.out_of_region(1, 2)
    assert outside, "区间外改动必须被列出（这是阻断 approve 的依据）"


def test_unmeasured_region_is_none_not_an_empty_list():
    """红线 4：区间未测量 ⇒ None（「无法判定」），绝不是 []（「已判定干净」）。"""
    old = _docx(["p0", "p1"])
    new = _docx(["p0", "p1-x"])
    d = D.diff_documents(old, new)
    assert d.out_of_region(None, 2) is None
    assert d.out_of_region(0, None) is None


# --------------------------------------------------------------------------- #
# 3. 状态机：pending → committed / rejected                                     #
# --------------------------------------------------------------------------- #
def test_approve_commits_and_mints_a_real_approval_id():
    store = _store()
    v, review = _pending(store, content=b"edited-bytes")
    assert v.state == rs.PENDING and v.is_pending is True
    assert review.status == rs.REVIEW_PENDING
    approval_id = review.approval_id
    assert approval_id  # 真实 ID，不是「逻辑上应该会生成」

    v2, r2 = store.approve(TENANT_A, "art-1", v.version, actor="human-1")
    assert v2.state == rs.COMMITTED and v2.is_committed is True
    assert v2.committed_at is not None
    assert r2.status == rs.REVIEW_APPROVED
    assert r2.approval_id == approval_id  # 同一个 ID 贯穿
    assert store.read_content(TENANT_A, "art-1", v2.version) == b"edited-bytes"


def test_reject_produces_no_committed_version_and_leaves_the_original_intact():
    store = _store()
    original = b"original-bytes"
    v, _ = _pending(store, content=original)
    v2, r2 = store.reject(TENANT_A, "art-1", v.version, actor="human-1", reason="改写过头")

    assert v2.state == rs.REJECTED
    assert v2.is_committed is False
    assert r2.status == rs.REVIEW_REJECTED
    assert r2.reason == "改写过头"  # 原因进入 T16
    assert store.latest_committed(TENANT_A, "art-1") is None  # 没有 committed 版本
    # 原件（唯一落盘的内容）字节不变
    assert store.read_content(TENANT_A, "art-1", v.version) == original


def test_approving_a_non_pending_version_is_refused():
    """非 pending 版本 approve ⇒ 409（语义），不得重复提交。"""
    store = _store()
    v, _ = _pending(store)
    store.approve(TENANT_A, "art-1", v.version)
    with pytest.raises(rs.VersionNotPending):
        store.approve(TENANT_A, "art-1", v.version)

    w, _ = _pending(store, content=b"other")
    store.reject(TENANT_A, "art-1", w.version)
    with pytest.raises(rs.VersionNotPending):
        store.approve(TENANT_A, "art-1", w.version)


def test_cross_tenant_access_is_refused():
    store = _store()
    v, _ = _pending(store, tenant=TENANT_A)
    # 写路径：跨租户 approve 被显式拒绝
    with pytest.raises(rs.CrossTenantArtifact):
        store.approve(TENANT_B, "art-1", v.version)
    # 读路径：本租户看不到 ⇒ None（不泄漏），但「别处确实存在」时 read_content 显式报错
    assert store.get_version(TENANT_B, "art-1", v.version) is None
    assert store.list_versions(TENANT_B, "art-1") == []
    with pytest.raises(rs.CrossTenantArtifact):
        store.read_content(TENANT_B, "art-1", v.version)
    # 本租户仍然正常可见
    assert store.get_version(TENANT_A, "art-1", v.version) is not None


def test_unscoped_write_is_refused_fail_closed():
    store = _store()
    with pytest.raises(ValueError):
        store.create_pending_version(None, artifact_id="art-1", content=b"x")


# --------------------------------------------------------------------------- #
# 4. 区间外变化 ⇒ 阻断 approve（可 override 且记录）                              #
# --------------------------------------------------------------------------- #
def test_out_of_region_blocks_approve():
    store = _store()
    v, _ = _pending(
        store,
        out_of_region=[{"old_index": 7, "kind": "modified"}],
    )
    with pytest.raises(rs.OutOfRegionBlocked):
        store.approve(TENANT_A, "art-1", v.version)
    assert store.latest_committed(TENANT_A, "art-1") is None


def test_override_allows_approve_and_is_recorded():
    store = _store()
    v, _ = _pending(store, out_of_region=[{"old_index": 7, "kind": "modified"}])
    v2, r2 = store.approve(
        TENANT_A, "art-1", v.version, override=True, override_reason="用户知情确认"
    )
    assert v2.state == rs.COMMITTED
    assert r2.override is True
    assert r2.override_reason == "用户知情确认"


def test_clean_diff_does_not_block_approve():
    store = _store()
    v, _ = _pending(store, out_of_region=[])
    v2, _ = store.approve(TENANT_A, "art-1", v.version)
    assert v2.state == rs.COMMITTED


# --------------------------------------------------------------------------- #
# 4b. 修订模式（.tracked.docx，<w:ins>/<w:del>）                                 #
# --------------------------------------------------------------------------- #
def test_tracked_docx_carries_ins_and_del_and_passes_structural_validation():
    from forgeflow.documents import tracked_changes as tc

    old = _docx(["第一段", "第二段", "第三段"])
    new = _docx(["第一段", "第二段（已改写）", "第三段"])
    tracked = tc.build_tracked_docx(old, new)
    assert tracked and tracked != old  # 真的产出了新字节

    report = tc.validate_tracked_docx(tracked)
    assert report["valid"] is True, f"结构校验未通过: {report['errors']}"
    assert report["ins_count"] >= 1
    assert report["del_count"] >= 1
    assert report["errors"] == []


def test_validate_tracked_docx_rejects_garbage():
    """阴性：非 docx / 无修订标记的字节不得被判为 valid。"""
    from forgeflow.documents import tracked_changes as tc

    report = tc.validate_tracked_docx(b"not-a-zip-at-all")
    assert report["valid"] is False
    assert report["errors"], "失败必须给出原因，不能静默"

    plain = _docx(["只有一段"])  # 合法 docx 但没有修订标记
    report2 = tc.validate_tracked_docx(plain)
    assert report2["valid"] is False


# --------------------------------------------------------------------------- #
# 5. 反事实 —— 摘掉机制 ⇒ 行为必须翻转                                            #
# --------------------------------------------------------------------------- #
def test_counterfactual_removing_the_pending_gate_flips_the_409(monkeypatch):
    """证明是「pending 门」在拦截，而不是别的什么。"""
    store = _store()
    v, _ = _pending(store)
    store.approve(TENANT_A, "art-1", v.version)

    # 基线：第二次 approve 被拒
    with pytest.raises(rs.VersionNotPending):
        store.approve(TENANT_A, "art-1", v.version)

    # 反事实：摘掉 pending 门 ⇒ 不再拒绝（行为翻转）
    monkeypatch.setattr(
        rs.ArtifactReviewStore,
        "_require_pending",
        lambda self, tenant, artifact_id, version: self._load_version(
            tenant, artifact_id, version
        ),
    )
    store.approve(TENANT_A, "art-1", v.version)  # 不抛 ⇒ 基线确实是这道门造成的


def test_counterfactual_removing_out_of_region_detection_flips_the_block(monkeypatch):
    """证明是「区间外检测」在阻断，而不是 approve 自身失败。"""
    store = _store()
    v, _ = _pending(store, out_of_region=[{"old_index": 7, "kind": "modified"}])

    # 基线：被阻断
    with pytest.raises(rs.OutOfRegionBlocked):
        store.approve(TENANT_A, "art-1", v.version)

    # 反事实：把越界判定摘掉 ⇒ approve 通过（行为翻转）
    monkeypatch.setattr(
        D.DocumentDiff,
        "out_of_region",
        lambda self, start, end: [],
    )
    monkeypatch.setattr(
        rs.ArtifactReviewStore,
        "_require_pending",
        lambda self, tenant, artifact_id, version: (
            lambda row: (row.__setitem__("out_of_region", []), row)[1]
        )(self._load_version(tenant, artifact_id, version)),
    )
    v2, _ = store.approve(TENANT_A, "art-1", v.version)
    assert v2.state == rs.COMMITTED
