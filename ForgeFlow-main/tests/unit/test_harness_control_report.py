import copy
import importlib.util
import json
from pathlib import Path

import pytest


def test_comparison_keeps_failures_and_rejects_partial_runs(tmp_path, monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("complete_harness_control", scripts / "complete_harness_control.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    baseline = {"complete": False, "dataset_hash": "fixed", "model_digest": "fixed", "repetitions": 3,
                "temperature": 0, "num_ctx": 8192, "think": False, "source_hash": "old",
                "cases": [{"domain": domain, "case_id": f"{domain}-{i}", "repetition": r, "passed": False,
                           "false_completion": False, "seconds": 1} for domain in ("code", "document", "research") for i in range(10) for r in range(1, 4)]}
    upgraded = copy.deepcopy(baseline)
    upgraded["source_hash"] = "new"
    for name, value in [("baseline", baseline), ("upgraded", upgraded)]:
        (tmp_path / (name + ".json")).write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="finish"):
        module.write_comparison(tmp_path)
    assert not (tmp_path / "comparison.json").exists()
    baseline["complete"] = upgraded["complete"] = True
    baseline["cases"][0]["passed"] = True
    for name, value in [("baseline", baseline), ("upgraded", upgraded)]:
        (tmp_path / (name + ".json")).write_text(json.dumps(value), encoding="utf-8")
    result = module.write_comparison(tmp_path)
    assert result["non_regression"] is False
    assert result["automatically_published"] is False
    assert "0/30" in (tmp_path / "REPORT.md").read_text(encoding="utf-8")
