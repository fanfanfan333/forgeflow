"""013 - TenantId contract: ``tenant_id`` / ``team_id`` become opaque ``TEXT``.

The application layer has **always** treated a tenant id as an opaque string —
``RequestContext.tenant_id: str``, the ``"default"`` sentinel
(``Settings.default_tenant_id``), test tenants like ``"t-inc12"``. The hub
tables created by migrations 010/011/012 nevertheless declared ``tenant_id`` /
``team_id`` as ``UUID``. Six repositories plus the marketplace bridge papered
over the mismatch with a private UUID-coercion helper that silently coerced any
non-UUID tenant to ``NULL`` — which is itself a **cross-tenant leak**: two
distinct tenants (``"t-alpha"`` / ``"t-beta"``) both collapsed onto the single
``NULL`` bucket, while the memory backend (which stores the raw string) kept
them apart. The two backends therefore disagreed about isolation, and no test
caught it.

INC12-A2 removes that coercion at the source. This migration makes the storage
type honest so no coercion is needed: the column stores exactly what the
application hands it.

Mechanism — **runtime discovery**, so it is robust to whichever subset of the
010/011/012 tables a given database actually has:

  * find every column named ``tenant_id`` or ``team_id`` whose type is still
    ``uuid``;
  * ``ALTER TABLE <t> ALTER COLUMN <c> TYPE TEXT USING <c>::text``.

``workspace_id`` is deliberately **not** touched: it still references
``workspaces(id)`` (a genuine UUID foreign key) and is outside this contract's
scope. No ``tenant_id`` / ``team_id`` column carries a foreign key, so widening
them cannot orphan a constraint.

Re-entrancy: the ``data_type = 'uuid'`` gate means a second ``upgrade`` finds
nothing to do and is a no-op — on an empty database (fresh ``upgrade head``) and
a populated one alike. The reverse direction mirrors
``007_run_user_id_text``: non-UUID values cannot cast back to ``UUID``, so they
are nulled first (the honest pre-013 semantics, where a non-UUID tenant was
stored as ``NULL``).

Revision ID: 013
Revises: 012
Create Date: 2026-10-01
"""

from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None

# ``information_schema.columns.data_type`` reports ``uuid`` for a ``UUID``
# column. Discovery (rather than a hard-coded table list) keeps the migration
# correct for whatever set of 010/011/012 tables this database actually has.
_UPGRADE_TARGETS = """
SELECT table_name, column_name
FROM information_schema.columns
WHERE table_schema = current_schema()
  AND column_name = ANY (ARRAY['tenant_id', 'team_id'])
  AND data_type = 'uuid'
ORDER BY table_name, column_name
"""

_DOWNGRADE_TARGETS = """
SELECT table_name, column_name
FROM information_schema.columns
WHERE table_schema = current_schema()
  AND column_name = ANY (ARRAY['tenant_id', 'team_id'])
  AND data_type IN ('text', 'character varying')
ORDER BY table_name, column_name
"""

#: Canonical UUID shape — the same pattern 007 uses. Anything that does not
#: match cannot be cast back to ``UUID`` and is nulled in the downgrade.
_UUID_SHAPE = (
    "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    "[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _targets(query: str) -> list[tuple[str, str]]:
    """Return ``(table_name, column_name)`` pairs for ``query``."""
    conn = op.get_bind()
    return [(row[0], row[1]) for row in conn.exec_driver_sql(query).fetchall()]


def upgrade() -> None:
    for table, column in _targets(_UPGRADE_TARGETS):
        op.execute(
            f'ALTER TABLE "{table}" '
            f'ALTER COLUMN "{column}" TYPE TEXT USING "{column}"::text'
        )


def downgrade() -> None:
    for table, column in _targets(_DOWNGRADE_TARGETS):
        # A non-UUID value (the "default" sentinel or any slug) cannot cast back
        # to UUID — null it first, exactly as 007 does for
        # ``workflow_runs.user_id``, so the type change cannot fail on data that
        # the widened type legitimately holds.
        op.execute(
            f'UPDATE "{table}" SET "{column}" = NULL '
            f"WHERE \"{column}\" IS NOT NULL AND \"{column}\" !~ '{_UUID_SHAPE}'"
        )
        op.execute(
            f'ALTER TABLE "{table}" '
            f'ALTER COLUMN "{column}" TYPE UUID USING "{column}"::uuid'
        )
