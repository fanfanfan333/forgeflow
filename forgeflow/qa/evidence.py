"""Immutable, per-run evidence storage (layer L1, §42/§43/§44).

Every QA run owns one directory, ``<project_root>/qa_tmp/qa/<run_id>/``, and this
store is the only writer into it. The two invariants that make the evidence
*non-forgeable*:

* **A run directory is created once and never reused.** :meth:`for_run` refuses
  to open a directory that already exists, so a re-run must use a fresh
  ``run_id`` — history is append-only, never overwritten (§43/§92).
* **A written file is never replaced.** Every write lands in a hidden
  ``mkstemp`` temp file and is then published with an *exclusive* atomic create
  (an ``os.link`` hard link; an ``O_CREAT | O_EXCL`` open wherever hard links
  are unavailable). A target that already exists is never clobbered: the write
  raises :class:`FileExistsError` and the incumbent file is left byte-for-byte
  intact, with no window in which a racing writer could be silently overwritten.

Layout under the run directory::

    <run_dir>/capability_inventory.json  test_plan.json  execution_summary.json
    <run_dir>/discovery_report.json      failure_analysis.json  final_report.md
    <run_dir>/evidence/<test_id>.json
    <run_dir>/artifacts/<test_id>.stdout.txt  artifacts/<test_id>.stderr.txt
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from forgeflow.qa.config import QaRunConfig
from forgeflow.qa.models import Evidence

logger = logging.getLogger(__name__)

_SAFE_ID_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"


def safe_id(value: str) -> str:
    """Map an arbitrary test id to a filesystem-safe filename stem."""
    cleaned = "".join(ch if ch in _SAFE_ID_CHARS else "_" for ch in value)
    return cleaned or "unnamed"


def _json_default(obj: Any) -> Any:
    """``json.dumps`` fallback for the model/enum/path types QA writes."""
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"object of type {type(obj).__name__} is not JSON serializable")


class EvidenceStore:
    """Owns one run directory and enforces the no-overwrite contract."""

    def __init__(self, run_dir: Path) -> None:
        self._run_dir = Path(run_dir)

    @property
    def run_dir(self) -> Path:
        """The absolute run directory this store writes into."""
        return self._run_dir

    @property
    def evidence_dir(self) -> Path:
        """Directory holding the per-test ``evidence/*.json`` files."""
        return self._run_dir / "evidence"

    @property
    def artifacts_dir(self) -> Path:
        """Directory holding raw ``stdout``/``stderr`` artifacts."""
        return self._run_dir / "artifacts"

    @classmethod
    def for_run(cls, config: QaRunConfig) -> EvidenceStore:
        """Create the run directory for ``config``; refuse to reuse an existing one."""
        run_dir = Path(config.artifacts_dir)
        if run_dir.exists():
            raise RuntimeError(
                f"run directory already exists — a run_id is unique and evidence is "
                f"immutable, refusing to reuse it: {run_dir}"
            )
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "evidence").mkdir()
        (run_dir / "artifacts").mkdir()
        logger.info("evidence run directory created: %s", run_dir)
        return cls(run_dir)

    # ── write helpers ────────────────────────────────────────────────────
    def _resolve_safe(self, rel_path: str) -> Path:
        """Resolve ``rel_path`` under the run dir, rejecting any escape."""
        candidate = (self._run_dir / rel_path).resolve()
        base = self._run_dir.resolve()
        if candidate != base and base not in candidate.parents:
            raise ValueError(f"refusing to write outside the run directory: {rel_path!r}")
        return candidate

    def _atomic_write(self, rel_path: str, data: bytes) -> Path:
        """Atomically create ``rel_path``; raise if it already exists.

        The bytes are first written to a hidden temp file (so no partially
        written content is ever visible under the target name) and then
        published with an *exclusive* atomic create —
        :func:`_commit_exclusive` links the temp file into place, which fails
        with :class:`FileExistsError` rather than overwriting an existing
        target. There is therefore no TOCTOU window between an ``exists()``
        check and the commit.
        """
        target = self._resolve_safe(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            _commit_exclusive(tmp_name, target)
        except FileExistsError as exc:
            _silent_unlink(tmp_name)
            raise FileExistsError(
                f"refusing to overwrite existing evidence artifact: {target}"
            ) from exc
        except BaseException:
            _silent_unlink(tmp_name)
            raise
        _silent_unlink(tmp_name)
        return target

    def write_json(self, name: str, payload: Any) -> Path:
        """Write ``payload`` as pretty JSON (fails if ``name`` already exists)."""
        text = json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default)
        return self._atomic_write(name, text.encode("utf-8"))

    def write_text(self, name: str, text: str) -> Path:
        """Write ``text`` (UTF-8) to ``name`` (fails if it already exists)."""
        return self._atomic_write(name, text.encode("utf-8"))

    def write_bytes(self, rel_path: str, data: bytes) -> Path:
        """Write raw ``data`` to ``rel_path`` (fails if it already exists)."""
        return self._atomic_write(rel_path, data)

    # ── evidence-specific helpers ────────────────────────────────────────
    def save_evidence(self, ev: Evidence) -> Path:
        """Persist one :class:`Evidence` to ``evidence/<test_id>.json``."""
        return self.write_json(f"evidence/{safe_id(ev.test_id)}.json", ev)

    def list_evidence(self) -> list[Evidence]:
        """Load every persisted evidence record, ordered by filename."""
        if not self.evidence_dir.is_dir():
            return []
        records: list[Evidence] = []
        for path in sorted(self.evidence_dir.glob("*.json")):
            records.append(Evidence.model_validate_json(path.read_text(encoding="utf-8")))
        return records


def _commit_exclusive(tmp_name: str, target: Path) -> None:
    """Publish ``tmp_name``'s content at ``target`` with an exclusive create.

    ``os.link`` is an atomic *create-if-absent*: it succeeds only when
    ``target`` does not exist yet and raises :class:`FileExistsError` otherwise,
    which is exactly the "a written file is never replaced" contract — there is
    no check-then-write window for a racing writer to slip through.

    On filesystems that cannot hard-link, the resulting ``OSError`` (anything
    other than :class:`FileExistsError`) falls back to an ``O_CREAT | O_EXCL``
    open, which is likewise atomic and likewise refuses to clobber an existing
    target. If that open also finds the target present, its
    :class:`FileExistsError` propagates to the caller unchanged.
    """
    try:
        os.link(tmp_name, str(target))
    except FileExistsError:
        raise
    except OSError:
        fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "wb") as handle:
            handle.write(Path(tmp_name).read_bytes())


def _silent_unlink(path: str) -> None:
    """Best-effort removal of a temp file; never raises."""
    try:
        os.unlink(path)
    except OSError:  # pragma: no cover - the temp file is already gone
        pass


__all__ = ["EvidenceStore", "safe_id"]
