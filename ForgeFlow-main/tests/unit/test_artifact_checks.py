import pytest

from forgeflow.documents.store import DocArtifactStore
from forgeflow.evaluation.artifact_checks import check_artifacts, run_verdict


def test_final_text_cannot_replace_actual_file_evidence():
    checks = check_artifacts([], [{"kind": "contains", "value": "done"}])
    assert not run_verdict({"status": "completed", "outcome": "success"}, checks)
    assert not run_verdict({"status": "completed", "outcome": "partial"}, {"file": True})


def test_research_report_needs_actual_verified_invocation():
    assertion = [{"kind": "research_verified", "format": "markdown", "value": "example.org"}]
    assert check_artifacts([("markdown", b"example.org")], assertion) == {
        "0:research_verified": False
    }


@pytest.mark.parametrize(
    "ref",
    [
        "C:/private.txt",
        "https://example.org/file",
        "/etc/passwd",
        "../outside.txt",
        "//server/share/file",
    ],
)
def test_artifact_store_rejects_absolute_and_url_references(tmp_path, ref):
    store = DocArtifactStore(tmp_path)
    with pytest.raises(ValueError):
        store.get(ref)


def test_artifact_store_keeps_real_content_addressed_files_readable(tmp_path):
    store = DocArtifactStore(tmp_path)
    reference = store.put(b"real bytes", ".txt")
    assert store.get(reference) == b"real bytes"


def test_verified_research_accepts_real_markdown_report():
    from forgeflow.evaluation.artifact_checks import check_artifacts

    receipt = {
        "tool": "research.verify",
        "status": "ok",
        "payload": {"validation": {"claims_supported": True, "citations_ok": True}},
    }
    assert all(
        check_artifacts(
            [("markdown", b"See https://docs.python.org/")],
            [{"kind": "research_verified", "value": "docs.python.org"}],
            [receipt],
        ).values()
    )


def test_definition_assertion_uses_actual_ast_receipt(tmp_path):
    from forgeflow.codeplane.navigation import locate_symbol
    from forgeflow.evaluation.artifact_checks import check_artifacts

    source = tmp_path / "module.py"
    source.write_text("def parse_total(value):\n    return int(value)\n")
    payload = locate_symbol([str(source)], "parse_total")
    receipt = {"tool": "code.lint", "status": "ok", "executed": True, "payload": payload}
    assert all(
        check_artifacts(
            [], [{"kind": "code_definition", "value": "parse_total"}], [receipt]
        ).values()
    )
    assert not all(
        check_artifacts([], [{"kind": "code_definition", "value": "other"}], [receipt]).values()
    )


@pytest.mark.parametrize(
    "assertion",
    [
        {"kind": "execute_python", "value": "print(1)"},
        {"kind": "contains", "value": ""},
        {"kind": "xlsx_cell", "cell": "A0", "sheet": "Sheet"},
    ],
)
def test_invalid_assertion_is_rejected_before_any_artifact_exists(assertion):
    with pytest.raises(ValueError):
        check_artifacts([], [assertion])


def test_business_report_checks_only_delivered_answer_not_question_or_logs():
    assertions = [
        {"kind": "report_answer_contains", "format": "markdown", "value": "required goal"}
    ]
    report = b"# Report\nrequired goal\n## \xe6\x9c\x80\xe7\xbb\x88\xe7\xad\x94\xe6\xa1\x88\nwrong answer\n## logs\nrequired goal"
    assert not all(check_artifacts([("markdown", report)], assertions).values())
    correct = report.replace(b"wrong answer", b"required goal")
    assert all(check_artifacts([("markdown", correct)], assertions).values())
    assert not all(check_artifacts([], assertions).values())
