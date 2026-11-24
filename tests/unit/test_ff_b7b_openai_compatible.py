"""FF-R1 B7b (FF-G15) — the openai_compatible provider.

Contract pinned here:

* ``base_url`` and ``api_key`` come ONLY from the environment
  (``OPENAI_COMPATIBLE_BASE_URL`` / ``OPENAI_COMPATIBLE_API_KEY``);
  the key appears in the ``Authorization`` header and NOWHERE else —
  not the URL, not the body, not logs, not errors, not ``repr``;
* missing configuration fails with a clear error naming what is
  missing (never a value);
* tool calls via ``bind_tools`` parse back into the platform's
  unified ``AIMessage.tool_calls`` format;
* HTTP errors and timeouts surface as clear provider errors.

Everything runs against a local fake HTTP server on 127.0.0.1 — no
real network, no paid models. The test key is an obvious fake.
"""

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from forgeflow.config import get_settings
from forgeflow.models.provider import get_model

FAKE_KEY = "sk-FAKE-B7B-KEY-000111222333"
FAKE_MODEL = "fake-model-1"


class _Capture:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.behavior = "reply"


class _Handler(BaseHTTPRequestHandler):
    capture: _Capture = None  # set by the fixture

    def log_message(self, *args):  # silence the test server
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.capture.requests.append(
            {
                "path": self.path,
                "headers": dict(self.headers),
                "body": body.decode("utf-8", "replace"),
            }
        )
        behavior = self.capture.behavior
        if behavior == "sleep":
            time.sleep(3)
        if behavior == "401":
            payload, status = {"error": {"message": "unauthorized"}}, 401
        elif behavior == "500":
            payload, status = {"error": {"message": "boom"}}, 500
        elif behavior == "tool_call":
            payload, status = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": json.dumps({"city": "Shanghai"}),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            }, 200
        else:
            payload, status = {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "fake reply"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            }, 200
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def fake_server():
    capture = _Capture()
    _Handler.capture = capture
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield capture, f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def configured_env(monkeypatch, fake_server):
    _capture, base_url = fake_server
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", base_url)
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", FAKE_KEY)
    monkeypatch.setenv("OPENAI_COMPATIBLE_MODEL", FAKE_MODEL)
    get_settings.cache_clear()
    return fake_server


# --------------------------------------------------------------------- #
# Plain reply + unified parsing                                          #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_openai_compatible_plain_reply(configured_env):
    model = get_model()
    result = await model.ainvoke([HumanMessage(content="hello")])
    assert result.content == "fake reply"


class _WeatherArgs(BaseModel):
    city: str


@pytest.mark.asyncio
async def test_openai_compatible_tool_call_parsing(configured_env):
    capture, _base_url = configured_env
    capture.behavior = "tool_call"

    async def _weather(city: str) -> str:
        return "sunny"

    tool = StructuredTool.from_function(
        coroutine=_weather, name="get_weather", description="weather lookup", args_schema=_WeatherArgs
    )
    model = get_model().bind_tools([tool])
    result = await model.ainvoke([HumanMessage(content="weather in Shanghai?")])
    assert result.tool_calls
    call = result.tool_calls[0]
    assert call["name"] == "get_weather"
    assert call["args"] == {"city": "Shanghai"}
    # The request carried the tool definition in OpenAI function format.
    sent = json.loads(capture.requests[0]["body"])
    assert sent["tools"][0]["function"]["name"] == "get_weather"
    assert sent["model"] == FAKE_MODEL


# --------------------------------------------------------------------- #
# Errors never carry the key                                             #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["401", "500"])
async def test_openai_compatible_http_errors_do_not_leak_key(configured_env, behavior, caplog):
    capture, _base_url = configured_env
    capture.behavior = behavior
    model = get_model()
    with caplog.at_level(logging.DEBUG), pytest.raises(Exception) as raised:
        await model.ainvoke([HumanMessage(content="hello")])
    assert FAKE_KEY not in str(raised.value)
    assert FAKE_KEY not in caplog.text


@pytest.mark.asyncio
async def test_openai_compatible_timeout(configured_env, monkeypatch):
    capture, _base_url = configured_env
    capture.behavior = "sleep"
    monkeypatch.setenv("OPENAI_COMPATIBLE_TIMEOUT_SECONDS", "0.3")
    model = get_model()
    started = time.perf_counter()
    with pytest.raises(Exception) as raised:
        await model.ainvoke([HumanMessage(content="hello")])
    assert time.perf_counter() - started < 2.5
    assert "timed out" in str(raised.value).lower() or "timeout" in str(raised.value).lower()
    assert FAKE_KEY not in str(raised.value)


# --------------------------------------------------------------------- #
# Missing configuration                                                  #
# --------------------------------------------------------------------- #


def test_openai_compatible_missing_base_url(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.delenv("OPENAI_COMPATIBLE_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", FAKE_KEY)
    get_settings.cache_clear()
    with pytest.raises(Exception) as raised:
        get_model()
    assert "OPENAI_COMPATIBLE_BASE_URL" in str(raised.value)
    assert FAKE_KEY not in str(raised.value)


def test_openai_compatible_missing_api_key(monkeypatch, fake_server):
    _capture, base_url = fake_server
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", base_url)
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    get_settings.cache_clear()
    with pytest.raises(Exception) as raised:
        get_model()
    assert "OPENAI_COMPATIBLE_API_KEY" in str(raised.value)


# --------------------------------------------------------------------- #
# The key lives ONLY in the Authorization header                         #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_openai_compatible_key_only_in_authorization_header(configured_env, caplog):
    capture, _base_url = configured_env
    model = get_model()
    assert FAKE_KEY not in repr(model)
    with caplog.at_level(logging.DEBUG):
        await model.ainvoke([HumanMessage(content="hello")])
    assert len(capture.requests) == 1
    request = capture.requests[0]
    assert request["headers"].get("Authorization") == f"Bearer {FAKE_KEY}"
    assert FAKE_KEY not in request["path"]
    assert FAKE_KEY not in request["body"]
    for name, value in request["headers"].items():
        if name.lower() != "authorization":
            assert FAKE_KEY not in value
    assert FAKE_KEY not in caplog.text
