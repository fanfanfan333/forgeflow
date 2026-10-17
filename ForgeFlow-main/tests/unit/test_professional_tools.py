from types import SimpleNamespace

import pytest

from forgeflow.runtime.professional_tools import evidence_read, store_evidence


@pytest.fixture
def evidence_root(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "forgeflow.runtime.professional_tools.get_settings",
        lambda: SimpleNamespace(resource_store_path=lambda: tmp_path),
    )


async def test_evidence_is_paged_versioned_and_actor_scoped(evidence_root):
    ctx = SimpleNamespace(tenant_id="one", user_id="alice")
    saved = store_evidence({"content": "abcdef" * 1000}, ctx)
    page = await evidence_read({"ref": saved["evidence_ref"], "limit": 20}, ctx)
    assert len(page["content"]) == 20
    assert page["next_offset"] == 20
    assert page["content_hash"] == saved["content_hash"]
    for other in (
        SimpleNamespace(tenant_id="two", user_id="alice"),
        SimpleNamespace(tenant_id="one", user_id="bob"),
    ):
        with pytest.raises(FileNotFoundError):
            await evidence_read({"ref": saved["evidence_ref"]}, other)
    with pytest.raises(ValueError):
        await evidence_read({"ref": saved["evidence_ref"], "content_hash": "wrong"}, ctx)


async def test_evidence_rejects_unbounded_reads(evidence_root):
    with pytest.raises(ValueError):
        await evidence_read(
            {"ref": "no", "limit": 100000}, SimpleNamespace(tenant_id="one", user_id="alice")
        )


async def test_evidence_rejects_out_of_range_offset(evidence_root):
    ctx = SimpleNamespace(tenant_id="one", user_id="alice")
    saved = store_evidence({"content": "small"}, ctx)
    with pytest.raises(ValueError, match="exceeds"):
        await evidence_read({"ref": saved["evidence_ref"], "offset": 10000}, ctx)


async def test_delegate_rejects_recursion_and_foreign_resources():
    from forgeflow.runtime.delegation import delegate

    parent = SimpleNamespace(delegation_role="research")
    task = SimpleNamespace(context={"resources": ["allowed"]})
    ctx = SimpleNamespace(runtime_context=parent, task_context=task)
    with pytest.raises(PermissionError, match="Nested"):
        await delegate({"role": "research", "instruction": "review"}, ctx)
    parent.delegation_role = None
    with pytest.raises(PermissionError, match="resources"):
        await delegate({"role": "research", "instruction": "review", "resources": ["foreign"]}, ctx)


