"""Resource Center domain models (INC25 W1).

Five resource kinds share one record shape (``ResourceRecord``); only the
``locator`` differs by kind. The ``summary`` carries the real, unit-testable
facts a user needs to judge whether a resource can support the task
(rows / columns / fields / data-quality for tables; chars / pages for text).

Honesty rules baked into these models (design §10 — "不得编造"):

  * ``ResourceSummary.keywords`` is ``None`` when there is **no real keyword
    source**, and :meth:`ResourceSummary.to_dict` then **omits** the key
    entirely (never an empty list placeholder). AC-5.
  * ``ResourceSummary.rows`` / ``columns`` / ``chars`` / ``pages`` are
    ``None`` when not measured — never a fabricated ``0``.
  * ``ResourceRecord.status`` uses only the resource-status vocabulary
    ``registered`` / ``parsed`` / ``metadata_only`` / ``ignored`` /
    ``unavailable``; ``metadata_only`` / ``ignored`` are degradation, not success.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from forgeflow.repositories.base import new_id

__all__ = [
    "ResourceKind",
    "RESOURCE_KINDS",
    "RESOURCE_STATUSES",
    "FILE_STATUSES",
    "FileLocator",
    "DatabaseLocator",
    "CodeLocator",
    "KbLocator",
    "ApiLocator",
    "ResourceSummary",
    "ResourceRecord",
]


class ResourceKind(StrEnum):
    """The five first-class task resource kinds (design §4)."""

    FILE = "file"
    DATABASE = "database"
    GIT_REPO = "git_repo"
    KNOWLEDGE_BASE = "knowledge_base"
    API = "api"


#: Canonical order used by the API/list views (stable, addressable per kind).
RESOURCE_KINDS: tuple[ResourceKind, ...] = (
    ResourceKind.FILE,
    ResourceKind.DATABASE,
    ResourceKind.GIT_REPO,
    ResourceKind.KNOWLEDGE_BASE,
    ResourceKind.API,
)

#: The only statuses a resource may carry (design §10). ``parsed`` means a real
#: summary was produced; ``metadata_only`` / ``ignored`` mean a dependency was
#: missing and the content was NOT parsed (degradation, never success).
RESOURCE_STATUSES: tuple[str, ...] = (
    "registered",
    "parsed",
    "metadata_only",
    "ignored",
    "unavailable",
)

#: File-resource statuses (a subset that also explains why a file was not parsed).
FILE_STATUSES: frozenset[str] = frozenset({"parsed", "metadata_only", "ignored"})


def resource_id() -> str:
    """A readable, collision-free resource id: ``res_<uuid4>``."""
    return f"res_{new_id()}"


@dataclass
class FileLocator:
    """Where a file resource's bytes live (+ their real, checkable fingerprint)."""

    filename: str = ""
    bytes: int = 0
    sha256: str = ""
    mime: str = ""
    storage_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DatabaseLocator:
    """A registered database table (offline profile has no real warehouse)."""

    table: str = ""
    backend_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CodeLocator:
    """A registered code source (GitHub/GitLab repo, local path, or ZIP)."""

    source_type: str = ""      # github | gitlab | local_path | zip
    identifier: str = ""       # repo full name / filesystem path / file name
    branch: str = ""
    files_count: int = 0
    languages: list[str] = field(default_factory=list)
    reachable: bool = True
    detail: str = ""           # verbatim failure reason when not reachable / read

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class KbLocator:
    """A registered knowledge-base resource."""

    kb_id: str = ""
    scope: str = ""
    reachable: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApiLocator:
    """A registered API connector resource."""

    connector: str = ""
    base_url: str = ""
    reachable: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResourceSummary:
    """The real, mechanically-checkable facts about a resource's content.

    All numeric fields are ``None`` when NOT measured. ``keywords`` is ``None``
    when there is no real keyword source and is then omitted from the wire form.
    """

    kind: str = ""
    rows: int | None = None
    columns: int | None = None
    fields: list[str] = field(default_factory=list)
    chars: int | None = None
    pages: int | None = None
    keywords: list[str] | None = None
    quality: dict[str, Any] = field(default_factory=dict)
    stub: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Wire form. ``keywords`` is emitted ONLY when there is a real source."""
        data: dict[str, Any] = {
            "kind": self.kind,
            "rows": self.rows,
            "columns": self.columns,
            "fields": list(self.fields),
            "chars": self.chars,
            "pages": self.pages,
            "quality": dict(self.quality),
            "stub": self.stub,
            "note": self.note,
        }
        if self.keywords:
            data["keywords"] = list(self.keywords)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ResourceSummary":
        """Rebuild a summary from its wire form (round-trip for the PG backend)."""
        d = dict(data or {})
        raw_keywords = d.get("keywords")
        keywords = [str(k) for k in raw_keywords] if isinstance(raw_keywords, list) else None
        return cls(
            kind=str(d.get("kind") or ""),
            rows=d.get("rows"),
            columns=d.get("columns"),
            fields=[str(f) for f in (d.get("fields") or [])],
            chars=d.get("chars"),
            pages=d.get("pages"),
            keywords=keywords,
            quality=dict(d.get("quality") or {}),
            stub=bool(d.get("stub", False)),
            note=str(d.get("note") or ""),
        )


@dataclass
class ResourceRecord:
    """One registered resource (mirrors a ``resources`` row)."""

    id: str = field(default_factory=resource_id)
    tenant_id: str | None = None
    kind: str = ResourceKind.FILE.value
    name: str = ""
    created_by: str = ""
    created_at: str = ""
    status: str = "registered"
    detail: str = ""
    summary: ResourceSummary = field(default_factory=ResourceSummary)
    locator: Any = field(default_factory=dict)

    @property
    def parsed(self) -> bool:
        """True only when the resource content was really parsed (status parsed)."""
        return self.status == "parsed"

    def _locator_dict(self) -> dict[str, Any]:
        if hasattr(self.locator, "to_dict"):
            return self.locator.to_dict()
        return dict(self.locator or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "kind": str(self.kind),
            "name": self.name,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "status": self.status,
            "detail": self.detail,
            "summary": self.summary.to_dict(),
            "locator": self._locator_dict(),
            "parsed": self.parsed,
        }
