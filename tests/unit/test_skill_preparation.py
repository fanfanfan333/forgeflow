import pytest

from forgeflow.skills.runtime import preparation_calls


def test_preparation_reuses_task_intent_without_mutating_the_skill():
    step = {"tool": "document.inspect", "execution": "prepare", "args": {"query": "$intent", "limit": 1}}
    skills = [{"id": "review", "procedure": [step]}]
    assert preparation_calls(skills, "Read T15 goal") == [
        {"name": "document.inspect", "args": {"query": "Read T15 goal", "limit": 1}, "id": "skill-prepare:review"}
    ]
    assert step["args"] == {"query": "$intent", "limit": 1}
    assert preparation_calls([{"procedure": [{"tool": "document.inspect"}]}], "Read") == []


@pytest.mark.parametrize("tool", ["document.edit", "textfile.edit", "research.read", "code.execute", "report.render"])
def test_preparation_cannot_execute_writes_scripts_or_network_calls(tool):
    with pytest.raises(ValueError, match="local inspection"):
        preparation_calls([{"procedure": [{"tool": tool, "execution": "prepare"}]}], "Read")


@pytest.mark.parametrize("args", [{"paths": ["C:/private"]}, {"query": "cached test answer"}, {"tenant_id": "other"}, [], {"query": ["$intent"]}, {"limit": 0}, {"limit": 21}, {"limit": True}])
def test_preparation_cannot_override_resources_identity_or_embed_answers(args):
    with pytest.raises(ValueError, match="task-owned"):
        preparation_calls([{"procedure": [{"tool": "document.inspect", "execution": "prepare", "args": args}]}], "Read")


def test_multiple_preparations_are_rejected_before_any_execution():
    skill = {"procedure": [{"tool": "document.inspect", "execution": "prepare"}]}
    with pytest.raises(ValueError, match="Only one"):
        preparation_calls([skill, skill], "Read")


def test_exact_selector_binds_only_an_unambiguous_task_and_preserves_the_field():
    skills = [{"id": "review", "procedure": [{"tool": "document.inspect", "execution": "prepare",
               "args": {"task_id": "$task_id", "field": "目标", "limit": 1}}]}]
    assert preparation_calls(skills, "读取 T15 的目标，按 T15 原文回答")[0]["args"] == {
        "task_id": "T15", "field": "目标", "limit": 1,
    }
    assert preparation_calls(skills, "比较 T15 和 T16 的目标") == []
    assert preparation_calls(skills, "读取所有目标") == []


@pytest.mark.parametrize("args", [{"task_id": "$task_id"}, {"field": "目标"},
                                  {"task_id": "T15", "field": "目标"}])
def test_recipe_cannot_assume_a_fixed_task_or_partial_selector(args):
    with pytest.raises(ValueError):
        preparation_calls([{"procedure": [{"tool": "document.inspect", "execution": "prepare", "args": args}]}], "T16")
