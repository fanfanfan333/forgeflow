import json

from forgeflow.runtime.tool_handlers import HANDLERS
from forgeflow.skills.builtin_packages import builtin_packages


def test_four_builtins_use_actual_tools_and_explicit_synthetic_cases():
    packages = builtin_packages()
    assert len(packages) == 4
    assert len({item["hash"] for item in packages}) == 4
    for item in packages:
        from forgeflow.skills.package_import import package_spec
        spec = package_spec(item["files"])
        assert "skills.read" in spec["tools"]
        assert item["version"] == "1.0.1"
        assert set(spec["tools"]) <= (set(HANDLERS) | {"research.read", "research.verify", "evidence.read", "skills.read"})
        cases = json.loads(item["files"]["assets/evals/cases.json"])
        assert cases["provenance"] == "synthetic_regression"
        assert cases["cases"][0]["assertions"]
        assert spec["import_provenance"]["scripts_executed"] is False
