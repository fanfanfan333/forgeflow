import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from scripts import run_viking_model_adapter as adapter


async def test_native_budget_thinking_and_tool_contract(tmp_path, monkeypatch):
    config = tmp_path / "ov.conf"
    config.write_text(json.dumps({"vlm": {"api_key": "private", "model": "local"}}))
    monkeypatch.setattr(adapter, "CONFIG", config)
    monkeypatch.setattr(adapter, "pending", [])
    monkeypatch.setattr(adapter, "ready", asyncio.Event())
    seen = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json):
            seen.append(json)
            return SimpleNamespace(
                is_error=False,
                json=lambda: {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {"function": {"name": "read", "arguments": {"uri": "owned"}}}
                        ],
                    },
                    "prompt_eval_count": 10,
                    "eval_count": 4,
                },
            )

    monkeypatch.setattr(adapter.httpx, "AsyncClient", Client)
    worker = asyncio.create_task(adapter.native_worker())
    try:
        result = await adapter.completion(
            {
                "model": "local",
                "max_tokens": 9000,
                "messages": [{"role": "user", "content": "test"}],
                "tools": [{"type": "function", "function": {"name": "read"}}],
            },
            authorization="Bearer private",
        )
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    assert seen[0]["think"] is False
    assert seen[0]["options"]["num_ctx"] == 16384
    assert seen[0]["options"]["num_predict"] == 2048
    assert result["usage"]["total_tokens"] == 14
    assert json.loads(
        result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    ) == {"uri": "owned"}


async def test_adapter_rejects_foreign_credential_before_enqueue(tmp_path, monkeypatch):
    config = tmp_path / "ov.conf"
    config.write_text(json.dumps({"vlm": {"api_key": "private", "model": "local"}}))
    monkeypatch.setattr(adapter, "CONFIG", config)
    with pytest.raises(HTTPException) as error:
        await adapter.completion({"model": "local"}, authorization="Bearer foreign")
    assert error.value.status_code == 401
