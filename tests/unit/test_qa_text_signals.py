"""Core + boundary tests for :mod:`forgeflow.qa.text_signals`.

``text_signals`` is the **single fact source** for the "did the transcript say
nothing ran?" heuristic, shared by :mod:`forgeflow.qa.executor` and
:mod:`forgeflow.qa.analyzer`. These tests pin its contract directly so the two
callers can never drift apart again:

* a line that *is* the notice (bare / timed / ``=``-banner wrapped) matches;
* a phrase merely *embedded* in a longer line (e.g. a passing parametrised id or
  prose) must **not** match;
* blank / whitespace-only / unrelated lines never match.

The test bodies are deliberately loop/table driven (no parametrised ids) so the
literal phrases are not echoed into this suite's own verbose output — leaking
them is exactly what the original drift exploited.
"""

from __future__ import annotations

from forgeflow.qa.text_signals import (
    COLLECTION_NOTICE_PHRASES,
    NOTHING_RAN_PHRASES,
    line_is_notice,
    output_has_notice,
)

# ── fixtures (module-level literals; never used as pytest ids) ───────────────

_LINES_THAT_ARE_NOTICES = (
    "no tests ran",
    "no tests ran in 0.01s",
    "====== no tests ran in 0.01s ======",
    "============ no tests ran in 0.01s ============",
    "   no tests ran in 0.01s   ",
    "collected 0 items",
    "==== collected 0 items ====",
    "collected 0 items / 1 error",
)

_LINES_THAT_ARE_NOT_NOTICES = (
    "",
    "   ",
    "\t",
    "=",
    "=====",
    "120 passed",
    "1 passed in 0.10s",
    "3574 passed, 27 skipped, 1 xfailed, 416 warnings in 223.85s (0:03:43)",
    # an unrelated PASSING parametrised id echoing the phrase mid-line:
    "tests/unit/test_qa_plan_exec_edges.py::test_executor_status_matrix"
    "[ok-0-no tests ran in 0.01s-INCONCLUSIVE] PASSED [ 73%]",
    # the same, for the other phrase:
    "tests/unit/test_qa_plan_exec_edges.py::test_executor_status_matrix"
    "[ok-0-collected 0 items-INCONCLUSIVE] PASSED [ 73%]",
    # prose that merely mentions the words:
    "WARNING: description text that says collected 0 items in passing",
    "see docs: a run that reports no tests ran should be INCONCLUSIVE",
)


# ── line_is_notice ───────────────────────────────────────────────────────────


def test_line_is_notice_accepts_bare_banner_and_timed_forms() -> None:
    for line in _LINES_THAT_ARE_NOTICES:
        assert line_is_notice(line, NOTHING_RAN_PHRASES) is True, repr(line)


def test_line_is_notice_accepts_collection_only_phrasings() -> None:
    assert line_is_notice("errors during collection", COLLECTION_NOTICE_PHRASES) is True
    assert line_is_notice("error during collection", COLLECTION_NOTICE_PHRASES) is True
    assert line_is_notice("===== errors during collection =====", COLLECTION_NOTICE_PHRASES) is True


def test_line_is_notice_rejects_embedded_blank_and_unrelated() -> None:
    for line in _LINES_THAT_ARE_NOT_NOTICES:
        assert line_is_notice(line, NOTHING_RAN_PHRASES) is False, repr(line)


def test_line_is_notice_embedded_only_is_false_for_collection_set_too() -> None:
    embedded = (
        "tests/unit/test_qa_plan_exec_edges.py::test_executor_status_matrix"
        "[ok-0-collected 0 items-INCONCLUSIVE] PASSED [ 73%]"
    )
    assert line_is_notice(embedded, COLLECTION_NOTICE_PHRASES) is False


def test_line_is_notice_prefix_match_covers_suffixed_variant() -> None:
    # "collected 0 items / 1 error" is a *prefix* match of "collected 0 items".
    assert line_is_notice("collected 0 items / 1 error", NOTHING_RAN_PHRASES) is True


# ── phrase constants ─────────────────────────────────────────────────────────


def test_collection_phrase_set_is_a_superset_of_nothing_ran() -> None:
    for phrase in NOTHING_RAN_PHRASES:
        assert phrase in COLLECTION_NOTICE_PHRASES


def test_phrase_constants_are_lowercase_prefixes() -> None:
    for phrase in COLLECTION_NOTICE_PHRASES:
        assert phrase == phrase.lower()
        # a notice must be a *prefix*, so it may not start/end with padding
        assert phrase == phrase.strip()


# ── output_has_notice ────────────────────────────────────────────────────────


def test_output_has_notice_true_when_any_line_is_a_notice() -> None:
    output = "===== test session starts =====\nall good\ncollected 0 items\nmore text\n"
    assert output_has_notice(output, NOTHING_RAN_PHRASES) is True


def test_output_has_notice_false_when_no_line_is_a_notice() -> None:
    output = "===== 3574 passed, 27 skipped in 223.85s =====\nall good\nstill fine\n"
    assert output_has_notice(output, NOTHING_RAN_PHRASES) is False


def test_output_has_notice_false_for_empty_and_whitespace_only() -> None:
    assert output_has_notice("", NOTHING_RAN_PHRASES) is False
    assert output_has_notice("\n   \n\t\n", NOTHING_RAN_PHRASES) is False


def test_output_has_notice_ignores_embedded_occurrences_across_lines() -> None:
    output = (
        "tests/unit/test_qa_plan_exec_edges.py::test_executor_status_matrix"
        "[ok-0-no tests ran in 0.01s-INCONCLUSIVE] PASSED [ 73%]\n"
        "tests/unit/test_qa_plan_exec_edges.py::test_executor_status_matrix"
        "[ok-0-collected 0 items-INCONCLUSIVE] PASSED [ 73%]\n"
        "===== 1 failed, 3573 passed in 200.00s =====\n"
    )
    assert output_has_notice(output, NOTHING_RAN_PHRASES) is False
    assert output_has_notice(output, COLLECTION_NOTICE_PHRASES) is False
