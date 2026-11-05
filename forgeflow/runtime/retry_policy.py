"""Unified step policy for the ReAct core loop: retry / timeout /
cancellation, structured error observations, dead-loop guards, caps.

Design notes (ideas borrowed, code original to this codebase):

* LangGraph's ``RetryPolicy`` shape: a step is retried only when the
  error is classified retryable, with exponential backoff + jitter
  between attempts and a bounded attempt count.
* Anthropic's "Building Effective Agents": keep the loop simple and
  let explicit, observable safeguards (caps, stop reasons) do the
  work — every guard here records *why* it fired on the run meta.
* This module is the single place the react loop resolves those
  knobs. ``ToolExecutor``'s built-in transient retry for read-only
  tools already uses the same defaults (3 attempts total, 0.5s base
  backoff, 0.25s jitter — see ``tool_executor._TRANSIENT_*``); the
  loop-level policy governs model calls and the loop's own tool
  wrapper, and never stacks a second retry on top of an outcome the
  executor already retried.

Resolution follows the codebase's existing pattern (module constants
+ ``task.context`` overrides, fail-closed like
``_resolve_max_iterations``): an override counts only when it has the
right type and is in range; anything else falls back to the default,
so a stray value can never disable a safeguard.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_DEAD_LOOP_ERROR_LIMIT",
    "DEFAULT_DEAD_LOOP_IDENTICAL_LIMIT",
    "DEFAULT_INITIAL_BACKOFF_SECONDS",
    "DEFAULT_JITTER_SECONDS",
    "DEFAULT_LOOP_TOKEN_BUDGET",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_BACKOFF_SECONDS",
    "DEFAULT_MODEL_TIMEOUT_SECONDS",
    "DEFAULT_TOOL_FEEDBACK_MAX_CHARS",
    "TRUNCATION_MARKER",
    "LoopGuardTracker",
    "StepPolicy",
    "backoff_delay",
    "classify_error",
    "classify_error_text",
    "is_write_class_tool",
    "resolve_step_policy",
    "structured_error_observation",
    "truncate_feedback",
    "write_retry_allowed",
]

# --- defaults ---------------------------------------------------------------- #

#: Total attempts per step (first try + retries). Mirrors
#: ``tool_executor``'s transient-retry budget (1 + _TRANSIENT_MAX_RETRIES=2).
DEFAULT_MAX_ATTEMPTS: int = 3
DEFAULT_INITIAL_BACKOFF_SECONDS: float = 0.5
DEFAULT_MAX_BACKOFF_SECONDS: float = 8.0
DEFAULT_JITTER_SECONDS: float = 0.25
#: Per-model-call wall clock. The whole run is already bounded by the
#: execution budget (600s); a single call gets half of that by default
#: so one hung provider cannot eat the entire run.
DEFAULT_MODEL_TIMEOUT_SECONDS: float = 300.0
#: Consecutive identical tool calls that mark a dead loop.
DEFAULT_DEAD_LOOP_IDENTICAL_LIMIT: int = 3
#: Consecutive tool errors tolerated before the loop stops. The
#: default sits above the stuck-pattern detector's same-error
#: salvage (warn at 3 identical errors, force a final answer on the
#: next turn): that pre-existing path gets first chance to close the
#: run gracefully, and this cap is the backstop for error streaks the
#: pattern detector cannot see (mixed tools / mixed messages).
DEFAULT_DEAD_LOOP_ERROR_LIMIT: int = 5
#: Total model tokens (input + output, summed over the run's usage
#: entries) one react attempt chain may spend before the loop stops.
DEFAULT_LOOP_TOKEN_BUDGET: int = 250_000
#: Model-visible tool feedback cap (chars). Records keep full payloads.
DEFAULT_TOOL_FEEDBACK_MAX_CHARS: int = 16_000

#: Marker that replaces the tail of over-long tool feedback.
TRUNCATION_MARKER = "[输出已截断"

#: HTTP statuses that are worth retrying (throttling / transient 5xx).
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
#: Provider exception class names that are transient by contract.
_RETRYABLE_TYPE_NAMES = frozenset(
    {
        "RateLimitError",
        "APITimeoutError",
        "APIConnectionError",
        "InternalServerError",
        "ServiceUnavailableError",
        "OverloadedError",
        "TransientError",
    }
)
#: Types that are never retryable: the call itself is malformed or
#: forbidden; retrying cannot change the answer. Checked first, so a
#: validation message that happens to mention "connection" is not
#: misread as transient.
_NON_RETRYABLE_TYPES = (PermissionError, TypeError, ValueError, KeyError, AttributeError)
_RETRYABLE_TEXT_TOKENS = (
    "timeout",
    "timed out",
    "超时",
    "rate limit",
    "rate_limit",
    "429",
    "temporar",
    "connection",
    "connect error",
    "overloaded",
    "502",
    "503",
    "504",
    "service unavailable",
)


# --- policy value object ----------------------------------------------------- #


@dataclass(frozen=True)
class StepPolicy:
    """Resolved knobs for one run's react loop (immutable)."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    initial_backoff_seconds: float = DEFAULT_INITIAL_BACKOFF_SECONDS
    max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS
    jitter_seconds: float = DEFAULT_JITTER_SECONDS
    model_timeout_seconds: float | None = DEFAULT_MODEL_TIMEOUT_SECONDS
    #: Loop-level per-tool-step timeout; ``None`` defers to
    #: ToolExecutor's own per-tool wall clock (settings-based).
    tool_timeout_seconds: float | None = None
    dead_loop_identical_limit: int = DEFAULT_DEAD_LOOP_IDENTICAL_LIMIT
    dead_loop_error_limit: int = DEFAULT_DEAD_LOOP_ERROR_LIMIT
    loop_token_budget: int = DEFAULT_LOOP_TOKEN_BUDGET
    tool_feedback_max_chars: int = DEFAULT_TOOL_FEEDBACK_MAX_CHARS

    def summary(self) -> dict[str, Any]:
        """JSON-safe view recorded on the run meta for observability."""
        return {
            "max_attempts": self.max_attempts,
            "initial_backoff_seconds": self.initial_backoff_seconds,
            "max_backoff_seconds": self.max_backoff_seconds,
            "jitter_seconds": self.jitter_seconds,
            "model_timeout_seconds": self.model_timeout_seconds,
            "tool_timeout_seconds": self.tool_timeout_seconds,
            "dead_loop_identical_limit": self.dead_loop_identical_limit,
            "dead_loop_error_limit": self.dead_loop_error_limit,
            "loop_token_budget": self.loop_token_budget,
            "tool_feedback_max_chars": self.tool_feedback_max_chars,
        }


