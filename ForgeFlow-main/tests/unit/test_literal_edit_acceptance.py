from types import SimpleNamespace

import pytest

from forgeflow.documents.textfile_edit import verify_literal_edits
from forgeflow.runtime.acceptance import assess, requirement_list
from forgeflow.runtime.tool_handlers import textfile_edit

INTENT = "优化代码，将 hello word 修正为 hello world，并在 main 函数结束前添加 return 0;"
ORIGINAL = b'#include<stdio.h>\nmain()\n{\nprintf("hello word");\n}\n'


def test_literal_checks_reject_partial_and_accept_values_containing_old_literal():
    intent = '将“客户”替换为“客户名称”，把 old 改为 new'
    assert all(row["satisfied"] for row in verify_literal_edits("客户 old".encode(), "客户名称 new".encode(), intent))
    assert not all(row["satisfied"] for row in verify_literal_edits("客户 old".encode(), "客户名称 old".encode(), intent))
    assert verify_literal_edits(ORIGINAL, ORIGINAL, "优化代码") == []


@pytest.mark.asyncio
async def test_real_text_editor_produces_independent_compound_receipts(tmp_path):
    path = tmp_path / "main.txt"
    path.write_bytes(ORIGINAL)
    result = await textfile_edit({"paths": [str(path)], "intent": INTENT, "edits": [
        {"op": "replace_text", "match": "hello word", "replace": "hello world"}
    ]}, SimpleNamespace(intent=INTENT))
    assert result["ok"] is True, result
    assert result["validation"]["edit_ok"] is True
    assert result["validation"]["return_ok"] is True
    assert assess(requirement_list(INTENT, {"resources": ["sample"]}), [
        {"tool": "textfile.edit", "status": "ok", "payload": result}
    ])["complete"] is True
    assert path.read_bytes() == ORIGINAL


@pytest.mark.asyncio
async def test_model_cannot_weaken_original_literal_requirement(tmp_path):
    path = tmp_path / "main.txt"
    path.write_bytes(ORIGINAL)
    result = await textfile_edit({"paths": [str(path)], "intent": "只添加 return 0;", "edits": [
        {"op": "replace_text", "match": "hello word", "replace": "another value"}
    ]}, SimpleNamespace(intent=INTENT))
    assert result["ok"] is False
    assert result["validation"]["edit_ok"] is False
    assert "artifact_ref" not in result


@pytest.mark.parametrize("prefix", ["代码中的", "该文件里的", "文档中的", "代码中的“"])
def test_context_prefix_is_not_part_of_literal(prefix):
    old = prefix + "hello word" + ("”" if prefix.endswith("“") else "")
    findings = verify_literal_edits(ORIGINAL, ORIGINAL.replace(b"hello word", b"hello world"),
                                    f"将{old}修正为 hello world，并添加 return 0;")
    assert findings == [{"before": "hello word", "after": "hello world", "satisfied": True}]


async def test_literal_correction_and_return_without_optimization_keyword(tmp_path):
    path = tmp_path / "main.txt"
    path.write_bytes(ORIGINAL)
    intent = "将代码中的 hello word 修正为 hello world，并在 main 函数结束前添加 return 0;，返回修改后的文件。"
    result = await textfile_edit({"paths": [str(path)], "intent": intent, "edits": [
        {"op": "replace_text", "match": "hello word", "replace": "hello world"}
    ]}, SimpleNamespace(intent=intent))
    assert result["ok"] is True, result
    assert result["validation"]["edit_ok"] is True
    assert result["validation"]["return_ok"] is True
