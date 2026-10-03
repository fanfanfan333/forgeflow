"""INC44 T04 — the text / code plane through the runtime handlers + projection.

``textfile.inspect`` / ``textfile.edit`` are the two new plan tools (design §1.3).
They are ``real`` stdlib implementations: inspect measures the real line / EOL /
encoding structure and edit is the Tool layer that really writes the bytes —
preserving EOL, encoding and BOM.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from forgeflow.runtime.artifacts import ARTIFACT_KIND_TEXT, artifacts_from_invocations
from forgeflow.runtime.gate import PLATFORM_PLAN_TOOLS, TOOL_PERMISSION_MAP


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(run_id="run-txt", tenant_id="t-txt", role="manager", args={})


@pytest.fixture
def tmp_doc_store(tmp_path, monkeypatch):
    from forgeflow.documents import store as store_mod

    real = store_mod.DocArtifactStore

    def _factory(root=None):
        return real(root=root if root is not None else tmp_path / "docstore")

    import forgeflow.documents as docs_pkg

    monkeypatch.setattr(store_mod, "DocArtifactStore", _factory)
    monkeypatch.setattr(docs_pkg, "DocArtifactStore", _factory)
    return tmp_path


async def test_textfile_inspect_reports_real_structure(tmp_path):
    from forgeflow.runtime.tool_handlers import textfile_inspect

    source = tmp_path / "app.py"
    source.write_bytes(b"x = 40\nprint(x)\n")

    ok = await textfile_inspect({"paths": [str(source)]}, _ctx())

    assert ok["ok"] is True
    assert ok["provider"] == "stdlib"
    assert ok["format"] == "text"
    assert ok["structure"]["lines"] == 2
    assert ok["structure"]["eol"] == "lf"


async def test_textfile_edit_writes_bytes_and_preserves_eol(tmp_path, tmp_doc_store):
    from forgeflow.documents import DocArtifactStore
    from forgeflow.runtime.tool_handlers import textfile_edit

    source = tmp_path / "app.py"
    source.write_bytes(b"value = 40\r\nprint(value)\r\n")  # CRLF on purpose

    result = await textfile_edit(
        {"paths": [str(source)], "edits": [{"op": "replace_text", "match": "40", "replace": "38"}]},
        _ctx(),
    )

    assert result["ok"] is True
    assert result["provider"] == "stdlib"
    assert result["format"] == "text"
    assert result["filename"] == "app.edited.py"
    assert result["artifact_ref"].endswith(".txt")
    assert result["modified"] == 1
    assert "content" not in result

    blob = DocArtifactStore(root=tmp_doc_store / "docstore").get(result["artifact_ref"])
    assert b"38" in blob
    # The CRLF convention survived the edit.
    assert b"\r\n" in blob and b"\n" not in blob.replace(b"\r\n", b"")


async def test_textfile_edit_exhaustion_fails_honestly(tmp_path, tmp_doc_store):
    from forgeflow.runtime.tool_handlers import textfile_edit

    source = tmp_path / "app.py"
    source.write_bytes(b"value = 40\n")
    result = await textfile_edit(
        {
            "paths": [str(source)],
            "edits": [{"op": "replace_text", "match": "40", "replace": "38"}],
            "max_chars": 1,
        },
        _ctx(),
    )
    assert result["ok"] is False
    assert "校验未通过" in result["reason"]
    assert "artifact_ref" not in result


async def test_textfile_edit_uses_the_shared_error_for_an_illegal_op(tmp_path):
    """An illegal op is refused by the caller's ``UnknownEditOpError``."""
    from forgeflow.documents import UnknownEditOpError
    from forgeflow.runtime.tool_handlers import textfile_edit

    source = tmp_path / "app.py"
    source.write_bytes(b"value = 40\n")
    result = await textfile_edit(
        {"paths": [str(source)], "edits": [{"op": "delete_everything"}]}, _ctx()
    )
    assert result["ok"] is False
    assert "非法" in result["reason"]
    # Sanity: the same class is what a direct call raises.
    from forgeflow.documents import TextEditOp

    with pytest.raises(UnknownEditOpError):
        TextEditOp.from_dict({"op": "delete_everything"})


async def test_text_payload_projects_the_text_file_artifact():
    inv = {
        "tool": "textfile.edit",
        "status": "ok",
        "result_ref": "ab/ef",
        "started_at": "2026-10-03T00:00:00",
        "payload": {
            "ok": True,
            "format": "text",
            "filename": "app.edited.py",
            "modified": 1,
            "added": 0,
            "removed": 0,
            "numeric_changes": 1,
            "artifact_ref": "ab/ef.txt",
        },
    }
    artifacts = artifacts_from_invocations("run-t", [inv])
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact["kind"] == ARTIFACT_KIND_TEXT == "text_file"
    assert artifact["format"] == "text"
    assert artifact["title"] == "app.edited.py"
    assert artifact["content_ref"] == "ab/ef.txt"
    assert artifact["source"] == "textfile.edit"


async def test_textfile_tools_are_platform_tools_without_a_narrow_grant():
    for tool in ("textfile.inspect", "textfile.edit"):
        assert tool in PLATFORM_PLAN_TOOLS
        assert tool not in TOOL_PERMISSION_MAP
