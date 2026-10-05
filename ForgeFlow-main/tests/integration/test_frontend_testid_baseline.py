"""Task B — full-tree ``data-testid`` baseline zero-regression.

The registered-contract pins
(``test_inc43_no_testid_regression.py`` Appendix A, ``test_inc46_testid_regression.py``
T06) freeze only the *named* ids. This module adds a **full-tree** baseline: a
committed snapshot of *every* ``data-testid`` the shared comment-aware scanner
finds under the declared scan domain ``frontend/src/**/*.{ts,tsx}``. ``REMOVED``
must be 0; **adding** ids is allowed (reported, never red).

Why a *committed snapshot* and not ``git show HEAD``: the removed scratch script
``tests/qa_independent/qa_frontend_testid.py`` used ``git show HEAD:<path>`` as
its own baseline, so once a removal is committed HEAD already contains it and
``REMOVED`` is identically 0 — the check self-invalidates. A committed snapshot
is independent of HEAD (and of the working tree being clean).

Baseline regeneration: re-run this same scanner over ``frontend/src`` and
rewrite the JSON's ``testids`` array (sorted) — i.e. call
``test_inc43_no_testid_regression.py::_scan_testids`` over ``FRONTEND_SRC`` and
dump ``sorted(ids)`` into ``tests/data/frontend_testid_baseline.json`` (the
snapshot was produced exactly this way against the current tree).

Scanner reuse: exactly one implementation —
``test_inc43_no_testid_regression.py::_scan_testids`` (loaded by file path, the
same pattern ``test_inc46_testid_regression.py`` uses; the test tree has no
importable package name under rootdir).

Citation discipline: ``file.py::symbol`` anchors, never line numbers.
"""

from __future__ import annotations

import importlib.util
import json
import warnings
from pathlib import Path

# Reuse the INC43 comment-aware scanner verbatim rather than fork a second copy.
_INC43_PATH = Path(__file__).resolve().parent / "test_inc43_no_testid_regression.py"
_spec = importlib.util.spec_from_file_location("_inc43_testid_scanner", _INC43_PATH)
assert _spec is not None and _spec.loader is not None
inc43 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inc43)

_BASELINE_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "frontend_testid_baseline.json"
)


def _baseline() -> dict:
    """Load the committed full-tree snapshot."""
    return json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))


def test_full_testid_baseline_removed_is_zero():
    """REMOVED == 0 over the FULL snapshot; additions are allowed but reported.

    Scan domain: ``frontend/src/**/*.{ts,tsx}`` (asserted against the snapshot so
    a future domain change forces a deliberate re-baseline). Adding ids does not
    fail this test; the additions are surfaced as a warning for review.
    """
    baseline = _baseline()
    assert baseline["scan_domain"] == inc43.SCAN_DOMAIN
    baseline_ids = set(baseline["testids"])
    # Guard against a corrupt/empty snapshot passing vacuously.
    assert len(baseline_ids) >= 200, (
        f"baseline snapshot looks truncated/empty ({len(baseline_ids)} ids)"
    )
    found = inc43._scan_testids(inc43.FRONTEND_SRC)  # noqa: SLF001
    removed = baseline_ids - found
    added = found - baseline_ids
    assert removed == set(), (
        f"data-testid baseline broken (REMOVED={len(removed)}): {sorted(removed)}; "
        f"scan={inc43.SCAN_DOMAIN} baseline={len(baseline_ids)} current={len(found)} "
        f"added={len(added)}"
    )
    if added:
        warnings.warn(
            f"frontend data-testid additions (allowed — REMOVED==0): {sorted(added)}",
            stacklevel=1,
        )


def test_baseline_positive_control_reports_a_renamed_id(tmp_path):
    """Control: the REMOVED computation *can* go red on a removed id.

    A single source file is copied twice — once verbatim, once with one real
    ``data-testid`` attribute renamed. Only the renamed copy grows the removed
    set, so the control proves the check is not vacuous (the scanner genuinely
    reports a removed id).
    """
    baseline_ids = set(_baseline()["testids"])
    target = "conv-followup"
    assert target in baseline_ids, "fixture drift: pinned control id left the baseline"

    src = inc43.FRONTEND_SRC / "views" / "runs" / "FollowUpComposer.tsx"
    raw = src.read_text(encoding="utf-8")
    attr = f'data-testid="{target}"'
    assert attr in inc43._strip_comments(raw)  # noqa: SLF001

    before_dir = tmp_path / "before"
    before_dir.mkdir()
    (before_dir / "FollowUpComposer.tsx").write_text(raw, encoding="utf-8")
    after_dir = tmp_path / "after"
    after_dir.mkdir()
    (after_dir / "FollowUpComposer.tsx").write_text(
        raw.replace(attr, f'data-testid="{target}-renamed"'), encoding="utf-8"
    )

    removed_before = baseline_ids - inc43._scan_testids(before_dir)  # noqa: SLF001
    removed_after = baseline_ids - inc43._scan_testids(after_dir)  # noqa: SLF001
    assert target not in removed_before, "fixture drift: id missing before tamper"
    assert target in removed_after, (
        "scanner failed to report the removed id (this check would not go red)"
    )
    # The rename adds exactly one removed id — a tight, non-vacuous delta.
    assert removed_after - removed_before == {target}
