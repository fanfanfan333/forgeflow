"""Request/response schemas for the Resource Center API (INC25 W1).

Kept in a dedicated module (rather than widening ``api/hub_schemas.py``) so the
existing hub schemas and their consumers stay byte-identical — the same additive
discipline ``api/resource_schemas.py`` mirrors from ``api/hub_schemas.py``'s own
docstring.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ResourceResponse(BaseModel):
    """One resource, verbatim from ``ResourceRecord.to_dict()``."""

    id: str
    tenant_id: str | None = None
    kind: str
    name: str
    created_by: str = ""
    created_at: str
    status: str
    detail: str = ""
    summary: dict[str, Any] = Field(default_factory=dict)
    locator: dict[str, Any] = Field(default_factory=dict)
    #: True only when the content was really parsed (status == "parsed").
    parsed: bool = False


class ResourceListResponse(BaseModel):
    total: int
    items: list[ResourceResponse] = Field(default_factory=list)


class ResourceDeleteResponse(BaseModel):
    """Result of ``DELETE /resources/{id}`` (INC40).

    A real JSON body (never 204 / empty): the SPA's ``request<T>`` wrapper calls
    ``res.json()`` on any 2xx, and an empty body would raise ``SyntaxError`` and
    masquerade a successful delete as a failure.
    """

    deleted: bool
    resource_id: str


class ResourcePreviewResponse(BaseModel):
    """A content preview (first N rows / lines). Honest empty state when N/A."""

    id: str
    kind: str = ""
    available: bool = False
    format: str = "none"           # table | text | none
    columns: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    content: str = ""
    truncated: bool = False
    note: str = ""


class ResourceLimitsResponse(BaseModel):
    """Read-only upload limits + type whitelist (INC26 T01 / P0-2).

    Both facts are resolved **live** from the backend's single source of truth —
    ``Settings.multimodal_max_bytes`` (the size ceiling) and
    ``resources/summaries.SUPPORTED_FILE_EXTENSIONS`` (the type whitelist) — never
    from a frozen literal and never duplicated on the front-end. ``note`` states
    that provenance so a consumer cannot mistake these for hard-coded values.
    """

    max_bytes: int
    supported_extensions: list[str] = Field(default_factory=list)
    note: str = ""


# --------------------------------------------------------------------------- #
# Registration request bodies (one per kind → each independently addressable)  #
# --------------------------------------------------------------------------- #
class DatabaseResourceRequest(BaseModel):
    table: str = Field(..., min_length=1, description="Registered table name (explicit).")
    name: str = Field("", description="Optional display name (defaults to the table).")


class CodeResourceRequest(BaseModel):
    """A code source: GitHub/GitLab repo, local path, or ZIP (optional branch)."""

    source_type: str = Field(
        ..., description="github | gitlab | local_path | zip"
    )
    identifier: str = Field("", description="owner/repo | filesystem path | file name")
    repository: str = Field("", description="Alias for identifier (repo sources).")
    path: str = Field("", description="Alias for identifier (local_path source).")
    branch: str = Field("", description="Optional branch / ref (repo sources).")
    filename: str = Field("", description="Original file name (zip source).")
    zip_base64: str = Field("", description="Base64 archive bytes (zip source).")


class KnowledgeBaseResourceRequest(BaseModel):
    kb_id: str = Field(..., min_length=1)
    scope: str = Field("", description="Optional knowledge-base scope / namespace.")


class ApiResourceRequest(BaseModel):
    connector: str = Field(..., min_length=1)
    base_url: str = Field("", description="Base URL of the registered connector.")
