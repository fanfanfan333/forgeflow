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
    "SandboxMode",
    "RESTRICTED_ALLOWED_CLASSES",
    "structural_score",
    "generate_tests",
    "run_tests",
]


class SandboxMode:
    """The two sandbox execution modes (INC46 T04).

    * :data:`DECLARATIVE` (default) — the INC43 offline predicate sandbox:
      assertions are a fixed vocabulary over the contract's *declared* shape.
    * :data:`RESTRICTED` — simulate the contract's tool plan under **mock**
      bindings, gating each step against the four-level privilege model
      (``READ/WRITE/EXTERNAL/DANGEROUS``); an over-privileged step is
      fail-closed to ``error``.
    """

    DECLARATIVE = "declarative"
    RESTRICTED = "restricted"


#: Classes the restricted sandbox is permitted to run. ``DANGEROUS`` (money
#: movement / data egress / privilege change / ``code.commit``) is **not** —
#: it is the fail-closed line (design §1.4).
RESTRICTED_ALLOWED_CLASSES: frozenset[str] = frozenset({"READ", "WRITE", "EXTERNAL"})

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


def _normalize_sandbox(sandbox: str) -> str:
    """Map ``sandbox`` onto a :class:`SandboxMode` value (default declarative).

    Only the literal ``"restricted"`` selects restricted execution; anything
    else — an empty value, ``None`` or an unknown string — falls back to the
    byte-identical ``"declarative"`` default, so existing callers are unaffected.
    """
    value = getattr(sandbox, "value", sandbox)
    text = str(value or "").strip().lower()
    if text == SandboxMode.RESTRICTED:
        return SandboxMode.RESTRICTED
    return SandboxMode.DECLARATIVE


def _restricted_plan(contract: SkillContract) -> list[str]:
    """The ordered tool ids a restricted run would execute (stable, deduped)."""
    plan: list[str] = []
    for tool in contract.tools or []:
        name = str(tool or "").strip()
        if name and name not in plan:
            plan.append(name)
    return plan


def _run_tests_restricted(
    contract: SkillContract, cases: list[SkillTestCase]
) -> list[SkillTestRun]:
    """Execute ``cases`` by **really running** the contract's plan under mocks.

    The contract's declared tools (its executable plan) are walked in order and
    each step is executed by invoking its **mock** handler (``kind="development"``
    / ``provider="sandbox-mock"``). The mock is a pure coroutine with no I/O, no
    network and no DB — so a restricted run has **zero production side effects**
    — yet the handler genuinely runs (the design's "逐步执行").

    Fail-closed rules (``error`` is never a ``pass``):

    * a step whose :func:`tool_permissions.classify_tool` class is **not** in
      :data:`RESTRICTED_ALLOWED_CLASSES` short-circuits the case to
      ``verdict="error"`` with the verbatim message
      ``"沙箱不允许 <CLASS> 工具 '<t>'，已按越权拦截"``;
    * a step with no mock binding, a mock that raises, or a mock whose result is
      not a successful dict ⇒ ``verdict="error"`` with a verbatim reason.

    The registry is switched to mocks **inside a ``try``/``finally``**: the
    pre-call snapshot is restored before returning, so the process is *never*
    left running on mocks. ``register_mock_bindings()`` only overwrites existing
    tool ids, so the orphan guard ``set(PLATFORM_PLAN_TOOLS) ==
    set(known_ids())`` is preserved.
    """
    from forgeflow.runtime import tool_registry
    from forgeflow.skills import tool_permissions

    snapshot = tool_registry.snapshot_bindings()
    try:
        tool_registry.register_mock_bindings()
        plan = _restricted_plan(contract)
        runs: list[SkillTestRun] = []
        for case in cases:
            verdict, detail = _simulate_restricted_plan(plan, tool_permissions, tool_registry)
            runs.append(SkillTestRun(case_id=case.id, verdict=verdict, detail=detail))
        return runs
    finally:
        # Unconditional restore — a restricted run must never leak mock providers
        # into the global registry (they would silently replace every real
        # handler for the rest of the process).
        tool_registry.restore_bindings(snapshot)


