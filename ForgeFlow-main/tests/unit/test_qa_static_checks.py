"""Unit tests for :mod:`forgeflow.qa.static_checks`."""

from __future__ import annotations

from forgeflow.qa.paths import find_project_root
from forgeflow.qa.static_checks import StaticChecks


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_route_registration_detects_orphan_router(tmp_path):
    routers = tmp_path / "forgeflow" / "api" / "routers"
    _write(
        routers / "wired.py",
        "from fastapi import APIRouter\nrouter = APIRouter()\n@router.get('/x')\ndef x():\n    ...\n",
    )
    _write(
        routers / "orphan.py",
        "from fastapi import APIRouter\nrouter = APIRouter()\n@router.post('/y')\ndef y():\n    ...\n",
    )
    _write(
        tmp_path / "forgeflow" / "api" / "main.py",
        'app.include_router(wired.router, prefix="/wired")\n',
    )
    result = StaticChecks(tmp_path).route_registration()
    assert result["status"] == "OK"
    assert result["unmapped_routers"] == ["orphan"]
    assert result["registered_prefixes"] == ["/wired"]
    assert result["route_decorators"] == 2


def test_permission_registration_inconclusive_when_missing(tmp_path):
    result = StaticChecks(tmp_path).permission_registration()
    assert result["status"] == "INCONCLUSIVE"
    assert result["mapped_routes"] == 0


def test_permission_registration_counts_entries(tmp_path):
    _write(
        tmp_path / "forgeflow" / "rbac" / "policies.py",
        'ROUTE_PERMISSION_MAP = {("GET", "/x"): ("read", "x")}\n',
    )
    result = StaticChecks(tmp_path).permission_registration()
    assert result["status"] == "OK"
    assert result["mapped_routes"] == 1


def test_missing_env_vars_reports_diff(tmp_path):
    _write(
        tmp_path / "forgeflow" / "config.py",
        "class Settings:\n    alpha_field: str = Field('x')\n    beta_field: int = Field(1)\n",
    )
    _write(tmp_path / ".env.example", "ALPHA_FIELD=x\n")
    assert StaticChecks(tmp_path).missing_env_vars() == ["BETA_FIELD"]


def test_client_backend_mismatch_detects_port_difference(tmp_path):
    _write(
        tmp_path / "frontend" / "vite.config.ts",
        "const DEFAULT_API_TARGET = 'http://localhost:5173'\n",
    )
    _write(
        tmp_path / "forgeflow" / "bootstrap" / "runtime.py",
        "DEFAULT_PORT = 8010\n",
    )
    mismatch = StaticChecks(tmp_path).client_backend_mismatch()
    assert mismatch and "5173" in mismatch[0] and "8010" in mismatch[0]


def test_real_project_checks_are_healthy():
    checker = StaticChecks(find_project_root())
    routes = checker.route_registration()
    assert routes["status"] == "OK"
    assert routes["router_files"]
    assert "/auth" in routes["registered_prefixes"]

    permissions = checker.permission_registration()
    assert permissions["status"] == "OK"
    assert permissions["mapped_routes"] > 0

    assert isinstance(checker.unresolved_imports(), list)
    assert isinstance(checker.missing_env_vars(), list)
    # ForgeFlow's dev proxy and backend both default to :8010 — no mismatch.
    assert checker.client_backend_mismatch() == []


def test_run_all_summary_shape():
    result = StaticChecks(find_project_root()).run_all()
    assert set(result) >= {
        "route_registration",
        "permission_registration",
        "unresolved_imports",
        "missing_env_vars",
        "client_backend_mismatch",
        "summary",
    }
    assert result["summary"]["router_files"] > 0
