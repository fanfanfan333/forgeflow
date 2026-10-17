import hashlib
from types import SimpleNamespace

import httpx
from pydantic import SecretStr

from forgeflow.context.openviking import recall
from forgeflow.runtime.react_executor import ReactExecutor


def settings(**kwargs):
    return SimpleNamespace(
        openviking_url="http://127.0.0.1:1933",
        openviking_user_keys={"acme:alice": SecretStr("user-key")},
        openviking_timeout_seconds=1,
        **kwargs,
    )


async def test_http_contract_and_foreign_uri_rejection(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "result": {
                    "memories": [
                        {
                            "uri": "viking://user/alice/memories/style.md",
                            "abstract": "客户报告采用中文",
                            "score": 2.3,
                        },
                        {"uri": "viking://user/bob/memories/private.md", "abstract": "SECRET"},
                    ]
                },
            },
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs),
    )
    hits, trace = await recall(settings(), "acme", "alice", "报告")
    assert len(hits) == 1 and "SECRET" not in str(hits)
    assert hits[0]["remote_score"] == 2.3  # Not misreported as cosine similarity.
    assert trace["rejected"] == 1
    assert seen[0].url.path == "/api/v1/search/find"
    assert seen[0].headers["X-API-Key"] == "user-key"
    assert hashlib.sha256(b"acme").hexdigest()[:24] in seen[0].content.decode()


async def test_no_shared_identity_fallback():
    hits, trace = await recall(settings(), "foreign", "alice", "query")
    assert hits == [] and trace["status"] == "unconfigured_identity"


async def test_selected_detail_is_read_bounded_not_the_whole_index(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "result": {
                        "resources": [
                            {
                                "uri": "viking://user/alice/resources/project.md",
                                "abstract": "brief",
                                "level": 2,
                            }
                        ]
                    },
                },
            )
        assert request.url.params["limit"] == "80"
        return httpx.Response(200, json={"status": "ok", "result": "Detailed actual source text"})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs),
    )
    hits, trace = await recall(settings(), "acme", "alice", "project")
    assert len(requests) == 2 and hits[0]["level"] == 2
    assert hits[0]["text"] == "Detailed actual source text" and trace["detail_reads"] == 1


async def test_unavailable_server_does_not_break_local_context(monkeypatch):
    def handler(request):
        return httpx.Response(503)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs),
    )
    hits, trace = await recall(settings(), "acme", "alice", "query")
    assert not hits and trace["fallback"] == "local"


def test_retrieved_evidence_reaches_model_as_untrusted_data():
    task = SimpleNamespace(intent="生成客户报告", workflow_type="generic", context={})
    ctx = SimpleNamespace(
        available_skills=[],
        injected_skills=[],
        retrieved_context=[{"source": "memory", "ref_id": "m1", "text": "客户报告采用中文"}],
    )
    messages = ReactExecutor()._messages(task, ctx, [])
    assert "客户报告采用中文" in messages[-1].content
    assert "<UNTRUSTED_TOOL_OUTPUT" in messages[-1].content
    assert "m1" in messages[-1].content


async def test_context_builder_excludes_other_users_and_teams():
    from forgeflow.experience.context_builder import build_context
    from forgeflow.experience.memory_store import save_memory

    tenant = "viking-privacy-test"
    own = await save_memory(tenant, "user", "own report convention", actor_id="alice")
    foreign = await save_memory(tenant, "user", "secret report convention", actor_id="bob")
    team = await save_memory(tenant, "team", "secret team report", team_id="other-team")
    bundle = await build_context(tenant, "report", user_id="alice", team_id="my-team", k_memory=10)
    ids = {s["ref_id"] for s in bundle.sections}
    assert own.id in ids and foreign.id not in ids and team.id not in ids