def _invoke_mock_handler_sync(handler: Any, args: dict[str, Any]) -> Any:
    """Run an async mock ``handler`` from this synchronous function — safely.

    ``run_tests`` is synchronous; the mock handlers are coroutines. When there
    is no running loop we drive the coroutine with :func:`asyncio.run`; when a
    loop *is* already running (an async caller, e.g. pytest-asyncio) we run it on
    a short-lived worker thread with its own loop. The mock does no I/O and holds
    no DB pool, so this cannot re-trigger event-loop/pool cross-talk.
    """
    import asyncio

    def _drive() -> Any:
        return asyncio.run(handler(args, None))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _drive()

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(_drive).result()


def _simulate_restricted_plan(
    plan: list[str], tool_permissions: Any, tool_registry: Any
) -> tuple[str, str]:
    """Really execute ``plan`` under mocks; return ``(verdict, detail)``.

    Each step is gated by class (fail-closed) and then executed by invoking the
    mock handler. Any failure (missing/unbound mock, raised exception, non-dict
    or unsuccessful result) is reported as ``error`` — never a vacuous ``pass``.
    """
    if not plan:
        return ("error", "受限沙箱无可执行工具计划，无法在无副作用下执行")

    executed = 0
    provider = ""
    for index, tool_id in enumerate(plan):
        tool_class = tool_permissions.classify_tool(tool_id)
        if tool_class not in RESTRICTED_ALLOWED_CLASSES:
            return (
                "error",
                f"沙箱不允许 {tool_class} 工具 '{tool_id}'，已按越权拦截",
            )
        binding = tool_registry.resolve(tool_id)
        if binding is None or binding.kind != "development":
            return (
                "error",
                f"受限沙箱缺少 '{tool_id}' 的 mock 绑定，无法在无副作用下执行",
            )
        args = {"tool": tool_id, "step": index}
        try:
            result = _invoke_mock_handler_sync(binding.handler, args)
        except Exception as exc:  # noqa: BLE001 — any mock failure is fail-closed
            return (
                "error",
                f"受限沙箱执行 '{tool_id}' 失败：{type(exc).__name__}: {exc}",
            )
        if not isinstance(result, dict) or result.get("ok") is not True:
            return (
                "error",
                f"受限沙箱执行 '{tool_id}' 未返回成功结果（ok={result!r}）",
            )
        executed += 1
        provider = str(getattr(binding, "provider", ""))

    return (
        "pass",
        f"受限沙箱以 mock 真执行 {executed} 步（provider={provider}）",
    )


def run_tests(
    contract: SkillContract,
    cases: list[SkillTestCase],
    *,
    sandbox: str = SandboxMode.DECLARATIVE,
) -> list[SkillTestRun]:
    """Execute ``cases`` against ``contract``.

    Args:
        contract: the contract under test.
        cases: the generated cases.
        sandbox: :data:`SandboxMode.DECLARATIVE` (default) runs the original
            offline predicate sandbox — its output is **byte-for-byte** the
            pre-INC46 behaviour. :data:`SandboxMode.RESTRICTED` simulates the
            contract's tool plan under mock bindings and gates each step against
            the four-level privilege model (over-privilege ⇒ ``error``).

    Returns:
        One :class:`SkillTestRun` per case. A run's verdict is ``pass`` /
        ``fail`` from the predicate (declarative) or the plan simulation
        (restricted), or ``error`` when the sandbox cannot decide or a step is
        over-privileged. ``error`` is **never** reported as ``pass``.
    """
    if _normalize_sandbox(sandbox) == SandboxMode.RESTRICTED:
        return _run_tests_restricted(contract, cases)

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
