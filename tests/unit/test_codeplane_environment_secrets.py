import pytest

from forgeflow.codeplane.engine import AgentServerOpenHandsEngine, SubprocessOpenHandsEngine
from forgeflow.codeplane.runner.run_code_task import safe_process_environment


@pytest.mark.parametrize("key", ["POSTGRES_URL", "DATABASE_URL", "JWT_SECRET", "OPENAI_API_KEY",
                                "SESSION_API_KEY", "TESTRELIC_API_KEY", "CUSTOM_BUSINESS_SECRET"])
def test_repository_process_environment_excludes_platform_secrets(monkeypatch, key):
    monkeypatch.setenv(key, "test-only-secret")
    monkeypatch.setenv("PATH", "system-tools")
    env = safe_process_environment()
    assert key not in env
    assert env["PATH"] == "system-tools"
    assert env["PYTEST_ADDOPTS"] == "-p no:testrelic_pytest"


def test_both_transports_use_sanitized_environment(monkeypatch):
    monkeypatch.setenv("POSTGRES_URL", "test-only-database")
    monkeypatch.setenv("SESSION_API_KEY", "ambient-token")
    env = SubprocessOpenHandsEngine._env(None)
    assert "POSTGRES_URL" not in env
    assert "SESSION_API_KEY" not in env
    env = AgentServerOpenHandsEngine._env("server-owned-token")
    assert "POSTGRES_URL" not in env
    assert env["SESSION_API_KEY"] == "server-owned-token"


def test_terminal_overrides_server_nonce_and_legacy_failure_is_closed(monkeypatch):
    import sys
    from types import ModuleType, SimpleNamespace

    from forgeflow.codeplane.runner.run_code_task import _explicit_tools

    terminal = ModuleType("openhands.tools.terminal")
    terminal.TerminalTool = SimpleNamespace(name="terminal")
    editor = ModuleType("openhands.tools.file_editor")
    editor.FileEditorTool = SimpleNamespace(name="file_editor")
    sdk = ModuleType("openhands.sdk")
    sdk.Tool = lambda **kwargs: kwargs
    for name, module in ((terminal.__name__, terminal), (editor.__name__, editor), (sdk.__name__, sdk)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setenv("SESSION_API_KEY", "test-server-nonce")
    tools = _explicit_tools()
    assert tools[0]["params"]["env"]["SESSION_API_KEY"] == ""

    def legacy(**kwargs):
        if "params" in kwargs:
            raise TypeError("unsupported params")
        return kwargs

    sdk.Tool = legacy
    with pytest.raises(RuntimeError, match="isolated terminal environment"):
        _explicit_tools()
