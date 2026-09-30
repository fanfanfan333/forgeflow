"""Resource Center package (INC25 W1).

Public surface:

  * the domain models (``ResourceKind`` / ``ResourceRecord`` / ``ResourceSummary``
    / the five locators) — importable without pulling in the service layer;
  * ``ResourceService`` + ``get_resource_service`` — exported lazily via
    :func:`__getattr__` so importing a *model* (e.g. from
    ``forgeflow.repositories.postgres.resource_repo``) never drags in the
    repository factory or the storage layer, which keeps the import graph
    cycle-free.
"""

from __future__ import annotations

from typing import Any

from forgeflow.resources.models import (
    RESOURCE_KINDS,
    RESOURCE_STATUSES,
    ApiLocator,
    CodeLocator,
    DatabaseLocator,
    FileLocator,
    KbLocator,
    ResourceKind,
    ResourceRecord,
    ResourceSummary,
    resource_id,
)

__all__ = [
    "ResourceKind",
    "RESOURCE_KINDS",
    "RESOURCE_STATUSES",
    "FileLocator",
    "DatabaseLocator",
    "CodeLocator",
    "KbLocator",
    "ApiLocator",
    "ResourceSummary",
    "ResourceRecord",
    "resource_id",
    "ResourceService",
    "get_resource_service",
]


def __getattr__(name: str) -> Any:
    """Lazily expose the service layer (avoids an import cycle at package import)."""
    if name in ("ResourceService", "get_resource_service"):
        from forgeflow.resources import service

        return getattr(service, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
