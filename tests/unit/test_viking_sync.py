import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from forgeflow.context import viking_sync as sync


def test_parallel_journal_writes_are_atomic_and_leave_no_temporary_files(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "job.json"
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: sync._save(path, {"index": index, "content": "完整记录" * 100}), range(24)))
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["index"] in range(24)
    assert result["content"] == "完整记录" * 100
    assert list(tmp_path.iterdir()) == [path]


def test_failed_atomic_replace_keeps_previous_journal_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / "job.json"
    sync._save(path, {"status": "pending"})

    def fail_replace(source, target):
        raise OSError("replacement failed")

    monkeypatch.setattr(sync.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replacement failed"):
        sync._save(path, {"status": "done"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "pending"}
    assert list(tmp_path.iterdir()) == [path]


def test_registered_code_does_not_index_hidden_secrets_or_dependency_tree(tmp_path):
    (tmp_path / "main.py").write_text('print("indexed-source")', encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=do-not-index", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "bad.py").write_text("dependency-secret", encoding="utf-8")
    record = SimpleNamespace(locator=SimpleNamespace(identifier=str(tmp_path)))
    content = sync._registered_code(record)
    assert "indexed-source" in content
    assert "do-not-index" not in content and "dependency-secret" not in content


async def test_delete_cancels_pending_write_and_scopes_credentials(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        openviking_sync_enabled=True,
        openviking_url="http://127.0.0.1:1933",
        openviking_outbox_root=tmp_path,
        openviking_user_keys={"a:u": SecretStr("private")},
    )
    monkeypatch.setattr(sync, "get_settings", lambda: settings)
    assert not await sync.enqueue("b", "u", "resource", "r", {"content": "foreign"})
    assert await sync.enqueue("a", "u", "resource", "r", {"content": "owned"})
    await sync.enqueue("a", "u", "delete_resource", "r", {})
    records = [json.loads(p.read_text()) for p in tmp_path.glob("*.json")]
    assert {r["kind"]: r["status"] for r in records} == {
        "resource": "cancelled",
        "delete_resource": "pending",
    }
    assert "private" not in str(records)

    from forgeflow.context.viking_history import revoked_prefixes

    assert len(revoked_prefixes(settings, "a", "u")) == 1
    assert revoked_prefixes(settings, "b", "u") == []
    assert revoked_prefixes(settings, "a", "other") == []
    await sync.enqueue("a", "u", "resource", "r", {"content": "restored"})
    assert revoked_prefixes(settings, "a", "u") == []
    records = [json.loads(p.read_text()) for p in tmp_path.glob("*.json")]
    assert {r["kind"]: r["status"] for r in records} == {
        "resource": "pending",
        "delete_resource": "cancelled",
    }


async def test_failed_extraction_is_never_completed():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "result": {"status": "failed", "task_id": "task", "error": "parse error"},
            },
        )

    job = {"user": "u", "id": "r", "kind": "run", "payload": {}, "result": {"task_id": "task"}}
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ValueError, match="remote_extraction_failed"):
            await sync._process(client, job)
    assert not job["result"].get("task_id")
    assert job["failed_tasks"][0]["error"] == "parse error"


async def test_skill_uses_package_api_with_version_and_private_target():
    seen = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(404)
        seen.append(json.loads(request.content))
        assert request.url.path == "/api/v1/skills"
        return httpx.Response(200, json={"status": "ok", "result": {"task_id": "index"}})

    job = {
        "user": "u",
        "id": "s",
        "kind": "skill",
        "payload": {
            "name": "文档清洗",
            "description": "清洗",
            "version": "v2",
            "content": "保留金额",
        },
    }
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        result = await sync._process(client, job)
    assert result["state"] == "waiting"
    assert seen[0]["target_uri"] == "viking://user/u/skills"
    assert "Version: v2" in seen[0]["data"]["content"]
