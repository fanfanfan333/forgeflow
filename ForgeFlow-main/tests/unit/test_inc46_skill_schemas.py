"""INC46 T07 — unit tests for the seven-segment skill contract (Pydantic).

Coverage (task book TABLE 11):
* **positive** — all seven segments present ⇒ validation passes, and the contract
  renders a SKILL.md that T18's own validator accepts;
* **negative** — a missing Examples / Knowledge segment is an explicit error
  (never a silent completion); an illegal ``name`` slug is rejected per rule;
  an empty required field is a per-field error, never a vague "参数错误";
* **findings** — an over-size body and a too-deep reference are *findings only*
  (no truncation, no error);
* **completion** — derivable/defaultable values are filled **with provenance**;
  a missing segment is never fabricated;
* **counterfactual** — :data:`segments.REQUIRED_SEGMENTS` genuinely governs the
  missing-segment errors (proven physically in the task run).

The contract JSON Schema snapshot (``docs/inc46/skill_contract_schema.json``) is
asserted here so an accidental shape change is caught.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forgeflow.skills import segments as seg
from forgeflow.skills.contract_completion import complete_contract, missing_required_segments
from forgeflow.skills.schemas import (
    ContractValidationReport,
    SkillContractDocument,
    SkillContractValidationError,
    contract_json_schema,
    require_valid_contract,
    seven_segments_from_draft_spec,
    seven_segments_from_skill_contract,
    validate_contract_document,
)
from forgeflow.skills.spec_mapping import SEVEN_SECTIONS, render_skill_md
from forgeflow.skills.spec_validator import validate_skill_md

_SCHEMA_SNAPSHOT = (
    Path(__file__).resolve().parents[2] / "docs" / "inc46" / "skill_contract_schema.json"
)


def _valid_contract(**overrides: object) -> dict:
    """A fully-populated, spec-compliant seven-segment contract for the probes."""
    base: dict = {
        "manifest": {
            "name": "docx-section-rewrite",
            "display_name": "章节改写",
            "version": "1.0.0",
            "description": "改写指定章节并保持格式；当用户要求把某部分改得更正式时使用。",
        },
        "knowledge": {
            "references": [
                {"file": "references/style-guide.md", "summary": "文风指南"},
            ]
        },
        "procedure": {"steps": ["定位目标章节", "改写文本", "复核格式"]},
        "policies": {"constraints": ["保持金额与日期不变", "不得改动目标区间外内容"]},
        "tool_bindings": {"tools": ["docx_edit"], "scripts": ["scripts/rewrite.py"]},
        "evaluation": {"ref": "assets/evals/", "note": "golden 集见 DB"},
        "examples": {"examples": ["把第三章改得更正式，保持金额不变。"]},
    }
    for key, value in overrides.items():
        if value is None:
            base.pop(key, None)
        else:
            base[key] = value
    return base


# --------------------------------------------------------------------------- #
# positive probe                                                               #
# --------------------------------------------------------------------------- #
def test_seven_sections_are_the_t18_mapping():
    """The contract's seven segments are exactly T18's authoritative order."""
    assert seg.SEGMENT_ORDER == tuple(SEVEN_SECTIONS)
    assert len(seg.SEGMENT_ORDER) == 7


def test_all_seven_segments_required_by_default():
    """T07 rule: all seven segments are required (Knowledge/Examples included)."""
    assert tuple(seg.REQUIRED_SEGMENTS) == tuple(SEVEN_SECTIONS)
    assert seg.OPTIONAL_SEGMENTS == ()


@pytest.mark.parametrize(
    "key",
    ["tool_bindings", "Tool bindings", "tool-bindings", "tool bindings"],
)
def test_segment_key_spellings_normalise(key):
    assert seg.normalise_segment_key(key) == "tool_bindings"


def test_full_contract_passes_validation():
    """Positive: all seven segments present ⇒ validation passes."""
    report = validate_contract_document(_valid_contract())
    assert report.ok, report.errors
    assert report.errors == []
    assert report.document is not None
    assert report.document.present_segments() == list(SEVEN_SECTIONS)
    assert report.document.missing_segments() == []


def test_require_valid_contract_returns_document():
    document = require_valid_contract(_valid_contract())
    assert isinstance(document, SkillContractDocument)
    assert isinstance(document.manifest, type(document.manifest))


def test_contract_renders_a_spec_compliant_skill_md():
    """The contract projects into T18's renderer and passes T18's validator."""
    document = require_valid_contract(_valid_contract())
    text = render_skill_md(document.to_contract_dict())
    result = validate_skill_md(text, parent_dir="docx-section-rewrite")
    assert result.ok, result.errors
    assert result.frontmatter["name"] == "docx-section-rewrite"
    assert result.frontmatter["metadata"]["display_name"] == "章节改写"
    assert result.frontmatter["metadata"]["version"] == "1.0.0"


# --------------------------------------------------------------------------- #
# negative probe — missing segments (must be explicit, never silent)           #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("segment", list(SEVEN_SECTIONS))
def test_negative_each_missing_segment_is_an_explicit_error(segment):
    report = validate_contract_document(_valid_contract(**{segment: None}))
    assert not report.ok
    assert any(
        "缺少必需段" in e and segment in e for e in report.errors
    ), f"{segment} 缺失未产生显式错误：{report.errors}"


def test_negative_missing_examples_is_an_explicit_error():
    """Negative (task book): missing Examples ⇒ explicit error, no completion."""
    report = validate_contract_document(_valid_contract(examples=None))
    assert not report.ok
    assert any("examples" in e and "缺少必需段" in e for e in report.errors)


def test_negative_missing_knowledge_is_an_explicit_error():
    """Negative (task book): missing Knowledge ⇒ explicit error, no completion."""
    report = validate_contract_document(_valid_contract(knowledge=None))
    assert not report.ok
    assert any("knowledge" in e and "缺少必需段" in e for e in report.errors)


def test_missing_segments_are_never_silently_completed():
    """Completion leaves a missing required segment missing (no fabrication)."""
    result = complete_contract(_valid_contract(examples=None))
    assert not result.ok
    assert "examples" not in result.contract
    assert "examples" in result.report.to_dict()["missing_segments"]


# --------------------------------------------------------------------------- #
# negative probe — field-level errors                                          #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("My-Skill", "大写"),
        ("my--skill", "连续连字符"),
        ("-my-skill", "开头"),
        ("my-skill-", "结尾"),
        ("a" * 65, "超过"),
        ("中文名", "ASCII"),
        ("my_skill", "非法字符"),
    ],
)
def test_illegal_name_slug_is_rejected_per_rule(name, reason):
    report = validate_contract_document(
        _valid_contract(manifest={**_valid_contract()["manifest"], "name": name})
    )
    assert not report.ok, f"非法 name {name!r} 未被拒绝"
    assert any(
        "manifest.name" in e and reason in e for e in report.errors
    ), f"未给出具体规则（{reason}）：{report.errors}"


def test_name_must_match_parent_directory():
    report = validate_contract_document(
        _valid_contract(), parent_dir="other-directory"
    )
    assert not report.ok
    assert any("父目录" in e for e in report.errors)


def test_empty_required_field_is_a_per_field_error():
    report = validate_contract_document(_valid_contract(procedure={"steps": []}))
    assert not report.ok
    assert any("procedure.steps" in e for e in report.errors)


def test_empty_description_is_a_per_field_error():
    report = validate_contract_document(
        _valid_contract(manifest={**_valid_contract()["manifest"], "description": ""})
    )
    assert not report.ok
    assert any("manifest.description" in e for e in report.errors)


def test_unknown_segment_is_an_explicit_error():
    contract = _valid_contract()
    contract["manifestt"] = {"name": "x"}
    report = validate_contract_document(contract)
    assert not report.ok
    # extra="forbid" names the offending key at the top level.
    assert any("manifestt" in e for e in report.errors)


def test_overlong_compatibility_is_a_per_field_error():
    report = validate_contract_document(
        _valid_contract(
            manifest={**_valid_contract()["manifest"], "compatibility": "x" * 501}
        )
    )
    assert not report.ok
    assert any("compatibility" in e and "超过" in e for e in report.errors)


def test_errors_are_never_collapsed_to_a_generic_message():
    report = validate_contract_document("not-a-mapping")
    assert not report.ok
    assert report.errors[0].startswith("契约必须是键值映射")

    multi = validate_contract_document(_valid_contract(examples=None, knowledge=None))
    assert len([e for e in multi.errors if "缺少必需段" in e]) == 2


# --------------------------------------------------------------------------- #
# findings — advisory only, never errors and never truncation                  #
# --------------------------------------------------------------------------- #
def test_oversize_body_produces_findings_not_errors_and_no_truncation():
    huge_steps = [f"步骤 {i}: " + "内容" * 40 for i in range(600)]
    report = validate_contract_document(_valid_contract(procedure={"steps": huge_steps}))
    assert report.ok, report.errors
    assert report.findings, "超限正文必须产生 finding"
    assert any("tokens" in f or "行" in f for f in report.findings)
    # nothing truncated — the document still carries every step verbatim
    assert report.document is not None
    assert len(report.document.procedure.steps) == 600


def test_oversize_body_is_not_an_error_but_a_finding():
    huge = [f"x{i} " + "y" * 80 for i in range(520)]
    report = validate_contract_document(_valid_contract(procedure={"steps": huge}))
    assert report.errors == []
    assert report.findings


def test_reference_deeper_than_one_level_is_a_finding_not_an_error():
    report = validate_contract_document(
        _valid_contract(
            knowledge={
                "references": [
                    {"file": "references/sub/deep.md", "summary": "深引用"},
                ]
            }
        )
    )
    assert report.ok, report.errors
    assert any("超过一层" in f for f in report.findings)


def test_reference_depth_helper():
    assert seg.is_reference_too_deep("references/a.md") is False
    assert seg.is_reference_too_deep("references/a/b.md") is True
    assert seg.is_reference_too_deep("scripts/run.py") is False
    assert seg.normalize_reference("examples.md") == "references/examples.md"


# --------------------------------------------------------------------------- #
# contract completion — provenance, never fabrication                          #
# --------------------------------------------------------------------------- #
def test_completion_derives_slug_with_provenance():
    contract = _valid_contract(
        manifest={
            "display_name": "Section Rewrite",
            "version": "1.0.0",
            "description": "改写指定章节并保持格式。",
        }
    )
    result = complete_contract(contract, parent_dir="section-rewrite")
    assert result.ok, result.errors
    assert result.contract["manifest"]["name"] == "section-rewrite"
    actions = {(p.field, p.action) for p in result.provenance}
    assert ("manifest.name", "derived") in actions


def test_completion_refuses_to_derive_a_non_ascii_slug():
    contract = _valid_contract(
        manifest={
            "display_name": "只有中文",
            "version": "1.0.0",
            "description": "描述",
        }
    )
    result = complete_contract(contract)
    assert not result.ok
    assert any(p.action == "skipped" for p in result.provenance)
    assert not result.contract["manifest"].get("name"), "不得凭空编造 slug"


def test_completion_defaults_evaluation_ref_with_provenance():
    contract = _valid_contract(evaluation={"note": "note only"})
    result = complete_contract(contract)
    assert result.contract["evaluation"]["ref"] == "assets/evals/"
    assert any(
        p.field == "evaluation.ref" and p.action == "defaulted" for p in result.provenance
    )


def test_completion_records_key_normalisation():
    contract = _valid_contract()
    contract["Tool bindings"] = contract.pop("tool_bindings")
    result = complete_contract(contract)
    assert result.ok, result.errors
    assert any(p.action == "normalised" for p in result.provenance)


def test_completion_never_invents_content():
    result = complete_contract(_valid_contract(examples=None, knowledge=None))
    assert not result.ok
    # The two absent segments are reported, and NOT fabricated into the contract.
    assert set(result.report.to_dict()["missing_segments"]) == {"knowledge", "examples"}
    assert "examples" not in result.contract
    assert "knowledge" not in result.contract


def test_missing_required_segments_helper():
    assert missing_required_segments(_valid_contract()) == []
    assert missing_required_segments(_valid_contract(examples=None)) == ["examples"]


# --------------------------------------------------------------------------- #
# adapters from the existing contract layer                                    #
# --------------------------------------------------------------------------- #
def test_draft_spec_adapter_yields_a_partial_contract():
    data = seven_segments_from_draft_spec(
        {"prompt": "改写章节", "steps": ["a", "b"], "tools": ["docx_edit"]},
        slug="docx-section-rewrite",
        display_name="章节改写",
        version="1.0.0",
        description="改写章节",
    )
    report = validate_contract_document(data)
    # DraftSpec cannot express Knowledge/Examples ⇒ they surface, not fabricate.
    assert not report.ok
    assert any("knowledge" in e for e in report.errors)
    assert any("examples" in e for e in report.errors)


def test_skill_contract_adapter_maps_procedure_and_tools():
    from forgeflow.skills.contracts import SkillContract

    contract = SkillContract(
        goal="改写章节",
        procedure=["定位", "改写"],
        tools=["docx_edit"],
        policies=["保持格式"],
        inputs={"doc": "path"},
    )
    data = seven_segments_from_skill_contract(
        contract, slug="docx-section-rewrite", display_name="章节改写", version="1.0.0"
    )
    assert data["procedure"]["steps"] == ["定位", "改写"]
    assert data["tool_bindings"]["tools"] == ["docx_edit"]
    assert data["policies"]["constraints"] == ["保持格式"]


# --------------------------------------------------------------------------- #
# JSON Schema snapshot evidence                                                #
# --------------------------------------------------------------------------- #
def test_contract_json_schema_matches_snapshot():
    assert _SCHEMA_SNAPSHOT.exists(), f"缺少契约 JSON Schema 快照：{_SCHEMA_SNAPSHOT}"
    committed = json.loads(_SCHEMA_SNAPSHOT.read_text(encoding="utf-8"))
    assert committed == contract_json_schema()


def test_report_serialises():
    report = validate_contract_document(_valid_contract())
    payload = report.to_dict()
    assert payload["ok"] is True
    assert len(payload["present_segments"]) == 7
    assert isinstance(report, ContractValidationReport)


def test_skill_contract_validation_error_carries_report():
    with pytest.raises(SkillContractValidationError) as exc:
        require_valid_contract(_valid_contract(examples=None))
    assert not exc.value.report.ok
    assert any("examples" in e for e in exc.value.report.errors)
