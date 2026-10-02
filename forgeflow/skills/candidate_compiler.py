"""Skill Candidate Compiler — the "missing link" of the closed loop (docs §2.1).

Compiles ≥N similar ``Experience`` records into a reusable skill draft
(prompt / steps / tools / io_schema / applicable_when) and persists a
``SkillCandidate`` together with its ``candidate_experience`` N:M provenance.

Draft generation is rule-based by default so it works with no LLM; when a real
provider is configured a structured-output LLM path can refine it (kept behind
``_llm_draft`` and only attempted when ``LLM_PROVIDER != mock``).
"""

from __future__ import annotations

import json
import math
from collections import Counter
from typing import Any

from forgeflow.config import get_settings
from forgeflow.repositories import get_experience_repository, get_skill_candidate_repository
from forgeflow.skills.draft_spec import DraftSpec
from forgeflow.skills.models import SkillCandidateRecord

# Fallback tools used when experiences carry no reusable steps.
_FALLBACK_TOOLS = ["research.search", "data.query", "report.render"]
_FALLBACK_STEPS = ["检索上下文", "执行任务", "生成结论"]


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _tools_from(experiences: list[Any]) -> list[str]:
    counter: Counter[str] = Counter()
    for exp in experiences:
        for step in getattr(exp, "reusable_steps", []) or []:
            tool = step.get("tool") if isinstance(step, dict) else None
            if tool:
                counter[str(tool)] += 1
    if not counter:
        return list(_FALLBACK_TOOLS)
    return [tool for tool, _ in counter.most_common()]


def _normalise_tool_ids(tools: list[str] | None) -> list[str]:
    """Keep only **real platform tool ids** from a declared ``tools`` list.

    A draft's ``tools`` element must name tools the platform can actually
    dispatch: ``skills/trust_baseline.verify_trust_baseline`` fails closed on
    anything outside the platform catalogue — that check is correct and stays.
    The problem is **who** violates it: an LLM asked for 「工具名」 answers with
    natural-language phrases (observed with the real ``qwen3:8b`` provider:
    ``["文本理解", "信息筛选"]``), which turned every LLM-drafted candidate into a
    permanent 403 at promote time — the Skill loop could never close.

    So the compiler guarantees the invariant instead: the LLM's output is
    normalised against the catalogue, unknown entries are **dropped** (never
    silently rewritten into a different tool — that would be fabrication), and
    order + de-duplication are preserved.
    """
    from forgeflow.skills.trust_baseline import allowed_tool_set

    catalogue = allowed_tool_set()
    out: list[str] = []
    for tool in tools or []:
        name = str(tool or "").strip()
        if name and name in catalogue and name not in out:
            out.append(name)
    return out


def _reconcile_draft_tools(draft: DraftSpec, experiences: list[Any]) -> DraftSpec:
    """Force a draft's ``tools`` onto the platform catalogue (see above).

    Falls back to the experiences' **real** tool usage (``_tools_from`` reads
    ``reusable_steps[].tool``, i.e. tools that really ran, else
    ``_FALLBACK_TOOLS``) when nothing the draft declared is a platform tool — so
    ``tools`` is never left empty, which ``DraftSpec.is_complete`` would reject
    (and would make the candidate un-promotable all over again).
    """
    tools = _normalise_tool_ids(draft.tools)
    if not tools:
        tools = _normalise_tool_ids(_tools_from(experiences)) or list(_FALLBACK_TOOLS)
    draft.tools = tools
    return draft


def _steps_from(experiences: list[Any], tools: list[str], threshold: int) -> list[str]:
    """Steps = tool sequence ordered by first appearance, filtered by frequency."""
    counter: Counter[str] = Counter()
    order: list[str] = []
    for exp in experiences:
        for step in getattr(exp, "reusable_steps", []) or []:
            tool = step.get("tool") if isinstance(step, dict) else None
            label = step.get("note") if isinstance(step, dict) else None
            key = str(tool or label or "")
            if not key:
                continue
            counter[key] += 1
            if key not in order:
                order.append(key)
    kept = [tool for tool in order if counter[tool] >= threshold]
    if not kept:
        kept = order[: max(1, len(order))] or list(_FALLBACK_STEPS)
    return kept


def _infer_io_schema(experiences: list[Any]) -> dict[str, Any]:
    """Infer an input/output JSON schema from the cluster's step payloads."""
    input_keys: set[str] = set()
    output_keys: set[str] = set()
    for exp in experiences:
        for step in getattr(exp, "reusable_steps", []) or []:
            if not isinstance(step, dict):
                continue
            for key in ("input", "input_data"):
                value = step.get(key)
                if isinstance(value, dict):
                    input_keys.update(str(k) for k in value)
            for key in ("output", "output_data"):
                value = step.get(key)
                if isinstance(value, dict):
                    output_keys.update(str(k) for k in value)
    if not input_keys:
        input_keys = {"intent", "context"}
    if not output_keys:
        output_keys = {"summary", "outcome"}
    return {
        "input": {key: "string" for key in sorted(input_keys)},
        "output": {key: "string" for key in sorted(output_keys)},
    }


