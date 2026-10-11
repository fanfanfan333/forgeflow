"""Edge/boundary/exception tests for :mod:`forgeflow.qa.evidence`.

Covers the *unhappy* and *boundary* surfaces the happy-path suite omits:

* ``safe_id`` boundary inputs (empty, unicode, separators, dots);
* symlink-escape rejection through ``_resolve_safe``;
* temp-file cleanup when the atomic commit fails;
* ``list_evidence`` on a missing / empty directory;
* ``_json_default`` for ``Path`` / ``Enum`` / ``BaseModel`` and its ``TypeError``;
* the concurrency invariant "same path, two writers ⇒ exactly one wins".

The last one is a plain regression test: the store now publishes each artifact
with an *exclusive* atomic create (``os.link``, or ``O_CREAT | O_EXCL`` where
hard links are unavailable), so a racing writer wins outright and the loser gets
``FileExistsError`` rather than silently clobbering the winner (the old
``exists()`` + ``os.replace`` sequence had a TOCTOU window).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

import pytest

import forgeflow.qa.evidence as evidence_mod
from forgeflow.qa.config import QaRunConfig
from forgeflow.qa.evidence import EvidenceStore, safe_id
from forgeflow.qa.models import Evidence, EvidenceStatus

# ── helpers ──────────────────────────────────────────────────────────────────


def _store(tmp_path: Path, run_id: str = "run-1") -> EvidenceStore:
    cfg = QaRunConfig(
        project_root=tmp_path,
        artifacts_dir=tmp_path / "qa_tmp" / "qa" / run_id,
        run_id=run_id,
    )
    return EvidenceStore.for_run(cfg)


def _evidence(test_id: str = "make:check", run_id: str = "run-1") -> Evidence:
    return Evidence(
        evidence_id=f"{run_id}:{test_id}",
        run_id=run_id,
        capability_id="BUILD:check",
        test_id=test_id,
        command=["make", "PY=x", "check"],
        environment={"cwd": "/x"},
        exit_code=0,
        status=EvidenceStatus.PASS,
        timestamp=datetime.now(UTC).isoformat(),
    )


class _Color(Enum):
    RED = "red"


# ── safe_id boundaries ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", "unnamed"),
        ("make:check", "make_check"),
        ("a/b", "a_b"),
        ("a\\b", "a_b"),
        ("..", ".."),
        ("../evil", ".._evil"),
        ("日本語", "___"),
        ("with space", "with_space"),
        ("keep.dots-and_underscores", "keep.dots-and_underscores"),
    ],
)
def test_safe_id_boundaries(raw: str, expected: str) -> None:
    assert safe_id(raw) == expected


def test_safe_id_never_yields_a_separator() -> None:
    for raw in ["/", "\\", ":/", "a/../b", "..\\..", "C:\\Windows"]:
        cleaned = safe_id(raw)
        assert "/" not in cleaned
        assert "\\" not in cleaned
        assert cleaned  # never collapses to empty


def test_save_evidence_with_traversal_shaped_id_stays_inside(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_evidence(_evidence("../../escape"))
    # The record must live under evidence/ and nowhere near the parent dir.
    assert (store.evidence_dir / ".._.._escape.json").is_file()
    assert not (tmp_path / "escape.json").exists()
    assert len(store.list_evidence()) == 1


# ── _resolve_safe escape rejection ───────────────────────────────────────────


def test_resolve_safe_rejects_parent_traversal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.write_bytes("../escape.txt", b"nope")
    with pytest.raises(ValueError):
        store.write_text("../../escape.md", "nope")


def test_resolve_safe_rejects_absolute_path_escape(tmp_path: Path) -> None:
    store = _store(tmp_path)
    outside = str(tmp_path / "elsewhere" / "pwned.txt")
    with pytest.raises(ValueError):
        store.write_bytes(outside, b"nope")
    assert not (tmp_path / "elsewhere" / "pwned.txt").exists()


def test_resolve_safe_rejects_symlink_escape(tmp_path: Path) -> None:
    store = _store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = store.run_dir / "evidence" / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows w/o privilege
        pytest.skip("symlink creation not permitted on this host")
    if not link.is_symlink():  # pragma: no cover - host silently neuters symlinks
        pytest.skip("host does not create real symlinks (os.symlink is a no-op shim)")
    with pytest.raises(ValueError):
        store.write_bytes("evidence/link/pwned.txt", b"nope")
    assert not (outside / "pwned.txt").exists()


# ── temp cleanup on failure ──────────────────────────────────────────────────


def test_failed_replace_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the atomic commit cannot complete, no temp file is left behind.

    The commit primitive is the exclusive-create step now (``_commit_exclusive``;
    historically a non-exclusive ``os.replace``). We inject a failure there and
    assert the staged temp file is unlinked and the error propagates.
    """
    store = _store(tmp_path)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated atomic commit failure")

    monkeypatch.setattr(evidence_mod, "_commit_exclusive", _boom)
    with pytest.raises(OSError):
        store.write_bytes("artifacts/x.txt", b"data")
    leftovers = list((store.run_dir / "artifacts").glob(".*.tmp"))
    assert leftovers == [], f"temp files not cleaned up: {leftovers}"


