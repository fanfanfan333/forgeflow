"""INC46 T26 — 保真语料库集成测试（fidelity corpus）。

目标（TABLE 25）
----------------
用**真实结构语料**而非 12 个用例证明保真，并防止「某特征无样本」造成空覆盖：

* 语料 ≥40 份 docx，覆盖 15 类特征（每类 ≥2 份）；特征矩阵见
  ``tests/corpus/manifest.yaml``；
* 两类往返：**空操作往返**（未编辑 ⇒ 规范化后逐部件相同，仅白名单元数据变化）；
  **定向编辑往返**（仅目标段落变化）；
* 阴性探针：故意破坏的变体（丢 part / 改段落）必须被检出；矩阵缺样本必须门禁报错。

非空洞性（DoD ④）
-----------------
* 阳性：全矩阵空操作 + 定向编辑均通过，输出 ``pass_matrix.md``（特征 × 操作）；
* 阴性：``test_corrupted_*`` 断言比较器**必须**报出差异；
* 反事实：去掉格式比较（``compare_packages`` 只比 ``word/document.xml``）⇒ 丢失
  part 的篡改变体用例转红（已在 T26 交付记录中实测并还原）。

所有语料均由 ``tests/corpus/build_corpus.py`` **合成**，不含真实客户数据（红线 16）。
本文件只新增用例，不改动既有测试（红线 1）。
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from forgeflow.documents import EditOp, apply_edits
from tests.corpus.build_corpus import (
    CORPUS_DIR,
    EXPECTED_FEATURES,
    PLAN,
    CorpusGateError,
    assert_full_coverage,
    build_sample,
    load_manifest,
)
from tests.corpus.package_compare import (
    body_paragraph_signatures,
    compare_packages,
    read_package,
)

# --------------------------------------------------------------------------- #
# 载入矩阵 / 语料                                                              #
# --------------------------------------------------------------------------- #
MANIFEST: dict[str, Any] = load_manifest()
FLAT_SAMPLES: list[tuple[str, dict[str, Any]]] = [
    (feature["id"], sample)
    for feature in MANIFEST["features"]
    for sample in feature["samples"]
]
SAMPLE_IDS = [sample["file"] for _, sample in FLAT_SAMPLES]


def corpus_path(sample: dict[str, Any]) -> Path:
    return CORPUS_DIR / sample["file"]


def read_corpus(sample: dict[str, Any]) -> bytes:
    return corpus_path(sample).read_bytes()


def _repack(parts: dict[str, bytes]) -> bytes:
    """把 parts 重新打包为确定性 zip（供构造篡改变体）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(parts):
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 3, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, parts[name])
    return buffer.getvalue()


def _drop_part(name: str) -> Callable[[bytes], bytes]:
    def corrupt(data: bytes) -> bytes:
        parts = read_package(data)
        assert name in parts, f"变体构造前提：{name} 必须存在"
        del parts[name]
        return _repack(parts)

    return corrupt


def _mutate_text(old: str, new: str) -> Callable[[bytes], bytes]:
    def corrupt(data: bytes) -> bytes:
        parts = read_package(data)
        blob = parts["word/document.xml"]
        assert old.encode("utf-8") in blob, f"变体构造前提：{old} 必须存在"
        parts["word/document.xml"] = blob.replace(old.encode("utf-8"), new.encode("utf-8"))
        return _repack(parts)

    return corrupt


# --------------------------------------------------------------------------- #
# A. 矩阵与语料完整性（阳性 + 门禁）                                            #
# --------------------------------------------------------------------------- #
def test_manifest_declares_all_fifteen_features():
    """矩阵必须登记 TABLE 25 的 15 类特征，顺序与计划一致。"""
    feature_ids = [feature["id"] for feature in MANIFEST["features"]]
    assert feature_ids == list(EXPECTED_FEATURES)
    assert MANIFEST["feature_count"] == len(EXPECTED_FEATURES) == 15


def test_corpus_size_at_least_forty_samples():
    """>=40 份 docx，每类 >=2 份（防空覆盖硬指标）。"""
    assert MANIFEST["sample_count"] >= 40
    assert len(FLAT_SAMPLES) == MANIFEST["sample_count"] >= 40
    for feature in MANIFEST["features"]:
        assert len(feature["samples"]) >= 2, f"特征 {feature['id']} 样本不足"


def test_coverage_gate_passes_on_real_manifest():
    """真实矩阵满足覆盖门禁。"""
    assert_full_coverage(MANIFEST, EXPECTED_FEATURES)