def _domain_of(experiences: list[Any]) -> str:
    for exp in experiences:
        for tag in getattr(exp, "tags", []) or []:
            if tag:
                return str(tag)
    return "general"


def _build_rule_draft(experiences: list[Any], threshold: int) -> DraftSpec:
    """Deterministic, dependency-free draft spec (offline fallback)."""
    domain = _domain_of(experiences)
    summaries = [getattr(exp, "summary", "") for exp in experiences if getattr(exp, "summary", "")]
    decisions: list[str] = []
    for exp in experiences:
        for decision in getattr(exp, "decisions", []) or []:
            if isinstance(decision, dict):
                text = decision.get("reason") or decision.get("detail") or decision.get("decision")
                if text:
                    decisions.append(str(text))

    prompt_lines = [
        f"为「{domain}」类任务生成可复用技能。",
        f"目标：{summaries[0] if summaries else '完成同类任务并输出结论'}",
    ]
    if decisions:
        prompt_lines.append("关键决策：" + "；".join(decisions[:3]))
    prompt = "\n".join(prompt_lines)

    tools = _tools_from(experiences)
    steps = _steps_from(experiences, tools, threshold)
    tags: list[str] = []
    for exp in experiences:
        for tag in getattr(exp, "tags", []) or []:
            if tag and str(tag) not in tags:
                tags.append(str(tag))

    return DraftSpec(
        prompt=prompt,
        steps=steps,
        tools=tools,
        io_schema=_infer_io_schema(experiences),
        applicable_when={
            "domain": domain,
            "tags": tags,
            "min_experiences": len(experiences),
        },
    )


# JSON-schema hint passed to Ollama's native structured-output mode. ChatOllama
# forwards a dict ``format`` straight to Ollama's /api/chat, which constrains the
# decoder to emit matching JSON.
_DRAFT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "string"}},
        "tools": {"type": "array", "items": {"type": "string"}},
        "io_schema": {"type": "object"},
        "applicable_when": {"type": "object"},
    },
    "required": ["prompt", "steps", "tools", "io_schema"],
}

_DRAFT_JSON_INSTRUCTION = (
    "只输出一个 JSON 对象（不要解释、不要 markdown 代码块），字段："
    "prompt(string 提示词)、steps(string 数组 步骤)、tools(string 数组 **平台工具 id**)、"
    "io_schema(对象，含 input 与 output)。"
)


def _coerce_draft(value: Any) -> DraftSpec | None:
    """Normalise a provider's structured-output result into a ``DraftSpec``."""
    if isinstance(value, DraftSpec):
        return value
    if isinstance(value, dict):
        return DraftSpec.from_dict(value)
    if hasattr(value, "model_dump"):
        try:
            return DraftSpec.from_dict(dict(value.model_dump()))
        except Exception:  # noqa: BLE001 — best-effort coercion
            return None
    return None


