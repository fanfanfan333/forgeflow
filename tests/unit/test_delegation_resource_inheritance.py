"""Delegation must not die on resources the model names but the parent
did not declare, and a specialist child must inherit the parent's real
attached files.

Regression for B run ff727731 at bbc9916: three ``agent.delegate``
calls all failed with "Delegation resources must be selected parent
resources", B never reached ``code.execute`` and started zero
containers. The parent's real inputs were a ``repo_path`` and a
server-derived handoff DOCX — neither a declared resource id — so the
old check could never pass, and a child created with only
``{"resources": [...]}`` had no platform-provided path for
``textfile.inspect`` / ``document.inspect`` at all.
"""

import os
from types import SimpleNamespace

import pytest

from forgeflow.runtime.delegation import (
    _child_inputs,
    _filter_resources,
    _validate_delegation_item,
)


def _ctx(context):
    parent = SimpleNamespace(delegation_role=None, allowed_tools=None)
    parent_task = SimpleNamespace(context=context)
    return SimpleNamespace(runtime_context=parent, task_context=parent_task)


def test_filter_resources_keeps_declared_and_drops_unknown():
    ctx = _ctx({"resources": ["res-1"], "llm_runtime": {"acceptance": {"requirements": []}}})
    kept, dropped = _filter_resources(["res-1", "calc.py", "handoff-doc"], ctx)
    assert kept == ["res-1"]
    assert dropped == ["calc.py", "handoff-doc"]
    with pytest.raises(ValueError):
        _filter_resources("not-a-list", ctx)


def test_validate_drops_unknown_resources_instead_of_raising():
    ctx = _ctx({"resources": [], "llm_runtime": {"acceptance": {"requirements": []}}})
    brief = _validate_delegation_item(
        {"role": "code_review", "instruction": "审阅 calc.py", "resources": ["calc.py"]}, ctx
    )
    assert brief["resources"] == []
    assert brief["resources_dropped"] == ["calc.py"]
    with pytest.raises(ValueError):
        _validate_delegation_item({"role": "unknown", "instruction": "x", "resources": []}, ctx)


def test_child_inputs_inherit_repo_files_and_scope_paths(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "notes.md").write_text("# notes\n")
    (repo / ".hidden.py").write_text("x = 1\n")
    scope = SimpleNamespace(
        resources={},
        inherited_inputs={
            "document_paths": ["/tmp/doc1"],
            "document_names": ["规范.docx"],
            "paths": ["/tmp/doc1"],
        },
    )
    # _child_inputs reads current_input_scope from input_scope module:
    # patch it there.
    from forgeflow.runtime import input_scope as input_scope_mod

    token = input_scope_mod.current_input_scope.set(scope)
    try:
        out = _child_inputs(_ctx({"repo_path": str(repo)}))
    finally:
        input_scope_mod.current_input_scope.reset(token)
    assert str(repo) in out["repo_path"]
    assert os.path.join(str(repo), "calc.py") in out["text_paths"]
    assert os.path.join(str(repo), "notes.md") in out["text_paths"]
    assert not any(".hidden" in p for p in out["text_paths"])
    assert "/tmp/doc1" in out["document_paths"]
    assert "/tmp/doc1" in out["paths"]


def test_child_inputs_resolve_explicit_handoff_documents(monkeypatch, tmp_path):
    doc = tmp_path / "a.docx"
    doc.write_text("fake docx bytes")

    class _FakeStore:
        def resolve(self, ref):
            assert ref == "ref-a"
            return doc

    monkeypatch.setattr("forgeflow.documents.store.DocArtifactStore", _FakeStore)
    ctx = _ctx(
        {
            "handoff_results": [
                {"kind": "document", "title": "规范.docx", "content_ref": "ref-a"},
                {"kind": "document", "title": "无引用", "content_ref": ""},
            ]
        }
    )
    out = _child_inputs(ctx)
    assert str(doc) in out["document_paths"]
    assert "规范.docx" in out["document_names"]
