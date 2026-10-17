from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from forgeflow.connectors.base import PermanentError
from forgeflow.connectors.sap import SAPConnector


@pytest.mark.asyncio
async def test_csrf_fetch_rejects_private_address_before_connecting(monkeypatch):
    factory = Mock()
    monkeypatch.setattr("httpx.AsyncClient", factory)
    connector = SAPConnector(base_url="http://127.0.0.1", username="u", password="p")
    with pytest.raises(PermanentError, match="SSRF-blocked"):
        await connector._fetch_csrf_token()
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_csrf_fetch_uses_verified_transport(monkeypatch):
    from forgeflow.security.ssrf_guard import PinnedTransport, _CheckedURL

    monkeypatch.setattr(
        "forgeflow.security.ssrf_guard.checked_url",
        AsyncMock(
            return_value=_CheckedURL("https://sap.example/path", "sap.example", "93.184.216.34")
        ),
    )
    response = Mock(headers={"x-csrf-token": "token"}, cookies=httpx.Cookies())
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.head.return_value = response
    factory = Mock(return_value=client)
    monkeypatch.setattr("httpx.AsyncClient", factory)
    connector = SAPConnector(base_url="https://sap.example", username="u", password="p")
    await connector._fetch_csrf_token()
    options = factory.call_args.kwargs
    assert isinstance(options["transport"], PinnedTransport)
    assert options["follow_redirects"] is False
    assert options["trust_env"] is False
    assert connector._csrf_token == "token"


@pytest.mark.asyncio
async def test_write_keeps_csrf_session_and_cookie_path_rules(monkeypatch):
    from forgeflow.security import ssrf_guard

    seen = []

    async def response(request):
        if request.method == "HEAD":
            return httpx.Response(
                200,
                headers={
                    "x-csrf-token": "session-token",
                    "set-cookie": "SAP_SESSIONID=test; Path=/sap; Secure",
                },
            )
        seen.append(request)
        return httpx.Response(200, json={"d": {}})

    monkeypatch.setattr(
        ssrf_guard, "PinnedTransport", lambda checked: httpx.MockTransport(response)
    )
    connector = SAPConnector(base_url="https://sap.example", username="u", password="p")
    await connector.create_sales_order(sold_to_party="001", sales_organization="1710")
    assert seen[0].headers["x-csrf-token"] == "session-token"
    assert seen[0].headers["cookie"] == "SAP_SESSIONID=test"
    await connector._request("GET", "/outside-cookie-path")
    assert "cookie" not in seen[1].headers


@pytest.mark.asyncio
async def test_csrf_retry_uses_rotated_cookie_and_token(monkeypatch):
    from forgeflow.security import ssrf_guard

    fetches = []
    writes = []

    async def response(request):
        if request.method == "HEAD":
            version = str(len(fetches) + 1)
            fetches.append(version)
            return httpx.Response(
                200,
                headers={
                    "x-csrf-token": version,
                    "set-cookie": f"SAP_SESSIONID={version}; Path=/sap; Secure",
                },
            )
        writes.append((request.headers["x-csrf-token"], request.headers["cookie"]))
        return httpx.Response(403 if len(writes) == 1 else 200, json={"d": {}})

    monkeypatch.setattr(
        ssrf_guard, "PinnedTransport", lambda checked: httpx.MockTransport(response)
    )
    connector = SAPConnector(base_url="https://sap.example", username="u", password="p")
    await connector.create_sales_order(sold_to_party="001", sales_organization="1710")
    assert writes == [("1", "SAP_SESSIONID=1"), ("2", "SAP_SESSIONID=2")]