def _int_override(context: dict, key: str, default: int, *, lo: int, hi: int) -> int:
    raw = context.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return default
    return raw if lo <= raw <= hi else default


def _float_override(
    context: dict, key: str, default: float | None, *, lo: float, hi: float
) -> float | None:
    raw = context.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return default
    value = float(raw)
    return value if lo < value <= hi else default


def resolve_step_policy(task: Any) -> StepPolicy:
    """Resolve the effective :class:`StepPolicy` for ``task``.

    ``task.context`` overrides (fail-closed, mirroring
    ``_resolve_max_iterations``): ``retry_max_attempts``,
    ``retry_initial_backoff_seconds``, ``retry_max_backoff_seconds``,
    ``retry_jitter_seconds``, ``model_timeout_seconds``,
    ``tool_step_timeout_seconds``, ``dead_loop_identical_limit``,
    ``dead_loop_error_limit``, ``loop_token_budget``,
    ``tool_feedback_max_chars``. ``model_timeout_seconds: 0`` is
    invalid (ignored), never a way to disable the timeout.
    """
    context = getattr(task, "context", None) or {}
    if not isinstance(context, dict):
        context = {}
    return StepPolicy(
        max_attempts=_int_override(context, "retry_max_attempts", DEFAULT_MAX_ATTEMPTS, lo=1, hi=5),
        initial_backoff_seconds=_float_override(
            context,
            "retry_initial_backoff_seconds",
            DEFAULT_INITIAL_BACKOFF_SECONDS,
            lo=-1.0,
            hi=60.0,
        ),
        max_backoff_seconds=_float_override(
            context, "retry_max_backoff_seconds", DEFAULT_MAX_BACKOFF_SECONDS, lo=-1.0, hi=120.0
        ),
        jitter_seconds=_float_override(
            context, "retry_jitter_seconds", DEFAULT_JITTER_SECONDS, lo=-1.0, hi=10.0
        ),
        model_timeout_seconds=_float_override(
            context, "model_timeout_seconds", DEFAULT_MODEL_TIMEOUT_SECONDS, lo=0.0, hi=1800.0
        ),
        tool_timeout_seconds=_float_override(
            context, "tool_step_timeout_seconds", None, lo=0.0, hi=1800.0
        ),
        dead_loop_identical_limit=_int_override(
            context, "dead_loop_identical_limit", DEFAULT_DEAD_LOOP_IDENTICAL_LIMIT, lo=1, hi=100
        ),
        dead_loop_error_limit=_int_override(
            context, "dead_loop_error_limit", DEFAULT_DEAD_LOOP_ERROR_LIMIT, lo=1, hi=100
        ),
        loop_token_budget=_int_override(
            context, "loop_token_budget", DEFAULT_LOOP_TOKEN_BUDGET, lo=1, hi=100_000_000
        ),
        tool_feedback_max_chars=_int_override(
            context,
            "tool_feedback_max_chars",
            DEFAULT_TOOL_FEEDBACK_MAX_CHARS,
            lo=200,
            hi=1_000_000,
        ),
    )


