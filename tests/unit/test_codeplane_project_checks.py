import json
from types import SimpleNamespace

import pytest

from forgeflow.codeplane.engine import CodeJob, _merge_job_defaults
from forgeflow.codeplane.project import project_checks
from forgeflow.codeplane.tests_verdict import evaluate_test_output
from forgeflow.runtime.tool_handlers import _derive_code_outcome


def test_node_checks_follow_project_declarations(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {
        "typecheck": "tsc --noEmit", "test": "vitest", "build": "vite build"}}))
    checks = project_checks(str(tmp_path))
    assert checks["command"] == "npm run typecheck && npm run test -- --run && npm run build"
    assert checks["test_declared"] is True
    job = _merge_job_defaults(CodeJob(workspace_path=str(tmp_path), model="m", base_url="http://localhost"), SimpleNamespace())
    assert job.test_command == checks["command"]
    assert job._project_checks["content_hash"] == checks["content_hash"]


def test_build_only_is_not_fabricated_test_evidence(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts":{"build":"tsc -b"}}')
    assert project_checks(str(tmp_path))["test_declared"] is False
    tests = evaluate_test_output("build completed", 0).to_dict()
    assert _derive_code_outcome(status="ok", degraded=None, diff="changed", tests=tests) == "unverified"


def test_explicit_test_command_is_preserved(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest"}}')
    job = _merge_job_defaults(CodeJob(workspace_path=str(tmp_path), test_command="custom check", model="m", base_url="u"), SimpleNamespace())
    assert job.test_command == "custom check"


@pytest.mark.parametrize("content", ['broken', '[]', '{"scripts":[]}'])
def test_invalid_project_config_never_falls_back_to_python(tmp_path, content):
    (tmp_path / "package.json").write_text(content)
    with pytest.raises(ValueError):
        project_checks(str(tmp_path))


def test_unsupported_project_preserves_configured_check(tmp_path):
    job = _merge_job_defaults(CodeJob(workspace_path=str(tmp_path), model="m", base_url="u"), SimpleNamespace(codeplane_test_command="python -m pytest -q"))
    assert job.test_command == "python -m pytest -q"
