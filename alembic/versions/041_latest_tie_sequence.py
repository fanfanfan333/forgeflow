"""Break created_at ties in latest_* lookups with a monotonic sequence.

The ``latest_*`` / earliest-pick queries ordered by a timestamp plus
``id`` where ``id`` is a random UUID: two rows sharing a timestamp
picked a winner by random-UUID order — nondeterministic. Each table
below gets a ``seq`` column backed by an identity sequence, so insertion
order is monotonic and ties break deterministically (later insert wins
for latest-picks, earlier insert wins for the earliest-pick
``find_proposal``). The backfill assigns values in physical scan order —
arbitrary for pre-existing tied rows, which were already
nondeterministic; every new row is strictly ordered. All inserts into
these tables name their columns explicitly, so the new column needs no
insert-side change.
"""

from alembic import op

revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE skill_lifecycle_events "
        "ADD COLUMN seq BIGINT GENERATED ALWAYS AS IDENTITY"
    )
    op.execute(
        "ALTER TABLE benchmark_runs "
        "ADD COLUMN seq BIGINT GENERATED ALWAYS AS IDENTITY"
    )
    op.execute(
        "ALTER TABLE skill_rollouts "
        "ADD COLUMN seq BIGINT GENERATED ALWAYS AS IDENTITY"
    )
    op.execute(
        "ALTER TABLE metric_snapshots "
        "ADD COLUMN seq BIGINT GENERATED ALWAYS AS IDENTITY"
    )
    op.execute(
        "ALTER TABLE skill_merge_proposals "
        "ADD COLUMN seq BIGINT GENERATED ALWAYS AS IDENTITY"
    )


def downgrade():
    # IF EXISTS: a partially applied 041 (e.g. an earlier three-table
    # revision of this file) must still roll back cleanly.
    op.execute("ALTER TABLE skill_lifecycle_events DROP COLUMN IF EXISTS seq")
    op.execute("ALTER TABLE benchmark_runs DROP COLUMN IF EXISTS seq")
    op.execute("ALTER TABLE skill_rollouts DROP COLUMN IF EXISTS seq")
    op.execute("ALTER TABLE metric_snapshots DROP COLUMN IF EXISTS seq")
    op.execute("ALTER TABLE skill_merge_proposals DROP COLUMN IF EXISTS seq")
