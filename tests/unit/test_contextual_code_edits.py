from types import SimpleNamespace

import pytest

from forgeflow.documents.textfile_edit import UnknownEditOpError, apply_edits, resolve_main_return

SOURCE = b'#include<stdio.h>\nmain()\n{\n printf("hello world");\n}\n'


async def test_tool_uses_original_request_instead_of_models_spelling_only_edit(tmp_path):
    from forgeflow.documents import DocArtifactStore
    from forgeflow.runtime.tool_handlers import textfile_edit

    source = tmp_path / "code.txt"
    original = SOURCE.replace(b"world", b"word")
    source.write_bytes(original)
    result = await textfile_edit(
        {
            "text_paths": [str(source)],
            "_user_intent": "在该代码中增加一个返回语句",
            "intent": "修正拼写",
            "edits": [{"op": "replace_text", "match": "word", "replace": "world"}],
        },
        SimpleNamespace(
            run_id="return-regression", tenant_id="test-return", role="manager", args={}
        ),
    )
    assert result["ok"] and result["validation"]["requirement_ok"] is True
    content = DocArtifactStore().get(result["artifact_ref"])
    assert b"return 0;" in content and b"hello word" in content
    assert content.index(b"return 0;") < content.rindex(b"}")
    assert source.read_bytes() == original


def test_explicit_add_request_resolves_inside_main_without_changing_print():
    ops = resolve_main_return(SOURCE, "最后没有返回 return 0，增加该语句")
    result, _ = apply_edits(SOURCE, ops)
    assert result.index(b"return 0;") < result.rindex(b"}")
    assert b'printf("hello world");' in result
    assert resolve_main_return(SOURCE, "在里面增加合理代码") is None
    assert resolve_main_return(SOURCE, "删除 return 0;") is None
    assert resolve_main_return(SOURCE, "在该代码中增加一个返回语句") is not None
    assert resolve_main_return(SOURCE, "增加一条返回的语句") is not None
    assert resolve_main_return(SOURCE, "增加 return 1;") is None
    assert resolve_main_return(SOURCE, "不要增加返回语句") is None
    assert resolve_main_return(SOURCE.replace(b"main()", b"void main()"), "增加返回语句") is None


async def test_compound_tool_preserves_both_requirements_and_original_file(tmp_path):
    from forgeflow.documents import DocArtifactStore
    from forgeflow.runtime.tool_handlers import textfile_edit

    source = tmp_path / "compound.txt"
    original = SOURCE.replace(b"world", b"word")
    source.write_bytes(original)
    result = await textfile_edit(
        {
            "text_paths": [str(source)],
            "_user_intent": "优化文件中的代码，再加一条返回语句",
            "intent": "修正拼写",
            "edits": [{"op": "replace_text", "match": "word", "replace": "world"}],
        },
        SimpleNamespace(
            run_id="compound-regression", tenant_id="test-return", role="manager", args={}
        ),
    )
    assert result["ok"] and result["validation"]["requirement_ok"] is True
    content = DocArtifactStore().get(result["artifact_ref"])
    assert b"hello world" in content and b"hello word" not in content
    assert content.index(b"return 0;") < content.rindex(b"}")
    assert source.read_bytes() == original


@pytest.mark.parametrize(
    "intent",
    [
        "优化文件中的代码，再加一条返回语句",
        "修复拼写并加上返回语句",
        "优化代码，追加 return 0;",
        "将代码优化，并且在合适位置添加返回语句",
    ],
)
def test_compound_request_keeps_optimization_and_inserts_return(intent):
    from forgeflow.documents.textfile_edit import (
        TextEditOp,
        apply_requested_edits,
        main_return_satisfied,
    )

    original = SOURCE.replace(b"world", b"word")
    edited, changes = apply_requested_edits(
        original,
        [TextEditOp(op="replace_text", match="hello word", replace="hello world")],
        intent,
        "code.txt",
    )
    assert b"hello world" in edited and b"hello word" not in edited
    assert main_return_satisfied(edited)
    assert changes >= 2
    assert edited.index(b"return 0;") < edited.rindex(b"}")


@pytest.mark.parametrize("eol", [b"\r\n", b"\r", b"\n"])
def test_compound_return_preserves_source_line_endings(eol):
    from forgeflow.documents.textfile_edit import TextEditOp, apply_requested_edits

    original = b'#include<stdio.h>\nmain()\n{\nprintf("hello word");\n}'.replace(b"\n", eol)
    edited, changes = apply_requested_edits(
        original,
        [TextEditOp(op="replace_text", match="hello word", replace="hello world")],
        "将代码优化，并且在合适位置添加返回语句",
        "code.txt",
    )
    assert b"hello world" in edited and changes >= 2
    assert b"return 0;" + eol + b"}" in edited
    assert edited.replace(eol, b"").count(b"\n") == 0
    assert edited.replace(eol, b"").count(b"\r") == 0


def test_compound_request_cannot_drop_main_and_claim_success():
    from forgeflow.documents.textfile_edit import TextEditOp, apply_requested_edits

    with pytest.raises(UnknownEditOpError, match="组合需求"):
        apply_requested_edits(
            SOURCE,
            [TextEditOp(op="replace_text", match="main", replace="other")],
            "优化代码，再加一条返回语句",
            "code.txt",
        )


@pytest.mark.parametrize("replacement", ["}\nreturn 0;", "return 0;"])
def test_requested_return_uses_scope_instead_of_model_brace_replacement(replacement):
    from forgeflow.documents.textfile_edit import TextEditOp, apply_requested_edits

    source = b'#include<stdio.h>\r\nmain()\r\n{\r\nprintf("hello word");\r\n}'
    edited, _ = apply_requested_edits(
        source,
        [TextEditOp(op="replace_text", match="}", replace=replacement)],
        "将代码优化，并且在合适位置添加返回语句",
        "code.txt",
    )
    assert b"int main(void)" in edited
    assert b'printf("hello word");' in edited
    assert edited.endswith(b"    return 0;\r\n}")


def test_return_outside_function_is_rejected_even_when_uploaded_as_txt():
    with pytest.raises(UnknownEditOpError, match="return"):
        apply_edits(
            SOURCE,
            [{"op": "replace_text", "match": "}\n", "replace": "}return 0;\n"}],
            filename="code.txt",
        )


def test_return_inside_function_preserves_existing_logic():
    result, changes = apply_edits(
        SOURCE,
        [{"op": "replace_text", "match": "}\n", "replace": " return 0;\n}\n"}],
        filename="code.txt",
    )
    assert changes == 1
    assert b'printf("hello world");\n return 0;\n}' in result


def test_replacing_closing_brace_with_return_is_rejected():
    with pytest.raises(UnknownEditOpError, match="大括号"):
        apply_edits(
            SOURCE,
            [{"op": "replace_text", "match": "}", "replace": "return 0;"}],
            filename="code.txt",
        )


def test_return_in_string_or_comment_is_not_a_scope_error():
    source = b'#include<stdio.h>\nint main(){ puts("return}"); return 0;}\n// return 0;\n'
    assert apply_edits(source, [], filename="code.txt")[0] == source


@pytest.mark.parametrize(
    "source",
    [
        b'int main(){ puts("return 0;"); }',
        b"int main(){ /* return 0; */ }",
        b"int main(){ if (0) { return 0; } }",
        b"// main(){ return 0; }\nint other(){ return 0; }",
    ],
)
def test_requirement_check_rejects_comment_string_and_nested_only_return(source):
    from forgeflow.documents.textfile_edit import main_return_satisfied

    assert not main_return_satisfied(source)
