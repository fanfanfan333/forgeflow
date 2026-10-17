import pytest

from forgeflow.codeplane.navigation import locate_symbol


def test_actual_definition_and_references_are_located_without_executing_source(tmp_path):
    (tmp_path / "maths.py").write_text(
        "def add(a, b):\n    return a+b\nx = add(1, 2)\n", encoding="utf-8"
    )
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules/hidden.py").write_text("def add(): pass")
    result = locate_symbol([str(tmp_path)], "add")
    assert result["files_checked"] == 1
    assert [match["kind"] for match in result["matches"]] == ["definition", "text_reference"]
    assert [match["line"] for match in result["matches"]] == [1, 3]
    assert result["semantic_resolution"] is False


def test_navigation_is_bounded_and_literal(tmp_path):
    path = tmp_path / "main.c"
    path.write_text("int main(void) {}\nint x = main();\n")
    result = locate_symbol([str(path)], "main", limit=1)
    assert result["truncated"] and len(result["matches"]) == 1
    with pytest.raises(ValueError):
        locate_symbol([str(path)], ".*")
    with pytest.raises(ValueError):
        locate_symbol([str(path)], "main", limit=True)


def test_delivery_phrase_is_not_an_unrequested_second_edit():
    from forgeflow.runtime.acceptance import requirement_list

    items = requirement_list(
        "在 main 函数结束前增加 return 0;，保留输出文字，返回修改后的完整文件。",
        {"resources": ["file"]},
    )
    assert [item["id"] for item in items] == ["return"]


def test_legacy_main_is_modernized_after_model_whole_file_edit():
    from forgeflow.documents.textfile_edit import TextEditOp, apply_requested_edits

    source = b'main() { printf("hello word"); }\n'
    edited, _ = apply_requested_edits(
        source,
        [
            TextEditOp(
                op="replace_text",
                match=source.decode().strip(),
                replace='main() { printf("hello world"); return 0; }',
            )
        ],
        "优化代码，将 hello word 修改为 hello world，补上 main 函数内的 return 0;",
        "legacy.c",
    )
    assert b"int main(void)" in edited
    assert b"hello world" in edited and b"return 0;" in edited


def test_return_insertion_after_closing_brace_uses_actual_function_scope():
    from forgeflow.documents.textfile_edit import (
        TextEditOp,
        apply_requested_edits,
        main_return_satisfied,
    )

    source = b'int main(void) {\r\n printf("hello word");\r\n}\r\n'
    edited, _ = apply_requested_edits(
        source,
        [
            TextEditOp(op="replace_text", match="hello word", replace="hello world"),
            TextEditOp(op="insert_after", match="}", lines=["return 0;"]),
        ],
        "将 hello word 修改为 hello world，在 main 函数内增加 return 0;",
        "main.c",
    )
    assert main_return_satisfied(edited)
    assert edited.rstrip().endswith(b"}")
    assert b"\r\n" in edited


def test_verified_retry_recovers_same_file_but_not_another_file_or_later_failure():
    from forgeflow.runtime.acceptance import recovered_tool_errors

    def failed(path):
        return {
            "tool": "textfile.edit",
            "step_id": "failed",
            "status": "error",
            "error": "invalid op",
            "payload": {"file": path},
        }

    success = {
        "tool": "textfile.edit",
        "status": "ok",
        "payload": {
            "file": "a.txt",
            "artifact_ref": "real",
            "validation": {"requirement_ok": True},
        },
    }
    message = "工具 'textfile.edit' 未成功执行：invalid op"
    assert message in recovered_tool_errors([failed("a.txt"), success])[0]
    assert message not in recovered_tool_errors([failed("b.txt"), success])[0]
    assert message not in recovered_tool_errors([failed("a.txt"), success, failed("a.txt")])[0]
    assert message not in recovered_tool_errors([failed("a.txt"), failed("b.txt"), success])[0]