@pytest.mark.parametrize("cancelled", [False, True])
async def test_delegation_intersects_permissions_and_preserves_usage(monkeypatch, cancelled):
    import asyncio
    from unittest.mock import AsyncMock

    from forgeflow.runtime.delegation import delegate
    from forgeflow.runtime.execution_budget import ExecutionBudget, current_budget
    from forgeflow.runtime.orchestrator import RunRecord

    saved = []
    store = SimpleNamespace(save=AsyncMock(side_effect=lambda record: saved.append(record)))
    bus = SimpleNamespace(emit=AsyncMock())
    record = RunRecord(
        run_id="child",
        thread_id="child",
        tenant_id="one",
        agent_id=None,
        intent="review",
        status="running",
        outcome="",
        steps=[],
        errors=[],
        created_at="2026-10-14T00:00:00Z",
    )
    monkeypatch.setattr("forgeflow.runtime.orchestrator.register_running_run", lambda **kw: record)
    monkeypatch.setattr("forgeflow.workspace.store.get_workspace_store", lambda: store)
    monkeypatch.setattr("forgeflow.runtime.events.get_event_bus", lambda: bus)

    async def prepare(task, child):
        assert child.allowed_tools == {"research.read"}
        assert child.retrieved_context == []
        assert child.injected_skills == [
            {"id": "selected", "version": "1.0.0", "content_hash": "hash"}
        ]
        assert child._skill_read_versions == {"selected": ("1.0.0", "hash")}
        assert child._skill_read_versions is not parent._skill_read_versions
        return None

    async def run(self, task, child, bus, run_id):
        task.context["llm_usage"] = [{"input_tokens": 12, "output_tokens": 4, "model": "local"}]
        if cancelled:
            raise asyncio.CancelledError()
        task.context["tool_invocations"] = [
            {"tool": "research.read", "status": "ok", "executed": True, "payload": "non-object"}
        ]
        task.context["llm_runtime"] = {"final_answer": {"text": "reviewed"}}
        return [], []

    monkeypatch.setattr("forgeflow.runtime.input_scope.prepare_input_scope", prepare)
    monkeypatch.setattr("forgeflow.runtime.react_executor.ReactExecutor.run", run)
    parent_task = SimpleNamespace(context={"resources": [], "llm_runtime": {}})
    parent = SimpleNamespace(
        allowed_tools={"research.read"},
        delegation_role=None,
        injected_skills=[
            {
                "id": "selected",
                "version": "1.0.0",
                "content_hash": "hash",
                "instructions": "parent-only body",
            }
        ],
        _skill_read_versions={"selected": ("1.0.0", "hash")},
    )
    ctx = SimpleNamespace(
        runtime_context=parent,
        task_context=parent_task,
        tenant_id="one",
        user_id="alice",
        role="manager",
        run_id="parent",
    )
    token = current_budget.set(ExecutionBudget())
    try:
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await delegate({"role": "research", "instruction": "review"}, ctx)
        else:
            result = await delegate({"role": "research", "instruction": "review"}, ctx)
            assert result["ok"] is True
            assert result["total_tokens"] == 16
            assert result["evidence"][0]["source_ref"] is None
    finally:
        current_budget.reset(token)
    assert parent_task.context["llm_usage"][0]["input_tokens"] == 12
    assert saved[-1].status == ("aborted" if cancelled else "completed")
    assert saved[-1].llm["usage"][0]["output_tokens"] == 4
    assert saved[-1].parent_run_id == ""
    assert saved[-1].llm["delegation_parent_run_id"] == "parent"


async def test_selected_skill_is_hash_pinned_and_resources_are_read_only(monkeypatch):
    from unittest.mock import AsyncMock

    from forgeflow.runtime.professional_tools import skill_read

    version = SimpleNamespace(semver="1.0.0", spec={"instructions": "Inspect files first"})
    skill = SimpleNamespace(id="skill", name="review", status="published", current_version="1.0.0")
    registry = SimpleNamespace(
        get=AsyncMock(return_value=skill), versions=AsyncMock(return_value=[version])
    )
    monkeypatch.setattr("forgeflow.skills.registry.SkillRegistry", lambda: registry)
    runtime = SimpleNamespace(available_skills=["skill"], injected_skills=[])
    ctx = SimpleNamespace(runtime_context=runtime, tenant_id="one")
    first = await skill_read({"skill_id": "skill"}, ctx)
    assert len(first["content_hash"]) == 64

    class Loader:
        def __init__(self, **kwargs):
            assert list(kwargs["versions"]) == ["skill"]

        def load_resource(self, skill_id, ref):
            assert ref == "scripts/check.py"
            return "raise RuntimeError('must never run')"

    monkeypatch.setattr("forgeflow.skills.progressive_loader.ProgressiveLoader", Loader)
    resource = await skill_read({"skill_id": "skill", "ref": "scripts/check.py"}, ctx)
    assert resource["script_executed"] is False
    assert resource["content_hash"] == first["content_hash"]
    version.spec["instructions"] = "Changed in place"
    with pytest.raises(ValueError, match="changed"):
        await skill_read({"skill_id": "skill"}, ctx)
    with pytest.raises(PermissionError):
        await skill_read({"skill_id": "foreign"}, ctx)


