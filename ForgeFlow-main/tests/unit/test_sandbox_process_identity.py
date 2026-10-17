import os

import pytest

import forgeflow.skills.sandbox_isolated as sandbox


def test_reused_parent_pid_does_not_import_older_unrelated_process(monkeypatch):
    monkeypatch.setattr(sandbox, "_process_children_map", lambda: {10: [11, 12], 12: [13]})
    times = {10: 100, 11: 50, 12: 120, 13: 140}
    monkeypatch.setattr(sandbox, "_pid_creation_ticks", lambda pid: times[pid])
    assert sandbox.descendant_pids(10) == [10, 12, 13]
    # Without identity evidence the quota guard stays conservative, rather than
    # hiding an unmeasured process to make the budget appear satisfied.
    monkeypatch.setattr(sandbox, "_pid_creation_ticks", lambda pid: 0)
    assert 11 in sandbox.descendant_pids(10)


@pytest.mark.skipif(not sandbox._IS_WINDOWS, reason="Windows process identity API")
def test_native_process_creation_is_actually_measured():
    assert sandbox._pid_creation_ticks(os.getpid()) > 0


def test_exited_root_retains_identity_for_descendant_filter(monkeypatch):
    monkeypatch.setattr(sandbox, "_process_children_map", lambda: {10: [11, 12]})
    monkeypatch.setattr(sandbox, "_pid_creation_ticks", lambda pid: {10: 0, 11: 50, 12: 120}[pid])
    assert sandbox.descendant_pids(10, root_created=100) == [10, 12]

@pytest.mark.skipif(not sandbox._IS_WINDOWS, reason="Windows suspended child identity")
def test_supervisor_captures_identity_before_child_can_exit(monkeypatch):
    original_init = sandbox._Supervisor.__init__
    original_resume = sandbox._resume_main_thread
    measured = []
    resumed = False

    def capture(self, *args, **kwargs):
        assert not resumed
        original_init(self, *args, **kwargs)
        measured.append(self.root_created)

    def resume(pid):
        nonlocal resumed
        resumed = True
        return original_resume(pid)

    monkeypatch.setattr(sandbox._Supervisor, "__init__", capture)
    monkeypatch.setattr(sandbox, "_resume_main_thread", resume)
    result = sandbox.run_isolated("print('done')")
    assert measured and measured[0] > 0
    assert result.verdict == "pass", result.to_dict()
