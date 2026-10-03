"""INC46 T18 — SKILL.md official-spec validation (agentskills.io 规范对齐).

Validates a ``SKILL.md`` document against the upstream Agent Skills
specification (snapshot: ``docs/inc46/skill_md_spec_snapshot.md``). Every
message names the concrete field and the concrete reason — a validation
failure must be actionable, never a vague "参数错误" (失败表现).

Hard rules (errors)
-------------------
* ``name`` — required; 1–64 chars; ASCII lowercase letters / digits / hyphens
  only; no leading or trailing hyphen; no consecutive hyphens; must equal the
  parent directory name (when the caller supplies it). A Chinese display name
  used directly as ``name`` is rejected with an explicit "provide an ASCII
  slug" reason (A3).
* ``description`` — required; 1–1024 chars; should state what the skill does
  and when to use it (the what/when wording is guidance, not machine-checkable;
  the length/non-emptiness rules are enforced).
* ``compatibility`` — optional; ≤500 chars when present.
* ``metadata`` — optional; a map of string keys to string values.
* ``allowed-tools`` — optional; a space-separated string (experimental,
  **declaration only** — enforcement stays in ForgeFlow's four-level
  permission code, A12).

Progressive-disclosure findings (findings, never silent truncation)
-------------------------------------------------------------------
* body over the recommended 500 lines / ~5000 tokens ⇒ a *finding* (the
  document is still returned whole — nothing is ever silently cut);
* file references nested deeper than one level below ``SKILL.md`` ⇒ a finding.

skills-ref (A5)
---------------
The upstream ``skills-ref validate`` binary is **not installed** in this
environment, so :func:`validate_with_skills_ref` reports
``{"status": "skipped", "passed": None}`` with the explicit reason — it never
fabricates a PASS (红线 10). The call point is pluggable: the day the binary
exists, the same function runs it for real.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "NAME_MAX",
    "DESCRIPTION_MAX",
    "COMPATIBILITY_MAX",
    "BODY_LINE_RECOMMENDED_MAX",
    "BODY_TOKEN_RECOMMENDED_MAX",
    "SkillValidationReport",
    "validate_skill_name",
    "validate_description",
    "validate_frontmatter",
    "parse_skill_md",
    "validate_skill_md",
    "skills_ref_path",
    "validate_with_skills_ref",
]

NAME_MAX = 64
DESCRIPTION_MAX = 1024
COMPATIBILITY_MAX = 500

#: Progressive-disclosure recommendations (spec: "Keep your main SKILL.md
#: under 500 lines", "< 5000 tokens recommended"). Exceeding them is a
#: finding, never a hard error and never a silent truncation.
BODY_LINE_RECOMMENDED_MAX = 500
BODY_TOKEN_RECOMMENDED_MAX = 5000

#: File references must stay one level deep from SKILL.md (spec §File references).
_REFERENCE_RE = re.compile(r"(?:scripts|references|assets)/[^\s)\]\"']+")


@dataclass
class SkillValidationReport:
    """The outcome of validating one SKILL.md document.

    ``ok`` reflects *errors only* — findings are advisory (progressive
    disclosure), reported honestly instead of being silently enforced.
    """

    errors: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "findings": list(self.findings),
        }


# --------------------------------------------------------------------------- #
# field-level rules                                                            #
# --------------------------------------------------------------------------- #
def validate_skill_name(name: Any, *, parent_dir: str | None = None) -> list[str]:
    """Validate the ``name`` frontmatter field (spec §name field).

    Each rule gets its own message so the author learns *which* convention
    broke: length / uppercase or illegal characters / leading-trailing hyphen /
    consecutive hyphens / parent-directory mismatch. A non-ASCII value (e.g. a
    Chinese display name) is rejected with the explicit slug requirement (A3).
    """
    if not isinstance(name, str) or not name.strip():
        return ["name 为必填字段且不能为空"]
    value = name.strip()
    errors: list[str] = []
    if len(value) > NAME_MAX:
        errors.append(f"name 长度 {len(value)} 超过 {NAME_MAX} 字符上限")
    if not value.isascii():
        errors.append(
            "name 仅允许 ASCII 小写字母、数字与连字符；"
            f"'{value}' 含非 ASCII 字符——中文显示名不能直接作为 name，"
            "请显式提供 ASCII slug（A3：slug 系统生成并锁定，改名不变目录名）"
        )
    else:
        if value != value.lower():
            errors.append("name 不得包含大写字母（仅允许小写字母、数字与连字符）")
        illegal = sorted({ch for ch in value if not (ch.islower() or ch.isdigit() or ch == "-")})
        if illegal:
            errors.append(
                f"name 含非法字符 {' '.join(illegal)}（仅允许小写字母、数字与连字符 -）"
            )
    if value.startswith("-"):
        errors.append("name 不得以连字符开头")
    if value.endswith("-"):
        errors.append("name 不得以连字符结尾")
    if "--" in value:
        errors.append("name 不得包含连续连字符（--）")
    if parent_dir is not None and value != parent_dir:
        errors.append(f"name '{value}' 必须与父目录名 '{parent_dir}' 一致")
    return errors


def validate_description(description: Any) -> list[str]:
    """Validate the ``description`` frontmatter field (spec §description field)."""
    if not isinstance(description, str) or not description.strip():
        return [
            "description 为必填字段且不能为空"
            "（应写明「做什么」与「何时用」，并含助于识别的关键词）"
        ]
    value = description.strip()
    if len(value) > DESCRIPTION_MAX:
        errors_msg = (
            f"description 长度 {len(value)} 超过 {DESCRIPTION_MAX} 字符上限"
        )
        return [errors_msg]
    return []


def validate_frontmatter(
    frontmatter: Any, *, parent_dir: str | None = None
) -> list[str]:
    """Validate a parsed frontmatter mapping (required + optional fields)."""
    if not isinstance(frontmatter, dict):
        return ["frontmatter 必须是 YAML 对象（键值映射）"]
    errors = validate_skill_name(frontmatter.get("name"), parent_dir=parent_dir)
    errors += validate_description(frontmatter.get("description"))

    compatibility = frontmatter.get("compatibility")
    if compatibility is not None:
        if not isinstance(compatibility, str) or not compatibility.strip():
            errors.append("compatibility 如提供，必须是非空字符串")
        elif len(compatibility.strip()) > COMPATIBILITY_MAX:
            errors.append(
                f"compatibility 长度 {len(compatibility.strip())} "
                f"超过 {COMPATIBILITY_MAX} 字符上限"
            )

    metadata = frontmatter.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict) or any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in metadata.items()
        ):
            errors.append("metadata 必须是字符串键 → 字符串值的映射")

    license_ = frontmatter.get("license")
    if license_ is not None and not isinstance(license_, str):
        errors.append("license 必须是字符串（许可证名或随附许可文件名）")

    allowed_tools = frontmatter.get("allowed-tools")
    if allowed_tools is not None and not isinstance(allowed_tools, str):
        errors.append(
            "allowed-tools 必须是空格分隔的字符串（实验性字段，仅声明；"
            "权限强制在 ForgeFlow 四级权限代码，A12）"
        )
    return errors


# --------------------------------------------------------------------------- #
# document-level rules                                                         #
# --------------------------------------------------------------------------- #
def parse_skill_md(text: str) -> tuple[dict[str, Any] | None, str, str | None]:
    """Split ``SKILL.md`` into (frontmatter, body, parse_error).

    ``frontmatter`` is ``None`` when the document has no YAML frontmatter
    block; ``parse_error`` carries the concrete reason when the block exists
    but is not a valid YAML mapping.
    """
    import yaml

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text, "缺少 YAML frontmatter（文档必须以 '---' 起始）"
    closing = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing = index
            break
    if closing is None:
        return None, text, "frontmatter 未闭合（缺少结束的 '---'）"
    raw = "\n".join(lines[1:closing])
    body = "\n".join(lines[closing + 1 :]).strip("\n")
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return None, body, f"frontmatter 不是合法 YAML：{exc}"
    if not isinstance(parsed, dict):
        return None, body, "frontmatter 必须是 YAML 对象（键值映射）"
    return parsed, body, None


def _body_findings(body: str) -> list[str]:
    """Progressive-disclosure findings — advisory, never silently enforced."""
    findings: list[str] = []
    line_count = len(body.splitlines()) if body.strip() else 0
    if line_count > BODY_LINE_RECOMMENDED_MAX:
        findings.append(
            f"正文 {line_count} 行，超过建议的 {BODY_LINE_RECOMMENDED_MAX} 行上限"
            "（渐进披露：详细内容应移至 references/，本校验只提示、不截断）"
        )
    # Rough token estimate (≈4 chars/token); reported *as* an estimate.
    token_estimate = len(body) // 4
    if token_estimate > BODY_TOKEN_RECOMMENDED_MAX:
        findings.append(
            f"正文约 {token_estimate} tokens（粗估），超过建议的 "
            f"{BODY_TOKEN_RECOMMENDED_MAX} tokens（渐进披露：拆分至 references/）"
        )
    for match in sorted(set(_REFERENCE_RE.findall(body))):
        remainder = match.split("/", 1)[1]
        if "/" in remainder:
            findings.append(
                f"文件引用 '{match}' 距 SKILL.md 超过一层"
                "（规范要求引用保持一层深，避免嵌套引用链）"
            )
    return findings


def validate_skill_md(text: str, *, parent_dir: str | None = None) -> SkillValidationReport:
    """Validate a full ``SKILL.md`` document against the upstream spec.

    Returns a :class:`SkillValidationReport`; ``ok`` is True only when there
    are no errors. The body is carried verbatim in the report — findings about
    length are advisory and nothing is ever truncated.
    """
    frontmatter, body, parse_error = parse_skill_md(text)
    report = SkillValidationReport(frontmatter=frontmatter or {}, body=body)
    if parse_error is not None:
        report.errors.append(parse_error)
        return report
    report.errors.extend(validate_frontmatter(frontmatter, parent_dir=parent_dir))
    report.findings.extend(_body_findings(body))
    return report


# --------------------------------------------------------------------------- #
# skills-ref (pluggable; honestly skipped when the binary is absent, A5)       #
# --------------------------------------------------------------------------- #
def skills_ref_path() -> str | None:
    """Locate the upstream ``skills-ref`` binary, or ``None`` when absent."""
    return shutil.which("skills-ref")


def validate_with_skills_ref(skill_dir: str) -> dict[str, Any]:
    """Run ``skills-ref validate`` on a skill directory — for real, or skip honestly.

    A5 裁决: the binary is not installed in this environment, so the result is
    ``{"status": "skipped", "passed": None}`` with the explicit reason — never
    a fabricated PASS (红线 10). ``passed`` is ``None`` (unmeasured), not
    ``False``. When the binary exists the command runs for real and the exit
    code decides ``passed``.
    """
    exe = skills_ref_path()
    if exe is None:
        return {
            "status": "skipped",
            "passed": None,
            "reason": "环境中未安装 skills-ref（A5 裁决：记 skipped，不得冒充 PASS）",
            "output": None,
        }
    completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [exe, "validate", str(skill_dir)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    passed = completed.returncode == 0
    return {
        "status": "passed" if passed else "failed",
        "passed": passed,
        "reason": "" if passed else f"skills-ref validate 退出码 {completed.returncode}",
        "output": (completed.stdout + completed.stderr).strip() or None,
    }
