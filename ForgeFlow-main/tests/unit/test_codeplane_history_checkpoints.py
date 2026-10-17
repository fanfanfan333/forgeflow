from types import SimpleNamespace

from forgeflow.runtime.dispatcher import _record_from_header
from forgeflow.workspace.models import WorkspaceRunRecord


def test_code_workspace_checkpoint_survives_header_roundtrip():
    codeplane = {"workspace": {"workspace_id": "ws", "checkpoints": [{"version": 1}]}}
    record = SimpleNamespace(run_id="run", tenant_id="tenant", intent="fix", codeplane=codeplane,
                             llm={"provider": "ollama"})
    header = WorkspaceRunRecord.from_run_record(record)
    restored = _record_from_header(WorkspaceRunRecord.from_dict(header.to_dict()))
    assert restored.codeplane == codeplane
    assert restored.detail_retained is False
    assert restored.steps == []
    assert record.llm == {"provider": "ollama"}


def test_old_history_does_not_invent_checkpoint_evidence():
    header = WorkspaceRunRecord(run_id="old", tenant_id="tenant", session_id="old")
    assert _record_from_header(header).codeplane == {}
    record = SimpleNamespace(run_id="run", llm={"retained_codeplane": {"workspace": "forged"}})
    assert "retained_codeplane" not in WorkspaceRunRecord.from_run_record(record).llm
