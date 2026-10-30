"""A handoff/code parent must be able to read its own inputs.

Regression for B run 6ecb399c at 0199b7d: the parent could not
``document.inspect`` the handoff DOCX (no declared document) and could
not ``textfile.inspect`` its repo files (no declared text file), so
the model had nothing to read before ``code.execute`` and looped on
delegation instead. Both gaps are now closed at the platform-args
layer: handoff documents are appended to ``_document_args``, repo
top-level text files fill ``_textfile_args`` when nothing else was
declared.
"""

from types import SimpleNamespace

from forgeflow.runtime import orchestrator as orch


def _cap(explicit):
    return SimpleNamespace(explicit_inputs=explicit)


def test_document_args_append_handoff_documents(monkeypatch, tmp_path):
    doc = tmp_path / "a.docx"
    doc.write_text("fake")

    class _FakeStore:
        def resolve(self, ref):
            assert ref == "ref-a"
            return doc

    monkeypatch.setattr("forgeflow.documents.store.DocArtifactStore", _FakeStore)
    task = SimpleNamespace(
        context={
            "handoff_results": [
                {"kind": "document", "title": "规范.docx", "content_ref": "ref-a"},
            ]
        }
    )
    out = orch._document_args(task, _cap({}))
    assert str(doc) in out["document_paths"]
    assert "规范.docx" in out["document_names"]


def test_textfile_args_list_repo_files_when_nothing_declared(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "test_calc.py").write_text("def test_add():\n    assert add(1, 2) == 3\n")
    task = SimpleNamespace(context={})
    out = orch._textfile_args(task, _cap({"repo_path": str(repo)}))
    assert out["text_paths"][0].endswith("calc.py")
    assert out["text_paths"][1].endswith("test_calc.py")
    assert out["paths"] == out["text_paths"]


def test_textfile_args_declared_files_keep_priority(tmp_path):
    task = SimpleNamespace(context={})
    out = orch._textfile_args(
        task, _cap({"text_paths": ["/tmp/declared.py"], "repo_path": "/tmp/repo"})
    )
    assert out["text_paths"] == ["/tmp/declared.py"]


def test_handoff_document_paths_derived_from_source_run(monkeypatch, tmp_path):
    """Derived handoff (handoff_from_run_id) must resolve document paths
    from the source run's artifacts, not from the derived items (which
    carry no content_ref — the validator forbids it). The old code
    always returned [] here, so a parent/child could never
    document.inspect the derived DOCX."""
    from types import SimpleNamespace

    from forgeflow.runtime import orchestrator as orch

    doc = tmp_path / "spec.docx"
    doc.write_text("fake")

    class _FakeStore:
        def resolve(self, ref):
            assert ref == "ref-spec"
            return doc

    monkeypatch.setattr("forgeflow.documents.store.DocArtifactStore", _FakeStore)

    class _FakeWorkspaceStore:
        def get(self, run_id):
            assert run_id == "source-run"
            return SimpleNamespace(
                run_id="source-run",
                tenant_id="t",
                actor_user_id="u",
                artifacts=[
                    {
                        "title": "规范.docx",
                        "content_ref": "ref-spec",
                        "content": "",
                        "format": "docx",
                    }
                ],
                llm={},
                codeplane={},
                errors=[],
            )

    monkeypatch.setattr(
        "forgeflow.workspace.store.get_workspace_store", lambda: _FakeWorkspaceStore()
    )
    paths, names = orch._handoff_document_paths(
        {"handoff_from_run_id": "source-run"}, tenant_id="t", user_id="u"
    )
    assert str(doc) in paths
    assert "规范.docx" in names


def test_scoped_parent_falls_back_to_workspace_store_after_restart(monkeypatch):
    """A handoff/continued parent from before an API restart must still
    resolve: the in-process run store is empty after a restart, but the
    workspace store (Postgres) still holds the run."""
    from types import SimpleNamespace

    from forgeflow.runtime import orchestrator as orch

    class _EmptyRunStore:
        def get(self, run_id):
            return None

    class _FakeWorkspaceStore:
        def get(self, run_id):
            return SimpleNamespace(run_id=run_id, tenant_id="t", artifacts=[])

    monkeypatch.setattr(orch, "get_run_store", lambda: _EmptyRunStore())
    monkeypatch.setattr(
        "forgeflow.workspace.store.get_workspace_store", lambda: _FakeWorkspaceStore()
    )
    parent = orch._scoped_parent("old-run")
    assert parent is not None
    assert parent.run_id == "old-run"
