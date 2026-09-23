"""010 - AgentFlow hubs: 12 new tables + column extensions.

Implements the closed-loop persistence layer described in
docs/sop/02-ARCHITECTURE.md §3.2. New tables:

  tasks, run_steps, experiences, experience_memory, skill_candidates,
  candidate_experience, skills, skill_versions, skill_evaluations,
  policies, agent_approvals, skill_listings

Plus additive columns:
  workspaces(plan, isolation_level), workflow_runs(task_id, agent_id),
  memory_vectors(scope, team_id)

All statements use IF NOT EXISTS / ADD COLUMN IF NOT EXISTS so the migration is
re-entrant and safe to re-run. Importing this module never opens a connection —
it only declares the upgrade/downgrade bodies (alembic runs them).

Revision ID: 010
Revises: 009
Create Date: 2026-09-23
"""

from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- extensions (idempotent; no-ops when already present) ---
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # --- tasks -----------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   UUID,
            title       VARCHAR(256) NOT NULL DEFAULT '',
            intent      TEXT NOT NULL DEFAULT '',
            status      VARCHAR(24) NOT NULL DEFAULT 'pending',
            created_by  VARCHAR(128),
            context     JSONB NOT NULL DEFAULT '{}',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_tasks_tenant ON tasks (tenant_id)")

    # --- run_steps -------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS run_steps (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id     UUID NOT NULL,
            tenant_id  UUID,
            step_index INTEGER NOT NULL DEFAULT 0,
            step_type  VARCHAR(32) NOT NULL DEFAULT 'tool',
            tool       VARCHAR(128),
            input      JSONB NOT NULL DEFAULT '{}',
            output     JSONB NOT NULL DEFAULT '{}',
            latency_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_run_steps_run ON run_steps (run_id, step_index)")

    # --- experiences -----------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS experiences (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id      UUID,
            team_id        UUID,
            run_id         UUID,
            summary        TEXT NOT NULL DEFAULT '',
            decisions      JSONB NOT NULL DEFAULT '[]',
            outcome        VARCHAR(16) NOT NULL DEFAULT 'success',
            reusable_steps JSONB NOT NULL DEFAULT '[]',
            tags           TEXT[] NOT NULL DEFAULT '{}',
            embedding      vector(1536),
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_experiences_tenant ON experiences (tenant_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_experiences_run ON experiences (run_id)")

    # --- experience_memory (N:M) ----------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS experience_memory (
            experience_id UUID NOT NULL REFERENCES experiences(id) ON DELETE CASCADE,
            memory_id     UUID NOT NULL,
            relation      VARCHAR(16) NOT NULL DEFAULT 'source',
            PRIMARY KEY (experience_id, memory_id)
        )
        """
    )

    # --- skills / skill_versions ----------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS skills (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       UUID,
            name            VARCHAR(128) NOT NULL,
            domain          VARCHAR(64) NOT NULL DEFAULT 'general',
            owner           VARCHAR(128),
            description     TEXT NOT NULL DEFAULT '',
            current_version VARCHAR(32),
            status          VARCHAR(16) NOT NULL DEFAULT 'draft',
            usage_count     INTEGER NOT NULL DEFAULT 0,
            featured        BOOLEAN NOT NULL DEFAULT FALSE,
            tags            TEXT[] NOT NULL DEFAULT '{}',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_skills_tenant ON skills (tenant_id)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_versions (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id             UUID,
            skill_id              UUID NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
            semver                VARCHAR(32) NOT NULL,
            spec                  JSONB NOT NULL DEFAULT '{}',
            changelog             TEXT NOT NULL DEFAULT '',
            eval_score            DOUBLE PRECISION,
            source_experience_ids UUID[] NOT NULL DEFAULT '{}',
            approved_by           VARCHAR(128),
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (skill_id, semver)
        )
        """
    )

    # --- skill_candidates / candidate_experience ------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_candidates (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        UUID,
            name             VARCHAR(160) NOT NULL DEFAULT '',
            domain           VARCHAR(64) NOT NULL DEFAULT 'general',
            experience_ids   UUID[] NOT NULL DEFAULT '{}',
            draft_spec       JSONB NOT NULL DEFAULT '{}',
            similarity_score DOUBLE PRECISION NOT NULL DEFAULT 0,
            status           VARCHAR(16) NOT NULL DEFAULT 'draft',
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_candidates_tenant ON skill_candidates (tenant_id)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS candidate_experience (
            candidate_id  UUID NOT NULL REFERENCES skill_candidates(id) ON DELETE CASCADE,
            experience_id UUID NOT NULL,
            similarity    DOUBLE PRECISION NOT NULL DEFAULT 0,
            PRIMARY KEY (candidate_id, experience_id)
        )
        """
    )

    # --- skill_evaluations -----------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_evaluations (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id  UUID,
            target_id  UUID NOT NULL,
            dataset    VARCHAR(128),
            metrics    JSONB NOT NULL DEFAULT '{}',
            verdict    VARCHAR(16) NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_evaluations_target ON skill_evaluations (target_id)")

    # --- skill_listings (marketplace, jump ⑥) ---------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_listings (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   UUID,
            skill_id    UUID NOT NULL,
            version     VARCHAR(32) NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            installs    INTEGER NOT NULL DEFAULT 0,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (skill_id, version)
        )
        """
    )

    # --- policies --------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS policies (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   UUID,
            subject     VARCHAR(128) NOT NULL DEFAULT '*',
            resource    VARCHAR(128) NOT NULL DEFAULT '*',
            action      VARCHAR(64)  NOT NULL DEFAULT '*',
            condition   JSONB NOT NULL DEFAULT '{}',
            effect      VARCHAR(8)   NOT NULL DEFAULT 'allow',
            description TEXT NOT NULL DEFAULT '',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_policies_tenant ON policies (tenant_id)")

    # --- agent_approvals (HITL for the hubs; legacy approval_requests untouched)
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_approvals (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        UUID,
            run_id           UUID,
            risk_level       VARCHAR(16) NOT NULL DEFAULT 'low',
            requested_action TEXT NOT NULL DEFAULT '',
            requester        VARCHAR(128),
            approver         VARCHAR(128),
            decision         VARCHAR(16),
            status           VARCHAR(16) NOT NULL DEFAULT 'pending',
            note             TEXT NOT NULL DEFAULT '',
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            resolved_at      TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_agent_approvals_tenant ON agent_approvals (tenant_id)")

    # --- additive columns ------------------------------------------------------
    op.execute("ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS plan VARCHAR(32) NOT NULL DEFAULT 'standard'")
    op.execute("ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS isolation_level VARCHAR(16) NOT NULL DEFAULT 'row'")
    op.execute("ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS task_id UUID")
    op.execute("ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS agent_id VARCHAR(128)")
    op.execute("ALTER TABLE memory_vectors ADD COLUMN IF NOT EXISTS scope VARCHAR(24) NOT NULL DEFAULT 'semantic'")
    op.execute("ALTER TABLE memory_vectors ADD COLUMN IF NOT EXISTS team_id UUID")


def downgrade() -> None:
    # Reverse the additive columns first (drops are safe/no-ops when absent).
    op.execute("ALTER TABLE memory_vectors DROP COLUMN IF EXISTS team_id")
    op.execute("ALTER TABLE memory_vectors DROP COLUMN IF EXISTS scope")
    op.execute("ALTER TABLE workflow_runs DROP COLUMN IF EXISTS agent_id")
    op.execute("ALTER TABLE workflow_runs DROP COLUMN IF EXISTS task_id")
    op.execute("ALTER TABLE workspaces DROP COLUMN IF EXISTS isolation_level")
    op.execute("ALTER TABLE workspaces DROP COLUMN IF EXISTS plan")

    # Drop tables in FK-safe reverse order.
    for table in (
        "agent_approvals",
        "policies",
        "skill_listings",
        "skill_evaluations",
        "candidate_experience",
        "skill_candidates",
        "skill_versions",
        "skills",
        "experience_memory",
        "experiences",
        "run_steps",
        "tasks",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")