def test_coverage_gate_errors_when_a_feature_has_no_samples():
    """阴性：某特征样本被清空 ⇒ 门禁必须报错（防空覆盖）。"""
    mutated = yaml.safe_load(yaml.safe_dump(MANIFEST))
    for feature in mutated["features"]:
        if feature["id"] == "footnotes":
            feature["samples"] = []
    with pytest.raises(CorpusGateError) as excinfo:
        assert_full_coverage(mutated, EXPECTED_FEATURES)
    assert "footnotes" in str(excinfo.value)


def test_coverage_gate_errors_when_a_feature_is_missing():
    """阴性：整类特征从矩阵里删除 ⇒ 门禁必须报错（防空覆盖）。"""
    mutated = yaml.safe_load(yaml.safe_dump(MANIFEST))
    mutated["features"] = [f for f in mutated["features"] if f["id"] != "equations"]
    with pytest.raises(CorpusGateError) as excinfo:
        assert_full_coverage(mutated, EXPECTED_FEATURES)
    assert "equations" in str(excinfo.value)


@pytest.mark.parametrize("feature_id,sample", FLAT_SAMPLES, ids=SAMPLE_IDS)
def test_corpus_file_exists_and_hash_matches_manifest(feature_id, sample):
    """语料清单与哈希：每份文件存在且 sha256 与矩阵一致（防伪造样本/哈希）。"""
    path = corpus_path(sample)
    assert path.exists(), f"语料缺失：{path}"
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    assert actual == sample["sha256"]


@pytest.mark.parametrize("feature_id,sample", FLAT_SAMPLES, ids=SAMPLE_IDS)
def test_corpus_is_reproducible_from_generator(feature_id, sample):
    """哈希可复现：用生成器重建该样本，字节必须与提交的语料完全一致。"""
    spec = next(s for s in PLAN if s.slug == Path(sample["file"]).stem)
    regenerated = build_sample(spec)
    assert hashlib.sha256(regenerated).hexdigest() == sample["sha256"]
    assert regenerated == read_corpus(sample)


# --------------------------------------------------------------------------- #
# B. 空操作往返（阳性）                                                        #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("feature_id,sample", FLAT_SAMPLES, ids=SAMPLE_IDS)
def test_noop_roundtrip_is_part_identical(feature_id, sample):
    """未编辑 ⇒ 规范化后逐部件相同（仅白名单元数据变化）。"""
    original = read_corpus(sample)
    produced, changes = apply_edits(original, [])
    assert changes == 0
    diff = compare_packages(original, produced)
    assert diff.is_empty, f"{sample['file']} 空操作往返不保真：{diff.summary()}"
    # 白名单只允许文档时间戳类元数据
    assert all(
        part in ("docProps/core.xml", "docProps/app.xml") for part in diff.whitelisted
    )


# --------------------------------------------------------------------------- #
# C. 定向编辑往返（阳性：仅目标段落变化）                                      #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("feature_id,sample", FLAT_SAMPLES, ids=SAMPLE_IDS)
def test_directed_edit_changes_only_target_paragraph(feature_id, sample):
    """定向编辑 ⇒ 仅目标段落签名变化，其余 body 段落逐字节不变。"""
    original = read_corpus(sample)
    before = body_paragraph_signatures(original)
    edited, changes = apply_edits(
        original,
        [EditOp(op="replace_text", match=sample["marker"], replace=sample["replacement"])],
    )
    assert changes == 1, f"{sample['file']} 期望恰好改动 1 处，实得 {changes}"
    after = body_paragraph_signatures(edited)
    assert len(before) == len(after)
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert len(changed) == 1, f"{sample['file']} 变化的段落不止一处：{changed}"
    # 目标段落确实是含标记的那一段，且已写入替换文本
    assert sample["replacement"] in after[changed[0]]


# --------------------------------------------------------------------------- #
# D. 阴性探针：篡改变体必须被检出                                             #
# --------------------------------------------------------------------------- #
def _missing_part_cases() -> list[tuple[str, str, str]]:
    """(用例名, 语料文件, 将被删除的 part)。"""
    return [
        ("comments", "comments_01.docx", "word/comments.xml"),
        ("footnotes", "footnotes_01.docx", "word/footnotes.xml"),
        ("endnotes", "footnotes_01.docx", "word/endnotes.xml"),
        ("header", "headers_footers_01.docx", "word/header1.xml"),
        ("image", "floating_images_01.docx", "word/media/image1.png"),
    ]


