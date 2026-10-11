"""Re-index existing registered files and approved current skills, never raw disk."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace


async def main():
    config = json.loads(Path(".runtime/openviking/integration.json").read_text())
    for key, value in config.items():
        os.environ[key] = json.dumps(value) if isinstance(value, dict) else str(value)
    from forgeflow.config import get_settings
    from forgeflow.context.viking_sync import queue_resource, queue_run, queue_skill
    from forgeflow.repositories import get_experience_repository, get_skill_repository
    from forgeflow.resources.service import get_resource_service
    from forgeflow.workspace.store import get_workspace_store

    service = get_resource_service()
    tenants = {identity.partition(":")[0] for identity in get_settings().openviking_user_keys}
    resources = skills = runs = 0
    for tenant in tenants:
        offset = 0
        while rows := await service.list(tenant, limit=100, offset=offset):
            for record in rows:
                if f"{tenant}:{record.created_by}" in get_settings().openviking_user_keys:
                    await queue_resource(service, record)
                    resources += 1
            offset += len(rows)
        repo = get_skill_repository()
        rows, _ = await repo.list_skills(tenant, limit=1000)
        for skill in rows:
            if skill.status not in {"published", "ready", "active"} or not skill.current_version:
                continue
            version = await repo.get_version(tenant, skill.id, skill.current_version)
            if version and version.approved_by:
                await queue_skill(tenant, skill.owner or "", skill, version.spec)
                skills += 1
        experiences = {
            record.run_id: record.id
            for record in await get_experience_repository().list(tenant, limit=10000)
        }
        for record in await get_workspace_store().list_recent_for_tenant(tenant, limit=10000):
            if (
                record.outcome != "success"
                or not record.llm.get("final_answer")
                or f"{tenant}:{record.actor_user_id}" not in get_settings().openviking_user_keys
            ):
                continue
            await queue_run(
                SimpleNamespace(tenant_id=tenant, user_id=record.actor_user_id),
                SimpleNamespace(**record.to_dict(), experience_id=experiences.get(record.run_id)),
            )
            runs += 1
    print(
        json.dumps(
            {
                "registered_resources_queued": resources,
                "approved_skills_queued": skills,
                "successful_historical_runs_queued": runs,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
