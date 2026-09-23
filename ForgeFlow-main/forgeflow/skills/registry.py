"""SkillRegistry — register / search / select skills (docs §2 ⑦)."""

from __future__ import annotations

from typing import Any

from forgeflow.repositories import get_skill_repository
from forgeflow.skills.models import SkillRecord, SkillVersionRecord

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

    async def select(self, tenant_id: str | None, intent: str, k: int = 3) -> list[SkillRecord]:
        """Pick the top-``k`` published skills whose name/domain matches ``intent``.

        Keyword-based (no embedding dependency) so agent runtimes can inject
        ``ctx.available_skills`` offline.
        """
        skills, _ = await self.list_skills(tenant_id, limit=200)
        published = [s for s in skills if s.status == "published"]
        needle = (intent or "").lower()
        tokens = [t for t in needle.replace("，", " ").replace(",", " ").split() if t]

        def score(skill: SkillRecord) -> int:
            haystack = f"{skill.name} {skill.domain} {skill.description}".lower()
            return sum(1 for token in tokens if token in haystack)

        ranked = sorted(published, key=lambda s: (score(s), s.usage_count), reverse=True)
        return ranked[:k]

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
