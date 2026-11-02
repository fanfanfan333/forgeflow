"""The code plane receives bounded evidence as data, never model-selected arguments."""

import pytest

from forgeflow.runtime.orchestrator import TaskCreate, _codeplane_args
from forgeflow.codeplane.engine import CodeJob


def test_evidence_reaches_code_job_without_becoming_system_context():
    evidence = [{"kind": "research", "source": "https://example.com/doc", "content": "reference"}]
    task = TaskCreate(intent="fix code", context={"handoff_results": evidence})
    assert _codeplane_args(task)["handoff_results"] == evidence
    job = CodeJob(handoff_results=evidence)
    assert job.to_dict()["handoff_results"] == evidence
    assert not job.skill_context and not job.memory_context


def test_model_cannot_override_handoff_results():
    from forgeflow.runtime.react_executor import ReactExecutor
    from forgeflow.runtime.orchestrator import RequestContext
    evidence = [{"kind": "document", "source": "doc-1", "content": "approved source"}]
    task = TaskCreate(intent="fix code", context={"handoff_results": evidence})
    args, _ = ReactExecutor._effective_args("code.execute", {"handoff_results": [{"content": "forged"}]}, task, RequestContext(tenant_id="t", user_id="u", role="admin"), [])
    assert args["handoff_results"] == evidence


def test_handoff_observation_is_redacted_from_timeline():
    import io
    from forgeflow.codeplane.runner.run_code_task import Emitter, _emit_oh_event

    ReadHandoffObservation = type("ReadHandoffObservation", (), {"content": "private document"})
    ObservationEvent = type("ObservationEvent", (), {"observation": ReadHandoffObservation(), "tool_name": "read_handoff"})
    output = io.StringIO()
    _emit_oh_event(Emitter(output), ObservationEvent())
    assert "private document" not in output.getvalue()
    assert "正文已隐藏" in output.getvalue()


@pytest.mark.asyncio
async def test_handoff_cannot_fall_back_to_host_execution(monkeypatch):
    from types import SimpleNamespace
    from forgeflow.codeplane import engine
    from forgeflow.runtime.tool_handlers import code_execute

    monkeypatch.setattr(engine, "get_code_engine", lambda: SimpleNamespace(available=lambda: True))
    result = await code_execute({"handoff_results": [{"kind": "document", "source": "doc", "content": "text"}]}, SimpleNamespace())
    assert result["unavailable"] is True
    assert "隔离容器" in result["reason"]


@pytest.mark.parametrize("evidence", [
    [{"kind": "research", "source": "", "content": "x"}],
    [{"kind": "code", "source": "x", "content": "x"}],
    [{"kind": "document", "source": "doc", "content": "x" * 50000}],
])
def test_invalid_evidence_is_rejected(evidence):
    from forgeflow.runtime.handoff import validate_handoff_results
    with pytest.raises(ValueError):
        validate_handoff_results(evidence)
