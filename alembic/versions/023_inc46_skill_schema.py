"""023 - INC46 T10: the 11 skill-contract persistence tables.

T07 gave ForgeFlow the seven-segment skill contract (Manifest / Knowledge /
Procedure / Policies / Tool bindings / Evaluation / Examples,
``forgeflow/skills/schemas.py`` + ``segments.py``). Until now that contract lived
only in memory. This migration makes it durable: ten **new** tables that the
runtime / materialiser / evaluator write per ``(tenant_id, skill_id, version)``,
plus an additive extension of the pre-existing ``skill_evaluations`` table so it
can carry the contract's **Evaluation** segment.

Ownership: migration ``019`` = T01, ``020`` = T02, ``021`` = T05, ``022`` = T15,
this ``023`` = T10 — per the §九 migration registry (T15=022, T10=023), so no two
tasks contend for one migration file.

The eleven tables (names are fixed by the task book, §5.5)
----------------------------------------------------------
  * ``skill_steps``         — Procedure segment (ordered steps).
  * ``skill_tools``         — Tool-bindings segment (tools + ``scripts/`` refs).
  * ``skill_policies``      — Policies segment (constraints + allowed-tool decls).
  * ``skill_evaluations``   — Evaluation segment. **Pre-existing** (migration
                              ``010``, extended by ``013`` to TEXT tenant); see
                              below — this migration only *adds* columns.
  * ``skill_test_cases``    — T11 test fixtures for a version.
  * ``skill_test_runs``     — per-case execution of a version (T11/T12).
  * ``skill_experiences``   — N:M link version ↦ source ``experiences``.
  * ``skill_feedback``      — feedback about the **Skill itself** (distinct from
                              T16's ``feedback_events``, which signal a single
                              *run*; the two are deliberately never merged).
  * ``skill_permissions``   — declared tool permissions (A12 four-level).
  * ``skill_knowledge_refs``— Knowledge segment (``references/*.md`` index).
  * ``skill_examples``      — Examples segment (inline examples).

Field design vs. the T07 contract
---------------------------------
Every segment that carries structure is persisted one row per element:
``procedure.steps`` → ``skill_steps``; ``tool_bindings.tools|scripts`` →
``skill_tools.kind`` ∈ {``tool``, ``script``}; ``policies.constraints|
allowed_tools`` → ``skill_policies.kind`` ∈ {``constraint``, ``allowed_tool``};
``knowledge.references[].{file,summary}`` → ``skill_knowledge_refs``;
``examples[].examples`` → ``skill_examples``; ``evaluation.{ref,note}`` →
``skill_evaluations.segment_ref|segment_note``. The Manifest segment already has
a home in the existing ``skills`` / ``skill_versions`` tables (identity + spec)
and is not duplicated here.

``skill_evaluations`` conflict (documented, resolved additively)
----------------------------------------------------------------
The task book lists ``skill_evaluations`` among the eleven tables, but a table
with that exact name already exists (migration ``010``) with its own semantics
(``target_id`` = the candidate/asset evaluated). The house rule "加性，不改既有
列语义" therefore forbids recreating it with a different shape — that would make
a fresh DB and an existing DB diverge. This migration **adopts** it: it never
issues a divergent ``CREATE`` for it, and instead adds the four columns the
Evaluation segment needs (``skill_id`` / ``version`` / ``segment_ref`` /
``segment_note``) via ``ADD COLUMN IF NOT EXISTS``. Existing columns
(``target_id``/``dataset``/``metrics``/``verdict``) keep their exact semantics,
so ``PgSkillCandidateRepository.save_evaluation`` is unaffected.

Additive discipline (§九)
-------------------------
* Ten new tables only, plus nullable added columns on ``skill_evaluations``; no
  existing column's type or nullability changes.
* **Unmeasured ⇒ NULL, never 0/''**: every *measured* number
  (``skill_test_runs.duration_ms`` / ``score``, ``skill_experiences.similarity``,
  ``skill_feedback.rating``) is nullable with **no default**, so "no measurement
  happened" is stored as ``NULL`` and can never masquerade as a real ``0``.

Tenant discipline
-----------------
``tenant_id`` is opaque ``TEXT NOT NULL`` on every new table (the migration-020
single-yardstick contract). The application layer fails closed — an unresolved
tenant never writes and never reads (``IS NOT DISTINCT FROM`` predicates in
``repositories/{memory,postgres}/skill_schema_repo.py``).

Re-entrancy
-----------
Every statement is guarded with ``IF NOT EXISTS`` (extension / table / column /
index), so ``upgrade head`` twice is a clean no-op the second time. Importing
this module opens no connection.

Revision ID: 023
Revises: 022
Create Date: 2026-10-07
"""

from alembic import op

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None

