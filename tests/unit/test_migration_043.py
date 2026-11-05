"""043 — RLS policy for ``run_checkpoints`` (042 created it without one).

Structural checks in the style of test_migration_011: revision
chaining, the exact RLS surface modeled on 035/039, a symmetric
downgrade, and that 042 itself is untouched (043 is additive).
"""

from __future__ import annotations

import importlib.util
import pathlib
from types import ModuleType

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "alembic" / "versions"


def _load(path: pathlib.Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_source(filename: str) -> str:
    return (MIGRATIONS / filename).read_text(encoding="utf-8")


def test_revision_chain():
    mig = _load(MIGRATIONS / "043_run_checkpoints_rls.py", "mig_043_under_test")
    assert mig.revision == "043"
    assert mig.down_revision == "042"
    assert callable(mig.upgrade)
    assert callable(mig.downgrade)


def test_upgrade_enables_forced_rls_with_tenant_policy():
    source = _read_source("043_run_checkpoints_rls.py")
    upgrade = source.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    assert "ALTER TABLE run_checkpoints ENABLE ROW LEVEL SECURITY" in upgrade
    assert "ALTER TABLE run_checkpoints FORCE ROW LEVEL SECURITY" in upgrade
    assert "CREATE POLICY tenant_isolation ON run_checkpoints" in upgrade
    # The 035 fail-closed condition: the policy compares tenant_id
    # against the session setting, with an empty setting yielding
    # NULL (matches nothing) rather than no restriction. (The
    # condition lives in the module-level _CONDITION constant the
    # upgrade interpolates.)
    assert "current_setting('app.tenant_id', true)" in source
    assert "NULLIF" in source
    assert "USING" in upgrade and "WITH CHECK" in upgrade
    # Re-entrant: an existing policy is dropped before re-creation.
    assert "DROP POLICY IF EXISTS tenant_isolation ON run_checkpoints" in upgrade


def test_downgrade_is_symmetric():
    source = _read_source("043_run_checkpoints_rls.py")
    downgrade = source.split("def downgrade()", 1)[1]
    assert "DROP POLICY IF EXISTS tenant_isolation ON run_checkpoints" in downgrade
    assert "ALTER TABLE run_checkpoints NO FORCE ROW LEVEL SECURITY" in downgrade
    assert "ALTER TABLE run_checkpoints DISABLE ROW LEVEL SECURITY" in downgrade
    # The table itself belongs to 042 and must survive the downgrade.
    assert "DROP TABLE" not in downgrade


def test_does_not_touch_042():
    mig_042 = _load(MIGRATIONS / "042_run_checkpoints.py", "mig_042_under_test")
    assert mig_042.revision == "042"
    assert mig_042.down_revision == "041"
    source_043 = _read_source("043_run_checkpoints_rls.py")
    # 043 only adds the policy; it never re-creates or alters the table
    # definition owned by 042.
    assert "CREATE TABLE" not in source_043
