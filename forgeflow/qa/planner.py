"""Test planner — ``capability_inventory + Make targets → test_plan.json`` (§7/§56/§57).

Layer L3. The planner is a *pure module*: it reads the discovered capability
inventory and the Makefile's real targets, and emits a deterministic list of
:class:`TestPlanItem`. It never talks to an LLM or the network, so the plan is
reproducible offline.

The profile mapping follows §57/§9:

* **fast** → the static + unit + format + typecheck gate (``check``).
* **full** → ``check`` plus the integration suite (``test-integration``).

Every item's ``task_target`` is validated against
:meth:`TaskRunnerAdapter.has_target` at plan time; a target that does not exist
is recorded as ``INCONCLUSIVE`` and never executed (§15). The
``QA_MAKE_TARGETS`` environment override lets the end-to-end test reduce the
profile to a cheap real target without touching production semantics (see
:func:`forgeflow.qa.config.profile_targets`).
"""

from __future__ import annotations

import logging
from pathlib import Path

from forgeflow.qa.config import DEFAULT_TIMEOUT_S, Profile, profile_targets
from forgeflow.qa.models import (
    CapabilityInventory,
    Layer,
    Risk,
    TestPlanItem,
    TestStatus,
    TestType,
)
from forgeflow.qa.task_runner import TaskRunnerAdapter

logger = logging.getLogger(__name__)

#: Semantic test type for each known Make target (a label, not behaviour).
_TEST_TYPE_BY_TARGET: dict[str, TestType] = {
    "check": TestType.STATIC,
    "lint": TestType.STATIC,
    "lint-gate": TestType.STATIC,
    "lint-ratchet": TestType.STATIC,
    "format-check": TestType.STATIC,
    "typecheck": TestType.STATIC,
    "typecheck-gate": TestType.STATIC,
    "test-unit": TestType.UNIT,
    "test-fast": TestType.UNIT,
    "test": TestType.UNIT,
    "test-integration": TestType.INTEGRATION,
    "test-all": TestType.INTEGRATION,
    "test-document-delivery": TestType.INTEGRATION,
    "cov": TestType.UNIT,
}

#: Priority (§56 subset) and risk (§18) per target; conservative defaults apply.
_PRIORITY_BY_TARGET: dict[str, str] = {
    "check": "P0",
    "lint": "P0",
    "test-unit": "P0",
    "test-integration": "P1",
}
_RISK_BY_TARGET: dict[str, Risk] = {
    "check": Risk.HIGH,
    "test": Risk.HIGH,
    "test-unit": Risk.HIGH,
    "test-integration": Risk.MEDIUM,
}

#: Extra service requirements per target (drive environment-failure attribution).
_REQUIRES_BY_TARGET: dict[str, list[str]] = {
    "test-integration": [],
    "test": [],
}


class TestPlanner:
    """Builds a deterministic :class:`TestPlanItem` list for a profile."""

    __test__ = False  # not a pytest test class despite the ``Test`` name

    def __init__(self, root: Path, tasks: TaskRunnerAdapter) -> None:
        self._root = Path(root)
        self._tasks = tasks

    def plan(
        self,
        inventory: CapabilityInventory,
        *,
        profile: Profile,
    ) -> list[TestPlanItem]:
        """Return the plan for ``profile``; every executable target is real."""
        del inventory  # the inventory is context; the plan is driven by targets
        targets = profile_targets(profile)
        items: list[TestPlanItem] = []
        for target in targets:
            exists = self._tasks.has_target(target)
            items.append(
                TestPlanItem(
                    test_id=f"make:{target}",
                    capability_id=f"BUILD:{target}",
                    layer=Layer.BUILD,
                    test_type=_TEST_TYPE_BY_TARGET.get(target, TestType.STATIC),
                    description=f"Run `make {target}` ({profile.value} profile)",
                    priority=_PRIORITY_BY_TARGET.get(target, "P2"),  # type: ignore[arg-type]
                    risk=_RISK_BY_TARGET.get(target, Risk.LOW),
                    adapter="make",
                    task_target=target,
                    command=self._tasks.command_for(target) if exists else [],
                    cwd=str(self._root),
                    timeout_s=DEFAULT_TIMEOUT_S,
                    requires=list(_REQUIRES_BY_TARGET.get(target, [])),
                    status=TestStatus.PENDING if exists else TestStatus.INCONCLUSIVE,
                )
            )
            if not exists:
                logger.warning(
                    "planner: Make target %r does not exist — recording INCONCLUSIVE",
                    target,
                )
        return items


__all__ = ["TestPlanner"]
