"""Regression controls for the security audit; no live network or credentials."""
import io
import zipfile

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.middleware.security import SecurityMiddleware
from forgeflow.security.archive import extract_zip
from forgeflow.security.ssrf_guard import PinnedTransport, SSRFBlocked, _CheckedURL, safe_get


@pytest.mark.asyncio
async def test_pin_transport_preserves_host_and_tls_name(monkeypatch):
    seen = []
    async def send(self, request):
        seen.append((request.url.host, request.headers["Host"], request.extensions["sni_hostname"]))
        return httpx.Response(200, content=b"safe")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    request = httpx.Request("GET", "https://public.example:8443/path", headers={"Host": "127.0.0.1"})
    async with PinnedTransport(_CheckedURL(str(request.url), "public.example", "93.184.216.34")) as transport:
        await transport.handle_async_request(request)
    assert seen == [("93.184.216.34", "public.example:8443", "public.example")]
    assert request.url.host == "public.example"


@pytest.mark.asyncio
async def test_safe_get_limits_stream_and_rechecks_redirect(monkeypatch):
    from forgeflow.security import ssrf_guard
    seen = []
    async def checked(url):
        seen.append(url)
        if "127.0.0.1" in url:
            raise SSRFBlocked("private")
        return _CheckedURL(url, "public.example", "93.184.216.34")
    async def send(self, request):
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
    monkeypatch.setattr(ssrf_guard, "checked_url", checked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    with pytest.raises(SSRFBlocked, match="private"):
        await safe_get("https://public.example/start")
    assert seen == ["https://public.example/start", "http://127.0.0.1/admin"]
    async def large(self, request):
        return httpx.Response(200, content=b"a" * 100)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", large)
    with pytest.raises(SSRFBlocked, match="exceeds"):
        await safe_get("https://public.example/large", max_bytes=16)


@pytest.mark.parametrize("name", ["../outside.txt", "C:/outside.txt", "..\\outside.txt", "/outside.txt", "folder/../../outside.txt"])
def test_zip_rejects_entire_archive_before_any_write(tmp_path, name):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("safe.txt", "safe")
        archive.writestr(name, "unsafe")
    with zipfile.ZipFile(data) as archive, pytest.raises(ValueError):
        extract_zip(archive, tmp_path)
    assert not list(tmp_path.iterdir())


def test_security_scanner_failure_does_not_execute_handler(monkeypatch):
    from forgeflow.middleware import security
    def broken(value):
        raise RuntimeError("scanner unavailable")
    monkeypatch.setattr(security, "_walk", broken)
    app = FastAPI()
    app.add_middleware(SecurityMiddleware)
    calls = []
    @app.post("/task")
    def task():
        calls.append(True)
        return {}
    response = TestClient(app).post("/task", json={"intent": "hello"})
    assert response.status_code == 503 and not calls


def test_route_prefix_requires_path_segment_boundary():
    assert RBACMiddleware._resolve_permission("GET", "/workflows_admin")[0] == ""
    assert RBACMiddleware._resolve_permission("GET", "/workflows/run")[0]


@pytest.mark.asyncio
async def test_safe_get_decodes_gzip_once(monkeypatch):
    import gzip

    from forgeflow.security import ssrf_guard
    async def checked(url):
        return _CheckedURL(url, "public.example", "93.184.216.34")
    async def send(self, request):
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=gzip.compress(b"safe"))
    monkeypatch.setattr(ssrf_guard, "checked_url", checked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert (await safe_get("https://public.example")).content == b"safe"


@pytest.mark.parametrize("addresses",[["198.18.0.9"],["8.8.8.8","127.0.0.1"],["169.254.169.254"]])
def test_secure_dns_never_exempts_private_or_mixed_answers(monkeypatch,addresses):
    from forgeflow.security import ssrf_guard
    monkeypatch.setattr(ssrf_guard,"_resolve_addresses",lambda host:addresses)
    with pytest.raises(SSRFBlocked):
        ssrf_guard.check_url("https://public.example/data")


def test_secure_dns_uses_fixed_tls_endpoint_and_no_environment_proxy(monkeypatch):
    from types import SimpleNamespace

    from forgeflow.security import ssrf_guard
    monkeypatch.setattr("forgeflow.config.get_settings",lambda:SimpleNamespace(ssrf_dns_over_https=True))
    observed=[]
    class Client:
        def __init__(self,**options):assert options["trust_env"] is False and options["follow_redirects"] is False
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def get(self,url,**options):
            observed.append((url,options))
            return httpx.Response(200,json={"Status":0,"Answer":[{"type":1,"data":"8.8.8.8"}]},request=httpx.Request("GET",url))
    monkeypatch.setattr(ssrf_guard.httpx,"Client",Client)
    checked=ssrf_guard.check_url("https://public.example/data")
    assert checked.pinned_ip=="8.8.8.8"
    assert len(observed)==2 and all(url=="https://1.1.1.1/dns-query" for url,_ in observed)