# --- error classification ------------------------------------------------------ #


def _status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None) if response is not None else None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def classify_error_text(text: str) -> tuple[str, bool]:
    """Classify an error *message* (tool outcomes carry text, not types)."""
    lowered = (text or "").lower()
    if any(token in lowered for token in ("timeout", "timed out", "超时")):
        return "timeout", True
    if "429" in lowered or "rate limit" in lowered or "rate_limit" in lowered:
        return "rate_limit", True
    if any(token in lowered for token in ("502", "503", "504")):
        return "server_error", True
    if "connection" in lowered or "connect error" in lowered or "temporar" in lowered:
        return "connection", True
    if "overloaded" in lowered or "service unavailable" in lowered:
        return "server_error", True
    return "tool_error", False


def classify_error(exc: BaseException) -> tuple[str, bool]:
    """Classify an exception as ``(kind, retryable)``.

    Order matters: malformed/forbidden calls are non-retryable even
    when their message mentions a transient word; structured signals
    (status codes, provider exception types, builtin timeout /
    connection types) beat message tokens, which are the last resort.
    ``asyncio.CancelledError`` is never classified here — callers must
    let it propagate (cancellation is not a retryable failure).
    """
    if isinstance(exc, _NON_RETRYABLE_TYPES):
        return type(exc).__name__, False
    if isinstance(exc, TimeoutError):
        return "timeout", True
    if isinstance(exc, (ConnectionError, BrokenPipeError)):
        return "connection", True
    status = _status_code(exc)
    if status is not None:
        if status in _RETRYABLE_STATUS:
            return ("rate_limit" if status == 429 else "server_error"), True
        return f"http_{status}", False
    if type(exc).__name__ in _RETRYABLE_TYPE_NAMES:
        return type(exc).__name__, True
    if isinstance(exc, OSError):
        return "connection", True
    kind, retryable = classify_error_text(str(exc))
    if retryable:
        return kind, True
    return type(exc).__name__, False


def backoff_delay(policy: StepPolicy, retry_index: int) -> float:
    """Delay before retry ``retry_index`` (0 = first retry).

    Exponential (``initial * 2**retry_index``), capped at
    ``max_backoff_seconds``, plus uniform jitter in
    ``[0, jitter_seconds]`` — the returned value always lies in
    ``[base, base + jitter]`` for the capped ``base``.
    """
    base = max(
        0.0,
        min(
            policy.max_backoff_seconds,
            policy.initial_backoff_seconds * (2 ** max(0, retry_index)),
        ),
    )
    return base + random.uniform(0, policy.jitter_seconds)


# --- write-tool retry gate ----------------------------------------------------- #


def is_write_class_tool(tool: str) -> bool:
    """Conservative write classification for retry gating.

    Anything not on ToolExecutor's read-only whitelist (exact,
    individually verified tool ids) is treated as write-class: when
    in doubt, a tool may have side effects, so it does not get
    automatic retries.
    """
    from forgeflow.runtime.tool_executor import _is_read_only_tool

    return not _is_read_only_tool(tool)


