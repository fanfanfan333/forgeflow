"""INC17 T01 — the new ``react`` runtime mode and its provider resolution.

INC17 adds a **third** executor mode, ``react`` (the Qwen-driven multi-round
tool-calling closed loop), and changes the ``auto`` default so a **real**
provider now resolves to it. This file pins the whole resolution table:

  * ``auto`` + ``mock`` / ``""``  ⇒ ``deterministic`` (offline profile unchanged);
  * ``auto`` + a real provider   ⇒ ``react`` (the INC17 default experience);
  * explicit ``llm`` / ``react`` / ``deterministic`` pin the path regardless of
    provider (so the offline suite can still exercise each path);
  * an unknown / invalid value falls back to ``auto`` (with a warning).

The settings are read through a read-through **proxy** (the ``Settings`` model is
frozen-shape, so a test cannot set an undeclared attribute on an instance) — the
same technique the pre-existing ``test_runtime_llm_agent`` uses.
"""

from __future__ import annotations

import forgeflow.runtime.orchestrator as orch
from forgeflow.config import get_settings
from forgeflow.runtime.orchestrator import (
    _AGENT_RUNTIME_MODES,
    resolve_agent_runtime_mode,
)

_UNSET = object()


class _SettingsProxy:
    """Read-through proxy overriding ``agent_runtime_mode`` / ``llm_provider``."""

    def __init__(self, real, *, mode: object = _UNSET, provider: object = _UNSET) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_mode", mode)
        object.__setattr__(self, "_provider", provider)

    def __getattr__(self, item: str):
        if item == "agent_runtime_mode" and self._mode is not _UNSET:
            return self._mode
        if item == "llm_provider" and self._provider is not _UNSET:
            return self._provider
        return getattr(self._real, item)


def _patch_settings(monkeypatch, *, mode=None, provider=None) -> None:
    proxy = _SettingsProxy(
        get_settings(),
        mode=_UNSET if mode is None else mode,
        provider=_UNSET if provider is None else provider,
    )
    monkeypatch.setattr(orch, "get_settings", lambda: proxy)


# --------------------------------------------------------------------------- #
# 1. "react" is a first-class mode                                              #
# --------------------------------------------------------------------------- #
def test_react_is_an_accepted_mode(force_memory_backend):
    assert "react" in _AGENT_RUNTIME_MODES
    # The other three are unchanged (no accidental removal).
    assert {"auto", "llm", "deterministic"} <= set(_AGENT_RUNTIME_MODES)


# --------------------------------------------------------------------------- #
# 2. auto resolves from the provider — a real provider ⇒ react (INC17 default)   #
# --------------------------------------------------------------------------- #
def test_auto_with_a_real_provider_resolves_to_react(monkeypatch, force_memory_backend):
    for provider in ("ollama", "OLLAMA", "some-real-provider"):
        _patch_settings(monkeypatch, mode="auto", provider=provider)
        assert resolve_agent_runtime_mode() == "react", provider


def test_auto_with_mock_or_empty_resolves_to_deterministic(monkeypatch, force_memory_backend):
    for provider in ("mock", "MOCK", "", "  "):
        _patch_settings(monkeypatch, mode="auto", provider=provider)
        assert resolve_agent_runtime_mode() == "deterministic", repr(provider)


# --------------------------------------------------------------------------- #
# 3. Explicit modes pin the path regardless of provider                          #
# --------------------------------------------------------------------------- #
def test_explicit_llm_pins_the_llm_path(monkeypatch, force_memory_backend):
    # Even with the offline provider, an explicit "llm" selects the llm path
    # (this is what keeps the pre-INC17 path testable offline).
    _patch_settings(monkeypatch, mode="llm", provider="mock")
    assert resolve_agent_runtime_mode() == "llm"
    _patch_settings(monkeypatch, mode="llm", provider="ollama")
    assert resolve_agent_runtime_mode() == "llm"


def test_explicit_react_pins_the_react_path(monkeypatch, force_memory_backend):
    # An explicit "react" is reachable even offline (so the loop is unit-testable
    # with a scripted fake model and no daemon).
    _patch_settings(monkeypatch, mode="react", provider="mock")
    assert resolve_agent_runtime_mode() == "react"
    _patch_settings(monkeypatch, mode="react", provider="ollama")
    assert resolve_agent_runtime_mode() == "react"


def test_explicit_deterministic_wins_even_with_a_real_provider(monkeypatch, force_memory_backend):
    _patch_settings(monkeypatch, mode="deterministic", provider="ollama")
    assert resolve_agent_runtime_mode() == "deterministic"


# --------------------------------------------------------------------------- #
# 4. An invalid value falls back to auto (→ provider-derived)                    #
# --------------------------------------------------------------------------- #
def test_unknown_mode_falls_back_to_auto(monkeypatch, force_memory_backend):
    _patch_settings(monkeypatch, mode="bogus", provider="mock")
    assert resolve_agent_runtime_mode() == "deterministic"
    _patch_settings(monkeypatch, mode="bogus", provider="ollama")
    assert resolve_agent_runtime_mode() == "react"
