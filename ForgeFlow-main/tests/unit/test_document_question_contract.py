import json

from forgeflow.documents.question_contract import questions_for, validate_answers

QUESTIONS = ["前置是什么", "预算是多少"]
EVIDENCE = "[paragraph:0] 依赖: T28 前置 T12\n[paragraph:1] 预算: 25 步\n"


def test_missing_numbered_answer_is_rejected():
    assert validate_answers('{"answers": []}', QUESTIONS, EVIDENCE)[1]


def test_quotes_must_belong_to_the_claimed_source():
    answers = [
        {"index": 1, "text": "T12", "source": "paragraph:0", "quote": "25 步"},
        {"index": 2, "text": "25", "source": "paragraph:1", "quote": "25 步"},
    ]
    assert validate_answers(json.dumps({"answers": answers}), QUESTIONS, EVIDENCE)[1]
    answers[0]["quote"] = "T28 前置 T12"
    text, error = validate_answers(json.dumps({"answers": answers}), QUESTIONS, EVIDENCE)
    assert not error and "T12" in text and "25 步" in text


def test_contract_applies_only_to_explicit_readonly_numbered_questions():
    assert len(questions_for("只读取文档，1. 前置是什么？2. 预算是多少？")) == 2
    assert not questions_for("1. 修改标题。2. 新增内容。")
