"""INC46 T07 — the seven-segment skill contract (Pydantic v2).

The contract
------------
A :class:`SkillContractDocument` is the reviewable, spec-aligned shape every
materialised skill must satisfy. It has exactly seven segments — Manifest,
Knowledge, Procedure, Policies, Tool bindings, Evaluation, Examples — whose
names and SKILL.md destinations are the authoritative T18 mapping
(:mod:`forgeflow.skills.spec_mapping`). This module turns that mapping into
enforceable Pydantic models and a per-field validation report.

Failure behaviour (失败表现)
---------------------------
Validation never collapses to a vague "参数错误". Every problem is a
per-field / per-segment message naming the concrete field and reason:

* a **missing required segment** (e.g. Examples, Knowledge) is an explicit
  error that says so and refuses to fabricate it (禁止静默补全);
* an **illegal field** (a ``name`` that is not a spec ``slug``, an empty
  ``description``, an over-long ``compatibility``, an unknown key, …) is
  reported at ``<segment>.<field>`` with the concrete rule that broke;
* **body volume** over the recommended size produces a *finding* only — the
  content is never truncated and never becomes an error (T08 consumes the
  finding signal).

Relationship to the existing contract layer (无破坏，纯加法)
----------------------------------------------------------
* :class:`forgeflow.skills.draft_spec.DraftSpec` — the compiler's four-element
  draft. :func:`seven_segments_from_draft_spec` adapts it into a *partial*
  contract (the spec-only segments Knowledge / Evaluation / Examples cannot be
  expressed by a draft, so they are left empty and surface as honest errors
  until authored — never invented).
* :class:`forgeflow.skills.contracts.SkillContract` — the INC43 review
  dataclass. :func:`seven_segments_from_skill_contract` adapts it the same way.
  Neither existing type, nor any other module, is modified.
* :meth:`SkillContractDocument.to_contract_dict` projects the contract into the
  exact dict shape :func:`forgeflow.skills.spec_mapping.render_skill_md`
  consumes, so T11/T30 render a SKILL.md from one source of truth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from forgeflow.skills import segments as _seg
from forgeflow.skills.segments import (
    SEGMENT_EVALUATION,
    SEGMENT_EXAMPLES,
    SEGMENT_KNOWLEDGE,
    SEGMENT_MANIFEST,
    SEGMENT_POLICIES,
    SEGMENT_PROCEDURE,
    SEGMENT_TOOL_BINDINGS,
)
from forgeflow.skills.spec_validator import (
    COMPATIBILITY_MAX,
    validate_description,
    validate_skill_name,
)

__all__ = [
    "SkillContractValidationError",
    "ManifestSegment",
    "KnowledgeReference",
    "KnowledgeSegment",
    "ProcedureSegment",
    "PoliciesSegment",
    "ToolBindingsSegment",
    "EvaluationSegment",
    "ExamplesSegment",
    "SkillContractDocument",
    "ContractValidationReport",
    "validate_contract_document",
    "require_valid_contract",
    "contract_json_schema",
    "seven_segments_from_draft_spec",
    "seven_segments_from_skill_contract",
]

#: A tools/scripts entry must be a single token (frontmatter joins with spaces).
_TOKEN_RE = re.compile(r"^\S+$")


def _raise_value_error(messages: list[str]) -> None:
    """Raise one ValueError carrying every concrete rule that broke."""
    if messages:
        raise ValueError("；".join(messages))


# --------------------------------------------------------------------------- #
# segment models                                                               #
# --------------------------------------------------------------------------- #
class _SegmentModel(BaseModel):
    """Shared base: forbid unknown fields, trim whitespace for emptiness checks."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ManifestSegment(_SegmentModel):
    """Manifest — the identity / metadata segment (→ SKILL.md frontmatter, T18).

    ``name`` is the ASCII **slug** (≤64, lowercase letters/digits/hyphen, no
    leading/trailing or consecutive hyphen, must equal the parent directory
    name, A3: system-generated and locked). The display name and version live in
    ``metadata.display_name`` and ``metadata.version`` (a **string**) — never in
    ``name``.
    """

    name: str = Field(..., min_length=1, description="ASCII slug（≤64，A3）")
    description: str = Field(..., min_length=1, description="做什么 + 何时用，1–1024")
    display_name: str = Field(..., min_length=1, description="任意语言显示名")
    version: str = Field(..., min_length=1, description="版本字符串（红线 6）")
    license: str | None = None
    compatibility: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    #: Optional cross-check target: the skill's parent directory name (T18 rule).
    parent_dir: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        # Parent-dir equality is checked in the model validator (it needs both).
        errors = [e for e in validate_skill_name(value) if "父目录" not in e]
        _raise_value_error(errors)
        return value

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        _raise_value_error(validate_description(value))
        return value

    @field_validator("compatibility")
    @classmethod
    def _validate_compatibility(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("compatibility 如提供，必须是非空字符串")
        if len(value) > COMPATIBILITY_MAX:
            raise ValueError(
                f"compatibility 长度 {len(value)} 超过 {COMPATIBILITY_MAX} 字符上限"
            )
        return value

    @field_validator("allowed_tools")
    @classmethod
    def _validate_allowed_tools(cls, value: list[str]) -> list[str]:
        for tool in value:
            if not _TOKEN_RE.match(str(tool)):
                raise ValueError(
                    f"allowed-tools 项 {tool!r} 必须是单个 token（空格分隔声明）"
                )
        return value

    @model_validator(mode="after")
    def _check_parent_dir(self) -> "ManifestSegment":
        if self.parent_dir:
            errors = [
                e for e in validate_skill_name(self.name, parent_dir=self.parent_dir)
                if "父目录" in e
            ]
            _raise_value_error(errors)
        return self


class KnowledgeReference(_SegmentModel):
    """One Knowledge entry → a ``references/*.md`` file (one level deep)."""

    file: str = Field(..., min_length=1)
    summary: str = ""


class KnowledgeSegment(_SegmentModel):
    """Knowledge — domain docs materialised under ``references/`` (渐进披露)."""

    references: list[KnowledgeReference] = Field(..., min_length=1)


class ProcedureSegment(_SegmentModel):
    """Procedure — the ordered steps rendered into the SKILL.md body."""

    steps: list[str] = Field(..., min_length=1)

    @field_validator("steps")
    @classmethod
    def _non_empty_steps(cls, value: list[str]) -> list[str]:
        for step in value:
            if not str(step).strip():
                raise ValueError("procedure.steps 不能含空步骤")
        return value


class PoliciesSegment(_SegmentModel):
    """Policies — body「约束」semantics + ``allowed-tools`` **declaration only**.

    The ``allowed_tools`` here is a declaration; permission enforcement stays in
    ForgeFlow's four-level permission code (A12) — this model grants nothing.
    """

    constraints: list[str] = Field(..., min_length=1)
    allowed_tools: list[str] = Field(default_factory=list)

    @field_validator("constraints")
    @classmethod
    def _non_empty_constraints(cls, value: list[str]) -> list[str]:
        for item in value:
            if not str(item).strip():
                raise ValueError("policies.constraints 不能含空条目")
        return value

    @field_validator("allowed_tools")
    @classmethod
    def _valid_allowed_tools(cls, value: list[str]) -> list[str]:
        for tool in value:
            if not _TOKEN_RE.match(str(tool)):
                raise ValueError(
                    f"policies.allowed_tools 项 {tool!r} 必须是单个 token"
                )
        return value


class ToolBindingsSegment(_SegmentModel):
    """Tool bindings — the declared tools plus ``scripts/`` references.

    At least one binding (a tool or a script) is required. Script references
    must stay one level deep from ``SKILL.md`` (deeper nesting is a finding).
    """

    tools: list[str] = Field(default_factory=list)
    scripts: list[str] = Field(default_factory=list)

    @field_validator("tools")
    @classmethod
    def _valid_tools(cls, value: list[str]) -> list[str]:
        for tool in value:
            if not _TOKEN_RE.match(str(tool)):
                raise ValueError(f"tool_bindings.tools 项 {tool!r} 必须是单个 token")
        return value

    @model_validator(mode="after")
    def _require_one_binding(self) -> "ToolBindingsSegment":
        if not self.tools and not self.scripts:
            raise ValueError(
                "tool_bindings 至少需声明一个工具或一个 scripts/ 脚本"
            )
        return self


class EvaluationSegment(_SegmentModel):
    """Evaluation — not covered by the upstream spec.

    Materialises under ``assets/evals/`` (or a ``metadata`` reference); the
    **authoritative** evaluation record always lives in the ForgeFlow DB.
    """

    ref: str = Field(..., min_length=1, description="assets/evals/ 或 metadata 引用")
    note: str = ""


class ExamplesSegment(_SegmentModel):
    """Examples — a few inline examples, or ``references/examples.md``."""

    examples: list[str] = Field(..., min_length=1)

    @field_validator("examples")
    @classmethod
    def _non_empty_examples(cls, value: list[str]) -> list[str]:
        for item in value:
            if not str(item).strip():
                raise ValueError("examples.examples 不能含空示例")
        return value


# --------------------------------------------------------------------------- #
# the document                                                                 #
# --------------------------------------------------------------------------- #
class SkillContractDocument(_SegmentModel):
    """The seven-segment contract.

    Each segment is optional at *construction* time so a partial contract can be
    built and completed (see :mod:`forgeflow.skills.contract_completion`);
    completeness is enforced by :func:`validate_contract_document` against
    :data:`forgeflow.skills.segments.REQUIRED_SEGMENTS`.
    """

    manifest: ManifestSegment | None = None
    knowledge: KnowledgeSegment | None = None
    procedure: ProcedureSegment | None = None
    policies: PoliciesSegment | None = None
    tool_bindings: ToolBindingsSegment | None = None
    evaluation: EvaluationSegment | None = None
    examples: ExamplesSegment | None = None

    # --- introspection ----------------------------------------------------- #
    def present_segments(self) -> list[str]:
        """Canonical keys of the segments actually present (in segment order)."""
        return [s for s in _seg.SEGMENT_ORDER if getattr(self, s) is not None]

    def missing_segments(self) -> list[str]:
        """Required segments still absent from this document."""
        return [s for s in _seg.REQUIRED_SEGMENTS if getattr(self, s) is None]

    # --- projection -------------------------------------------------------- #
    def to_contract_dict(self) -> dict[str, Any]:
        """Project into the dict shape ``spec_mapping.render_skill_md`` consumes.

        ``allowed-tools`` unions the Tool-bindings tools with the Policies
        declarations (declaration only, A12). The body lists (procedure /
        policies / tool_bindings / examples) and the knowledge/evaluation blocks
        map 1:1 onto T18's renderer.
        """
        manifest = self.manifest
        bindings = self.tool_bindings
        policies = self.policies
        knowledge = self.knowledge
        examples = self.examples
        evaluation = self.evaluation

        allowed_tools = _ordered_unique(
            (bindings.tools if bindings else []) + (policies.allowed_tools if policies else [])
        )
        tool_binding_lines: list[str] = []
        if bindings:
            tool_binding_lines += [f"工具：{t}" for t in bindings.tools]
            tool_binding_lines += [
                f"脚本：{_seg.normalize_reference(s, default_prefix='scripts/')}"
                for s in bindings.scripts
            ]
        return {
            "manifest": {
                "display_name": manifest.display_name if manifest else "",
                "slug": manifest.name if manifest else "",
                "version": manifest.version if manifest else "",
                "description": manifest.description if manifest else "",
                "license": (manifest.license if manifest else None),
                "compatibility": (manifest.compatibility if manifest else None),
                "allowed_tools": allowed_tools,
            },
            "procedure": list(self.procedure.steps) if self.procedure else [],
            "policies": list(policies.constraints) if policies else [],
            "tool_bindings": tool_binding_lines,
            "examples": list(examples.examples) if examples else [],
            "knowledge": (
                [{"file": r.file, "summary": r.summary} for r in knowledge.references]
                if knowledge
                else []
            ),
            "evaluation": (
                {"ref": evaluation.ref, "note": evaluation.note} if evaluation else {}
            ),
        }


def _ordered_unique(items: list[str]) -> list[str]:
    """De-duplicate while preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = str(item)
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


# --------------------------------------------------------------------------- #
# validation report                                                            #
# --------------------------------------------------------------------------- #
@dataclass
class ContractValidationReport:
    """Per-field outcome of validating one seven-segment contract.

    ``ok`` reflects **errors only**; ``findings`` are the advisory
    progressive-disclosure signals (T08 consumes them). ``document`` is the
    built contract when validation succeeded, else ``None``.
    """

    errors: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    document: SkillContractDocument | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "findings": list(self.findings),
            "present_segments": self.document.present_segments() if self.document else [],
            "missing_segments": self.document.missing_segments() if self.document else [],
        }


class SkillContractValidationError(ValueError):
    """Raised by :func:`require_valid_contract` with the full per-field report."""

    def __init__(self, report: ContractValidationReport) -> None:
        self.report = report
        super().__init__("；".join(report.errors) or "契约校验失败")


def _format_pydantic_errors(exc: ValidationError) -> list[str]:
    """Render a Pydantic error list as ``<segment>.<field>: <reason>`` strings."""
    out: list[str] = []
    for err in exc.errors():
        location = ".".join(str(part) for part in err.get("loc", ()))
        message = str(err.get("msg", ""))
        if message.startswith("Value error, "):
            message = message[len("Value error, ") :]
        out.append(f"{location}: {message}" if location else message)
    return out


def _document_findings(norm: Mapping[str, Any]) -> list[str]:
    """Advisory findings (body volume + reference depth) from a raw contract."""
    findings: list[str] = list(_seg.body_volume_findings(_seg.contract_body_text(norm)))

    refs: list[str] = []
    knowledge = norm.get(SEGMENT_KNOWLEDGE)
    if isinstance(knowledge, Mapping):
        for item in knowledge.get("references") or []:
            file = item.get("file") if isinstance(item, Mapping) else item
            refs.append(_seg.normalize_reference(file))
    bindings = norm.get(SEGMENT_TOOL_BINDINGS)
    if isinstance(bindings, Mapping):
        for script in bindings.get("scripts") or []:
            refs.append(_seg.normalize_reference(script, default_prefix="scripts/"))
    findings.extend(_seg.reference_depth_findings(refs))
    return findings


def validate_contract_document(
    data: Mapping[str, Any] | None,
    *,
    parent_dir: str | None = None,
) -> ContractValidationReport:
    """Validate a seven-segment contract and return a per-field report.

    Steps: (1) normalise segment keys; (2) report every missing required
    segment by name (never fabricating it); (3) validate present segments via
    the Pydantic models (per-field errors); (4) attach advisory findings.

    ``parent_dir`` is threaded into the Manifest so the ``name == parent
    directory`` rule is enforced when the caller knows the directory.
    """
    if not isinstance(data, Mapping):
        return ContractValidationReport(
            errors=["契约必须是键值映射（对象），得到 " f"{type(data).__name__}"]
        )

    errors: list[str] = []
    norm: dict[str, Any] = {}
    for key, value in data.items():
        norm[_seg.normalise_segment_key(key)] = value

    # (2) missing required segments — explicit, named, never fabricated.
    for segment in _seg.REQUIRED_SEGMENTS:
        if norm.get(segment) is None:
            errors.append(
                f"缺少必需段 {segment}（{_seg.segment_title(segment)}）："
                "禁止静默补全，请显式提供该段内容"
            )

    # (3) per-field validation of the present segments.
    to_validate = dict(norm)
    manifest = to_validate.get(SEGMENT_MANIFEST)
    if parent_dir and isinstance(manifest, Mapping) and not manifest.get("parent_dir"):
        to_validate[SEGMENT_MANIFEST] = {**manifest, "parent_dir": parent_dir}

    document: SkillContractDocument | None = None
    try:
        document = SkillContractDocument.model_validate(to_validate)
    except ValidationError as exc:
        errors.extend(_format_pydantic_errors(exc))

    # (4) advisory findings (never errors).
    findings = _document_findings(norm)

    return ContractValidationReport(errors=errors, findings=findings, document=document)


def require_valid_contract(
    data: Mapping[str, Any] | None,
    *,
    parent_dir: str | None = None,
) -> SkillContractDocument:
    """Validate and return the document, or raise with the full report.

    Raises:
        SkillContractValidationError: the contract has one or more errors; the
            exception carries the complete :class:`ContractValidationReport`.
    """
    report = validate_contract_document(data, parent_dir=parent_dir)
    if not report.ok or report.document is None:
        raise SkillContractValidationError(report)
    return report.document


def contract_json_schema() -> dict[str, Any]:
    """The JSON Schema of the seven-segment contract.

    This is the snapshot T07 commits as evidence (``docs/inc46/
    skill_contract_schema.json``); a test regenerates it and asserts it matches,
    so an accidental shape change is caught.
    """
    return SkillContractDocument.model_json_schema()


# --------------------------------------------------------------------------- #
# adapters from the existing contract layer (pure additions, no modification)  #
# --------------------------------------------------------------------------- #
def seven_segments_from_draft_spec(
    draft_spec: Mapping[str, Any] | None,
    *,
    slug: str = "",
    display_name: str = "",
    version: str = "",
    description: str = "",
) -> dict[str, Any]:
    """Lift a compiler ``DraftSpec`` dict into a **partial** seven-segment dict.

    Maps ``steps → procedure.steps``, ``tools → tool_bindings.tools`` and the
    ``applicable_when`` conditions onto the manifest description fallback. A
    ``DraftSpec`` cannot express Knowledge / Evaluation / Examples, and it is
    **never invented** here: those segments are left empty (and therefore
    reported as missing by :func:`validate_contract_document` until authored).
    """
    spec = dict(draft_spec or {})
    steps = [str(s) for s in (spec.get("steps") or []) if str(s)]
    tools = [str(t) for t in (spec.get("tools") or []) if str(t)]
    return {
        SEGMENT_MANIFEST: {
            "name": slug,
            "display_name": display_name,
            "version": version,
            "description": description or str(spec.get("prompt", "") or ""),
        },
        SEGMENT_PROCEDURE: {"steps": steps},
        SEGMENT_TOOL_BINDINGS: {"tools": tools},
        SEGMENT_KNOWLEDGE: {"references": []},
        SEGMENT_POLICIES: {"constraints": []},
        SEGMENT_EVALUATION: {"ref": "assets/evals/", "note": ""},
        SEGMENT_EXAMPLES: {"examples": []},
    }


def seven_segments_from_skill_contract(
    contract: Any,
    *,
    slug: str = "",
    display_name: str = "",
    version: str = "",
) -> dict[str, Any]:
    """Lift an INC43 :class:`contracts.SkillContract` into a **partial** contract.

    Maps ``procedure → procedure.steps``, ``tools → tool_bindings.tools`` and
    ``policies → policies.constraints``. As with the DraftSpec adapter, the
    spec-only segments are left empty rather than fabricated.
    """
    procedure = [str(s) for s in (getattr(contract, "procedure", []) or []) if str(s)]
    tools = [str(t) for t in (getattr(contract, "tools", []) or []) if str(t)]
    policies = [str(p) for p in (getattr(contract, "policies", []) or []) if str(p)]
    return {
        SEGMENT_MANIFEST: {
            "name": slug,
            "display_name": display_name,
            "version": version,
            "description": str(getattr(contract, "goal", "") or ""),
        },
        SEGMENT_PROCEDURE: {"steps": procedure},
        SEGMENT_TOOL_BINDINGS: {"tools": tools},
        SEGMENT_POLICIES: {"constraints": policies},
        SEGMENT_KNOWLEDGE: {"references": []},
        SEGMENT_EVALUATION: {"ref": "assets/evals/", "note": ""},
        SEGMENT_EXAMPLES: {"examples": []},
    }
