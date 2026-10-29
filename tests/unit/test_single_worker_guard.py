import pytest

from forgeflow.bootstrap.runtime import run_api_server


def test_managed_api_rejects_multiple_workers_before_starting(monkeypatch):
    monkeypatch.setattr(
        "forgeflow.bootstrap.runtime.install_windows_selector_policy",
        lambda: (_ for _ in ()).throw(RuntimeError("attempted startup")),
    )
    with pytest.raises(ValueError, match="single.*worker"):
        run_api_server(port=8010, workers=2)


def test_managed_api_rejects_environment_worker_count(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(ValueError, match="single.*worker"):
        run_api_server(port=8010)
