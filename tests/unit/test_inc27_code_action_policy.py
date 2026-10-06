"""INC27 — the code-action policy layer: the platform decides, OpenHands executes.

These tests pin the three-way taxonomy the platform must apply to a proposed
action (and which is the whole reason the control plane exists):

* **lossless format problem** -> *normalize*, then execute;
* **factual error** (a hallucinated ``old_str``) -> *reject* + structured
  observation, the agent retries;
* **boundary / permission violation** -> *hard reject*, never auto-corrected.

The runner is loaded by path (it must never be imported by ``forgeflow`` and it
needs no SDK at module scope), so these run in the offline profile. The actions
under test are plain pydantic models with the same fields as the SDK's
``FileEditorAction`` — the rules are duck-typed on purpose.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
from pydantic import BaseModel

_RUNNER_PATH = (
    Path(__file__).resolve().parents[2]
    / "forgeflow"
    / "codeplane"
    / "runner"
    / "run_code_task.py"
)


def _load_runner():
    spec = importlib.util.spec_from_file_location("ff_runner_inc27_policy", _RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner():
    return _load_runner()


class _Action(BaseModel):
    """Stand-in for the SDK's ``FileEditorAction`` (same fields, no SDK)."""

    command: str = "view"
    path: str = ""
    old_str: str | None = None
    new_str: str | None = None


class _RecordingExecutor:
    """Records what it was handed, so a test can prove what the platform let through."""

    def __init__(self, result: str = "EXECUTED") -> None:
        self.calls: list = []
        self.result = result

    def __call__(self, action, conversation=None):
        self.calls.append(action)
        return self.result


# ---------------------------------------------------------------------------
# the rule table
# ---------------------------------------------------------------------------


def test_policy_is_a_rule_table_with_a_stable_order(runner):
    """The rules are data, not ad-hoc ``if``s — and order is the evaluation order."""
    rules = runner._build_policy_rules("D:/work")
    assert [rule.name for rule in rules] == [
        "path_format",
        "workspace_boundary",
        "file_exists",
        "old_str_match",
    ]
    assert all(rule.name and isinstance(rule.kind, str) for rule in rules)


# ---------------------------------------------------------------------------
# class 1 — lossless format problem: normalize
# ---------------------------------------------------------------------------


def test_windows_drive_path_is_normalized_losslessly(runner, monkeypatch):
    """/D:/work/a.py -> D:/work/a.py. Same file, so the platform fixes it."""
    monkeypatch.setattr(os, "name", "nt")
    rule = runner._build_policy_rules("D:/work")[0]
    decision = rule.evaluate(_Action(command="view", path="/D:/work/a.py"))
    assert decision.verdict == runner._POLICY_NORMALIZE
    assert decision.action.path == "D:/work/a.py"
    assert decision.action.command == "view", "normalization must not touch other fields"


