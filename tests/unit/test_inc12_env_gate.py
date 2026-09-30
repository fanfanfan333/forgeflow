"""INC12 A3 — the environment gate for development-only stubs.

Pins three things:

  * ``Settings.environment()`` / ``allows_development_tools()`` normalise
    ``app_env`` (fail-closed to ``prod`` on an invalid value) and only ``dev``
    permits development tools;
  * ``Settings.is_production()`` keeps its **existing** return value unchanged;
  * the two silent-mock sites are gated: ``web_search`` refuses to return mock
    outside dev and ``query_db`` fails loud outside dev.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from forgeflow.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        api_secret_key="a-sufficiently-strong-secret",
        dev_login_enabled=False,
        dev_login_password="",
        llm_provider="ollama",
        cors_allow_origins="https://app.example.com",
        docs_enabled=False,
        otel_environment="development",
        trusted_proxy_count=1,
        # Declare the search key explicitly: the web_search gate test must not
        # depend on a developer's local .env (a real TAVILY_API_KEY there would
        # flip is_tavily_enabled() to True and make the refusal test meaninglessly
        # green/red by environment). Same "declare your env" discipline as the
        # storage-backend fixture.
        tavily_api_key="",
    )
    base.update(overrides)
    return Settings(**base)


# --------------------------------------------------------------------------- #
# 1. environment() / allows_development_tools()                                #
# --------------------------------------------------------------------------- #
def test_default_app_env_is_dev_and_allows_dev_tools():
    s = _settings()
    assert s.app_env == "dev"
    assert s.environment() == "dev"
    assert s.allows_development_tools() is True


@pytest.mark.parametrize("raw,expected", [("dev", "dev"), ("staging", "staging"), ("prod", "prod")])
def test_environment_normalises_legal_values(raw, expected):
    s = _settings(app_env=raw)
    assert s.environment() == expected
    assert s.allows_development_tools() is (expected == "dev")


def test_invalid_app_env_fails_closed_to_prod():
    s = _settings(app_env="not-a-real-env")
    assert s.environment() == "prod"
    assert s.allows_development_tools() is False


@pytest.mark.parametrize("raw,expected", [("staging", "staging"), ("production", "prod"), ("PROD", "prod")])
def test_non_dev_environments_refuse_dev_tools(raw, expected):
    s = _settings(app_env=raw)
    assert s.environment() == expected
    assert s.allows_development_tools() is False


# --------------------------------------------------------------------------- #
# 2. is_production() return value is unchanged                                 #
# --------------------------------------------------------------------------- #
def test_is_production_return_value_is_unchanged():
    assert _settings(otel_environment="production").is_production() is True
    assert _settings(otel_environment="prod").is_production() is True
    assert _settings(otel_environment="staging").is_production() is True
    assert _settings(otel_environment="development").is_production() is False
    # An unrecognised label still yields False, exactly as before.
    assert _settings(otel_environment="weird").is_production() is False
    # is_production() keys off otel_environment, NOT app_env.
    assert _settings(otel_environment="development", app_env="prod").is_production() is False


# --------------------------------------------------------------------------- #
# 3. validate_runtime — the new non-dev/mock inconsistency check               #
# --------------------------------------------------------------------------- #
def test_non_dev_env_with_mock_stub_is_flagged():
    problems = _settings(app_env="prod", llm_provider="mock").validate_runtime()
    assert any("development-only stub" in p for p in problems)


def test_dev_env_with_mock_stub_is_not_flagged_by_new_check():
    s = _settings(app_env="dev", llm_provider="mock")
    problems = s.validate_runtime()
    assert not any("development-only stub" in p for p in problems)


def test_default_profile_is_unchanged():
    # The default (app_env=dev) offline profile gains no new fatal problem.
    s = _settings()
    assert not any("development-only stub" in p for p in s.validate_runtime())


# --------------------------------------------------------------------------- #
# 4. web_search — no silent mock outside dev                                   #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_web_search_returns_mock_in_dev():
    from forgeflow.mcp.server.tools import search_tools

    with patch.object(search_tools, "get_settings", return_value=_settings(app_env="dev")):
        results = await search_tools.web_search("Stripe funding", max_results=3)
    assert isinstance(results, list) and results
    assert "content" in results[0]  # the development-stub mock
    assert "error" not in results[0]


@pytest.mark.asyncio
async def test_web_search_refuses_mock_outside_dev():
    from forgeflow.mcp.server.tools import search_tools

    with patch.object(search_tools, "get_settings", return_value=_settings(app_env="prod")):
        results = await search_tools.web_search("Stripe funding", max_results=3)
    assert results == [{"error": "tavily_unconfigured", "query": "Stripe funding"}]


# --------------------------------------------------------------------------- #
# 5. query_db — fail loud outside dev                                          #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_query_db_returns_rows_in_dev():
    from forgeflow.mcp.server.tools import data_tools

    with patch.object(data_tools, "get_settings", return_value=_settings(app_env="dev")):
        rows = await data_tools.query_db("leads")
    assert isinstance(rows, list) and rows


@pytest.mark.asyncio
async def test_query_db_raises_outside_dev():
    from forgeflow.mcp.server.tools import data_tools

    with patch.object(data_tools, "get_settings", return_value=_settings(app_env="staging")):
        with pytest.raises(RuntimeError):
            await data_tools.query_db("leads")


# --------------------------------------------------------------------------- #
# 6. Hermeticity seal — the suite must never inherit a developer's Tavily key  #
# --------------------------------------------------------------------------- #
def test_test_profile_has_no_tavily_key():
    """A developer's Tavily key must never un-seal the test suite.

    pydantic-settings resolves a field as ``process env var > .env file > field
    default``, so a real ``TAVILY_API_KEY`` — in ``.env`` **or exported in the
    parent shell** — would leak into the default ``Settings()`` and flip
    ``is_tavily_enabled()`` to True. That is exactly what turns
    ``research.search`` into a *real* tool and breaks the dev-stub honesty
    (``test_inc12_trust_loop``) and external-output-sanitisation
    (``test_inc12_tool_executor``) tests.

    Two assertions, deliberately:
      * ``os.environ["TAVILY_API_KEY"] == ""`` pins the **mechanism** — the pin
        must be an unconditional assignment. ``setdefault`` is a no-op when the
        shell already exports the var (QA-INC16 §D), which silently un-sealed the
        suite; asserting the forced-empty value fails if anyone reverts it.
      * the ``Settings()`` assertion pins the **effect** (a default profile with
        Tavily disabled).

    Delete the conftest pin and this goes red, so the seal cannot silently rot.
    """
    assert os.environ["TAVILY_API_KEY"] == ""  # forced pin, NOT setdefault
    assert Settings().is_tavily_enabled() is False
