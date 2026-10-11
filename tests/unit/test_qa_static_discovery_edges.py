"""Edge tests for :mod:`forgeflow.qa.static_checks` and :mod:`forgeflow.qa.discovery`.

Focuses on the *degraded* surfaces: an empty tree, a missing ``main.py``, an
unparseable ``policies.py`` (must be ``INCONCLUSIVE``, never a fake ``0``), and
the "all 19 layers always present" guarantee of the inventory.
"""

from __future__ import annotations

from pathlib import Path

from forgeflow.qa.discovery import RepositoryIntelligence
from forgeflow.qa.models import ALL_LAYERS, Layer
from forgeflow.qa.paths import find_project_root
from forgeflow.qa.static_checks import StaticChecks

# ── static_checks: empty / degraded tree ─────────────────────────────────────


def test_empty_tree_degrades_honestly(tmp_path: Path) -> None:
    checks = StaticChecks(tmp_path)
    routes = checks.route_registration()
    assert routes["status"] == "NOT_FOUND"
    assert routes["router_files"] == []

    perms = checks.permission_registration()
    assert perms["status"] == "INCONCLUSIVE"  # never a fabricated 0

    assert checks.unresolved_imports() == []
    assert checks.missing_env_vars() == []
    assert checks.client_backend_mismatch() == []

    summary = checks.run_all()["summary"]
    assert summary["permission_status"] == "INCONCLUSIVE"
    assert summary["router_files"] == 0


# ── static_checks: permission parsing ────────────────────────────────────────


def _write_policies(root: Path, body: str) -> None:
    target = root / "forgeflow" / "rbac"
    target.mkdir(parents=True, exist_ok=True)
    (target / "policies.py").write_text(body, encoding="utf-8")


def test_permission_map_annotated_form(tmp_path: Path) -> None:
    _write_policies(tmp_path, 'ROUTE_PERMISSION_MAP: dict[str, str] = {"a": "b", "c": "d"}\n')
    result = StaticChecks(tmp_path).permission_registration()
    assert result["status"] == "OK"
    assert result["mapped_routes"] == 2


def test_permission_map_plain_assign_form(tmp_path: Path) -> None:
    _write_policies(tmp_path, 'ROUTE_PERMISSION_MAP = {"only": "one"}\n')
    result = StaticChecks(tmp_path).permission_registration()
    assert result["status"] == "OK"
    assert result["mapped_routes"] == 1


def test_permission_map_unparseable_is_inconclusive(tmp_path: Path) -> None:
    _write_policies(tmp_path, "def broken(:\n")
    result = StaticChecks(tmp_path).permission_registration()
    assert result["status"] == "INCONCLUSIVE"


# ── static_checks: route registration ────────────────────────────────────────


def test_route_registration_flags_unmapped_router(tmp_path: Path) -> None:
    routers = tmp_path / "forgeflow" / "api" / "routers"
    routers.mkdir(parents=True)
    (routers / "__init__.py").write_text("", encoding="utf-8")
    (routers / "foo.py").write_text('@router.get("/x")\ndef x(): ...\n', encoding="utf-8")
    (routers / "bar.py").write_text('@router.post("/y")\ndef y(): ...\n', encoding="utf-8")
    main = tmp_path / "forgeflow" / "api" / "main.py"
    main.write_text('include_router(foo.router, prefix="/foo")\n', encoding="utf-8")

    result = StaticChecks(tmp_path).route_registration()
    assert result["status"] == "OK"
    assert result["unmapped_routers"] == ["bar"]
    assert result["registered_prefixes"] == ["/foo"]
    assert result["route_decorators"] == 2


def test_route_registration_without_main_marks_all_unmapped(tmp_path: Path) -> None:
    routers = tmp_path / "forgeflow" / "api" / "routers"
    routers.mkdir(parents=True)
    (routers / "foo.py").write_text('@router.get("/x")\ndef x(): ...\n', encoding="utf-8")
    result = StaticChecks(tmp_path).route_registration()
    assert result["unmapped_routers"] == ["foo"]


# ── static_checks: unresolved imports ────────────────────────────────────────


def test_unresolved_imports_detects_missing_module(tmp_path: Path) -> None:
    pkg = tmp_path / "forgeflow"
    pkg.mkdir()
    (pkg / "mod.py").write_text("import totally_missing_module_xyz_123\n", encoding="utf-8")
    assert "totally_missing_module_xyz_123" in StaticChecks(tmp_path).unresolved_imports()


# ── static_checks: env + port mismatch ───────────────────────────────────────


def test_client_backend_port_mismatch(tmp_path: Path) -> None:
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "vite.config.ts").write_text(
        'const DEFAULT_API_TARGET = "http://localhost:8000";\n', encoding="utf-8"
    )
    rt = tmp_path / "forgeflow" / "bootstrap"
    rt.mkdir(parents=True)
    (rt / "runtime.py").write_text("DEFAULT_PORT = 8001\n", encoding="utf-8")
    mismatches = StaticChecks(tmp_path).client_backend_mismatch()
    assert mismatches and "8000" in mismatches[0] and "8001" in mismatches[0]


def test_client_backend_ports_match(tmp_path: Path) -> None:
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "vite.config.ts").write_text(
        'const DEFAULT_API_TARGET = "http://localhost:8000";\n', encoding="utf-8"
    )
    rt = tmp_path / "forgeflow" / "bootstrap"
    rt.mkdir(parents=True)
    (rt / "runtime.py").write_text("DEFAULT_PORT = 8000\n", encoding="utf-8")
    assert StaticChecks(tmp_path).client_backend_mismatch() == []


# ── discovery ────────────────────────────────────────────────────────────────


def test_real_inventory_has_all_19_layers() -> None:
    inventory = RepositoryIntelligence(find_project_root()).discover(run_id="r")
    assert set(inventory.layers) == set(ALL_LAYERS)
    assert len(inventory.layers) == 19
    assert inventory.totals["_all"] == sum(inventory.totals[layer.value] for layer in ALL_LAYERS)


def test_empty_root_inventory_still_has_all_layers(tmp_path: Path) -> None:
    inventory = RepositoryIntelligence(tmp_path).discover(run_id="r")
    assert set(inventory.layers) == set(ALL_LAYERS)
    assert all(caps == [] for caps in inventory.layers.values())
    assert inventory.technology["has_makefile"] is False


def test_detect_technology_tolerates_malformed_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("this is not === toml [[[\n", encoding="utf-8")
    tech = RepositoryIntelligence(tmp_path).detect_technology()  # must not raise
    assert isinstance(tech, dict)


def test_discovery_report_carries_static_checks() -> None:
    report = RepositoryIntelligence(find_project_root()).discovery_report(run_id="r")
    assert "summary" in report.static_checks
    assert report.inventory.run_id == "r"
    assert isinstance(report.dead_capability_candidates, list)


def test_real_inventory_paths_exist_where_layers_nonempty() -> None:
    root = find_project_root()
    inventory = RepositoryIntelligence(root).discover(run_id="r")
    for layer, caps in inventory.layers.items():
        if layer is Layer.BUILD:
            continue
        for cap in caps:
            assert cap.detected_by  # detection provenance is recorded
            assert (root / cap.path).exists(), f"{cap.path} does not exist"
