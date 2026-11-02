"""Opt-in real container boundary check; no model or paid service is used."""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeflow.codeplane.docker_engine import DockerOpenHandsEngine
from forgeflow.codeplane.engine import CodeJob
from forgeflow.codeplane.workspace import WorkspaceManager

pytestmark = pytest.mark.skipif(os.environ.get("FORGEFLOW_RUN_DOCKER_TESTS") != "1", reason="Set FORGEFLOW_RUN_DOCKER_TESTS=1 to run the real Docker boundary check")


def test_actual_container_has_no_platform_secrets_or_writable_control_files(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEFLOW_PRIVATE_PROBE_SECRET", "synthetic-secret-not-forwarded")
    settings = SimpleNamespace(codeplane_enabled=True, codeplane_docker_image="python:3.13-slim",
                               codeplane_test_command="", codeplane_docker_test_command="")
    engine = DockerOpenHandsEngine(settings=settings)
    assert engine.available(), engine.reason
    workspace = WorkspaceManager(tmp_path / "workspaces").create("docker-probe", tenant_id="tenant")
    engine._networks["probe"] = ("none", "", "unused")
    command, _ = engine._launch(CodeJob(run_id="probe", workspace_path=workspace.path, base_url="http://127.0.0.1:11434"))
    position = command.index(engine._image_id or settings.codeplane_docker_image)
    probe = """import json, os
from pathlib import Path
result = {'uid': os.getuid(), 'platform_secret': os.getenv('FORGEFLOW_PRIVATE_PROBE_SECRET'), 'docker_socket': Path('/var/run/docker.sock').exists()}
Path('/workspace/probe.txt').write_text('workspace write is allowed')
result['workspace_write'] = True
for name, path in [('git_write','/workspace/.git/probe'), ('runner_write','/opt/forgeflow-runner/probe'), ('root_write','/etc/forgeflow-probe')]:
    try:
        Path(path).write_text('must not write')
        result[name] = True
    except OSError:
        result[name] = False
print(json.dumps(result))
"""
    command[position + 1:] = ["python", "-c", probe]
    result = subprocess.run(command, capture_output=True, timeout=45, check=False, env=engine._env())
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    facts = json.loads(result.stdout)
    assert facts == {"uid": 1000, "platform_secret": None, "docker_socket": False,
                     "workspace_write": True, "git_write": False, "runner_write": False, "root_write": False}
    assert (Path(workspace.path) / "probe.txt").read_text() == "workspace write is allowed"


def test_runtime_browser_can_verify_interaction_and_save_screenshot(tmp_path):
    settings = SimpleNamespace(codeplane_enabled=True, codeplane_docker_image="forgeflow-codeplane:1.49.6",
                               codeplane_test_command="", codeplane_docker_test_command="")
    engine = DockerOpenHandsEngine(settings=settings)
    assert engine.available(), engine.reason
    workspace = WorkspaceManager(tmp_path / "workspaces").create("browser-probe", tenant_id="tenant")
    engine._networks["browser"] = ("none", "", "unused")
    command, _ = engine._launch(CodeJob(run_id="browser", workspace_path=workspace.path, base_url="http://127.0.0.1:11434"))
    position = command.index(engine._image_id or settings.codeplane_docker_image)
    probe = """const { chromium } = require('@playwright/test');
(async () => {
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage();
  await page.setContent('<button onclick="this.textContent=Number(this.textContent)+1">0</button>');
  await page.getByRole('button').click();
  if (await page.getByRole('button').textContent() !== '1') throw new Error('Interaction failed');
  await page.screenshot({path:'/workspace/browser-proof.png'});
  await browser.close();
  console.log(JSON.stringify({interaction: true, screenshot: true}));
})().catch(error => { console.error(error); process.exit(1); });
"""
    command[position + 1:] = ["node", "-e", probe]
    result = subprocess.run(command, capture_output=True, timeout=60, check=False, env=engine._env())
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert json.loads(result.stdout) == {"interaction": True, "screenshot": True}
    assert (Path(workspace.path) / "browser-proof.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_project_declared_node_checks_run_in_actual_container(tmp_path):
    from forgeflow.codeplane.project import project_checks
    from forgeflow.codeplane.tests_verdict import evaluate_test_output

    settings = SimpleNamespace(codeplane_enabled=True, codeplane_docker_image="forgeflow-codeplane:1.49.6",
                               codeplane_test_command="python -m pytest -q", codeplane_docker_test_command="python -m pytest -q")
    engine = DockerOpenHandsEngine(settings=settings)
    assert engine.available(), engine.reason
    workspace = WorkspaceManager(tmp_path / "workspaces").create("node-checks", tenant_id="tenant")
    root = Path(workspace.path)
    (root / "package.json").write_text(json.dumps({"scripts": {"typecheck": "node --check math.js", "test": "node --test"}}))
    (root / "math.js").write_text("exports.add = (a,b) => a+b;\n")
    (root / "math.test.js").write_text("const {test}=require('node:test');const assert=require('node:assert/strict');const {add}=require('./math');test('add',()=>assert.equal(add(2,3),5));")
    check = project_checks(workspace.path)
    engine._networks["node-checks"] = ("none", "", "unused")
    command, _ = engine._launch(CodeJob(run_id="node-checks", workspace_path=workspace.path, base_url="http://127.0.0.1:11434"))
    position = command.index(engine._image_id)
    command[position + 1:] = ["sh", "-c", check["command"]]
    result = subprocess.run(command, capture_output=True, timeout=60, check=False, env=engine._env())
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    reviewed = evaluate_test_output(result.stdout.decode(), result.returncode)
    assert reviewed.measured and reviewed.verdict == "passed" and reviewed.passed == 1, result.stdout.decode()
