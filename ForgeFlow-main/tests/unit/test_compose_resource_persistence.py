from pathlib import Path

import yaml


def test_compose_persists_the_configured_resource_store():
    root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    api = compose["services"]["api"]
    path = api["environment"]["RESOURCE_STORE_ROOT"]
    assert path == "/var/lib/forgeflow/resources"
    assert f"resource_data:{path}" in api["volumes"]
    assert "resource_data" in compose["volumes"]
