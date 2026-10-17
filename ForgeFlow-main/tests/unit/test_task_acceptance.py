from forgeflow.runtime.acceptance import assess, requirement_list


def test_compound_edit_needs_measured_requirement_evidence():
    requirements = requirement_list("优化代码，再增加返回语句", {"resources": ["file"]})
    assert len(requirements) == 2
    records = [{"tool": "textfile.edit", "status": "ok", "payload": {"ok": True}}]
    assert not assess(requirements, records)["complete"]
    records[0]["payload"]["validation"] = {"requirement_ok": True}
    assert not assess(requirements, records)["complete"]
    records[0]["payload"]["validation"]["return_ok"] = True
    assert not assess(requirements, records)["complete"]
    records[0]["payload"]["validation"]["edit_ok"] = True
    assert assess(requirements, records)["complete"]


def test_search_result_is_not_source_reading():
    requirements = requirement_list("研究最新资料", {})
    assert not assess(
        requirements, [{"tool": "research.search", "status": "ok", "payload": {"ok": True}}]
    )["complete"]


def test_unverified_is_not_failure_or_fabricated_evidence():
    rows = assess(requirement_list("增加返回语句", {}), [])
    assert rows["requirements"][0]["status"] == "unverified"
    assert rows["requirements"][0]["evidence"] == []


def test_reading_a_source_does_not_verify_report_claims():
    records = [
        {
            "tool": "research.read",
            "status": "ok",
            "payload": {
                "source": "https://example.org",
                "content_hash": "hash",
                "content": "source text",
            },
        }
    ]
    assert not assess(requirement_list("research", {}), records)["complete"]


def test_no_requirements_is_not_a_measured_success():
    assert assess([], [])["complete"] is False


def test_latest_edit_invalidates_earlier_success_evidence():
    items = requirement_list("增加返回语句", {})
    records = [
        {"tool": "textfile.edit", "status": "ok", "payload": {"validation": {"return_ok": True}}},
        {
            "tool": "textfile.edit",
            "status": "error",
            "payload": {"validation": {"return_ok": False}},
        },
    ]
    result = assess(items, records)
    assert result["complete"] is False
    assert result["requirements"][0]["evidence"] == []


def test_execution_finished_does_not_mean_requirements_completed():
    from forgeflow.validation.validator import validate

    verdict = validate(
        {
            "status": "completed",
            "steps": [{"status": "ok"}],
            "acceptance": {
                "complete": False,
                "requirements": [
                    {"id": "edit", "description": "Edit correctly", "status": "unverified"}
                ],
            },
        }
    )
    assert verdict.outcome == "partial"


def test_return_only_is_not_duplicated_as_an_optimization_requirement():
    assert [r["id"] for r in requirement_list("增加返回语句", {"resources": ["file"]})] == [
        "return"
    ]
