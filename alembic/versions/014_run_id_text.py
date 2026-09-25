"""014 - ``run_id`` linkage columns become opaque ``TEXT`` (no silent NULL).

INC12-A2 deleted the seven private copies of ``_as_uuid`` and folded them into one
shared helper, ``repositories.base.uuid_or_none``. That is better engineering than
seven copies, but it **kept the same defect** on three columns that were still
``UUID``: ``experiences.run_id``, ``agent_approvals.run_id`` and
``context_build_stats.run_id``. A non-UUID hub run id (``"hub-abc"``) therefore
still became ``NULL`` on write — with no log and no exception — and, worse,
``experience_repo.list(run_id="hub-abc")`` then rendered the predicate
``run_id = NULL``, which is never true in SQL: the caller got an empty list and
could not tell "no data" apart from "the linkage was thrown away on write". This
is exactly the silent-NULL class of bug A2 removed for ``tenant_id``, one column
over.

The repository's own direction already points the other way: migration ``012``
declared ``agent_eval_samples.run_id`` as ``TEXT`` with **no** foreign key, on
purpose (``run_metrics.run_id`` is the genuine UUID FK it declined to reuse).
Widening the three linkage columns finishes that decision instead of leaving a
UUID island inside the hub schema.

Mechanism — **runtime discovery**, as in ``013_tenant_id_text``, so this is
correct for whichever subset of the hub tables a given database actually has:

  * find every **nullable** ``run_id`` column of type ``uuid`` that carries **no
    foreign key**;
  * ``ALTER TABLE <t> ALTER COLUMN <c> TYPE TEXT USING <c>::text``.

Both halves of that filter are load-bearing:

  * *no foreign key* — ``agent_traces.run_id``, ``run_metrics.run_id``,
    ``token_usage.run_id`` and ``approval_requests.run_id`` really do reference
    ``workflow_runs(id)`` (a UUID primary key) and must stay ``UUID``; a blanket
    ``column_name = 'run_id'`` rewrite would break those constraints.
  * *nullable* — a ``NOT NULL`` run_id is an identifier, not an optional
    linkage: widening it would also make the downgrade (which nulls values that
    cannot cast back to ``UUID``) fail on a NOT NULL violation. This is what
    keeps ``run_steps.run_id`` (010, ``NOT NULL``) and any application-created
    run table (e.g. a ``runs.run_id`` primary-ish column) out of scope. On a
    database holding the 010/011 hub tables the filter therefore resolves to
    exactly the three linkage columns above.

``agent_eval_samples.run_id`` needs no upgrade handling — it is already ``TEXT``,
so the ``data_type = 'uuid'`` gate skips it — but it **is** excluded by name from
the downgrade, because a downgrade must not narrow back a column that ``012``
deliberately created as ``TEXT`` (``012``'s own downgrade drops that table).

Re-entrancy: the ``data_type = 'uuid'`` gate makes a second ``upgrade`` a no-op,
on an empty database (fresh ``upgrade head``) and a populated one alike. The
reverse direction mirrors ``007_run_user_id_text`` (and ``013``): a non-UUID value
cannot cast back to ``UUID``, so it is nulled first — the honest pre-014
semantics, where a non-UUID run id was stored as ``NULL``.

Revision ID: 014
Revises: 013
Create Date: 2026-10-01
"""

from alembic import op

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None

# ``information_schema.columns.data_type`` reports ``uuid`` for a ``UUID``
# column, exactly as it does in 013. Discovery (rather than a hard-coded table
# list) keeps the migration correct for whatever set of hub tables this database
# actually has.
_UPGRADE_TARGETS = """
SELECT c.table_name, c.column_name
FROM information_schema.columns c
WHERE c.table_schema = current_schema()
  AND c.column_name = 'run_id'
  AND c.data_type = 'uuid'
  AND c.is_nullable = 'YES'
  AND NOT EXISTS (
      SELECT 1
      FROM information_schema.key_column_usage k
      JOIN information_schema.table_constraints t
        ON t.constraint_name = k.constraint_name
       AND t.constraint_schema = k.constraint_schema
       AND t.table_schema = k.table_schema
       AND t.table_name = k.table_name
      WHERE k.table_schema = c.table_schema
        AND k.table_name = c.table_name
        AND k.column_name = c.column_name
        AND t.constraint_type = 'FOREIGN KEY'
  )
ORDER BY c.table_name, c.column_name
"""

# Same criterion in reverse. ``agent_eval_samples`` is the one explicit name here:
# migration 012 created its ``run_id`` as TEXT on purpose (with no FK), so it is
# not a column this migration widened and must not be narrowed back — 012's own
# downgrade drops the table.
_DOWNGRADE_TARGETS = """
SELECT c.table_name, c.column_name
FROM information_schema.columns c
WHERE c.table_schema = current_schema()
  AND c.column_name = 'run_id'
  AND c.data_type IN ('text', 'character varying')
  AND c.is_nullable = 'YES'
  AND c.table_name <> 'agent_eval_samples'
  AND NOT EXISTS (
      SELECT 1
      FROM information_schema.key_column_usage k
      JOIN information_schema.table_constraints t
        ON t.constraint_name = k.constraint_name
       AND t.constraint_schema = k.constraint_schema
       AND t.table_schema = k.table_schema
       AND t.table_name = k.table_name
      WHERE k.table_schema = c.table_schema
        AND k.table_name = c.table_name
        AND k.column_name = c.column_name
        AND t.constraint_type = 'FOREIGN KEY'
  )
ORDER BY c.table_name, c.column_name
"""

#: Canonical UUID shape — the same pattern 007 / 013 use. Anything that does not
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
        # A non-UUID value (a hub run id such as "hub-abc") cannot cast back to
        # UUID — null it first, exactly as 007 does for ``workflow_runs.user_id``
        # and 013 for ``tenant_id``, so the type change cannot fail on data the
        # widened type legitimately holds. Only nullable columns are targeted, so
        # this can never hit a NOT NULL constraint.
        op.execute(
            f'UPDATE "{table}" SET "{column}" = NULL '
            f"WHERE \"{column}\" IS NOT NULL AND \"{column}\" !~ '{_UUID_SHAPE}'"
        )
        op.execute(
            f'ALTER TABLE "{table}" '
            f'ALTER COLUMN "{column}" TYPE UUID USING "{column}"::uuid'
        )
