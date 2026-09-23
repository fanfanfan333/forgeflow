python.exe : INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
所在位置 行:449 字符: 1
+ & $py -m alembic downgrade 010:009 --sql 2>&1 | Out-File -Encoding ut ...
+ ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    + CategoryInfo          : NotSpecified: (INFO  [alembic....PostgresqlImpl.:String) [], RemoteException
    + FullyQualifiedErrorId : NativeCommandError
 
INFO  [alembic.runtime.migration] Generating static SQL
INFO  [alembic.runtime.migration] Will assume transactional DDL.
BEGIN;

INFO  [alembic.runtime.migration] Running downgrade 010 -> 009, 010 - AgentFlow hubs: 12 new tables + column extensions
.
-- Running downgrade 010 -> 009

ALTER TABLE memory_vectors DROP COLUMN IF EXISTS team_id;

ALTER TABLE memory_vectors DROP COLUMN IF EXISTS scope;

ALTER TABLE workflow_runs DROP COLUMN IF EXISTS agent_id;

ALTER TABLE workflow_runs DROP COLUMN IF EXISTS task_id;

ALTER TABLE workspaces DROP COLUMN IF EXISTS isolation_level;

ALTER TABLE workspaces DROP COLUMN IF EXISTS plan;

DROP TABLE IF EXISTS agent_approvals;

DROP TABLE IF EXISTS policies;

DROP TABLE IF EXISTS skill_listings;

DROP TABLE IF EXISTS skill_evaluations;

DROP TABLE IF EXISTS candidate_experience;

DROP TABLE IF EXISTS skill_candidates;

DROP TABLE IF EXISTS skill_versions;

DROP TABLE IF EXISTS skills;

DROP TABLE IF EXISTS experience_memory;

DROP TABLE IF EXISTS experiences;

DROP TABLE IF EXISTS run_steps;

DROP TABLE IF EXISTS tasks;

UPDATE alembic_version SET version_num='009' WHERE alembic_version.version_num = '010';

COMMIT;

EXIT=0
