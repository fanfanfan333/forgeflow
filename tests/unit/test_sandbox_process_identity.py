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
