"""INC2-01 — config-layer extensions (docs/sop/05-ARCHITECTURE-INC2.md §7).

Verifies the new INC2 settings load with the frozen defaults, that the
``.env.example`` surface matches the config fields 1:1, and that the
comma-separated / JSON ``MODEL_FALLBACK_CHAIN`` parsing works.
"""

from __future__ import annotations

from pathlib import Path

from forgeflow.config import Settings, get_settings

# Every INC2 field added by this increment. Used to assert the .env.example
# surface stays in step with the code (a missing var is a docs/config drift bug).
NEW_INC2_FIELDS = [
    "cost_currency",
    "cost_warn_ratio",
    "cost_exceed_actions",
    "cost_savings_baseline_multiplier",
    "slo_critical_availability",
    "slo_critical_p95_ms",
    "slo_important_availability",
    "slo_important_p95_ms",
    "slo_edge_availability",
    "slo_edge_p95_ms",
    "context_budget_tokens",
    "context_per_item_ratio",
    "dedup_merge_threshold",
    "model_fallback_chain",
    "ollama_think",
    "marketplace_cross_tenant",
    "multimodal_max_bytes",
    "dlp_rules_file",
]


def test_inc2_defaults_load():
    settings = get_settings()
    # 'mock' is intentionally NOT part of the configured default chain: it is a
    # development-only stub appended implicitly by get_model() outside prod (T1).
    assert settings.model_fallback_chain == ["ollama"]
    assert settings.context_budget_tokens == 2000
    assert settings.dedup_merge_threshold == 0.95
    assert settings.ollama_think is False
    assert settings.marketplace_cross_tenant is False
    assert settings.multimodal_max_bytes == 5 * 1024 * 1024
    assert settings.dlp_rules_file == ""
    assert settings.cost_savings_baseline_multiplier == 1.0
    assert settings.cost_warn_ratio == 0.80
    assert settings.slo_critical_p95_ms == 1500
    assert settings.slo_important_p95_ms == 2000
    assert settings.slo_edge_p95_ms == 5000


def test_env_example_matches_config_fields():
    env_example = Path(__file__).resolve().parents[2] / ".env.example"
    assert env_example.exists(), "repo-root .env.example is required"
    text = env_example.read_text(encoding="utf-8")
    missing = [name for name in NEW_INC2_FIELDS if name.upper() not in text]
    assert not missing, f".env.example is missing INC2 vars: {missing}"


def test_env_example_json_array_form_for_chain():
    text = (Path(__file__).resolve().parents[2] / ".env.example").read_text(
        encoding="utf-8"
    )
    assert 'MODEL_FALLBACK_CHAIN=["ollama"]' in text


def test_fallback_chain_accepts_comma_separated():
    settings = Settings(model_fallback_chain="ollama,mock")
    assert settings.model_fallback_chain == ["ollama", "mock"]


def test_fallback_chain_accepts_json_array():
    settings = Settings(model_fallback_chain='["ollama","qwen2.5vl:3b","mock"]')
    assert settings.model_fallback_chain == ["ollama", "qwen2.5vl:3b", "mock"]


def test_fallback_chain_empty_falls_back_to_default():
    settings = Settings(model_fallback_chain="")
    assert settings.model_fallback_chain == ["ollama"]


def test_cost_exceed_action_list_parsing():
    settings = Settings(cost_exceed_actions="swap_model, trim_context ,pause_noncritical")
    assert settings.cost_exceed_action_list() == [
        "swap_model",
        "trim_context",
        "pause_noncritical",
    ]


def test_ollama_think_defaults_false():
    # Thinking models can consume the whole num_predict budget and return "".
    assert Settings().ollama_think is False


# --------------------------------------------------------------------------- #
# T1 — mock is an environment-controlled DEVELOPMENT fallback, not a prod one  #
# T2 — ollama_think must stay false in production (fail-closed)               #
# --------------------------------------------------------------------------- #


def _prod_settings(**overrides) -> Settings:
    """A baseline-secure production Settings, overridable per test.

    Mirrors tests/unit/test_enterprise_hardening.py::_settings so a *clean* prod
    config produces an empty problem list unless a specific flag is toggled.
    """
    base = dict(
        api_secret_key="a-sufficiently-strong-secret",
        dev_login_enabled=False,
        dev_login_password="",
        llm_provider="ollama",
        cors_allow_origins="https://app.example.com",
        docs_enabled=False,
        otel_environment="production",
        trusted_proxy_count=1,
    )
    base.update(overrides)
    return Settings(**base)


def test_prod_clean_config_without_mock_is_accepted():
    # The default chain is ["ollama"] — no mock — so a secure prod config is clean.
    assert _prod_settings().validate_runtime() == []


def test_prod_mock_in_fallback_chain_is_flagged():
    problems = _prod_settings(model_fallback_chain=["ollama", "mock"]).validate_runtime()
    assert any("mock" in p and "production" in p for p in problems)


def test_prod_mock_primary_provider_is_flagged():
    problems = _prod_settings(llm_provider="mock").validate_runtime()
    assert any("mock" in p for p in problems)


def test_prod_ollama_think_true_is_flagged():
    problems = _prod_settings(ollama_think=True).validate_runtime()
    assert any("OLLAMA_THINK" in p for p in problems)


def test_dev_mock_in_chain_is_not_flagged():
    # Outside production mock is the legitimate degrade target — never fatal.
    s = _prod_settings(
        otel_environment="development", model_fallback_chain=["ollama", "mock"]
    )
    assert s.validate_runtime() == []


def test_dev_ollama_think_is_not_flagged():
    s = _prod_settings(otel_environment="development", ollama_think=True)
    assert s.validate_runtime() == []