@pytest.mark.parametrize(
    "case,filename,part",
    _missing_part_cases(),
    ids=[c[0] for c in _missing_part_cases()],
)
def test_corrupted_missing_part_is_detected(case, filename, part):
    """丢掉任一 part ⇒ 比较器必须报出「仅 A 有该部件」。"""
    sample = next(s for _, s in FLAT_SAMPLES if s["file"] == filename)
    original = read_corpus(sample)
    corrupted = _drop_part(part)(original)
    diff = compare_packages(original, corrupted)
    assert not diff.is_empty, f"{case}: 丢 {part} 未被检出（假通过）"
    assert part in diff.only_in_a, f"{case}: 未在 only_in_a 中报出 {part}"


def test_corrupted_mutated_paragraph_is_detected():
    """篡改正文段落文本 ⇒ document.xml 必须报为内容不同。"""
    sample = next(s for _, s in FLAT_SAMPLES if s["file"] == "revisions_01.docx")
    original = read_corpus(sample)
    corrupted = _mutate_text("收尾段落", "收尾段落（被篡改）")(original)
    diff = compare_packages(original, corrupted)
    assert not diff.is_empty
    assert "word/document.xml" in [d.name for d in diff.differing]


def test_corrupted_relationship_is_detected():
    """破坏关系部件内容 ⇒ .rels 必须报为内容不同。"""
    sample = next(s for _, s in FLAT_SAMPLES if s["file"] == "hyperlinks_01.docx")

    def corrupt(data: bytes) -> bytes:
        parts = read_package(data)
        rels = parts["word/_rels/document.xml.rels"]
        parts["word/_rels/document.xml.rels"] = rels.replace(b"example.com", b"evil.example")
        return _repack(parts)

    original = read_corpus(sample)
    diff = compare_packages(original, corrupt(original))
    assert not diff.is_empty
    assert "word/_rels/document.xml.rels" in [d.name for d in diff.differing]


# --------------------------------------------------------------------------- #
# E. Pass matrix（特征 × 操作）+ 报告落盘                                      #
# --------------------------------------------------------------------------- #
def test_pass_matrix_all_features_pass_and_report_written():
    """对全语料跑两类往返，生成 pass matrix 报告并断言全绿（无空覆盖）。"""
    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    for feature in MANIFEST["features"]:
        feature_rows = []
        for sample in feature["samples"]:
            original = read_corpus(sample)
            # 空操作
            produced, changes = apply_edits(original, [])
            noop_ok = changes == 0 and compare_packages(original, produced).is_empty
            # 定向编辑
            edited, edit_changes = apply_edits(
                original,
                [EditOp(op="replace_text", match=sample["marker"], replace=sample["replacement"])],
            )
            before = body_paragraph_signatures(original)
            after = body_paragraph_signatures(edited)
            changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
            edit_ok = edit_changes == 1 and len(changed) == 1
            if not (noop_ok and edit_ok):
                failures.append(f"{sample['file']}(noop={noop_ok},edit={edit_ok})")
            feature_rows.append((sample["file"], noop_ok, edit_ok))
        rows.append((feature["id"], feature["name"], feature_rows))

    assert not failures, f"pass matrix 存在失败单元：{failures}"
    report = _render_pass_matrix(rows)
    # CORPUS_DIR = <repo>/tests/fixtures/docx_corpus → parents[1] = <repo>/tests
    (CORPUS_DIR.parents[1] / "corpus" / "pass_matrix.md").write_text(report, encoding="utf-8")
    assert report.count("PASS") >= 2 * len(FLAT_SAMPLES)


def _render_pass_matrix(rows: list[tuple[str, str, list[tuple[str, bool, bool]]]]) -> str:
    total = sum(len(samples) for _, _, samples in rows)
    lines = [
        "# T26 保真语料库 · Pass Matrix（特征 × 操作）",
        "",
        f"- 特征数：{len(rows)}；样本数：{total}；每类样本数 ≥2。",
        "- 操作：`noop` = 空操作往返（逐部件规范化相同）；`edit` = 定向编辑（仅目标段落变化）。",
        "",
        "| 特征 | 特征名 | 样本 | noop | edit |",
        "| --- | --- | --- | --- | --- |",
    ]
    for feature_id, name, samples in rows:
        for filename, noop_ok, edit_ok in samples:
            lines.append(
                f"| {feature_id} | {name} | {filename} | "
                f"{'PASS' if noop_ok else 'FAIL'} | {'PASS' if edit_ok else 'FAIL'} |"
            )
    lines += [
        "",
        "**结论**：全矩阵 noop 与 edit 均 PASS；无特征空覆盖。",
        "",
    ]
    return "\n".join(lines)
