"""Unit tests for :mod:`forgeflow.qa.discovery`."""

from __future__ import annotations

from forgeflow.qa.discovery import RepositoryIntelligence
from forgeflow.qa.models import ALL_LAYERS, Layer
from forgeflow.qa.paths import find_project_root


def test_inventory_has_all_19_layers():
    inventory = RepositoryIntelligence(find_project_root()).discover(run_id="r")
    assert len(inventory.layers) == 19
    assert set(inventory.layers) == set(ALL_LAYERS)
    expected = sum(inventory.totals[layer.value] for layer in ALL_LAYERS)
    assert inventory.totals["_all"] == expected
    assert inventory.totals["_all"] > 0


def test_every_discovered_path_really_exists():
    root = find_project_root()
    inventory = RepositoryIntelligence(root).discover(run_id="r")
    for layer, capabilities in inventory.layers.items():
        for capability in capabilities:
            assert (root / capability.path).exists(), capability.path
            assert capability.capability_id == f"{layer.value}:{capability.path}"


def test_detect_technology_reports_real_facts():
    tech = RepositoryIntelligence(find_project_root()).detect_technology()
    assert tech["has_makefile"] is True
    assert tech["python_package"] == "forgeflow"
    assert "check" in tech["make_targets"]
    assert tech["router_files"] > 0
    assert tech["has_ci_workflow"] is True
    assert tech["test_files"] > 0


def test_build_layer_carries_make_targets():
    inventory = RepositoryIntelligence(find_project_root()).discover(run_id="r")
    make_caps = [c for c in inventory.layers[Layer.BUILD] if c.path == "Makefile"]
    assert make_caps
    assert "check" in make_caps[0].entrypoints


def test_api_layer_lists_router_modules():
    inventory = RepositoryIntelligence(find_project_root()).discover(run_id="r")
    api_caps = [c for c in inventory.layers[Layer.API] if c.path == "forgeflow/api/routers"]
    assert api_caps
    assert "auth" in api_caps[0].entrypoints


def test_discovery_report_embeds_static_checks():
    report = RepositoryIntelligence(find_project_root()).discovery_report(run_id="r")
    assert report.run_id == "r"
    assert report.inventory.run_id == "r"
    assert "summary" in report.static_checks
    assert isinstance(report.dead_capability_candidates, list)
