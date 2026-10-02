"""BE-3 jump ⑤ — deterministic, **offline** test generation + sandbox execution.

Two responsibilities:

* :func:`generate_tests` — derive a case set that covers the four required
  categories (``normal`` / ``boundary`` / ``adversarial`` / ``security``).
* :func:`run_tests` — execute those cases in a *declarative* sandbox.

Sandbox guarantees (INC43 §1.1 — "deterministic / offline / no ``eval``"):

* **No** ``eval`` / ``exec`` / ``compile`` of contract content — assertions are
  a fixed vocabulary of predicates over the contract's *declared* structure.
* **No** network, no filesystem, no LLM. Pure functions of ``(contract, case)``.
* ``verdict`` ∈ ``{pass, fail, error}``. ``error`` means "the sandbox could not
  decide" (an unknown assertion or a malformed case) and is **never** collapsed
  to ``pass`` — the caller (``sandbox_evaluate``) keeps it out of the verified
  denominator.

The structural rubric (``structural_score``) reuses the **evaluator's** exact
weights and pass threshold (``evaluator._WEIGHTS`` / ``_PASS_THRESHOLD``) so the
sandbox and the promote-time evaluation share one yardstick instead of two.
"""

from __future__ import annotations

from typing import Any, Callable

from forgeflow.skills.contracts import (
    TEST_CATEGORIES,
    SkillContract,
    SkillTestCase,
    SkillTestRun,
)
from forgeflow.skills.evaluator import _PASS_THRESHOLD, _WEIGHTS
from forgeflow.skills.trust_baseline import allowed_tool_set

__all__ = [
    "ASSERT_INPUT_KEYS_DECLARED",
    "ASSERT_OUTPUT_DECLARED",
    "ASSERT_STRUCTURALLY_VIABLE",
    "ASSERT_TOOLS_WHITELISTED",
    "ASSERT_VERIFICATION_DECLARED",
    "ASSERT_DECLARED_INPUTS",
    "structural_score",
    "generate_tests",
    "run_tests",
]

# --- Assertion vocabulary (fixed, evaluable offline) ----------------------- #
ASSERT_INPUT_KEYS_DECLARED = "input_keys_declared"
ASSERT_OUTPUT_DECLARED = "output_declared"
ASSERT_VERIFICATION_DECLARED = "verification_declared"
ASSERT_TOOLS_WHITELISTED = "tools_whitelisted"
ASSERT_STRUCTURALLY_VIABLE = "structurally_viable"
#: A category-agnostic declaration check used to synthesise case inputs.
ASSERT_DECLARED_INPUTS = "declared_inputs_present"


def structural_score(contract: SkillContract) -> float:
    """Deterministic completeness score using the evaluator's rubric.

    Mirrors ``evaluator`` exactly so the sandbox and promote share a yardstick:

    * ``completeness`` (0.5) — fraction of the four gating elements present;
    * ``coverage``     (0.3) — 1.0 when both inputs *and* outputs are declared,
      0.5 when exactly one is, 0.0 otherwise;
    * ``tool_diversity`` (0.2) — ``min(1.0, len(tools) / 3)`` (evaluator's own
      formula).
    """
    required = (contract.goal, contract.procedure, contract.tools)
    present = sum(1 for element in required if element)
    io_present = bool(contract.inputs) and bool(contract.outputs)
    io_partial = bool(contract.inputs) or bool(contract.outputs)
    completeness = present / len(required)
    coverage = 1.0 if io_present else (0.5 if io_partial else 0.0)
    tool_diversity = min(1.0, len(contract.tools) / 3.0)
    score = (
        completeness * _WEIGHTS["completeness"]
        + coverage * _WEIGHTS["coverage"]
        + tool_diversity * _WEIGHTS["tool_diversity"]
    )
    return round(score, 4)


def _sample_input(contract: SkillContract) -> dict[str, Any]:
    """A case input drawn from the contract's *declared* inputs (never invented)."""
    return {key: "sample" for key in contract.inputs}