#: The ten genuinely-new tables (name → DDL). Created with ``IF NOT EXISTS`` so a
#: second ``upgrade`` is a no-op. ``skill_evaluations`` is deliberately excluded —
#: it pre-exists (migration 010) and is only extended below.
_NEW_TABLES: tuple[tuple[str, str], ...] = (
    (
        "skill_steps",
        """
        CREATE TABLE IF NOT EXISTS skill_steps (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   TEXT NOT NULL,
            skill_id    TEXT NOT NULL,
            version     TEXT NOT NULL,
            step_index  INTEGER NOT NULL,
            text        TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
    ),
    (
        "skill_tools",
        """
        CREATE TABLE IF NOT EXISTS skill_tools (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   TEXT NOT NULL,
            skill_id    TEXT NOT NULL,
            version     TEXT NOT NULL,
            -- 'tool' | 'script' (tool_bindings.tools / tool_bindings.scripts).
            kind        VARCHAR(16) NOT NULL,
            -- The tool name, or the scripts/ reference (contract Tool bindings).
            ref         TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
    ),
    (
        "skill_policies",
        """
        CREATE TABLE IF NOT EXISTS skill_policies (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   TEXT NOT NULL,
            skill_id    TEXT NOT NULL,
            version     TEXT NOT NULL,
            -- 'constraint' | 'allowed_tool' (Policies segment).
            kind        VARCHAR(16) NOT NULL,
            text        TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
    ),
    (
        "skill_test_cases",
        """
        CREATE TABLE IF NOT EXISTS skill_test_cases (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   TEXT NOT NULL,
            skill_id    TEXT NOT NULL,
            version     TEXT NOT NULL,
            ordinal     INTEGER NOT NULL,
            name        TEXT NOT NULL,
            input       JSONB NOT NULL DEFAULT '{}',
            -- NULL when the case declares no expectation (未测量 ⇒ NULL).
            expected    JSONB,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
    ),
    (
        "skill_test_runs",
        """
        CREATE TABLE IF NOT EXISTS skill_test_runs (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   TEXT NOT NULL,
            skill_id    TEXT NOT NULL,
            version     TEXT NOT NULL,
            case_id     TEXT,
            run_id      TEXT,
            status      VARCHAR(16) NOT NULL,
            passed      BOOLEAN NOT NULL,
            -- Measured ⇒ NULL when no timing was taken (never a fabricated 0).
            duration_ms DOUBLE PRECISION,
            -- Measured ⇒ NULL when unmeasured (red line 4: no default 0).
            score       DOUBLE PRECISION,
            detail      JSONB NOT NULL DEFAULT '{}',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
    ),
    (
        "skill_experiences",
        """
        CREATE TABLE IF NOT EXISTS skill_experiences (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id     TEXT NOT NULL,
            skill_id      TEXT NOT NULL,
            version       TEXT NOT NULL,
            experience_id TEXT NOT NULL,
            relation      VARCHAR(32),
            -- Measured ⇒ NULL when the link carries no similarity (never 0).
            similarity    DOUBLE PRECISION,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
    ),
    (
        "skill_feedback",
        """
        CREATE TABLE IF NOT EXISTS skill_feedback (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   TEXT NOT NULL,
            skill_id    TEXT NOT NULL,
            version     TEXT NOT NULL,
            -- Feedback about the *Skill itself* (未测量 ⇒ NULL, never 0).
            rating      DOUBLE PRECISION,
            -- 'up' | 'down' | NULL (a thumbs-less comment leaves it NULL).
            signal      VARCHAR(16),
            comment     TEXT,
            actor_id    TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
    ),
    (
        "skill_permissions",
        """
        CREATE TABLE IF NOT EXISTS skill_permissions (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   TEXT NOT NULL,
            skill_id    TEXT NOT NULL,
            version     TEXT NOT NULL,
            tool        TEXT NOT NULL,
            -- A12 four-level permission name (read | write | execute | admin).
            level       VARCHAR(32) NOT NULL,
            granted     BOOLEAN NOT NULL DEFAULT FALSE,
            note        TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
    ),
    (
        "skill_knowledge_refs",
        """
        CREATE TABLE IF NOT EXISTS skill_knowledge_refs (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   TEXT NOT NULL,
            skill_id    TEXT NOT NULL,
            version     TEXT NOT NULL,
            ordinal     INTEGER NOT NULL,
            -- Knowledge segment: a references/*.md file (KnowledgeReference.file).
            file        TEXT NOT NULL,
            -- NULL when the author gave no summary (未测量 ⇒ NULL, never '').
            summary     TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
    ),
    (
        "skill_examples",
        """
        CREATE TABLE IF NOT EXISTS skill_examples (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   TEXT NOT NULL,
            skill_id    TEXT NOT NULL,
            version     TEXT NOT NULL,
            ordinal     INTEGER NOT NULL,
            -- Examples segment: one inline example (ExamplesSegment.examples[]).
            text        TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
    ),
)

#: Indexes — one per table, all leading with ``tenant_id`` so the tenant-scoped
#: read path is always index-eligible. ``IF NOT EXISTS`` keeps re-runs no-ops.
_INDEXES: tuple[tuple[str, str], ...] = (
    (
        "idx_skill_steps_tenant",
        "CREATE INDEX IF NOT EXISTS idx_skill_steps_tenant "
        "ON skill_steps (tenant_id, skill_id, version, step_index)",
    ),
    (
        "idx_skill_tools_tenant",
        "CREATE INDEX IF NOT EXISTS idx_skill_tools_tenant "
        "ON skill_tools (tenant_id, skill_id, version)",
    ),
    (
        "idx_skill_policies_tenant",
        "CREATE INDEX IF NOT EXISTS idx_skill_policies_tenant "
        "ON skill_policies (tenant_id, skill_id, version)",
    ),
    (
        "idx_skill_test_cases_tenant",
        "CREATE INDEX IF NOT EXISTS idx_skill_test_cases_tenant "
        "ON skill_test_cases (tenant_id, skill_id, version, ordinal)",
    ),
    (
        "idx_skill_test_runs_tenant",
        "CREATE INDEX IF NOT EXISTS idx_skill_test_runs_tenant "
        "ON skill_test_runs (tenant_id, skill_id, version, created_at DESC)",
    ),
    (
        "idx_skill_experiences_tenant",
        "CREATE INDEX IF NOT EXISTS idx_skill_experiences_tenant "
        "ON skill_experiences (tenant_id, skill_id, experience_id)",
    ),
    (
        "idx_skill_feedback_tenant",
        "CREATE INDEX IF NOT EXISTS idx_skill_feedback_tenant "
        "ON skill_feedback (tenant_id, skill_id, created_at DESC)",
    ),
    (
        "idx_skill_permissions_tenant",
        "CREATE INDEX IF NOT EXISTS idx_skill_permissions_tenant "
        "ON skill_permissions (tenant_id, skill_id, version, tool)",
    ),
    (
        "idx_skill_knowledge_refs_tenant",
        "CREATE INDEX IF NOT EXISTS idx_skill_knowledge_refs_tenant "
        "ON skill_knowledge_refs (tenant_id, skill_id, version, ordinal)",
    ),
    (
        "idx_skill_examples_tenant",
        "CREATE INDEX IF NOT EXISTS idx_skill_examples_tenant "
        "ON skill_examples (tenant_id, skill_id, version, ordinal)",
    ),
)

#: The Evaluation-segment columns grafted onto the pre-existing
#: ``skill_evaluations`` table (migration 010). All nullable ⇒ no backfill, no
#: change to ``target_id``/``dataset``/``metrics``/``verdict``.
_EVALUATION_ADDITIVE_COLUMNS: tuple[str, ...] = (
    "skill_id",
    "version",
    "segment_ref",
    "segment_note",
)


def upgrade() -> None:
    # ``gen_random_uuid()`` (the ``id`` default) lives in pgcrypto on older
    # servers; migration 010 already created it. Harmless no-op when present.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    for _name, ddl in _NEW_TABLES:
        op.execute(ddl)

    for _name, ddl in _INDEXES:
        op.execute(ddl)

    # --- adopt the pre-existing skill_evaluations (additive only) ----------- #
    # It already exists on every database at revision ≥ 010, so a divergent
    # CREATE would be a no-op on existing DBs but create a *different* shape on
    # a fresh one. Adding columns keeps both paths identical.
    op.execute(
        "ALTER TABLE skill_evaluations ADD COLUMN IF NOT EXISTS skill_id TEXT"
    )
    op.execute(
        "ALTER TABLE skill_evaluations ADD COLUMN IF NOT EXISTS version TEXT"
    )
    op.execute(
        "ALTER TABLE skill_evaluations ADD COLUMN IF NOT EXISTS segment_ref TEXT"
    )
    op.execute(
        "ALTER TABLE skill_evaluations ADD COLUMN IF NOT EXISTS segment_note TEXT"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_evaluations_skill "
        "ON skill_evaluations (tenant_id, skill_id, created_at DESC)"
    )


def downgrade() -> None:
    # Reverse only what this migration added. The four extended columns are
    # dropped; the ten tables are dropped whole. ``skill_evaluations`` itself and
    # its pre-023 columns are left intact (it predates this revision).
    for name, _ddl in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")

    for column in _EVALUATION_ADDITIVE_COLUMNS:
        op.execute(
            f"ALTER TABLE skill_evaluations DROP COLUMN IF EXISTS {column}"
        )
    op.execute("DROP INDEX IF EXISTS idx_skill_evaluations_skill")

    for name, _ddl in reversed(_NEW_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {name}")
