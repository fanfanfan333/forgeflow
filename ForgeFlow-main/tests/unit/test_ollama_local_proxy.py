import sys
from types import ModuleType, SimpleNamespace

import pytest

from forgeflow.models.provider import _build_ollama_model


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://localhost:11434", {"trust_env": False}),
        ("http://127.0.0.1:11434", {"trust_env": False}),
        ("http://[::1]:11434", {"trust_env": False}),
        ("https://models.example.org", {}),
        ("https://localhost.example.org", {}),
    ],
)
def test_only_local_inference_bypasses_system_proxy(monkeypatch, url, expected):
    captured = {}

    def build(**kwargs):
        captured.update(kwargs)
        return captured

    module = ModuleType("langchain_ollama")
    module.ChatOllama = build
    monkeypatch.setitem(sys.modules, "langchain_ollama", module)
    settings = SimpleNamespace(ollama_base_url=url, ollama_think=False, ollama_num_ctx=8192)
    _build_ollama_model(settings, "qwen3:8b")
    assert captured["client_kwargs"] == expected
