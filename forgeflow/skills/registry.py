"""SkillRegistry — register / search / select skills (docs §2 ⑦)."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Mapping, Sequence

from forgeflow.config import get_settings
from forgeflow.repositories import get_skill_repository
from forgeflow.skills.canary import should_serve
from forgeflow.skills.models import SkillRecord, SkillVersionRecord
from forgeflow.skills.retrieval import RetrievedSkill, retrieve_skills

logger = logging.getLogger(__name__)

_FEATURED_SEED: list[dict[str, Any]] = [
    {
        "name": "客户流失分析",
        "domain": "数据分析",
        "description": "基于历史行为识别客户流失风险并给出挽留建议。",
        "tools": ["data.query", "analysis.score"],
        "steps": ["拉取客户行为数据", "计算流失概率", "生成挽留建议"],
        "usage_count": 128,
        "featured": True,
        "status": "published",
    },
    {
        "name": "合同审查",
        "domain": "企业知识库",
        "description": "自动识别合同风险点，生成审查报告。",
        "tools": ["docs.parse", "policy.check"],
        "steps": ["解析合同文本", "比对风险条款", "生成审查报告"],
        "usage_count": 96,
        "featured": True,
        "status": "published",
    },
    {
        "name": "项目复盘总结",
        "domain": "项目管理",
        "description": "基于项目文档与时间线生成复盘总结报告。",
        "tools": ["docs.parse", "report.render"],
        "steps": ["聚合项目文档", "提取关键事件", "生成复盘总结"],
        "usage_count": 74,
        "featured": True,
        "status": "published",
    },
    {
        "name": "代码质量检查",
        "domain": "代码开发",
        "description": "分析代码仓库问题，提供优化建议。",
        "tools": ["git.diff", "code.lint"],
        "steps": ["拉取代码变更", "静态检查", "生成优化建议"],
        "usage_count": 52,
        "featured": True,
        "status": "published",
    },
]


class SkillRegistry:
    """Thin domain service over the ``SkillRepository``."""

    def __init__(self, repo: Any | None = None) -> None:
        self._repo = repo

    def _repo_or_default(self) -> Any:
        return self._repo or get_skill_repository()

    async def create(
        self,
        tenant_id: str | None,
        name: str,
        domain: str,
        *,
        owner: str | None = None,
        description: str = "",
        status: str = "draft",
        tags: list[str] | None = None,
    ) -> SkillRecord:
        skill = SkillRecord(
            tenant_id=tenant_id,
            name=name,
            domain=domain,
            owner=owner,
            description=description,
            status=status,
            tags=list(tags or []),
        )
        return await self._repo_or_default().create_skill(skill)

    async def get(self, tenant_id: str | None, skill_id: str) -> SkillRecord | None:
        return await self._repo_or_default().get_skill(tenant_id, skill_id)

    async def get_by_name(self, tenant_id: str | None, name: str) -> SkillRecord | None:
        return await self._repo_or_default().get_skill_by_name(tenant_id, name)

    async def list_skills(
        self,
        tenant_id: str | None,
        *,
        domain: str | None = None,
        q: str | None = None,
        featured: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[SkillRecord], int]:
        return await self._repo_or_default().list_skills(
            tenant_id, domain=domain, q=q, featured=featured, limit=limit, offset=offset
        )

    async def versions(self, tenant_id: str | None, skill_id: str) -> list[SkillVersionRecord]:
        return await self._repo_or_default().list_versions(tenant_id, skill_id)

    async def select(
        self, tenant_id: str | None, intent: str, k: int = 3, *, seed: str | None = None
    ) -> list[SkillRecord]:
        """Pick the top-``k`` published skills whose name/domain matches ``intent``.

        Keyword-based (no embedding dependency) so agent runtimes can inject
        ``ctx.available_skills`` offline.

        INC9 B1 — controlled canary exposure also lives here (the *selection*
        layer). ``should_serve`` is consulted for every selected skill; with the
        default ``skill_canary_traffic_pct=0`` it always returns ``False``, so the
        returned ranking is byte-for-byte the pre-INC9 behaviour. When an operator
        raises the percentage, a deterministic ``sha1(seed)`` slice of requests
        sees a skill's canary version (a per-request *copy* — the stored skill is
        never mutated). ``seed`` defaults to the intent, so a caller without a
        request id still gets a deterministic, replayable decision.
        """
        skills, _ = await self.list_skills(tenant_id, limit=200)
        published = [s for s in skills if s.status == "published"]
        needle = (intent or "").lower()
        tokens = [t for t in needle.replace("，", " ").replace(",", " ").split() if t]

        def score(skill: SkillRecord) -> int:
            haystack = f"{skill.name} {skill.domain} {skill.description}".lower()
            return sum(1 for token in tokens if token in haystack)

        ranked = sorted(published, key=lambda s: (score(s), s.usage_count), reverse=True)
        return await self._apply_canary_exposure(
            tenant_id, ranked[:k], seed=seed if seed is not None else intent
        )

    async def retrieve(
        self,
        tenant_id: str | None,
        intent: str,
        k: int = 3,
        *,
        permissions: Mapping[str, Sequence[Any]] | None = None,
        required_tools: Sequence[str] | None = None,
        max_class: str | None = None,
        seed: str | None = None,
    ) -> list[RetrievedSkill]:
        """INC46 T09 — the full hybrid retrieval chain (see ``skills/retrieval.py``).

        ``Query -> Hybrid Retrieval -> RRF -> Capability Filter -> Rerank -> Top-K``，
        且 Tenant / RBAC / 状态闸在**候选池构造阶段**先行（增补 v3「权限先于一切」）。

        与 :meth:`select` 的关系（裁定 V-3 / V-5）:

        * :meth:`select` 的既有行为**一行未动**（关键词打分 + usage + canary 灰度），
          该方法另开一条链路，不改变任何既有调用方的召回结果；
        * 候选池与 :meth:`select` 同口径 —— 只取 ``status == "published"``，
          避免把未发布 / 归档技能注入上下文（保持既有召回语义，裁定 V-5）；
        * 权限与能力数据**懒加载**：``permissions`` / ``required_tools`` /
          ``max_class`` 都为 ``None`` 时不去读 ``skill_permissions`` 表，
          检索退化为「混合召回 + RRF + Rerank」，零额外 I/O。

        诚实纪律
        --------
        * 权限 / spec 仓储不可用时（如 memory backend 尚未实现该表）**降级放行**
          并记 warning，**不静默假装过滤过**；此时 :meth:`retrieve` 的过滤是空操作，
          这一点如实写在日志里，不声称已生效。
        * 相似度是**真实算出来的**稠密余弦；算不出来为 ``None``（红线 4：未测量 ⇒ None）。

        Returns:
            Top-K 命中，含每个分量的分值（见 ``retrieval.RetrievedSkill``）。
        """
        skills, _ = await self.list_skills(tenant_id, limit=200)
        published = [s for s in skills if s.status == "published"]

        perms = permissions
        if perms is None:
            perms = await self._permission_map(tenant_id)
        specs = None
        if required_tools or max_class:
            specs = await self._spec_map(tenant_id, published)

        hits = retrieve_skills(
            tenant_id,
            intent,
            published,
            permissions=perms,
            specs=specs,
            k=k,
            required_tools=required_tools,
            max_class=max_class,
        )
        exposed = await self._apply_canary_exposure(
            tenant_id, [hit.skill for hit in hits], seed=seed if seed is not None else intent
        )
        # 灰度分流只换「指向的版本」，不改变召回与分值（T34 在此接入，T09 不实现分流）。
        return [replace(hit, skill=skill) for hit, skill in zip(hits, exposed)]

    async def _permission_map(self, tenant_id: str | None) -> Mapping[str, Sequence[Any]] | None:
        """``skill_id -> [SkillPermission]``，或 ``None``（仓储不可用 ⇒ 降级放行）。

        ``None`` 与 ``{}`` 语义不同，必须区分：
        ``None`` = 权限数据**取不到**（过滤是空操作，记 warning）；
        ``{}`` = 权限数据**取到了，且没有任何声明**（按未声明限制放行）。
        """
        try:
            from forgeflow.repositories import get_skill_schema_repository

            rows = await get_skill_schema_repository().list_permissions(tenant_id, limit=500)
        except Exception as exc:  # noqa: BLE001 — 取不到权限不得拖垮召回
            logger.warning("skill retrieval: permission lookup unavailable (%s) — filter is a no-op", exc)
            return None
        grouped: dict[str, list[Any]] = {}
        for row in rows:
            grouped.setdefault(str(getattr(row, "skill_id", "")), []).append(row)
        return grouped

    async def _spec_map(self, tenant_id: str | None, skills: Sequence[SkillRecord]) -> dict[str, Mapping[str, Any]]:
        """``skill_id -> spec``（当前版本的 spec），取不到时为该 skill 记空 spec。

        仅在调用方给出 ``required_tools`` / ``max_class`` 时才被调用（懒加载）。
        """
        specs: dict[str, Mapping[str, Any]] = {}
        for skill in skills:
            try:
                versions = await self._repo_or_default().list_versions(tenant_id, skill.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("skill retrieval: spec lookup failed for %s (%s)", skill.id, exc)
                specs[str(skill.id)] = {}
                continue
            if not versions:
                specs[str(skill.id)] = {}
                continue
            current = next(
                (v for v in versions if getattr(v, "semver", None) == skill.current_version),
                versions[-1],
            )
            specs[str(skill.id)] = getattr(current, "spec", None) or {}
        return specs

    async def _apply_canary_exposure(
        self, tenant_id: str | None, skills: list[SkillRecord], *, seed: str
    ) -> list[SkillRecord]:
        """Swap in canary versions for the deterministic exposed fraction.

        Pure selection helper wired into :meth:`select`. At the default
        ``traffic_pct=0`` ``should_serve`` is always ``False`` ⇒ the input list is
        returned unchanged (no extra repository reads). Only when a skill is both
        selected for exposure *and* actually has a ``release_state="canary"``
        version is a copy returned with ``current_version`` pointing at it.
        """
        pct = int(getattr(get_settings(), "skill_canary_traffic_pct", 0) or 0)
        exposed: list[SkillRecord] = []
        for skill in skills:
            # INC46 T34 (additive 流量解析): a live staged rollout supersedes the
            # static global pct for THIS skill; ``None`` ⇒ keep the historical pct.
            staged = self.staged_rollout_pct(tenant_id, skill.id)
            effective_pct = pct if staged is None else staged
            if not should_serve(f"{seed}:{skill.id}", effective_pct):
                exposed.append(skill)
                continue
            canary = await self._canary_view(tenant_id, skill)
            exposed.append(canary if canary is not None else skill)
        return exposed

    async def _canary_view(
        self, tenant_id: str | None, skill: SkillRecord
    ) -> SkillRecord | None:
        """A *copy* of ``skill`` pointing at its canary version, or ``None``.

        Returns ``None`` when the skill has no canary version (or it is already
        current), so the caller keeps the original object. Never mutates the
        stored skill.
        """
        try:
            versions = await self._repo_or_default().list_versions(tenant_id, skill.id)
        except Exception:  # noqa: BLE001 — exposure must never break selection
            return None
        canary = next(
            (
                v
                for v in versions
                if getattr(v, "release_state", "promoted") == "canary"
            ),
            None,
        )
        if canary is None or canary.semver == skill.current_version:
            return None
        return replace(skill, current_version=canary.semver)

    def staged_rollout_pct(self, tenant_id: str | None, skill_id: str) -> int | None:
        """INC46 T34 — the live staged-rollout exposure for one skill (additive).

        Returns the candidate's **current stage** percentage (5 / 25 / 100) when
        the tenant has an *enabled*, in-flight canary rollout for ``skill_id``;
        otherwise ``None`` — the caller then keeps the historical static
        ``skill_canary_traffic_pct``, so with no rollout running the selection
        layer is byte-for-byte unchanged.

        Read-only and never raises (版本流量解析 must not break selection).
        """
        try:
            from forgeflow.rollout.controller import rollout_enabled
            from forgeflow.rollout.store import STATE_CANARY, get_rollout_store

            if not rollout_enabled(tenant_id):
                return None
            rollout = get_rollout_store().latest_rollout(tenant_id, skill_id)
            if rollout is None or rollout.state != STATE_CANARY:
                return None
            return int(rollout.stage_pct)
        except Exception:  # noqa: BLE001 — exposure must never break selection
            return None

    async def bump_usage(self, tenant_id: str | None, skill_id: str) -> None:
        skill = await self.get(tenant_id, skill_id)
        if skill is not None:
            skill.usage_count += 1
            await self._repo_or_default().update_skill(skill)

    async def set_status(
        self, tenant_id: str | None, skill_id: str, status: str
    ) -> SkillRecord | None:
        skill = await self.get(tenant_id, skill_id)
        if skill is None:
            return None
        skill.status = status
        return await self._repo_or_default().update_skill(skill)

    async def seed_featured(self, tenant_id: str | None) -> list[SkillRecord]:
        """Idempotently ensure the four built-in featured skills exist.

        Called by the factory when ``STORAGE_BACKEND=memory`` so the home page
        has real data to render in the offline demo (G3: ≥4 built-in skills).
        """
        repo = self._repo_or_default()
        created: list[SkillRecord] = []
        for seed in _FEATURED_SEED:
            existing = await repo.get_skill_by_name(tenant_id, seed["name"])
            if existing is not None:
                created.append(existing)
                continue
            skill = SkillRecord(
                tenant_id=tenant_id,
                name=seed["name"],
                domain=seed["domain"],
                description=seed["description"],
                status=seed["status"],
                featured=seed["featured"],
                usage_count=seed["usage_count"],
                tags=[seed["domain"]],
            )
            await repo.create_skill(skill)
            version = SkillVersionRecord(
                tenant_id=tenant_id,
                skill_id=skill.id,
                semver="1.0.0",
                spec={
                    "prompt": f"执行「{seed['name']}」并输出结论",
                    "steps": seed["steps"],
                    "tools": seed["tools"],
                    "io_schema": {
                        "input": {"intent": "string"},
                        "output": {"summary": "string"},
                    },
                    "applicable_when": {"domain": seed["domain"]},
                },
                changelog="[1.0.0] 初始内置技能",
                approved_by="system",
            )
            await repo.add_version(tenant_id, version)
            skill.current_version = "1.0.0"
            await repo.update_skill(skill)
            created.append(skill)
        return created
