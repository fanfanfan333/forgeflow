"""INC2-02 — migration 011 structure (docs/sop/05-ARCHITECTURE-INC2.md §3).

Checks the revision chaining, the re-entrant SQL surface, and — critically —
that revision 010 (and earlier) is left untouched. The module is loaded by file
path so importing it never requires ``alembic/versions`` to be a Python package
and never opens a database connection.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
from types import ModuleType

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "alembic" / "versions"


def _load(path: pathlib.Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_011() -> ModuleType:
    return _load(MIGRATIONS / "011_cost_slo_evolution.py", "mig_011_under_test")


def _load_010() -> ModuleType:
    return _load(MIGRATIONS / "010_agentflow_hubs.py", "mig_010_under_test")


def test_revision_chain():
    mig = _load_011()
    assert mig.revision == "011"
    assert mig.down_revision == "010"
    assert callable(mig.upgrade)
    assert callable(mig.downgrade)


def test_creates_expected_tables():
    source = _read_source("011_cost_slo_evolution.py")
    for table in ("cost_budgets", "skill_ratings", "context_build_stats"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in source, table


def test_adds_expected_columns():
    source = _read_source("011_cost_slo_evolution.py")
    expected = [
        ("skill_listings", "shared"),
        ("skill_listings", "listed_by"),
        ("skill_listings", "rating"),
        ("skill_listings", "rating_count"),
        ("experiences", "conflict_with"),
        ("experiences", "merged_from"),
        ("experiences", "dedup_key"),
        ("experiences", "confidence"),
        ("skills", "last_used_at"),
        ("skills", "retire_suggested"),
        ("agent_approvals", "kind"),
    ]
    for table, column in expected:
        assert f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column}" in source, (
            table,
            column,
        )


def test_downgrade_is_symmetric():
    source = _read_source("011_cost_slo_evolution.py")
    downgrade = source.split("def downgrade()", 1)[1]
    for table in ("cost_budgets", "skill_ratings", "context_build_stats"):
        assert f'"{table}"' in downgrade, table
    assert "DROP TABLE IF EXISTS {table}" in downgrade
    assert "DROP COLUMN IF EXISTS shared" in downgrade
    assert "DROP COLUMN IF EXISTS conflict_with" in downgrade
    assert "DROP COLUMN IF EXISTS last_used_at" in downgrade
    assert "DROP COLUMN IF EXISTS kind" in downgrade


def test_every_statement_is_reentrant():
    source = _read_source("011_cost_slo_evolution.py")
    # No bare CREATE TABLE / ADD COLUMN (all must be guarded with IF NOT EXISTS).
    assert re.search(r"CREATE TABLE(?! IF NOT EXISTS)", source) is None
    assert re.search(r"ADD COLUMN(?! IF NOT EXISTS)", source) is None


def test_does_not_touch_010():
    """Revision 010 and earlier must be byte-for-byte untouched by INC2."""
    mig_010 = _load_010()
    assert mig_010.revision == "010"
    assert mig_010.down_revision == "009"

    # And the 011 source must never reference dropping/altering 010's tables in
    # its upgrade body (it only extends 010's tables additively, which is fine).
    source_011 = _read_source("011_cost_slo_evolution.py")
    upgrade_body = source_011.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    assert "DROP TABLE" not in upgrade_body


def _read_source(filename: str) -> str:
    return (MIGRATIONS / filename).read_text(encoding="utf-8")
