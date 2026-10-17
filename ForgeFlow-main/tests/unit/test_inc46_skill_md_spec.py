"""INC46 T18 — SKILL.md 官方规范对齐：快照、映射、校验器。

Scope (六问 ③④):

* **阳性** — 合规 name/description 通过；按映射表从七段契约渲染的产物通过
  校验器，且在 skills-ref 可用时通过 ``skills-ref validate``（不可用 ⇒ 显式
  skipped+原因，A5/红线 10）。
* **阴性** — 大写 / 连续连字符 / 首尾连字符 / 超 64 / 与目录名不符 ⇒ 报错；
  description 空或 >1024 ⇒ 报错；中文显示名直接作 name ⇒ 拒绝并要求 slug。
* **快照** — docs/inc46/skill_md_spec_snapshot.md 必须存在且含 URL + 取得日期 +
  sha256；快照记载的哈希与原始存档（_inc46_harness/spec_ef4db182.bin）实测一致
  （红线 10：快照缺失 ⇒ 不得 PASS）。
* **反事实（套件内）** — 把 name 校验短路后阴性用例全部“通过”，证明阴性断言
  由真实校验器决定；物理摘除 name 校验的验证记录在 T18 汇报中。
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from forgeflow.skills import spec_mapping as sm
from forgeflow.skills import spec_validator as sv

FORGEFLOW_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = FORGEFLOW_ROOT.parent
SNAPSHOT_DOC = FORGEFLOW_ROOT / "docs" / "inc46" / "skill_md_spec_snapshot.md"
SAMPLE_DOC = FORGEFLOW_ROOT / "docs" / "inc46" / "skill_md_mapping_sample.md"
RAW_ARCHIVE = REPO_ROOT / "_inc46_harness" / "spec_ef4db182.bin"

#: The same seven-section contract the committed mapping sample was rendered from.
SAMPLE_CONTRACT = {
    "manifest": {
        "display_name": "文档章节改写",
        "slug": "docx-section-rewrite",
        "version": "1.0.0",
        "description": (
            "Rewrites a specified DOCX section while preserving the original "
            "formatting. Use when a document section must be reworded, "
            "condensed, or expanded without touching the rest of the file."
        ),
        "license": "Proprietary",
        "compatibility": "Requires python-docx and a local filesystem workspace",
        "allowed_tools": ["docx.read", "docx.write", "fs.read"],
    },
    "procedure": [
        "读取目标章节定位参数（章节标题或索引）",
        "用 docx.read 提取章节原文与格式信息",
        "按改写指令生成新文本（保持样式锚点）",
        "用 docx.write 写回并校验格式未漂移",
    ],
    "policies": ["不得改动非目标章节", "不得引入原文之外的引用来源"],
    "tool_bindings": ["docx.read — 章节提取", "docx.write — 格式化写回", "fs.read — 模板/样式读取"],
    "examples": ["输入：report.docx 第 3 章「太长，压缩一半」→ 输出：压缩后同格式章节"],
    "knowledge": [{"file": "docx-styles.md", "summary": "DOCX 样式锚点速查"}],
    "evaluation": {"ref": "assets/evals/", "note": "评估集物化于 assets/evals/；权威评估记录在 ForgeFlow DB。"},
}


def _doc(name: str = "pdf-processing", description: str = "Extracts PDF text. Use when handling PDFs.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# Body\n"


# --------------------------------------------------------------------------- #
# 0. snapshot (红线 10: 缺失 ⇒ 不得 PASS)                                       #
# --------------------------------------------------------------------------- #
def test_snapshot_exists_with_url_date_and_hash():
    assert SNAPSHOT_DOC.is_file(), "规范快照缺失 ⇒ 本任务不得 PASS（红线 10）"
    text = SNAPSHOT_DOC.read_text(encoding="utf-8")
    assert "https://agentskills.io/specification" in text, "快照必须含来源 URL"
    assert "2026-10-03" in text, "快照必须含取得日期"
    assert sm.SNAPSHOT_SHA256 in text, "快照必须含内容哈希 sha256"
    assert "name" in text and "description" in text and "allowed-tools" in text


def test_snapshot_hash_matches_the_raw_archive():
    """快照记载的哈希与原始存档实测一致 —— 不是抄来的字符串。

    ``RAW_ARCHIVE`` 位于**版本库之外**（checkout 的兄弟目录，主理人预取的材料），
    所以任何只 checkout 仓库的运行环境（CI）都不可能有它。缺失 ⇒ **据实 skip**：
    此时断言失败是假红（快照本身没错），而断言一个未实测的哈希是假绿。
    """
    if not RAW_ARCHIVE.is_file():
        pytest.skip(
            "原始存档缺失（主理人预取材料，位于版本库之外，CI 只 checkout 仓库 ⇒ 不可能存在）："
            f"{RAW_ARCHIVE} —— 快照哈希只能与实测存档比对，缺失时据实跳过，不伪造哈希"
        )
    digest = hashlib.sha256(RAW_ARCHIVE.read_bytes()).hexdigest()
    assert digest == sm.SNAPSHOT_SHA256
    assert digest == "4c649bdf0e0a51c9e215d9f91009ecdca05ee9d073edb6f37c20265bbd829e11"


# --------------------------------------------------------------------------- #
# 1. 阳性：合规 name / description 通过                                         #
# --------------------------------------------------------------------------- #
def test_valid_name_passes():
    for name in ("pdf-processing", "data-analysis", "code-review", "a", "a1-b2"):
        assert sv.validate_skill_name(name) == [], f"{name} 应通过"


def test_valid_name_matches_parent_directory():
    assert sv.validate_skill_name("pdf-processing", parent_dir="pdf-processing") == []


def test_valid_description_passes():
    assert sv.validate_description("Extracts PDF text and fills forms. Use when handling PDFs.") == []
    assert sv.validate_description("x" * 1024) == []


def test_minimal_valid_skill_md_passes():
    report = sv.validate_skill_md(_doc(), parent_dir="pdf-processing")
    assert report.ok, f"合规文档不得有误：{report.errors}"
    assert report.frontmatter["name"] == "pdf-processing"


# --------------------------------------------------------------------------- #
# 2. 阴性：name 五类违规 + 中文显示名                                          #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("bad", "hint"),
    [
        ("PDF-Processing", "大写"),
        ("pdf--processing", "连续连字符"),
        ("-pdf", "以连字符开头"),
        ("pdf-", "以连字符结尾"),
        ("a" * 65, "64"),
        ("pdf processing", "非法字符"),
    ],
)
def test_invalid_names_are_rejected_with_the_specific_reason(bad, hint):
    errors = sv.validate_skill_name(bad)
    assert errors, f"{bad!r} 必须被拒绝"
    assert any(hint in message for message in errors), (
        f"{bad!r} 的错误信息应指明「{hint}」：{errors}"
    )


def test_name_must_match_parent_directory():
    errors = sv.validate_skill_name("pdf-processing", parent_dir="other-dir")
    assert errors and "父目录" in errors[0]


def test_chinese_display_name_is_rejected_and_demands_a_slug():
    errors = sv.validate_skill_name("文档处理")
    assert errors, "中文显示名直接作 name 必须被拒绝"
    assert any("slug" in message for message in errors), "错误必须要求提供 slug"


def test_empty_name_is_required_error():
    assert sv.validate_skill_name("") == ["name 为必填字段且不能为空"]
    assert sv.validate_skill_name(None) == ["name 为必填字段且不能为空"]


# --------------------------------------------------------------------------- #
# 3. 阴性：description / 可选字段                                              #
# --------------------------------------------------------------------------- #
def test_empty_description_is_rejected():
    assert sv.validate_description(""), "空 description 必须报错"
    assert sv.validate_description("   "), "空白 description 必须报错"
    assert sv.validate_description(None), "缺失 description 必须报错"


def test_overlong_description_is_rejected():
    errors = sv.validate_description("x" * 1025)
    assert errors and "1024" in errors[0]


def test_optional_field_rules():
    base = {"name": "pdf-processing", "description": "Extracts PDF text. Use with PDFs."}
    assert sv.validate_frontmatter({**base, "compatibility": "x" * 501}), "compatibility >500 必须报错"
    assert sv.validate_frontmatter({**base, "metadata": {"version": 1.0}}), "metadata 非字符串值必须报错"
    assert sv.validate_frontmatter({**base, "allowed-tools": ["a", "b"]}), "allowed-tools 非字符串必须报错"
    assert sv.validate_frontmatter({**base, "license": 42}), "license 非字符串必须报错"
    ok = {
        **base,
        "compatibility": "Requires git",
        "metadata": {"display_name": "PDF 处理", "version": "1.0"},
        "allowed-tools": "docx.read docx.write",
        "license": "Proprietary",
    }
    assert sv.validate_frontmatter(ok) == []


def test_missing_frontmatter_is_an_error():
    report = sv.validate_skill_md("# 没有 frontmatter\n")
    assert not report.ok
    assert any("frontmatter" in e for e in report.errors)


# --------------------------------------------------------------------------- #
# 4. 渐进披露 finding（只提示，不静默截断）                                      #
# --------------------------------------------------------------------------- #
def test_overlong_body_yields_a_finding_not_truncation():
    body = "\n".join(f"第 {i} 行说明" for i in range(600))
    report = sv.validate_skill_md(_doc() + body)
    assert report.ok, "正文超限是 finding，不是 error"
    assert any("500" in f for f in report.findings)
    assert "第 599 行说明" in report.body, "正文必须完整保留（不静默截断）"


def test_nested_file_reference_yields_a_finding():
    report = sv.validate_skill_md(_doc() + "详见 references/deep/nested/file.md")
    assert any("超过一层" in f for f in report.findings)
    flat = sv.validate_skill_md(_doc() + "详见 references/REFERENCE.md")
    assert flat.findings == []


# --------------------------------------------------------------------------- #
# 5. 映射表 + slug（A3）+ 渲染                                                 #
# --------------------------------------------------------------------------- #
def test_mapping_table_covers_all_seven_sections():
    sections = [entry.section for entry in sm.SECTION_MAPPING]
    assert sections == list(sm.SEVEN_SECTIONS)
    by_section = {entry.section: entry for entry in sm.SECTION_MAPPING}
    assert "frontmatter" in by_section["manifest"].target
    assert "references/" in by_section["knowledge"].target
    assert "allowed-tools" in by_section["policies"].target
    assert "assets/evals/" in by_section["evaluation"].target
    assert "DB" in by_section["evaluation"].rule, "评估权威必须指向 ForgeFlow DB"


def test_suggest_slug_from_ascii_display_name():
    assert sm.suggest_slug("PDF Processing") == "pdf-processing"
    assert sm.suggest_slug("Data  Analysis!!") == "data-analysis"
    long_slug = sm.suggest_slug("very " * 30 + "long name")
    assert len(long_slug) <= 64
    assert sv.validate_skill_name(long_slug) == []


def test_suggest_slug_refuses_pure_chinese_and_demands_explicit_slug():
    with pytest.raises(ValueError) as exc:
        sm.suggest_slug("文档处理")
    assert "slug" in str(exc.value)


def test_rendered_sample_passes_validation_and_maps_every_section():
    text = sm.render_skill_md(SAMPLE_CONTRACT)
    report = sv.validate_skill_md(text, parent_dir="docx-section-rewrite")
    assert report.ok, f"按映射渲染的产物必须通过校验：{report.errors}"

    fm = report.frontmatter
    assert fm["name"] == "docx-section-rewrite", "Manifest.slug → frontmatter name"
    assert fm["metadata"]["display_name"] == "文档章节改写", "显示名 → metadata.display_name"
    assert fm["metadata"]["version"] == "1.0.0", "版本 → metadata.version（字符串）"
    assert fm["allowed-tools"] == "docx.read docx.write fs.read", "Tool bindings → allowed-tools"
    assert "## 步骤" in text, "Procedure → 正文步骤"
    assert "## 约束" in text, "Policies → 正文「约束」段"
    assert "references/docx-styles.md" in text, "Knowledge → references/*.md"
    assert "assets/evals/" in text, "Evaluation → assets/evals/ 引用"
    assert "## 示例" in text, "Examples → 正文示例段"


def test_committed_mapping_sample_is_the_renderer_output_and_valid():
    """映射样例（七段→SKILL.md）必须真实存在、与渲染器逐字一致且通过校验。"""
    assert SAMPLE_DOC.is_file(), "映射样例缺失（证据要求）"
    text = SAMPLE_DOC.read_text(encoding="utf-8")
    assert text == sm.render_skill_md(SAMPLE_CONTRACT), "样例与渲染器输出漂移"
    report = sv.validate_skill_md(text, parent_dir="docx-section-rewrite")
    assert report.ok, f"提交的映射样例必须过检：{report.errors}"


# --------------------------------------------------------------------------- #
# 6. 漂移检测                                                                   #
# --------------------------------------------------------------------------- #
def test_drift_alert_is_none_when_hashes_match():
    assert sm.spec_drift_alert(sm.SNAPSHOT_SHA256) is None


def test_drift_alert_warns_without_blocking_on_mismatch():
    alert = sm.spec_drift_alert("0" * 64)
    assert alert is not None
    assert "告警" in alert and "不阻塞" in alert


def test_sha256_text_matches_stdlib():
    assert sm.sha256_text("abc") == hashlib.sha256(b"abc").hexdigest()


# --------------------------------------------------------------------------- #
# 7. skills-ref（A5：不可用 ⇒ 显式 skipped，不冒充 PASS）                        #
# --------------------------------------------------------------------------- #
def test_skills_ref_absent_reports_skipped_not_pass(monkeypatch):
    monkeypatch.setattr(sv.shutil, "which", lambda _exe: None)
    result = sv.validate_with_skills_ref("/nonexistent")
    assert result["status"] == "skipped"
    assert result["passed"] is None, "未测量 ⇒ None，绝不写 False 冒充结果"
    assert "未安装" in result["reason"]


def test_rendered_sample_with_skills_ref_binary(tmp_path):
    """skills-ref 可用 ⇒ 真跑 validate；不可用 ⇒ pytest.skip（红线 10）。"""
    if shutil.which("skills-ref") is None:
        pytest.skip("skills-ref 未安装（A5 裁决）：记 skipped+原因，不得冒充 PASS")
    skill_dir = tmp_path / "docx-section-rewrite"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(sm.render_skill_md(SAMPLE_CONTRACT), encoding="utf-8")
    result = sv.validate_with_skills_ref(str(skill_dir))
    assert result["status"] == "passed", f"skills-ref 校验失败：{result['output']}"


# --------------------------------------------------------------------------- #
# 8. 反事实（套件内）：name 校验是承重的                                        #
# --------------------------------------------------------------------------- #
def test_name_validation_is_loadbearing(monkeypatch):
    """把 name 校验短路 ⇒ 同一批阴性 name 全部“通过”，证明阴性断言非 vacuous。"""
    negatives = ["PDF-Processing", "pdf--processing", "-pdf", "a" * 65, "文档处理"]
    assert all(sv.validate_skill_name(bad) for bad in negatives), "前提：真实校验器拒绝"

    monkeypatch.setattr(sv, "validate_skill_name", lambda *a, **k: [])
    assert not any(sv.validate_skill_name(bad) for bad in negatives), (
        "短路后阴性用例必然“通过”——阴性断言由真实校验器决定"
    )