async def test_explicit_version_cannot_override_selected_skill_pin(monkeypatch):
    from unittest.mock import AsyncMock

    from forgeflow.runtime.professional_tools import skill_read

    version = SimpleNamespace(semver="2.0.0", spec={"instructions": "new"})
    skill = SimpleNamespace(id="skill", name="review", status="published", current_version="2.0.0")
    registry = SimpleNamespace(
        get=AsyncMock(return_value=skill), versions=AsyncMock(return_value=[version])
    )
    monkeypatch.setattr("forgeflow.skills.registry.SkillRegistry", lambda: registry)
    runtime = SimpleNamespace(
        available_skills=["skill"], injected_skills=[{"id": "skill", "version": "1.0.0"}]
    )
    ctx = SimpleNamespace(runtime_context=runtime, tenant_id="one")
    with pytest.raises(ValueError, match="version changed"):
        await skill_read({"skill_id": "skill", "version": "2.0.0"}, ctx)


async def test_functional_trial_reads_actual_package_without_publishing_or_execution(monkeypatch):
    from forgeflow.runtime.professional_tools import skill_read
    from forgeflow.skills.package_import import package_spec

    spec = package_spec({
        "SKILL.md": "---\nname: trial\ndescription: Inspect attached documents.\n---\nRead references/check.md before using tools.",
        "references/check.md": "Preserve the actual amounts.",
        "scripts/check.py": "raise RuntimeError('must never run')",
    })
    trial = SimpleNamespace(semver="draft", spec=spec, tenant_id="one", user_id="actor")
    runtime = SimpleNamespace(available_skills=["instruction-draft"], injected_skills=[],
                              _functional_skill_version=trial)
    ctx = SimpleNamespace(runtime_context=runtime, tenant_id="one", user_id="actor")
    def forbidden_registry():
        raise AssertionError("A private evaluation must not publish or query a draft registry entry")
    monkeypatch.setattr("forgeflow.skills.registry.SkillRegistry", forbidden_registry)
    body = await skill_read({"skill_id": "instruction-draft", "version": "draft"}, ctx)
    assert body["resources"] == ["references/check.md", "scripts/check.py"]
    resource = await skill_read({"skill_id": "instruction-draft", "ref": "references/check.md"}, ctx)
    assert resource["content"] == "Preserve the actual amounts."
    script = await skill_read({"skill_id": "instruction-draft", "ref": "scripts/check.py"}, ctx)
    assert script["script_executed"] is False
    ctx.tenant_id = "other"
    with pytest.raises(PermissionError, match="another identity"):
        await skill_read({"skill_id": "instruction-draft"}, ctx)
    ctx.tenant_id, ctx.user_id = "one", "other"
    with pytest.raises(PermissionError, match="another identity"):
        await skill_read({"skill_id": "instruction-draft"}, ctx)


def test_document_scope_parameters_are_visible_to_the_model():
    from forgeflow.runtime.react_executor import ReactExecutor

    schema = ReactExecutor._tool_schemas(["document.inspect"])[0]
    assert {"task_id", "field"} <= schema["function"]["parameters"]["properties"].keys()


async def test_large_skill_resource_retains_version_and_separate_hashes(evidence_root, monkeypatch):
    from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor
    from forgeflow.runtime.tool_registry import ToolBinding

    original = {"ok": True, "skill_id": "skill", "version": "1.0.0", "ref": "references/guide.md",
                "skill_content_hash": "a" * 64, "resource_hash": "b" * 64,
                "content": "resource content " * 1000, "script_executed": False}

    async def read(args, ctx):
        return original

    binding = ToolBinding(tool_id="skills.read", handler=read, kind="real", provider="skill-registry", description="read resource")
    monkeypatch.setattr("forgeflow.runtime.tool_executor.resolve", lambda tool: binding)
    ctx = ToolCallContext(run_id="test", step_id="test:0", tenant_id="one", user_id="alice", role="admin",
                          args={"skill_id": "skill", "ref": "references/guide.md"})
    invocation = await ToolExecutor().execute("skills.read", ctx=ctx)
    assert invocation.status == "ok"
    assert invocation.executed is True
    payload = invocation.payload
    assert payload["version"] == "1.0.0"
    assert payload["skill_content_hash"] == original["skill_content_hash"]
    assert payload["content_hash"] != payload["skill_content_hash"]
    assert payload["script_executed"] is False
    page = await evidence_read({"ref": payload["evidence_ref"], "content_hash": payload["content_hash"]}, ctx)
    assert page["content_hash"] == payload["content_hash"]