def write_retry_allowed(
    tool: str,
    *,
    idempotency_key: str | None = None,
    idempotency_persisted: bool = False,
    tool_idempotent: bool = False,
) -> bool:
    """Whether a failed call to ``tool`` may be retried at all.

    Read-only tools are always retry-eligible (subject to error
    classification). A write-class tool may be retried **only** when
    ALL of the following hold:

    * the call carries an idempotency key that has actually been
      persisted — a key that exists only in memory cannot dedupe a
      replay after a crash, so it does not open the gate;
    * the tool's binding explicitly declares it implements
      idempotency with that key (``ToolBinding.idempotent``) — the
      handler reads the key and skips re-applying a write it already
      applied under it. A persisted key alone is NOT enough: most
      handlers ignore the key entirely, so retrying them would
      re-execute the side effect once per attempt.

    MCP tools (``mcp.*``) are never write-retry-eligible: the key is
    deliberately never sent to the server (its argument schema would
    reject it), so no MCP handler can dedupe a replay — declared or
    not, the gate stays shut.
    """
    if not is_write_class_tool(tool):
        return True
    if tool.startswith("mcp."):
        return False
    return bool(idempotency_key) and bool(idempotency_persisted) and bool(tool_idempotent)


# --- structured error observations --------------------------------------------- #


def structured_error_observation(
    tool: str,
    *,
    status: str,
    error_kind: str,
    message: str,
    retryable: bool,
    attempts: int,
    timeout: bool = False,
) -> dict[str, Any]:
    """The model-visible shape of a failed tool step.

    The run record keeps the invocation's own payload verbatim; this
    observation is what the *model* is told, so it always carries the
    tool name, the error kind, the message, whether a retry could
    help, and how many attempts were spent.
    """
    return {
        "ok": False,
        "tool": tool,
        "status": status,
        "error": {
            "kind": error_kind,
            "message": message,
            "retryable": retryable,
        },
        "attempts": attempts,
        "timeout": timeout,
    }


# --- dead-loop tracking ---------------------------------------------------------- #


class LoopGuardTracker:
    """Consecutive-call accounting for dead-loop detection.

    Tracks (a) consecutive *executed* calls with an identical
    (tool, args) signature and (b) consecutive error outcomes. Any
    different call resets (a); any non-error outcome resets (b).
    Pure bookkeeping — the loop decides what the counts mean.
    """

    def __init__(self) -> None:
        self._last_signature: tuple[str, str] | None = None
        self.identical_count: int = 0
        self.error_count: int = 0

    def note_executed(self, signature: tuple[str, str]) -> int:
        """Record one executed call; returns its identical streak."""
        if signature == self._last_signature:
            self.identical_count += 1
        else:
            self._last_signature = signature
            self.identical_count = 1
        return self.identical_count

    def note_outcome(self, status: str) -> int:
        """Record one tool outcome; returns the error streak.

        Only genuine execution failures count (``error`` /
        ``unavailable``). Gate outcomes (``refused`` / ``blocked`` /
        ``awaiting_approval``) and ``outcome_unknown`` are not model
        flailing — they are governed by their own fail-closed paths.
        """
        if status in {"error", "unavailable"}:
            self.error_count += 1
        else:
            self.error_count = 0
        return self.error_count


# --- feedback truncation --------------------------------------------------------- #


def truncate_feedback(text: str, max_chars: int) -> tuple[str, bool]:
    """Truncate over-long model-visible feedback with an explicit marker.

    Returns ``(text, truncated)``. Only the model-visible copy is cut;
    stored records keep the full payload. The marker states the
    original length so the model knows it is looking at a prefix.

    ``max_chars`` caps the TOTAL length of the returned string,
    marker included — the shown prefix is shortened by the marker's
    own length (whose digit count depends on the prefix length, so
    the fit is solved iteratively; the shown count stated in the
    marker is the actual number of characters shown). A cap too small
    for the full marker degrades to a minimal marker, and to a
    hard-cut marker below that: the cap always wins.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    original = len(text)

    def _marker(shown: int) -> str:
        return (
            f"\n{TRUNCATION_MARKER}：原长 {original} 字符，仅显示前 {shown} 字符"
            "（完整结果已保留在运行记录中） / output truncated]"
        )

    shown = max_chars
    marker = _marker(shown)
    for _ in range(8):
        if shown + len(marker) <= max_chars:
            break
        shown -= shown + len(marker) - max_chars
        if shown < 0:
            break
        marker = _marker(shown)
    if shown >= 0 and shown + len(marker) <= max_chars:
        return text[:shown] + marker, True
    # Degenerate cap: even the full marker does not fit. A minimal
    # marker still names the original length; below that, hard-cut.
    minimal = f"{TRUNCATION_MARKER}：原长 {original} 字符 / output truncated]"
    if len(minimal) <= max_chars:
        return text[: max_chars - len(minimal)] + minimal, True
    return minimal[:max_chars], True