def _extract_json(text: str | None) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model reply (tolerates ``` fences)."""
    if not text:
        return None
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`")
        if body[:4].lower() == "json":
            body = body[4:]
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(body[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _structured_draft(model: Any, prompt: str) -> DraftSpec | None:
    """Ask ``model`` for a ``DraftSpec`` using the most reliable path available.

    Tried in order, each guarded so a provider/version quirk degrades instead
    of aborting the compile:
      1. native structured output (Ollama tool-calling / JSON-schema);
      2. Ollama JSON-schema mode (``format`` = schema dict) — the path that works
         for local models, whose ``with_structured_output`` binding is not
         reliable across langchain-ollama/langchain-core versions;
      3. a plain prompt asking for JSON, parsed leniently.
    """
    # 1) provider-native structured output.
    try:
        draft = _coerce_draft(model.with_structured_output(DraftSpec).invoke(prompt))
        if draft is not None and draft.is_complete():
            return draft
    except Exception:  # noqa: BLE001
        pass

    # 2) Ollama JSON-schema mode.
    try:
        message = model.bind(format=_DRAFT_JSON_SCHEMA).invoke(prompt)
        draft = DraftSpec.from_dict(_extract_json(getattr(message, "content", "")) or {})
        if draft.is_complete():
            return draft
    except Exception:  # noqa: BLE001
        pass

    # 3) plain prompt + lenient JSON parse.
    try:
        message = model.invoke(f"{prompt}\n{_DRAFT_JSON_INSTRUCTION}")
        draft = DraftSpec.from_dict(_extract_json(getattr(message, "content", "")) or {})
        if draft.is_complete():
            return draft
    except Exception:  # noqa: BLE001
        pass

    return None


def _llm_draft(experiences: list[Any], threshold: int) -> DraftSpec | None:
    """Optional LLM refinement. Returns ``None`` when unavailable."""
    settings = get_settings()
    if settings.llm_provider.lower() in ("", "mock"):
        return None
    try:
        from forgeflow.models import get_model  # lazy — avoids import-time LLM deps
        from forgeflow.skills.trust_baseline import allowed_tool_set

        model = get_model(strong=True)
        context = "\n".join(getattr(e, "summary", "") for e in experiences[:5])
        # The catalogue is stated **verbatim** so a compliant model can succeed
        # first time; `_reconcile_draft_tools` is the guarantee for the rest.
        catalogue = ", ".join(sorted(allowed_tool_set()))
        return _structured_draft(
            model,
            "把以下经验编译为技能草稿：\n"
            f"{context}\n\n"
            "tools 字段**只能**从下列平台工具 id 中原样照抄（不要翻译、不要自造、"
            f"不要填中文描述）：{catalogue}",
        )
    except Exception:  # noqa: BLE001 — any failure falls back to the rule path
        return None


async def compile_candidate(
    tenant_id: str | None,
    experience_ids: list[str] | None = None,
    mode: str = "auto",
    *,
    candidate_repo: Any | None = None,
    experience_repo: Any | None = None,
) -> SkillCandidateRecord:
    """Compile a ``SkillCandidate`` from similar experiences.

    Returns a record with ``status="insufficient"`` (not persisted) when fewer
    than ``SKILL_CANDIDATE_MIN_EXPERIENCES`` similar experiences are found.
    """
    settings = get_settings()
    min_experiences = settings.skill_candidate_min_experiences
    threshold = settings.skill_candidate_similarity_threshold

    exp_repo = experience_repo or get_experience_repository()
    cand_repo = candidate_repo or get_skill_candidate_repository()

    cluster: list[Any] = []
    similarities: list[float] = []
    seed = None

    if mode == "manual" and experience_ids:
        for exp_id in experience_ids:
            found = await exp_repo.get(tenant_id, exp_id)
            if found is not None:
                cluster.append(found)
        seed = cluster[0] if cluster else None
    else:
        all_experiences = await exp_repo.list(tenant_id, limit=200)
        if all_experiences:
            seed = all_experiences[0]
            cluster = [seed]
            scored = await exp_repo.find_similar(
                tenant_id,
                seed.embedding,
                k=max(min_experiences, 10),
                min_similarity=threshold,
                tags=seed.tags,
            )
            for record, sim in scored:
                if record.id == seed.id:
                    continue
                cluster.append(record)
                similarities.append(sim)

    if len(cluster) < min_experiences:
        return SkillCandidateRecord(
            tenant_id=tenant_id,
            name=f"{_domain_of(cluster)} 技能候选" if cluster else "技能候选",
            domain=_domain_of(cluster) if cluster else "general",
            experience_ids=[getattr(e, "id", "") for e in cluster],
            similarity_score=round(_avg(similarities), 4),
            status="insufficient",
            draft_spec={},
        )

    avg_similarity = _avg(similarities) if similarities else 1.0
    threshold_count = max(1, math.ceil(len(cluster) / 2))
    draft = _llm_draft(cluster, threshold_count) or _build_rule_draft(cluster, threshold_count)
    # INC-AUDIT —— 无论草稿来自 LLM 还是规则，`tools` 都必须是平台真实工具 id，
    # 否则发布链路会在可信基线处 403（见 `_normalise_tool_ids`）。
    draft = _reconcile_draft_tools(draft, cluster)
    domain = _domain_of(cluster)

    candidate = SkillCandidateRecord(
        tenant_id=tenant_id,
        name=f"{domain} 技能候选",
        domain=domain,
        experience_ids=[getattr(e, "id", "") for e in cluster],
        draft_spec=draft.to_dict(),
        similarity_score=round(avg_similarity, 4),
        status="draft",
    )
    await cand_repo.save_candidate(candidate)
    for record, sim in zip(cluster, [1.0] + similarities):
        await cand_repo.link_experience(tenant_id, candidate.id, record.id, float(sim))
    return candidate


async def compile_candidate_or_raise(
    tenant_id: str | None,
    experience_ids: list[str] | None = None,
    mode: str = "auto",
    **kwargs: Any,
) -> SkillCandidateRecord:
    """Like ``compile_candidate`` but raises on insufficient input."""
    from forgeflow.skills.errors import InsufficientExperiencesError

    result = await compile_candidate(tenant_id, experience_ids, mode, **kwargs)
    if result.status == "insufficient":
        raise InsufficientExperiencesError(
            found=len(result.experience_ids),
            required=get_settings().skill_candidate_min_experiences,
        )
    return result
