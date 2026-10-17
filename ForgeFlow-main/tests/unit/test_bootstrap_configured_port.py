from unittest.mock import Mock

import pytest

from forgeflow.bootstrap import runtime
from forgeflow.config import get_settings


@pytest.mark.parametrize("explicit,expected", [(None, 8123), (8124, 8124)])
def test_server_reads_config_unless_cli_overrides(monkeypatch, explicit, expected):
    monkeypatch.setattr(get_settings(), "api_port", 8123)
    server = Mock(started=True)
    factory = Mock(return_value=server)
    monkeypatch.setattr(runtime.uvicorn, "Server", factory)
    runtime.run_api_server(port=explicit)
    assert factory.call_args.args[0].port == expected
    server.run.assert_called_once()
