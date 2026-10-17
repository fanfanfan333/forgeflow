"""039 — globally unique feedback ownership, protected by forced RLS."""

from alembic import op

revision = "039"
down_revision = "038"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("LOCK TABLE workspace_runs, run_outcomes, feedback_events IN SHARE ROW EXCLUSIVE MODE")
    op.execute("CREATE TABLE feedback_run_owners (run_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL)")
    op.execute("""
        INSERT INTO feedback_run_owners(run_id, tenant_id)
        SELECT run_id, CASE WHEN count(DISTINCT tenant_id)=1
                             AND bool_and(tenant_id IS NOT NULL AND tenant_id<>'')
                           THEN max(tenant_id) ELSE '__ownership_conflict__' END
        FROM (
            SELECT run_id, tenant_id FROM run_outcomes
            UNION ALL SELECT run_id, tenant_id FROM feedback_events
            UNION ALL SELECT run_id, tenant_id FROM workspace_runs
        ) owners GROUP BY run_id
    """)
    for table in ("feedback_run_owners", "run_events"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id=current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id=current_setting('app.tenant_id', true))""")
    op.execute("""
        CREATE FUNCTION public.claim_feedback_run_owner() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
          IF NEW.tenant_id IS NOT NULL THEN
            INSERT INTO public.feedback_run_owners(run_id,tenant_id)
            VALUES(NEW.run_id,NEW.tenant_id) ON CONFLICT DO NOTHING;
            IF NOT EXISTS(SELECT 1 FROM public.feedback_run_owners
                          WHERE run_id=NEW.run_id AND tenant_id=NEW.tenant_id) THEN
              RAISE EXCEPTION 'Run ownership conflict';
            END IF;
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER claim_feedback_run_owner AFTER INSERT ON workspace_runs "
               "FOR EACH ROW EXECUTE FUNCTION public.claim_feedback_run_owner()")


def downgrade():
    op.execute("DROP TRIGGER claim_feedback_run_owner ON workspace_runs")
    op.execute("DROP FUNCTION public.claim_feedback_run_owner()")
    op.execute("DROP TABLE feedback_run_owners")
    op.execute("DROP POLICY tenant_isolation ON run_events")
    op.execute("ALTER TABLE run_events NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE run_events DISABLE ROW LEVEL SECURITY")
