"""Phase 9 B10 — generic structured-output repair (one retry, then raise)."""

from __future__ import annotations

import pytest

from forgeflow.runtime.structured_repair import invoke_with_repair


@pytest.mark.asyncio
async def test_first_attempt_success_needs_no_repair() -> None:
    calls: list = []

    async def invoke(prompt):
        calls.append(len(prompt))
        return "ok"

    result, repaired = await invoke_with_repair(invoke, ["p0"], repair_message=lambda e: "fix")
    assert (result, repaired) == ("ok", False)
    assert calls == [1]


@pytest.mark.asyncio
async def test_repair_retry_feeds_error_back_and_reports_repaired() -> None:
    calls: list = []

    async def invoke(prompt):
        calls.append([getattr(m, "content", m) for m in prompt])
        if len(calls) == 1:
            raise ValueError("bad json")
        return "fixed"

    result, repaired = await invoke_with_repair(
        invoke, ["p0"], repair_message=lambda e: f"unparsable: {e}"
    )
    assert (result, repaired) == ("fixed", True)
    assert calls[1] == ["p0", "unparsable: bad json"]


@pytest.mark.asyncio
async def test_both_attempts_fail_raises_the_repair_error() -> None:
    async def invoke(prompt):
        raise ValueError("still bad" if len(prompt) > 1 else "bad json")

    with pytest.raises(ValueError, match="still bad"):
        await invoke_with_repair(invoke, ["p0"], repair_message=lambda e: "fix")