def test_batch_acceptance_requires_every_selected_file():
    from forgeflow.runtime.acceptance import assess, requirement_list

    inputs = {"text_paths": ["one.txt", "two.txt"], "text_names": ["第一份", "第二份"]}
    rows = requirement_list("批量清洗全部文件", {"resources": ["one", "two"]}, inputs)
    assert len(rows) == 2
    first = {
        "tool": "textfile.edit",
        "status": "ok",
        "step_id": "one",
        "payload": {"file": "one.txt", "validation": {"requirement_ok": True}},
    }
    assert assess(rows, [first])["complete"] is False
    second = {
        **first,
        "step_id": "two",
        "payload": {"file": "two.txt", "validation": {"requirement_ok": True}},
    }
    assert assess(rows, [first, second])["complete"] is True
    returns = requirement_list(
        "所有文件的 main 内增加 return 0;", {"resources": ["one", "two"]}, inputs
    )
    assert len(returns) == 2 and all(row["kind"] == "return" for row in returns)


def test_file_selector_only_selects_admitted_paths_and_matching_names(monkeypatch):
    from forgeflow.runtime import orchestrator, planning
    from forgeflow.runtime.react_executor import ReactExecutor

    task = orchestrator.TaskCreate(intent="批量清洗全部文件")
    ctx = orchestrator.RequestContext()
    monkeypatch.setattr(orchestrator, "_capability_context", lambda *a, **kw: {})
    monkeypatch.setattr(planning, "resolve_inputs", lambda *a, **kw: ({}, []))
    monkeypatch.setattr(
        orchestrator,
        "_textfile_args",
        lambda *a: {"text_paths": ["a.txt", "b.txt"], "text_names": ["A", "B"]},
    )
    args, _ = ReactExecutor._effective_args(
        "textfile.inspect", {"file_index": 1, "text_paths": ["private.txt"]}, task, ctx, []
    )
    assert (
        args["text_paths"] == ["b.txt"]
        and args["text_names"] == ["B"]
        and args["paths"] == ["b.txt"]
    )
    for index in [-1, 2, True, "1"]:
        args, _ = ReactExecutor._effective_args(
            "textfile.inspect", {"file_index": index}, task, ctx, []
        )
        assert args["text_paths"] == [] and args["paths"] == []


def test_research_retry_requires_complete_independent_coverage():
    from forgeflow.runtime.acceptance import recovered_tool_errors

    failed = {
        "tool": "research.verify",
        "status": "error",
        "step_id": "old",
        "error": "missing subject",
    }
    passed = {
        "tool": "research.verify",
        "status": "ok",
        "payload": {
            "artifact_ref": "real-report",
            "validation": {"citations_ok": True, "claims_supported": True, "coverage_ok": False},
        },
    }
    message = "工具 'research.verify' 未成功执行：missing subject"
    assert message not in recovered_tool_errors([failed, passed])[0]
    passed["payload"]["validation"]["coverage_ok"] = True
    assert message in recovered_tool_errors([failed, passed])[0]


def test_extensionless_authorized_python_blob_still_has_ast_definitions(tmp_path):
    from forgeflow.codeplane.navigation import locate_symbol

    blob = tmp_path / "content-address"
    blob.write_text("def parse_total(value):\n    return int(value)\nparse_total('2')\n")
    result = locate_symbol([str(blob)], "parse_total")
    assert result["matches"][0]["kind"] == "definition"


def test_read_only_instruction_is_not_a_modification_request():
    from forgeflow.runtime.acceptance import requirement_list

    assert (
        requirement_list("定位函数定义与调用位置，只读检查，不修改文件", {"resources": ["code"]})
        == []
    )


def test_text_edit_schema_requires_the_actual_anchor_instead_of_an_index():
    from forgeflow.runtime.react_executor import ReactExecutor
    from forgeflow.documents.textfile_edit import SUPPORTED_OPS
    schema=ReactExecutor._tool_schemas(["textfile.edit"])[0]["function"]["parameters"]["properties"]["edits"]["items"]
    options={option["properties"]["op"]["const"]:option for option in schema["oneOf"]}
    assert set(options)==set(SUPPORTED_OPS)
    assert options["insert_after"]["required"]==["op","match","lines"]
    assert "index" not in options["insert_after"]["properties"]
