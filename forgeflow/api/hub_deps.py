"""Authenticated tenant resolution; no membership-free default fallback."""

from typing import Any

from fastapi import Depends, HTTPException

from forgeflow.api.dependencies import get_current_user, get_workspace_id
from forgeflow.auth.store import get_auth_store
from forgeflow.config import get_settings
from forgeflow.rbac.models import UserContext


async def resolve_tenant(
    workspace_id: str | None = Depends(get_workspace_id),
    user: UserContext = Depends(get_current_user),
    store: Any = Depends(get_auth_store),
) -> str:
    tenant = workspace_id or get_settings().default_tenant_id
    identity = await store.get_by_username(user.user_id)
    if (
        identity is None
        or identity["disabled"]
        or not await store.is_member(str(identity["id"]), tenant)
    ):
        raise HTTPException(status_code=403, detail="not a member of the requested workspace")
    return tenant