# ── list_evidence on missing / empty dir ─────────────────────────────────────


def test_list_evidence_on_missing_directory_is_empty(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "does-not-exist")
    assert store.list_evidence() == []


def test_list_evidence_on_empty_directory_is_empty(tmp_path: Path) -> None:
    assert _store(tmp_path).list_evidence() == []


def test_list_evidence_is_sorted_and_complete(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for tid in ("b", "a", "c"):
        store.save_evidence(_evidence(f"make:{tid}"))
    got = [record.test_id for record in store.list_evidence()]
    assert got == ["make:a", "make:b", "make:c"]


# ── _json_default ────────────────────────────────────────────────────────────


def test_json_default_handles_path_enum_and_model() -> None:
    sample = Path("x") / "y"
    assert json.loads(json.dumps(sample, default=evidence_mod._json_default)) == str(sample)
    assert json.loads(json.dumps(_Color.RED, default=evidence_mod._json_default)) == "red"
    dumped = json.loads(json.dumps(_evidence(), default=evidence_mod._json_default))
    assert dumped["test_id"] == "make:check"


def test_json_default_rejects_unknown_type() -> None:
    with pytest.raises(TypeError):
        json.dumps(object(), default=evidence_mod._json_default)


def test_write_json_serialises_nested_models_and_paths(tmp_path: Path) -> None:
    store = _store(tmp_path)
    path = store.write_json("nested.json", {"p": tmp_path, "ev": _evidence(), "c": _Color.RED})
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["ev"]["test_id"] == "make:check"
    assert payload["c"] == "red"
    assert payload["p"] == str(tmp_path)


# ── run-directory immutability ───────────────────────────────────────────────


def test_for_run_refuses_reuse(tmp_path: Path) -> None:
    _store(tmp_path)
    with pytest.raises(RuntimeError):
        _store(tmp_path)


def test_second_write_to_same_artifact_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write_bytes("artifacts/a.txt", b"one")
    with pytest.raises(FileExistsError):
        store.write_bytes("artifacts/a.txt", b"two")
    assert (store.run_dir / "artifacts" / "a.txt").read_bytes() == b"one"


# ── write atomicity: a racing creator must not be clobbered ──────────────────


def test_double_write_is_atomic_against_a_racing_creator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    target = store.run_dir / "contended.txt"
    real_mkstemp = evidence_mod.tempfile.mkstemp

    def racing_mkstemp(
        *,
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | None = None,
        text: bool = False,
    ) -> tuple[int, str]:
        fd, name = real_mkstemp(suffix=suffix, prefix=prefix, dir=dir, text=text)
        # Deterministically simulate a competing writer that commits AFTER our
        # exists() check but BEFORE our replace — the exact TOCTOU window.
        target.write_bytes(b"WINNER")
        return fd, name

    monkeypatch.setattr(evidence_mod.tempfile, "mkstemp", racing_mkstemp)
    with pytest.raises(FileExistsError):
        store.write_bytes("contended.txt", b"LOSER")
    assert target.read_bytes() == b"WINNER"  # the winner's bytes must survive
