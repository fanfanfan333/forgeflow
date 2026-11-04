"""Generic structured-output repair (Pydantic AI output-repair pattern).

Extracted from the supervisor (Phase 9 B10): when a model's
structured output fails to parse, feed the validation error back and
let the model correct its own output — exactly once. The caller owns
the fallback after both attempts fail (the supervisor's deterministic
route; any other caller, its own fallback). Repair is generic: the
caller supplies the repair-message builder, this module owns the
attempt counting and nothing else.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import HumanMessage


async def invoke_with_repair(
    invoke: Callable[[list], Awaitable[Any]],
    prompt: list,
    *,
    repair_message: Callable[[Exception], str],
) -> tuple[Any, bool]:
    """First attempt + at most one repair retry.

    Returns ``(result, repaired)`` where ``repaired`` is True only
    when the first attempt failed and the repair succeeded. Raises
    the *repair* exception when both attempts fail, so the caller's
    fallback logs the failure that actually ended the sequence.
    """
    try:
        return await invoke(prompt), False
    except Exception as first_exc:  # noqa: BLE001 — repair is the contract, not a catch-all
        repaired = await invoke([*prompt, HumanMessage(content=repair_message(first_exc))])
        return repaired, True
