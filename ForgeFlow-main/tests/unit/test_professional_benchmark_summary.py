"""Offline arithmetic/guard tests; not real-model performance evidence."""

import copy
import runpy
from pathlib import Path

import pytest

compare = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "scripts/summarize_professional_benchmark.py")
)["compare"]


def report(passed):
    return {
        "complete": True,
        "dataset_hash": "fixed",
        "model_digest": "fixed",
        "temperature": 0,
        "num_ctx": 8192,
        "think": False,
        "repetitions": 3,
        "cases": [
            {
                "case_id": domain + str(index),
                "domain": domain,
                "repetition": repeat,
                "passed": passed,
                "false_completion": False,
                "total_tokens": 10,
                "seconds": 2,
            }
            for domain in ("code", "document", "research")
            for index in range(10)
            for repeat in range(1, 4)
        ],
    }


def test_comparison_counts_real_pass_flags_without_overclaiming():
    rows = compare(report(False), report(True))
    assert len(rows) == 3 and all(
        row["baseline"]["passed"] == 0 and row["upgraded"]["passed"] == 30 for row in rows
    )
    degraded = report(True)
    degraded["cases"][0]["false_completion"] = True
    assert compare(report(False), degraded)[0]["nonregression"] is False


@pytest.mark.parametrize(
    "change", [{"complete": False}, {"num_ctx": 16384}, {"dataset_hash": "other"}]
)
def test_incomplete_or_incomparable_runs_never_produce_a_comparison(change):
    changed = copy.deepcopy(report(True))
    changed.update(change)
    with pytest.raises(ValueError):
        compare(report(False), changed)


def test_topic_case_calibration_preserves_raw_and_explicit_identifiers(tmp_path):
    import hashlib
    functions=runpy.run_path(str(Path(__file__).resolve().parents[2]/"scripts/summarize_professional_benchmark.py"))
    calibrate=functions["calibrate_topic_case"]
    artifact=tmp_path/"report.markdown"
    artifact.write_bytes(b"A row security policy restricts rows.")
    row={"case_id":"topic","domain":"research","status":"completed","outcome":"success","passed":False,"false_completion":True,"checks":{"0:research_verified":True,"1:contains":False},"artifact_hashes":[{"path":artifact.name,"sha256":hashlib.sha256(artifact.read_bytes()).hexdigest()}]}
    original={"cases":[row]}
    case={"id":"topic","intent":"研究 https://example.org/docs，解释行级安全策略的用途。","assertions":[{"kind":"research_verified"},{"kind":"contains","value":"POLICY"}]}
    calibrated=calibrate(original,[case],tmp_path)
    assert calibrated["cases"][0]["passed"] is True
    assert calibrated["cases"][0]["raw_passed"] is False and original["cases"][0]["passed"] is False
    case["intent"]="研究 https://example.org/docs，解释 POLICY 标识符。"
    assert calibrate(original,[case],tmp_path)["cases"][0]["passed"] is False
    case["intent"]="解释行级安全策略的用途。"
    artifact.write_bytes(b"changed")
    with pytest.raises(ValueError,match="changed"):
        calibrate(original,[case],tmp_path)
