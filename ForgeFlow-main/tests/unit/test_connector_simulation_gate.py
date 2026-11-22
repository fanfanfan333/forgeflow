from types import SimpleNamespace

import pytest

from forgeflow.connectors.base import BaseConnector, ConnectorDisabled


@pytest.mark.asyncio
async def test_missing_credentials_require_explicit_simulation(monkeypatch):
    monkeypatch.setattr(
        "forgeflow.config.get_settings", lambda: SimpleNamespace(connector_mock_enabled=False)
    )
    connector = BaseConnector("https://example.com", None)
    with pytest.raises(ConnectorDisabled):
        await connector._request("POST", "/write")


@pytest.mark.asyncio
async def test_explicit_simulation_returns_marked_result(monkeypatch):
    monkeypatch.setattr(
        "forgeflow.config.get_settings", lambda: SimpleNamespace(connector_mock_enabled=True)
    )
    connector = BaseConnector("https://example.com", None)
    result = await connector._request("POST", "/write")
    assert result["mock"] is True
