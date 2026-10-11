"""Explicit auth-store double for business-route tests issuing local test JWTs.

These tests exercise business contracts, not authentication. Security tests
must use actual users/memberships and never install this dependency override.
"""
import uuid

from fastapi import Depends, FastAPI, Request

from forgeflow.api.dependencies import get_current_user, get_workspace_id
from forgeflow.auth.store import get_auth_store
from forgeflow.config import get_settings
from forgeflow.rbac.models import UserContext


def authenticate_business_app(app: FastAPI) -> None:
    async def store(request: Request, context: UserContext = Depends(get_current_user), workspace_id: str | None = Depends(get_workspace_id)):
        user = context.user_id
        tenant = workspace_id or get_settings().default_tenant_id
        class Store:
            async def get_by_username(self, username: str):
                return {"id": uuid.uuid5(uuid.NAMESPACE_URL, user), "disabled": False} if username == user else None

            async def is_member(self, user_id: str, workspace_id: str) -> bool:
                return user_id == str(uuid.uuid5(uuid.NAMESPACE_URL, user)) and workspace_id == tenant
        return Store()
    app.dependency_overrides[get_auth_store] = store
