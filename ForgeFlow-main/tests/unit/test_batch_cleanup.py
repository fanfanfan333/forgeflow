from forgeflow.runtime.acceptance import pending_batch_cleanup


def test_explicit_office_batch_uses_scoped_indices_and_does_not_repeat_attempts():
    requirements = [{"id": "edit:document:0", "kind": "edit", "source_path": "word"},
                    {"id": "edit:sheet:0", "kind": "edit", "source_path": "sheet"}]
    tools = {"document.edit", "sheet.edit"}
    intent = "逐个清洗所有文件的多余空格，保留公式"
    calls = pending_batch_cleanup(intent, requirements, [], tools)
    assert [call["name"] for call in calls] == ["document.edit", "sheet.edit"]
    assert all(call["args"] == {"file_index": 0, "edits": [{"op": "normalize_whitespace"}]} for call in calls)
    records = [{"tool": "document.edit", "status": "error", "payload": {"file": "word"}}]
    assert [call["name"] for call in pending_batch_cleanup(intent, requirements, records, tools)] == ["sheet.edit"]
    assert pending_batch_cleanup(intent, requirements, [], set()) == []
    assert pending_batch_cleanup("清洗空格并删除客户信息", requirements, [], tools) == []
    assert pending_batch_cleanup("不要清洗全部文件的空格", requirements, [], tools) == []
    assert pending_batch_cleanup("只读检查全部文件是否需要清洗空格", requirements, [], tools) == []
    assert pending_batch_cleanup("解释清洗全部文件空格的方法", requirements, [], tools) == []
    assert pending_batch_cleanup(intent, requirements, [{"tool": "document.edit", "payload": None}], tools)[0]["name"] == "sheet.edit"
    blocked = [{"tool": "sheet.edit", "executed": False, "payload": {"not_executed": True}}]
    assert len(pending_batch_cleanup(intent, requirements, blocked, tools)) == 2
    assert pending_batch_cleanup(intent, requirements[:1], [], tools) == []
