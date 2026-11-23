"""Bind a HITL write approval to the payload it approved (BIZ-18).

Until now ``agent_approvals.requested_action`` only named the tool
(``mcp.write:<tool>``), so "approve one invoice_post" silently meant "approve
*any* invoice_post for the next 15 minutes" — the human never saw the arguments
and the authorisation was not tied to them.

``payload_digest`` is the ``sha256:`` of the canonical arguments the approver was
shown. It is additive and default-safe: every pre-043 row reads as ``''``, which
the read path treats as "this approval never named a payload" and therefore
refuses (fail-closed) instead of granting a wildcard.
"""

from alembic import op

revision = "043"
down_revision = "042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_approvals ADD COLUMN IF NOT EXISTS payload_digest "
        "VARCHAR(96) NOT NULL DEFAULT ''"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_approvals_payload "
        "ON agent_approvals (tenant_id, payload_digest)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_agent_approvals_payload")
    op.execute("ALTER TABLE agent_approvals DROP COLUMN IF EXISTS payload_digest")
