import copy

from forgeflow.validation.validator import validate


def test_only_later_verified_edits_resolve_old_file_selector_failures():
    run = {"status": "completed", "errors": [], "acceptance": {"complete": True}, "steps": [
        {"tool": "sheet.inspect", "status": "blocked", "blocked_reason": "未提供可读取的 .xlsx 文件路径"},
        {"tool": "sheet.edit", "status": "ok", "observation": {"executed": True, "payload": {
            "artifact_ref": "actual.xlsx", "validation": {"requirement_ok": True}}}},
    ]}
    original = copy.deepcopy(run)
    assert validate(run).outcome == "success"
    assert run == original
    run["steps"][0]["blocked_reason"] = "permission denied"
    assert validate(run).outcome == "partial"
    run = copy.deepcopy(original)
    run["steps"][1]["observation"]["payload"]["validation"]["requirement_ok"] = False
    assert validate(run).outcome == "partial"
    run = copy.deepcopy(original)
    run["acceptance"]["complete"] = False
    assert validate(run).outcome == "partial"
    run = copy.deepcopy(original)
    run["steps"].reverse()
    assert validate(run).outcome == "partial"
