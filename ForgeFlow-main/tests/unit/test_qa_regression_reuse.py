"""Reuse-point counter-evidence for Phase D (§1.5 G3).

Phase D reuses two existing modules by **aliased import** rather than
re-implementing them:

* :func:`forgeflow.evaluation.regression.check_dict` — the tolerance policy;
* :func:`forgeflow.rollout.metrics.wilson_lower_bound` — the interval lower bound.

This suite proves the reuse is real and offline (unmeasured ⇒ ``None``, no
baseline ⇒ not a regression) and that ``qa/regression.py`` **never shadows** a
referenced symbol (``check`` / ``check_dict`` / ``write_baseline`` /
``TOLERANCES`` / ``RegressionFinding``) — the name-hygiene rule that keeps the
``regression.py`` basename collision with ``evaluation/regression.py`` harmless.
"""

from __future__ import annotations

import ast
from pathlib import Path

from forgeflow.evaluation import regression as eval_regression
from forgeflow.qa import regression as qa_regression
from forgeflow.rollout.metrics import wilson_lower_bound

_QA_REGRESSION_PATH = Path(qa_regression.__file__)

_FORBIDDEN_DEFINED_NAMES = (
    "check",
    "check_dict",
    "write_baseline",
    "TOLERANCES",
    "RegressionFinding",
)


def test_check_dict_first_run_is_not_a_regression():
    assert eval_regression.check_dict({}, None).to_dict() == {"passed": True, "findings": []}
    assert eval_regression.check_dict(None, {"pass_rate": 1.0}).to_dict() == {
        "passed": True,
        "findings": [],
    }


def test_check_dict_flags_a_real_regression_with_qa_tolerances():
    report = eval_regression.check_dict(
        {"pass_rate": 0.5}, {"pass_rate": 1.0}, tolerances=qa_regression.QA_METRIC_TOLERANCES
    ).to_dict()
    assert report["passed"] is False
    assert report["findings"][0]["severity"] == "regression"


def test_wilson_lower_bound_unmeasured_is_none():
    assert wilson_lower_bound(0, 0) is None


def test_wilson_lower_bound_is_below_the_point_estimate():
    bound = wilson_lower_bound(9, 10)
    assert bound is not None
    assert bound < 0.9


def test_qa_regression_does_not_shadow_referenced_symbols():
    tree = ast.parse(_QA_REGRESSION_PATH.read_text(encoding="utf-8"))
    defined: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            defined.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
    assert not (set(_FORBIDDEN_DEFINED_NAMES) & defined)


def test_qa_regression_exposes_its_own_tolerance_table_only():
    for forbidden in _FORBIDDEN_DEFINED_NAMES:
        assert not hasattr(qa_regression, forbidden)
    assert hasattr(qa_regression, "QA_METRIC_TOLERANCES")
    assert hasattr(qa_regression, "RegressionAnalyzer")
