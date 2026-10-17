"""Unit tests for :mod:`forgeflow.qa.environment_guard`."""

from __future__ import annotations

from pathlib import Path

from forgeflow.qa.config import QaRunConfig
from forgeflow.qa.environment_guard import EnvironmentGuard
from forgeflow.qa.paths import find_project_root


def _config(tmp_path: Path) -> QaRunConfig:
    return QaRunConfig(
        project_root=find_project_root(),
        artifacts_dir=tmp_path / "run",
        run_id="env-run",
    )


def test_check_python_reports_real_interpreter(tmp_path):
    finding = EnvironmentGuard(_config(tmp_path)).check_python()
    assert finding.check == "wrong_python"
    assert finding.ok is True
    assert finding.severity == "info"
    assert finding.detail


def test_check_dependency_missing_is_blocking(tmp_path):
    finding = EnvironmentGuard(_config(tmp_path)).check_dependency(
        "definitely_not_a_real_module_xyz"
    )
    assert finding.check == "missing_dependency"
    assert finding.ok is False
    assert finding.severity == "blocking"


def test_check_dependency_present(tmp_path):
    finding = EnvironmentGuard(_config(tmp_path)).check_dependency("pytest")
    assert finding.ok is True


def test_check_service_unreachable_is_warning(tmp_path, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr("forgeflow.qa.environment_guard.socket.create_connection", _boom)
    finding = EnvironmentGuard(_config(tmp_path)).check_service("localhost", 12345)
    assert finding.check == "service_unavailable"
    assert finding.ok is False
    assert finding.severity == "warning"


def test_check_service_reachable(tmp_path, monkeypatch):
    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(
        "forgeflow.qa.environment_guard.socket.create_connection",
        lambda *_a, **_k: _Ctx(),
    )
    finding = EnvironmentGuard(_config(tmp_path)).check_service("localhost", 1)
    assert finding.ok is True


def test_check_port_free_and_in_use(tmp_path, monkeypatch):
    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def _boom(*_args, **_kwargs):
        raise OSError("closed")

    guard = EnvironmentGuard(_config(tmp_path))
    monkeypatch.setattr("forgeflow.qa.environment_guard.socket.create_connection", _boom)
    assert guard.check_port("localhost", 9).ok is True  # free
    monkeypatch.setattr(
        "forgeflow.qa.environment_guard.socket.create_connection",
        lambda *_a, **_k: _Ctx(),
    )
    assert guard.check_port("localhost", 9).ok is False  # in use


def test_check_browser_reports_a_finding(tmp_path):
    finding = EnvironmentGuard(_config(tmp_path)).check_browser()
    assert finding.check == "browser_unavailable"
    assert finding.severity in {"info", "warning"}
    assert finding.detail


def test_run_all_includes_python_probe(tmp_path):
    findings = EnvironmentGuard(_config(tmp_path)).run_all()
    assert any(f.check == "wrong_python" for f in findings)
    assert any(f.check == "missing_dependency" for f in findings)
    assert all(f.detail for f in findings)
