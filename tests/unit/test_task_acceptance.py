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


def _delegate_record(payload):
    return {"tool": "agent.delegate", "status": "ok", "payload": payload}


def test_delegated_research_satisfies_research_requirement():
    # 2026-11-01 6fad659 delegation run: experts read the sources and the
    # parent failed on "需求尚未全部验证" only because assess() counted
    # nothing but the parent's own research.verify calls.
    records = [
        _delegate_record(
            {
                "ok": True,
                "role": "research",
                "summary": "spec says add returns a + b",
                "evidence": [
                    {
                        "step_id": "s1",
                        "tool": "textfile.inspect",
                        "result_ref": "text-inspect:specification.txt:1",
                    }
                ],
            }
        )
    ]
    result = assess(requirement_list("请研究这份资料", {}), records)
    assert result["complete"] is True
    assert result["requirements"][0]["status"] == "satisfied"


def test_parallel_delegation_research_item_satisfies_despite_sibling_failure():
    records = [
        {
            "tool": "agent.delegate",
            "status": "error",
            "payload": {
                "ok": False,
                "ok_count": 1,
                "results": [
                    {
                        "role": "research",
                        "ok": True,
                        "evidence": [
                            {
                                "step_id": "s1",
                                "tool": "textfile.inspect",
                                "result_ref": "text-inspect:specification.txt:1",
                            }
                        ],
                    },
                    {"role": "code_review", "ok": False, "evidence": []},
                ],
            },
        }
    ]
    assert assess(requirement_list("请研究这份资料", {}), records)["complete"] is True


def test_delegated_research_without_evidence_does_not_satisfy():
    records = [
        _delegate_record({"ok": True, "role": "research", "summary": "says so", "evidence": []})
    ]
    assert assess(requirement_list("请研究这份资料", {}), records)["complete"] is False


def test_cached_delegation_keeps_research_satisfied():
    records = [
        _delegate_record(
            {
                "ok": True,
                "role": "research",
                "already_completed": True,
                "evidence": [
                    {
                        "step_id": "s1",
                        "tool": "textfile.inspect",
                        "result_ref": "text-inspect:specification.txt:1",
                    }
                ],
            }
        )
    ]
    assert assess(requirement_list("请研究这份资料", {}), records)["complete"] is True


def test_non_research_delegation_does_not_satisfy_research():
    records = [
        _delegate_record(
            {
                "ok": True,
                "role": "code_review",
                "evidence": [
                    {"step_id": "s1", "tool": "code.lint", "result_ref": "lint:calc.py"}
                ],
            }
        )
    ]
    assert assess(requirement_list("请研究这份资料", {}), records)["complete"] is False


def test_failed_delegation_recovers_only_when_acceptance_complete():
    # 2026-11-01 B run 3ba456dc: code diff + pytest 3/3, but the parent
    # failed because a side delegation read nothing. The delegate error
    # is historical once the run's own acceptance is complete; without
    # complete acceptance it must still fail the run.
    from forgeflow.runtime.acceptance import recovered_tool_errors

    record = {
        "tool": "agent.delegate",
        "step_id": "d1",
        "status": "error",
        "error": "子任务没有实际读取或检查任何证据，未完成审阅",
        "payload": {"ok": False, "role": "research"},
    }
    direct = "工具 'agent.delegate' 未成功执行：子任务没有实际读取或检查任何证据，未完成审阅"
    stale = "工具 'agent.delegate' 尚未修复：子任务没有实际读取或检查任何证据，未完成审阅"
    recovered, _ = recovered_tool_errors([record], acceptance_complete=True)
    assert direct in recovered
    assert stale in recovered
    recovered, _ = recovered_tool_errors([record])
    assert direct not in recovered
    assert stale not in recovered


def test_successful_delegation_is_not_a_recovered_error():
    from forgeflow.runtime.acceptance import recovered_tool_errors

    record = {
        "tool": "agent.delegate",
        "step_id": "d1",
        "status": "ok",
        "payload": {"ok": True, "role": "research", "evidence": [{"step_id": "s1"}]},
    }
    recovered, _ = recovered_tool_errors([record], acceptance_complete=True)
    assert not any("agent.delegate" in message for message in recovered)