def test_a_path_that_needs_no_fixing_is_left_alone(runner, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    rule = runner._build_policy_rules("D:/work")[0]
    for path in ("D:/work/a.py", r"D:\work\a.py"):
        assert rule.evaluate(_Action(command="view", path=path)).verdict == runner._POLICY_ALLOW


def test_posix_paths_are_not_rewritten(runner, monkeypatch):
    """On POSIX a leading slash is correct — normalizing there would be corruption."""
    monkeypatch.setattr(os, "name", "posix")
    rule = runner._build_policy_rules("/work")[0]
    assert rule.evaluate(_Action(command="view", path="/work/a.py")).verdict == runner._POLICY_ALLOW


def test_a_drive_less_absolute_path_is_read_as_workspace_relative(runner, monkeypatch, tmp_path):
    """INC28 W2 — the model writes ``/billing/__init__.py`` (leading slash, NO
    drive). That is the real output shape seen in the raw run, and today it is
    passed through unchanged, so the SDK rejects it ("should be an absolute
    path") and the agent retries forever. It must be read as workspace-root
    relative and normalized to an absolute path *inside* the workspace."""
    monkeypatch.setattr(os, "name", "nt")
    ws = str(tmp_path)
    rule = runner._build_policy_rules(ws)[0]
    decision = rule.evaluate(_Action(command="view", path="/billing/__init__.py"))
    assert decision.verdict == runner._POLICY_NORMALIZE
    assert decision.action.path == os.path.join(ws, "billing", "__init__.py")
    assert decision.action.command == "view", "normalization must not touch other fields"
    # Safety floor: the corrected path is now inside the workspace ⇒ boundary allows
    # it. (A genuine escape is still stopped — see test_traversal_is_rejected...)
    boundary = runner._WorkspaceBoundaryRule(ws)
    assert boundary.evaluate(decision.action).verdict == runner._POLICY_ALLOW


def test_workspace_relative_rule_needs_a_root_and_never_touches_unc(runner, monkeypatch, tmp_path):
    """The second parameter is optional (pre-INC28 callers keep their behaviour),
    and a UNC path is already host-absolute so it is never re-anchored."""
    monkeypatch.setattr(os, "name", "nt")
    assert runner._normalize_host_path("/billing/x.py") == "/billing/x.py"
    assert runner._normalize_host_path("/billing/x.py", str(tmp_path)) == os.path.join(
        str(tmp_path), "billing", "x.py"
    )
    assert runner._normalize_host_path("//host/share/x.py", str(tmp_path)) == "//host/share/x.py"
    # A real drive path is unaffected by the new branch.
    assert runner._normalize_host_path("D:/work/a.py", str(tmp_path)) == "D:/work/a.py"


# ---------------------------------------------------------------------------
# class 3 — boundary violation: hard reject, never auto-corrected
# ---------------------------------------------------------------------------


def test_a_path_escaping_the_workspace_is_a_security_reject(runner, tmp_path):
    outside = Path(os.path.sep) / "Windows" / "System32" / "hosts"
    rule = runner._WorkspaceBoundaryRule(str(tmp_path))
    decision = rule.evaluate(_Action(command="view", path=str(outside)))
    assert decision.verdict == runner._POLICY_REJECT_SECURITY
    assert "workspace" in decision.message


def test_a_path_inside_the_workspace_is_allowed(runner, tmp_path):
    inside = tmp_path / "calc.py"
    inside.write_text("x = 1\n", encoding="utf-8")
    rule = runner._WorkspaceBoundaryRule(str(tmp_path))
    assert rule.evaluate(_Action(command="view", path=str(inside))).verdict == runner._POLICY_ALLOW


#: The tests that exercise the **Windows** spelling table simulate that host with
#: ``os.name``, so their inputs must be Windows-shaped too. Handing them
#: ``tmp_path`` instead silently switches them onto a different branch: on a Linux
#: runner ``tmp_path`` is POSIX-absolute and drive-less, so it reads as the
#: "model spelled it workspace-relative" repair class and a traversal looks like a
#: format problem rather than the security violation it is.
_SIM_WINDOWS_ROOT = "D:/work"


def test_traversal_is_rejected_not_normalized(runner, monkeypatch):
    """Format fixing must never become a way around the boundary check.

    The host is simulated explicitly (``os.name``) *and* addressed in Windows
    path shapes — see :data:`_SIM_WINDOWS_ROOT`.
    """
    monkeypatch.setattr(os, "name", "nt")
    rules = runner._build_policy_rules(_SIM_WINDOWS_ROOT)
    escaped = _SIM_WINDOWS_ROOT + "/../secret.py"
    decisions = [rule.evaluate(_Action(command="view", path=escaped)) for rule in rules]
    verdicts = [d.verdict for d in decisions]
    assert runner._POLICY_REJECT_SECURITY in verdicts
    assert runner._POLICY_NORMALIZE not in verdicts, "a boundary breach is never 'fixed'"


# ---------------------------------------------------------------------------
# class 2 — factual error: reject + retryable observation
# ---------------------------------------------------------------------------


def test_editing_a_file_that_is_not_there_is_rejected_with_a_next_action(runner, tmp_path):
    rule = runner._FileExistsRule(str(tmp_path))
    decision = rule.evaluate(_Action(command="str_replace", path=str(tmp_path / "nope.py")))
    assert decision.verdict == runner._POLICY_REJECT_RETRYABLE
    assert "Required next action" in decision.message


def test_create_over_an_existing_file_is_rejected(runner, tmp_path):
    existing = tmp_path / "calc.py"
    existing.write_text("x = 1\n", encoding="utf-8")
    rule = runner._FileExistsRule(str(tmp_path))
    decision = rule.evaluate(_Action(command="create", path=str(existing)))
    assert decision.verdict == runner._POLICY_REJECT_RETRYABLE
    assert "str_replace" in decision.message


def test_hallucinated_old_str_is_rejected_and_the_file_is_untouched(runner, tmp_path):
    """The core of it: an invented ``old_str`` never reaches the file."""
    target = tmp_path / "calc.py"
    original = "def add(a, b):\n    return a - b\n"
    target.write_text(original, encoding="utf-8")
    rule = runner._OldStrMatchRule(str(tmp_path))
    decision = rule.evaluate(
        _Action(
            command="str_replace",
            path=str(target),
            old_str="    def add(self, a, b):\n        return a - b",
            new_str="    def add(self, a, b):\n        return a + b",
        )
    )
    assert decision.verdict == runner._POLICY_REJECT_RETRYABLE
    assert "old_str was not found" in decision.message
    assert "Required next action" in decision.message
    assert target.read_text(encoding="utf-8") == original, "the file must be byte-identical"


def test_old_str_is_checked_against_the_real_bytes(runner, tmp_path):
    target = tmp_path / "calc.py"
    target.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    rule = runner._OldStrMatchRule(str(tmp_path))
    assert rule.evaluate(
        _Action(command="str_replace", path=str(target), old_str="    return a - b")
    ).verdict == runner._POLICY_ALLOW


def test_an_ambiguous_old_str_is_rejected(runner, tmp_path):
    target = tmp_path / "dup.py"
    target.write_text("x = 1\nx = 1\n", encoding="utf-8")
    rule = runner._OldStrMatchRule(str(tmp_path))
    decision = rule.evaluate(_Action(command="str_replace", path=str(target), old_str="x = 1"))
    assert decision.verdict == runner._POLICY_REJECT_RETRYABLE
    assert "2 locations" in decision.message


def test_old_str_only_applies_to_str_replace(runner, tmp_path):
    rule = runner._OldStrMatchRule(str(tmp_path))
    assert rule.evaluate(_Action(command="view", path="/nowhere.py")).verdict == runner._POLICY_ALLOW


# ---------------------------------------------------------------------------
# the guarded executor
# ---------------------------------------------------------------------------


def test_guard_normalizes_then_executes(runner, monkeypatch, tmp_path):
    """The guard repairs the spelling, then hands the **corrected** path onward.

    The mis-spelling exercised is a stray leading slash on a workspace-relative
    path (``/calc.py``). It is deliberately not ``"/" + tmp_path + "/calc.py"``:
    that expression only represents "a stray slash in front of an absolute path"
    on a host whose absolute paths carry a drive. On POSIX it yields ``//tmp/...``
    — a *different* path, since POSIX leaves a leading ``//`` implementation-
    defined and ``//x`` is also how UNC is spelled, so the spelling rule
    deliberately refuses to guess there. The ``"/<drive>:/..."`` repair itself is
    pinned platform-invariantly by
    :func:`test_windows_drive_path_is_normalized_losslessly`.
    """
    monkeypatch.setattr(os, "name", "nt")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    inner = _RecordingExecutor()
    guard = runner._PolicyGuardedExecutor(inner, runner._build_policy_rules(str(tmp_path)))
    result = guard(_Action(command="view", path="/calc.py"))
    assert result == "EXECUTED"
    assert len(inner.calls) == 1
    assert inner.calls[0].path == os.path.join(str(tmp_path), "calc.py")


def test_guard_rejects_without_calling_the_executor(runner, tmp_path):
    target = tmp_path / "calc.py"
    target.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    inner = _RecordingExecutor()
    seen: list[str] = []
    guard = runner._PolicyGuardedExecutor(
        inner,
        runner._build_policy_rules(str(tmp_path)),
        observation_builder=lambda action, text: seen.append(text) or "OBSERVED",
    )
    result = guard(
        _Action(command="str_replace", path=str(target), old_str="    return a + b", new_str="x")
    )
    assert result == "OBSERVED"
    assert inner.calls == [], "a rejected action must never reach the executor"
    assert "ACTION_REJECTED" in seen[0]
    assert "old_str_match (retryable)" in seen[0]


def test_a_rejection_without_a_builder_raises(runner, tmp_path):
    # INC28 W2 — a drive-less leading-slash path is now read as *workspace
    # relative* (``/etc/passwd`` -> ``<tmp_path>/etc/passwd``), so to exercise a
    # **security** reject this nail uses a genuinely out-of-workspace absolute
    # path. The assertion (rejection raises with the security kind) is unchanged.
    outside = tmp_path.parent / "definitely_outside.py"
    guard = runner._PolicyGuardedExecutor(
        _RecordingExecutor(), runner._build_policy_rules(str(tmp_path))
    )
    with pytest.raises(runner.PolicyRejection) as excinfo:
        guard(_Action(command="view", path=str(outside)))
    assert excinfo.value.kind == runner._SECURITY_KIND


def test_policy_events_are_emitted(runner, tmp_path, monkeypatch):
    """The timeline must show what the platform did — normalize and reject alike.

    ``os.name`` is pinned so this exercises the same branch on every host. The
    drive-less repair is Windows-only **by design**: on POSIX a leading slash is
    correct and rewriting it would be corruption (see
    :func:`test_posix_paths_are_not_rewritten`). Left unpinned the normalize step
    simply does not happen on a Linux runner, so the test would be asserting a
    step the platform deliberately declines to take there.
    """
    monkeypatch.setattr(os, "name", "nt")
    events: list[dict] = []

    class _Rec:
        def emit(self, phase, kind, status, label, *, tool=None, detail="", data=None, latency_ms=None):
            events.append({"kind": kind, "status": status, "detail": detail})

    guard = runner._PolicyGuardedExecutor(
        _RecordingExecutor(),
        runner._build_policy_rules(str(tmp_path)),
        emitter=_Rec(),
        observation_builder=lambda action, text: "OBSERVED",
    )
    guard(_Action(command="view", path="/etc/passwd"))
    assert events, "a policy decision must be visible in the run's timeline"
    assert all(e["kind"] == "policy" for e in events)
    # INC28 W2 — this action produces BOTH kinds of visible decision: the
    # drive-less path is normalized, then the (now in-workspace, but absent) file
    # is rejected as retryable. Assert both are on the timeline.
    assert any(e["status"] == "ok" for e in events), "the normalize step must be visible"
    assert any(e["status"] == "error" for e in events), "the reject step must be visible"


def test_the_three_error_classes_are_distinct(runner):
    assert len({
        runner._POLICY_NORMALIZE,
        runner._POLICY_REJECT_RETRYABLE,
        runner._POLICY_REJECT_SECURITY,
    }) == 3


# ---------------------------------------------------------------------------
# the SDK's own path sentence is corrected for this host
# ---------------------------------------------------------------------------


def test_the_sdk_path_sentence_is_replaced_on_windows(runner, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    text = "base\n" + runner._SDK_BAD_PATH_LINE + "\ntail"
    fixed, replaced = runner._host_path_line_fix(text)
    assert replaced is True
    assert "starting with /" not in fixed, "the sentence that makes models emit /D:/... is gone"
    assert "drive letter" in fixed


def test_a_moved_sdk_sentence_is_appended_not_silently_skipped(runner, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    fixed, replaced = runner._host_path_line_fix("brand new wording")
    assert replaced is False
    assert "drive letter" in fixed, "the guidance must still reach the model"


def test_the_sdk_wording_is_left_alone_on_posix(runner, monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    text = "base\n" + runner._SDK_BAD_PATH_LINE + "\ntail"
    fixed, replaced = runner._host_path_line_fix(text)
    assert fixed == text and replaced is True
