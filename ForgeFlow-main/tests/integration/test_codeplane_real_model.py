"""Opt-in real Ollama + isolated code modification acceptance check."""

import os
from types import SimpleNamespace

import pytest

from forgeflow.codeplane.docker_engine import DockerOpenHandsEngine
from forgeflow.codeplane.engine import CodeJob
from forgeflow.codeplane.workspace import WorkspaceManager


@pytest.mark.skipif(os.getenv("FORGEFLOW_RUN_REAL_MODEL_TEST") != "1", reason="real local model test not enabled")
@pytest.mark.asyncio
async def test_real_model_fixes_and_tests_code_in_isolated_workspace(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (source / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    workspace = WorkspaceManager(tmp_path / "workspaces").create("real-model", str(source), tenant_id="test")
    settings = SimpleNamespace(codeplane_enabled=True, codeplane_docker_image="forgeflow-codeplane:1.49.6",
                               codeplane_test_command="python -m pytest -q", codeplane_docker_test_command="python -m pytest -q")
    result = await DockerOpenHandsEngine(settings=settings).run(CodeJob(
        run_id="real-model-test", workspace_path=workspace.path,
        task_intent="Fix calc.py so add returns the sum. Run pytest and report the result.",
        model="ollama_chat/qwen3:4b", base_url="http://127.0.0.1:11434",
        max_rounds=12, wall_timeout_s=180, test_command="python -m pytest -q",
    ))
    assert result.status == "ok", result.to_dict()
    assert result.tests.verdict == "passed", result.to_dict()
    assert (source / "calc.py").read_text(encoding="utf-8").endswith("return a - b\n")
