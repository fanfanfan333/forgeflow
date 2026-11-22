"""BIZ-16 — the recovery refusal should name a real cause, not a blanket rule.

Reported gap: "部分恢复场景尚不支持". Two of those refusals were blanket rather
than factual:

* a run whose inputs include a **directory** was marked unsupported because the
  directory digest was the literal string ``"directory:unverified"``;
* a run with any ``paths`` input was marked unsupported even though every
  regular file input is hashed and re-hashed on resume — drift was already
  refused by evidence.

What stays refused, deliberately: ``inflight`` (a dispatch may have landed — the
whole point of the guard) and code-plane tasks (there is no model checkpoint to
replay). These tests pin both the widening and the guards that must not move.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeflow.runtime import recovery
from forgeflow.runtime.recovery import RecoveryBlocked, RecoveryStore


def _tree_digest(root: Path) -> str:
    # Attribute access (not a from-import) so reverting the module produces a
    # behavioural "capability absent" failure instead of a collection error.
    return recovery.directory_digest(root)


def test_directory_digest_tracks_add_edit_remove(tmp_path):
    root = tmp_path / "inputs"
    root.mkdir()
    (root / "b.txt").write_text("bee", encoding="utf-8")
    (root / "a.txt").write_text("aye", encoding="utf-8")
    base = _tree_digest(root)
    assert base.startswith("tree:2:")

    # Stable across walks and independent of directory-entry order.
    assert _tree_digest(root) == base

    (root / "a.txt").write_text("AYE", encoding="utf-8")
    edited = _tree_digest(root)
    assert edited != base

    (root / "c.txt").write_text("sea", encoding="utf-8")
    added = _tree_digest(root)
    assert added != edited
    assert added.startswith("tree:3:")

    (root / "b.txt").unlink()
    removed = _tree_digest(root)
    assert removed not in {base, edited, added}


def test_directory_digest_does_not_follow_symlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("v1", encoding="utf-8")
    root = tmp_path / "inputs"
    root.mkdir()
    (root / "real.txt").write_text("real", encoding="utf-8")
    link = root / "link.txt"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted in this environment")

    before = _tree_digest(root)
    secret.write_text("v2", encoding="utf-8")
    # The link is neither hashed nor followed: its target lives outside the
    # approved input scope, so it must not be able to change the digest.
    assert _tree_digest(root) == before
    assert before.startswith("tree:1:")


def test_directory_digest_is_bounded_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "MAX_TREE_FILES", 2)
    root = tmp_path / "many"
    root.mkdir()
    for index in range(3):
        (root / f"f{index}.txt").write_text(str(index), encoding="utf-8")
    assert recovery.directory_digest(root) == "unhashed:too-many-files"
    assert recovery.MAX_TREE_FILES >= 1  # the real ceiling is still a sane positive number


def test_input_versions_hashes_a_directory_instead_of_marking_it_unverified(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "one.txt").write_text("1", encoding="utf-8")
    scope = SimpleNamespace(inherited_inputs={"paths": [str(root)]})

    versions = recovery.input_versions(scope)
    assert list(versions) == [str(root.resolve())]
    value = next(iter(versions.values()))
    assert value.startswith("tree:1:")
    assert "unverified" not in value


def test_unsupported_reason_only_fires_for_real_reasons(tmp_path):
    plain = tmp_path / "report.txt"
    plain.write_text("x", encoding="utf-8")
    good = recovery.input_versions(SimpleNamespace(inherited_inputs={"paths": [str(plain)]}))
    # A regular file input is versioned ⇒ resumable, and no longer refused just
    # for being a "path".
    assert recovery.unsupported_reason({"paths": [str(plain)]}, good) == ""

    assert "code-plane" in recovery.unsupported_reason({"repo_path": "/repo"}, {})
    assert "verified" in recovery.unsupported_reason({}, {"d": "unhashed:too-many-files"})


def _store(tmp_path: Path) -> RecoveryStore:
    return RecoveryStore(path=tmp_path / "journal.sqlite3")


def _state(**overrides):
    base = {
        "task": {"intent": "summarise the folder", "title": "t", "workflow_type": "generic"},
        "input_versions": {},
        "budget": {},
        "messages": [{"type": "human", "data": {"content": "go"}}],
        "source_run_id": "",
        "inflight": False,
        "evaluation_only": False,
        "unsupported": "",
    }
    base.update(overrides)
    return base


def test_a_directory_inputs_checkpoint_can_be_claimed(tmp_path):
    store = _store(tmp_path)
    state = _state(input_versions={str(tmp_path): "tree:1:abc"})
    version = store.save("t1", "u1", "run-1", state, status="paused")
    claimed = store.claim("t1", "u1", "run-1", version)
    assert claimed["input_versions"] == {str(tmp_path): "tree:1:abc"}
    assert claimed["inflight"] is False


def test_inflight_checkpoints_stay_refused(tmp_path):
    store = _store(tmp_path)
    version = store.save("t1", "u1", "run-2", _state(inflight=True), status="paused")
    with pytest.raises(RecoveryBlocked, match="待核实"):
        store.claim("t1", "u1", "run-2", version)


def test_a_checkpoint_naming_an_unverified_input_stays_refused(tmp_path):
    store = _store(tmp_path)
    blocked = _state(
        input_versions={str(tmp_path): "unhashed:too-many-files"},
        unsupported="input cannot be verified (unhashed:too-many-files)",
    )
    version = store.save("t1", "u1", "run-3", blocked, status="paused")
    with pytest.raises(RecoveryBlocked, match="可恢复的模型检查点"):
        store.claim("t1", "u1", "run-3", version)
