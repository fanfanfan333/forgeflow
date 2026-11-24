"""FF-R1 P5-FIX — every run-detail output path passes one sanitizer.

P5's projection stripped ``_``-prefixed keys, but only inside the
persisted projection: the LIVE response never went through it, and
the persisted ``llm`` column was copied verbatim on hydration — so
``_`` keys, secret-shaped values and absolute server paths leaked
in run-detail responses. All three provenances of
``GET /runs/{id}`` are pinned here:

* **live** — the record still in the run store;
* **restart** — the record rebuilt from the persisted header;
* **history / write path** — the header produced for persistence
  must already be clean before it is written.

One recursive sanitizer (``sanitize_detail``) now guards all of
them: ``_``-prefixed keys dropped, configured secret values and
the ``sk-`` / ``token=`` / ``password=`` / ``Bearer`` shapes
redacted, absolute paths replaced by ``<path>``. All secrets
below are obvious fakes.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from forgeflow.runtime.orchestrator import RunRecord, get_run_store, reset_run_store
from forgeflow.workspace.models import WorkspaceRunRecord
from forgeflow.workspace.store import get_workspace_store, reset_workspace_store

pytestmark = pytest.mark.asyncio

FAKES = [
    "_identical_call_state",
    "_internal_note",
    "sk-FAKESECRET123456789",
    "FAKEBEARERTOKEN",
    "hunter2fake",
    "FAKETOKENVALUE",
    "FAKE-CONFIGURED-SECRET",
    "/home/forgeflow/private",
    "/srv/forgeflow/models",
]

POISON_NOTE = (
    "key sk-FAKESECRET123456789 at /home/forgeflow/private/file.txt"
    " password=hunter2fake Bearer FAKEBEARERTOKEN token=FAKETOKENVALUE"
    " configured FAKE-CONFIGURED-SECRET"
)


@pytest.fixture()
def configured_secret(monkeypatch):
    from types import SimpleNamespace

    import forgeflow.config as config_module

    settings = SimpleNamespace(smtp_password=SecretStr("FAKE-CONFIGURED-SECRET"))
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)


@pytest.fixture()
def stores():
    reset_run_store()
    reset_workspace_store()
    yield
    reset_run_store()
    reset_workspace_store()


def _poisoned_record(run_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        thread_id="",
        tenant_id="t1",
        agent_id=None,
        intent="quarterly report",
        status="completed",
        outcome="success",
        steps=[
            {
                "step": 1,
                "tool": "docs.parse",
                "note": POISON_NOTE,
                "_identical_call_state": {"x": 1},
            }
        ],
        errors=["failed reading /home/forgeflow/private/file.txt with sk-FAKESECRET123456789"],
        created_at="2026-11-24T00:00:00+00:00",
        actor_user_id="u1",
        actor_role="sales_rep",
        llm={
            "provider": "ollama",
            "_internal_note": "x",
            "api_key": "sk-FAKESECRET123456789",
            "base_url": "/srv/forgeflow/models",
            "execution_budget": {"model_calls": 3},
        },
        tool_invocations=[
            {"tool": "docs.parse", "status": "ok", "arguments": {"q": "token=FAKETOKENVALUE"}}
        ],
    )


def _assert_clean(payload: str) -> None:
    for fake in FAKES:
        assert fake not in payload, f"leaked: {fake}"


async def test_live_detail_response_is_sanitized(stores, configured_secret):
    from forgeflow.api.routers.runs import get_run

    get_run_store().save(_poisoned_record("run-live"))
    response = await get_run("run-live", "t1")
    _assert_clean(str(response.model_dump()))
    # Legitimate content survives.
    assert response.steps[0]["tool"] == "docs.parse"
    assert response.llm["provider"] == "ollama"
    assert response.llm["execution_budget"] == {"model_calls": 3}


async def test_restart_detail_response_is_sanitized(stores, configured_secret):
    from forgeflow.api.routers.runs import get_run

    header = WorkspaceRunRecord.from_run_record(_poisoned_record("run-restart"))
    await get_workspace_store().save(header)
    response = await get_run("run-restart", "t1")
    _assert_clean(str(response.model_dump()))
    assert response.detail_retained is True
    assert response.steps[0]["tool"] == "docs.parse"
    assert response.llm["provider"] == "ollama"


async def test_the_persisted_header_is_sanitized_before_writing(stores, configured_secret):
    header = WorkspaceRunRecord.from_run_record(_poisoned_record("run-history"))
    _assert_clean(str(header.llm))
    _assert_clean(str(header.detail))
    await get_workspace_store().save(header)
    stored = await get_workspace_store().get("t1", "run-history")
    assert stored is not None
    _assert_clean(str(stored.llm))
    _assert_clean(str(stored.detail))
    assert stored.detail["steps"][0]["tool"] == "docs.parse"
