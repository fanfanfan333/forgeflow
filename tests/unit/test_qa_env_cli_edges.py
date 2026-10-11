"""Edge tests for :mod:`forgeflow.qa.environment_guard` and :mod:`forgeflow.qa.cli`.

* the guard reports *facts* and never raises on an unreachable host / missing
  module;
* the CLI exits ``2`` on a usage/programming error and ``0`` on ``--help``.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from forgeflow.qa.cli import build_parser, main
from forgeflow.qa.config import QaRunConfig
from forgeflow.qa.environment_guard import EnvironmentGuard


def _guard(tmp_path: Path) -> EnvironmentGuard:
    config = QaRunConfig(project_root=tmp_path, artifacts_dir=tmp_path / "run", run_id="r")
    return EnvironmentGuard(config)


# ── dependency probe ─────────────────────────────────────────────────────────


def test_missing_dependency_is_reported_not_raised(tmp_path: Path) -> None:
    finding = _guard(tmp_path).check_dependency("a_module_that_cannot_exist_xyz_123")
    assert finding.ok is False
    assert finding.check == "missing_dependency"
    assert finding.severity == "blocking"


def test_present_dependency_is_ok(tmp_path: Path) -> None:
    finding = _guard(tmp_path).check_dependency("sys")
    assert finding.ok is True


def test_optional_missing_dependency_can_be_warning(tmp_path: Path) -> None:
    finding = _guard(tmp_path).check_dependency(
        "a_module_that_cannot_exist_xyz_123", severity="warning"
    )
    assert finding.severity == "warning"


# ── service / port probes ────────────────────────────────────────────────────


def _listening_socket() -> tuple[socket.socket, int]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    return server, server.getsockname()[1]


def _free_port() -> int:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.close()
    return port


def test_check_service_reachable(tmp_path: Path) -> None:
    server, port = _listening_socket()
    try:
        finding = _guard(tmp_path).check_service("127.0.0.1", port)
        assert finding.ok is True
        assert finding.check == "service_unavailable"
    finally:
        server.close()


def test_check_service_unreachable_is_a_warning(tmp_path: Path) -> None:
    finding = _guard(tmp_path).check_service("127.0.0.1", _free_port(), timeout_s=0.5)
    assert finding.ok is False
    assert finding.severity == "warning"


def test_check_port_free_vs_in_use(tmp_path: Path) -> None:
    guard = _guard(tmp_path)
    assert guard.check_port("127.0.0.1", _free_port()).ok is True  # free → ok

    server, port = _listening_socket()
    try:
        finding = guard.check_port("127.0.0.1", port)
        assert finding.ok is False
        assert finding.severity == "warning"
    finally:
        server.close()


# ── python / browser / run_all ───────────────────────────────────────────────


def test_check_python_accepts_real_interpreter(tmp_path: Path) -> None:
    finding = _guard(tmp_path).check_python()
    assert finding.ok is True
    assert finding.check == "wrong_python"


def test_check_browser_returns_a_finding(tmp_path: Path) -> None:
    finding = _guard(tmp_path).check_browser()
    assert finding.check == "browser_unavailable"
    assert finding.severity == "info"  # never blocking in A–C


def test_run_all_covers_core_checks(tmp_path: Path) -> None:
    findings = _guard(tmp_path).run_all()
    checks = {finding.check for finding in findings}
    assert "wrong_python" in checks
    assert "missing_dependency" in checks
    assert "service_unavailable" in checks
    assert "browser_unavailable" in checks


# ── CLI parsing ──────────────────────────────────────────────────────────────


def test_default_stage_is_full() -> None:
    args = build_parser().parse_args([])
    assert args.stage == "full"
    assert args.profile == "fast"


def test_profile_and_stage_parsed() -> None:
    args = build_parser().parse_args(["plan", "--profile", "full"])
    assert args.stage == "plan"
    assert args.profile == "full"


def test_invalid_stage_exits_2() -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["nonsense"])
    assert exc.value.code == 2


def test_invalid_profile_exits_2() -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--profile", "turbo"])
    assert exc.value.code == 2


def test_help_exits_0() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_main_reports_error_as_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # tmp_path is not a ForgeFlow project root → resolution fails → clean exit 2.
    code = main(["discover", "--root", str(tmp_path), "--json"])
    assert code == 2
    assert "ERROR" in capsys.readouterr().err
