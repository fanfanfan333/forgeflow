"""015 - INC25 W1 Resource Center — the ``resources`` table.

Additive-only, single new table. Implements docs/sop/INC25-DESIGN.md §3.1 item
13. One tenant-scoped table backs all five resource kinds (file / database /
git_repo / knowledge_base / api) because they share one record shape
(``forgeflow/resources/models.py::ResourceRecord``): only ``summary`` and
``locator`` differ by kind, and both are open JSONB so a new kind or a new
locator field never needs a schema change.

Column choices:
  * ``id`` / ``tenant_id`` / ``created_at`` are ``TEXT``. ``tenant_id`` is TEXT
    by the post-``013`` hub convention (an opaque tenant id, never silently
    dropped to NULL). ``created_at`` is stored as an ISO-8601 string so both
    backends round-trip it byte-for-byte with no timezone drift.
  * ``summary`` / ``locator`` are ``JSONB`` (queried as opaque documents).
  * ``status`` carries the resource-status vocabulary (design §10):
    ``registered`` / ``parsed`` / ``metadata_only`` / ``ignored`` /
    ``unavailable``. ``metadata_only`` / ``ignored`` are honest degradation, not
    success — the value is stored verbatim.

Re-entrancy: every statement uses ``IF NOT EXISTS``, so ``upgrade head`` is
idempotent on a fresh and a populated database alike, and running it twice
(INC25 T01 acceptance) is a no-op the second time. Revision ``014`` and earlier
are untouched. Importing this module opens no connection — it only declares the
upgrade/downgrade bodies.

Revision ID: 015
Revises: 014
Create Date: 2026-10-02
"""

from alembic import op

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS resources (
            id          TEXT PRIMARY KEY,
            tenant_id   TEXT,
            kind        VARCHAR(32) NOT NULL,
            name        TEXT        NOT NULL DEFAULT '',
            created_by  TEXT        NOT NULL DEFAULT '',
            created_at  TEXT        NOT NULL,
            status      VARCHAR(32) NOT NULL DEFAULT 'registered',
            detail      TEXT        NOT NULL DEFAULT '',
            summary     JSONB       NOT NULL DEFAULT '{}',
            locator     JSONB       NOT NULL DEFAULT '{}'
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_resources_tenant_time "
        "ON resources (tenant_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_resources_tenant_kind "
        "ON resources (tenant_id, kind)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS resources")
