"""INC46 T18 — the seven-section contract ↔ SKILL.md mapping (官方规范对齐).

What this module is
-------------------
The Agent Skills official specification (snapshot:
``docs/inc46/skill_md_spec_snapshot.md``, fetched 2026-10-03 from
https://agentskills.io/specification, sha256
``4c649bdf0e0a51c9e215d9f91009ecdca05ee9d073edb6f37c20265bbd829e11``) defines
the *target* format every materialised skill must satisfy. ForgeFlow's internal
skill contract has seven sections (七段契约). :data:`SECTION_MAPPING` is the
single, explicit mapping between them — T07 (契约字段↔frontmatter), T11
(物化导出校验) and T30 (种子物化过检) all consume this table instead of
re-deriving it.

Rulings baked in
----------------
* **A3** — the ``name`` slug is system-generated and **locked**: renaming the
  display name never renames the directory. :func:`suggest_slug` derives a
  spec-compliant slug from an ASCII display name; a display name with no ASCII
  content (e.g. pure Chinese) is refused with an explicit "provide a slug"
  error — never silently transliterated into something unpronounceable.
* **A12** — ``allowed-tools`` is *declarative only* (experimental upstream);
  permission enforcement stays in ForgeFlow's four-level permission code. The
  mapping writes the declaration, it never grants anything.
* **Evaluation is not covered by the upstream spec** — it maps to
  ``assets/evals/`` (or a metadata reference); the authoritative evaluation
  record stays in the ForgeFlow DB.

Drift detection
---------------
:func:`spec_drift_alert` compares a freshly fetched spec hash against the
snapshot hash. A mismatch returns a human-readable **warning** — it never
blocks a publish (the snapshot stays the contractual baseline until a human
reviews the upstream change).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "SPEC_URL",
    "SPEC_MARKDOWN_URL",
    "SNAPSHOT_FETCHED_AT",
    "SNAPSHOT_SHA256",
    "SEVEN_SECTIONS",
    "MappingEntry",
    "SECTION_MAPPING",
    "suggest_slug",
    "render_skill_md",
    "sha256_text",
    "spec_drift_alert",
]

#: Upstream specification provenance (mirrors docs/inc46/skill_md_spec_snapshot.md).
SPEC_URL = "https://agentskills.io/specification"
SPEC_MARKDOWN_URL = "https://agentskills.io/specification.md"
SNAPSHOT_FETCHED_AT = "2026-10-03"
SNAPSHOT_SHA256 = "4c649bdf0e0a51c9e215d9f91009ecdca05ee9d073edb6f37c20265bbd829e11"

#: The seven sections of the ForgeFlow skill contract (七段契约), in order.
SEVEN_SECTIONS: tuple[str, ...] = (
    "manifest",
    "knowledge",
    "procedure",
    "policies",
    "tool_bindings",
    "evaluation",
    "examples",
)


@dataclass(frozen=True)
class MappingEntry:
    """One contract section → its SKILL.md (or skill-directory) destination."""

    section: str
    target: str
    rule: str

    def to_dict(self) -> dict[str, str]:
        return {"section": self.section, "target": self.target, "rule": self.rule}


#: The mapping table (TABLE 20). ``target`` uses the spec's own vocabulary so a
#: reviewer can check each row against the snapshot without translation.
SECTION_MAPPING: tuple[MappingEntry, ...] = (
    MappingEntry(
        "manifest",
        "frontmatter",
        "name=ASCII slug（≤64，A3：系统生成并锁定，改名不变目录名）；"
        "显示名→metadata.display_name；版本→metadata.version（字符串）",
    ),
    MappingEntry(
        "knowledge",
        "references/*.md",
        "知识/领域文档物化为 references/ 下的独立 Markdown，按需加载（渐进披露）",
    ),
    MappingEntry(
        "procedure",
        "body 步骤段",
        "过程步骤写入 SKILL.md 正文「步骤」编号列表",
    ),
    MappingEntry(
        "policies",
        "body「约束」段 + frontmatter allowed-tools",
        "策略/约束写入正文「约束」段；涉及工具白名单的声明进 allowed-tools"
        "（仅声明，强制在四级权限代码，A12）",
    ),
    MappingEntry(
        "tool_bindings",
        "frontmatter allowed-tools + scripts/",
        "工具绑定声明进 allowed-tools（空格分隔）；可执行脚本物化到 scripts/",
    ),
    MappingEntry(
        "evaluation",
        "assets/evals/ 或 metadata 引用",
        "上游规范未涵盖评估；评估集物化到 assets/evals/ 或以 metadata 引用，"
        "权威评估记录始终在 ForgeFlow DB",
    ),
    MappingEntry(
        "examples",
        "body 示例段 或 references/examples.md",
        "少量示例写正文「示例」段；较多时物化 references/examples.md 并在正文引用",
    ),
)

_SLUG_MAX = 64


def suggest_slug(display_name: str) -> str:
    """Derive a spec-compliant ``name`` slug from an ASCII display name.

    The result satisfies every upstream ``name`` rule: lowercase ASCII
    alphanumerics + single hyphens, no leading/trailing hyphen, ≤64 chars.

    Raises:
        ValueError: the display name carries no ASCII content (e.g. pure
            Chinese) — a slug cannot be derived, so the caller must supply one
            explicitly (A3: slugs are system-generated *and locked*; a Chinese
            display name is never used as ``name`` directly).
    """
    base = (
        unicodedata.normalize("NFKD", str(display_name or ""))
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    base = re.sub(r"[^a-z0-9]+", "-", base)
    base = re.sub(r"-{2,}", "-", base).strip("-")
    if not base:
        raise ValueError(
            f"显示名 {display_name!r} 无法派生 ASCII slug：name 仅允许小写字母、"
            "数字与连字符。请显式提供 slug（A3：slug 系统生成并锁定，中文显示名"
            "不能直接作为 name）。"
        )
    if len(base) > _SLUG_MAX:
        base = base[:_SLUG_MAX].rstrip("-")
    return base


def _yaml_frontmatter(fields: dict[str, Any]) -> str:
    """Serialise frontmatter deterministically (keys keep declaration order)."""
    import yaml

    return yaml.safe_dump(
        fields, allow_unicode=True, sort_keys=False, default_flow_style=False
    ).strip()


def render_skill_md(contract: Mapping[str, Any]) -> str:
    """Render a seven-section contract into a spec-compliant ``SKILL.md``.

    Expected shape (every non-manifest section optional):

    .. code-block:: python

        {
          "manifest": {
              "display_name": str,   # 任意语言显示名 → metadata.display_name
              "slug": str,           # 缺省时由 suggest_slug(display_name) 派生
              "version": str,        # → metadata.version（字符串）
              "description": str,    # → frontmatter description（做什么+何时用）
              "license": str,        # 可选
              "compatibility": str,  # 可选（≤500）
              "allowed_tools": [str] # 可选 → allowed-tools（空格分隔，仅声明）
          },
          "procedure": [str, ...],            # → 正文「步骤」
          "policies": [str, ...],             # → 正文「约束」
          "tool_bindings": [str, ...],        # → 正文「工具绑定」（声明）
          "examples": [str, ...],             # → 正文「示例」
          "knowledge": [{"file": str, "summary": str}],   # → references/*.md 索引
          "evaluation": {"ref": str, "note": str},        # → assets/evals/ 引用
        }

    The function only *renders* — spec compliance of the result is proven by
    :func:`forgeflow.skills.spec_validator.validate_skill_md` (T11/T30 call
    both; nothing here self-certifies).
    """
    manifest = dict(contract.get("manifest") or {})
    display_name = str(manifest.get("display_name") or "").strip()
    slug = str(manifest.get("slug") or "").strip() or suggest_slug(display_name)
    version = str(manifest.get("version") or "").strip()

    frontmatter: dict[str, Any] = {
        "name": slug,
        "description": str(manifest.get("description") or ""),
    }
    if manifest.get("license"):
        frontmatter["license"] = str(manifest["license"])
    if manifest.get("compatibility"):
        frontmatter["compatibility"] = str(manifest["compatibility"])
    metadata: dict[str, str] = {}
    if display_name:
        metadata["display_name"] = display_name
    if version:
        metadata["version"] = version
    if metadata:
        frontmatter["metadata"] = metadata
    allowed_tools = [str(t) for t in (manifest.get("allowed_tools") or []) if str(t)]
    if allowed_tools:
        frontmatter["allowed-tools"] = " ".join(allowed_tools)

    body: list[str] = [f"# {display_name or slug}", ""]

    procedure = [str(s) for s in (contract.get("procedure") or []) if str(s)]
    if procedure:
        body.append("## 步骤")
        body.append("")
        body.extend(f"{i}. {step}" for i, step in enumerate(procedure, 1))
        body.append("")

    policies = [str(p) for p in (contract.get("policies") or []) if str(p)]
    if policies:
        body.append("## 约束")
        body.append("")
        body.extend(f"- {policy}" for policy in policies)
        body.append("")

    bindings = [str(b) for b in (contract.get("tool_bindings") or []) if str(b)]
    if bindings:
        body.append("## 工具绑定")
        body.append("")
        body.extend(f"- {binding}" for binding in bindings)
        body.append("")

    examples = [str(e) for e in (contract.get("examples") or []) if str(e)]
    if examples:
        body.append("## 示例")
        body.append("")
        body.extend(f"- {example}" for example in examples)
        body.append("")

    knowledge = [dict(k) for k in (contract.get("knowledge") or []) if isinstance(k, Mapping)]
    if knowledge:
        body.append("## 参考")
        body.append("")
        for item in knowledge:
            file = str(item.get("file") or "").strip()
            summary = str(item.get("summary") or "").strip()
            ref = file if file.startswith("references/") else f"references/{file}"
            body.append(f"- [{summary or file}]({ref})" if summary else f"- {ref}")
        body.append("")

    evaluation = dict(contract.get("evaluation") or {})
    if evaluation:
        ref = str(evaluation.get("ref") or "assets/evals/").strip()
        note = str(evaluation.get("note") or "").strip()
        body.append("## 评估")
        body.append("")
        body.append(
            f"评估集见 {ref}（上游规范未涵盖评估段；权威评估记录在 ForgeFlow DB）。"
        )
        if note:
            body.append(f"{note}")
        body.append("")

    return "---\n" + _yaml_frontmatter(frontmatter) + "\n---\n\n" + "\n".join(body).rstrip() + "\n"


def sha256_text(text: str) -> str:
    """The sha256 hex digest of ``text`` (utf-8) — the drift-comparison currency."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def spec_drift_alert(
    fetched_sha256: str,
    *,
    snapshot_sha256: str = SNAPSHOT_SHA256,
) -> str | None:
    """Compare a freshly fetched spec hash against the snapshot (drift detection).

    Returns ``None`` when the hashes match; otherwise a human-readable
    **warning** naming both hashes. Drift never blocks a publish — the
    snapshot stays the contractual baseline until a human reviews the upstream
    change (hence a string, not an exception).
    """
    if fetched_sha256 == snapshot_sha256:
        return None
    return (
        "规范漂移告警：上游 agentskills.io 规范内容哈希与快照不符"
        f"（快照 {SNAPSHOT_FETCHED_AT} sha256={snapshot_sha256[:12]}…，"
        f"新抓取 sha256={str(fetched_sha256)[:12]}…）。"
        "不阻塞发布；请人工复核规范变更后更新 docs/inc46/skill_md_spec_snapshot.md。"
    )