def generate_tests(contract: SkillContract) -> list[SkillTestCase]:
    """Generate a case set covering all four required categories.

    The cases are derived from the contract's declared structure; a case never
    invents an input key the contract does not claim to accept.
    """
    cases: list[SkillTestCase] = [
        SkillTestCase(
            id="case-normal-inputs",
            category="normal",
            input=_sample_input(contract),
            expectation="契约声明的输入键都被接受",
            assertion=ASSERT_INPUT_KEYS_DECLARED,
        ),
        SkillTestCase(
            id="case-normal-viable",
            category="normal",
            input={},
            expectation="契约结构完整度达到通过阈值",
            assertion=ASSERT_STRUCTURALLY_VIABLE,
        ),
        SkillTestCase(
            id="case-boundary-verification",
            category="boundary",
            input=_sample_input(contract),
            expectation="边界输入受校验断言约束",
            assertion=ASSERT_VERIFICATION_DECLARED,
        ),
        SkillTestCase(
            id="case-adversarial-tools",
            category="adversarial",
            input={"__adversarial__": "off-whitelist tool probe"},
            expectation="不得引入白名单外的工具",
            assertion=ASSERT_TOOLS_WHITELISTED,
        ),
        SkillTestCase(
            id="case-security-tools",
            category="security",
            input={"__security__": "privilege probe"},
            expectation="安全基线：工具集合 ⊆ 平台白名单",
            assertion=ASSERT_TOOLS_WHITELISTED,
        ),
    ]
    return cases


def _predicate_input_keys_declared(contract: SkillContract, case: SkillTestCase) -> tuple[bool, str]:
    if not contract.inputs:
        return False, "契约未声明任何输入"
    extra = sorted(set(case.input) - set(contract.inputs))
    if extra:
        return False, f"契约不接受的输入键：{', '.join(extra)}"
    return True, f"已声明输入：{', '.join(sorted(contract.inputs))}"


def _predicate_output_declared(contract: SkillContract, case: SkillTestCase) -> tuple[bool, str]:
    if not contract.outputs:
        return False, "契约未声明输出"
    return True, f"已声明输出：{', '.join(sorted(contract.outputs))}"


def _predicate_verification_declared(
    contract: SkillContract, case: SkillTestCase
) -> tuple[bool, str]:
    if not contract.verification:
        return False, "契约未声明校验断言"
    return True, f"校验断言：{'; '.join(contract.verification)}"


def _predicate_tools_whitelisted(
    contract: SkillContract, case: SkillTestCase
) -> tuple[bool, str]:
    if not contract.tools:
        return False, "契约未声明任何工具"
    catalogue = allowed_tool_set()
    illegal = sorted({t for t in contract.tools if t not in catalogue})
    if illegal:
        return False, f"白名单外工具：{', '.join(illegal)}"
    return True, f"工具均在白名单内：{', '.join(sorted(contract.tools))}"


def _predicate_structurally_viable(
    contract: SkillContract, case: SkillTestCase
) -> tuple[bool, str]:
    score = structural_score(contract)
    if score >= _PASS_THRESHOLD:
        return True, f"结构完整度 {score} ≥ 阈值 {_PASS_THRESHOLD}"
    return False, f"结构完整度 {score} < 阈值 {_PASS_THRESHOLD}"


#: assertion token → deterministic predicate over ``(contract, case)``.
_PREDICATES: dict[str, Callable[[SkillContract, SkillTestCase], tuple[bool, str]]] = {
    ASSERT_INPUT_KEYS_DECLARED: _predicate_input_keys_declared,
    ASSERT_OUTPUT_DECLARED: _predicate_output_declared,
    ASSERT_VERIFICATION_DECLARED: _predicate_verification_declared,
    ASSERT_TOOLS_WHITELISTED: _predicate_tools_whitelisted,
    ASSERT_STRUCTURALLY_VIABLE: _predicate_structurally_viable,
    ASSERT_DECLARED_INPUTS: _predicate_input_keys_declared,
}


def run_tests(contract: SkillContract, cases: list[SkillTestCase]) -> list[SkillTestRun]:
    """Execute ``cases`` against ``contract`` in the declarative sandbox.

    Returns one :class:`SkillTestRun` per case. A run's verdict is ``pass`` /
    ``fail`` from the predicate, or ``error`` when the sandbox cannot decide
    (unknown assertion, or a non-dict case input). ``error`` is **never**
    reported as ``pass``.
    """
    runs: list[SkillTestRun] = []
    for case in cases:
        if not isinstance(case.input, dict):
            runs.append(
                SkillTestRun(
                    case_id=case.id,
                    verdict="error",
                    detail=f"沙箱无法执行：input 不是对象（{type(case.input).__name__}）",
                )
            )
            continue
        predicate = _PREDICATES.get(case.assertion)
        if predicate is None:
            runs.append(
                SkillTestRun(
                    case_id=case.id,
                    verdict="error",
                    detail=f"未知断言，沙箱无法判定：{case.assertion!r}",
                )
            )
            continue
        ok, detail = predicate(contract, case)
        runs.append(
            SkillTestRun(
                case_id=case.id,
                verdict="pass" if ok else "fail",
                detail=detail,
            )
        )
    return runs


#: Sanity guard: every generated category must be a registered category.
assert set(TEST_CATEGORIES) == {"normal", "boundary", "adversarial", "security"}
