"""Shared dependencies for the hub routers."""

from __future__ import annotations

from fastapi import Depends

from forgeflow.api.dependencies import get_workspace_id
from forgeflow.config import get_settings


async def resolve_tenant(workspace_id: str | None = Depends(get_workspace_id)) -> str:
    """Resolve the request tenant, defaulting to ``Settings.default_tenant_id``.

    Mirrors the explicit-parameter-first rule (docs §10.5): the JWT workspace
    claim wins; otherwise we fall back to the configured default tenant.
    """
    return workspace_id or get_settings().default_tenant_id
