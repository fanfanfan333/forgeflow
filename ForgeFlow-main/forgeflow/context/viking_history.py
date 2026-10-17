"""Revoke deleted source conversations from remote recall while retaining native audit."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from forgeflow.config import get_settings
from forgeflow.context.viking_sync import enqueue

logger = logging.getLogger(__name__)


def revoked_prefixes(settings: Any, tenant: str, user: str) -> list[str]:
    if not getattr(settings, "openviking_outbox_root", None):
        return []
    prefixes = []
    for path in Path(settings.openviking_outbox_root).glob("*.json"):
        job = json.loads(path.read_text(encoding="utf-8"))
        if job["tenant"] != tenant or job["user"] != user or job["status"] == "cancelled":
            continue
        stable = hashlib.sha256(job["id"].encode()).hexdigest()[:32]
        root = f"viking://user/{user}/"
        if job["kind"] == "delete_resource":
            prefixes.append(root + "resources/forgeflow/" + stable + "/")
        elif job["kind"] == "delete_skill":
            prefixes.append(root + "skills/ff-" + stable + "/")
        elif job["kind"] == "delete_episode":
            prefixes.append(root + "memories/forgeflow/episodes/" + stable + "/")
        elif job["kind"] == "delete_run":
            prefixes.extend(
                root + "sessions/" + sid + "/" for sid in job["payload"].get("sessions", [])
            )
    return prefixes


async def revoke_history(tenant: str | None, records: list[Any]) -> None:
    root = Path(get_settings().openviking_outbox_root)
    for record in records:
        user, run_id = record.actor_user_id, record.run_id
        if f"{tenant}:{user}" not in get_settings().openviking_user_keys:
            continue
        try:
            identity = hashlib.sha256(f"{tenant}:{user}:run:{run_id}".encode()).hexdigest()
            path = root / (identity + ".json")
            job = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            stable = hashlib.sha256(run_id.encode()).hexdigest()[:32]
            sessions = {"ff-" + stable}
            if job.get("result", {}).get("session_id"):
                sessions.add(job["result"]["session_id"])
            sessions.update(
                task["resource_id"]
                for task in job.get("failed_tasks", [])
                if task.get("resource_id")
            )
            await enqueue(tenant, user, "delete_run", run_id, {"sessions": sorted(sessions)})
            await enqueue(tenant, user, "delete_episode", run_id, {})
            for artifact in record.artifacts:
                await enqueue(tenant, user, "delete_resource", artifact["id"], {})
        except Exception as exc:
            logger.warning("OpenViking history revocation unavailable: %s", type(exc).__name__)
