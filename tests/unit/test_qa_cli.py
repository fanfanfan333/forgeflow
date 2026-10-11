"""Unit tests for :mod:`forgeflow.qa.cli` (parsing + a discover smoke run)."""

from __future__ import annotations

from forgeflow.qa.cli import build_parser, main


def test_parser_defaults():
    args = build_parser().parse_args([])
    assert args.stage == "full"
    assert args.profile == "fast"
    assert args.as_json is False


def test_parser_options():
    args = build_parser().parse_args(
        ["discover", "--profile", "full", "--json", "--timeout", "5", "--run-id", "abc"]
    )
    assert args.stage == "discover"
    assert args.profile == "full"
    assert args.as_json is True
    assert args.timeout == 5
    assert args.run_id == "abc"


def test_parser_rejects_unknown_stage():
    import pytest

    with pytest.raises(SystemExit):
        build_parser().parse_args(["not-a-stage"])


def test_main_discover_writes_inventory(tmp_path, monkeypatch):
    monkeypatch.setenv("QA_TMP_DIR", str(tmp_path / "qatmp"))
    rc = main(["discover"])
    assert rc == 0
    runs = list((tmp_path / "qatmp" / "qa").glob("*"))
    assert runs
    assert (runs[0] / "capability_inventory.json").is_file()


def test_main_invalid_root_returns_two(tmp_path):
    rc = main(["discover", "--root", str(tmp_path / "does-not-exist")])
    assert rc == 2
