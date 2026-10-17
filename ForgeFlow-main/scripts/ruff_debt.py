"""New-debt ratchet gate for Ruff lint findings — ``scripts/ruff_debt.py``.

ForgeFlow carries a large, **pre-existing** Ruff *lint debt*. The whole-repository
scan (``ruff check .``) is therefore RED and is treated as *information, never a
gate* — see the Makefile header. What *is* a gate is that the debt must never
**grow**: ``make lint-ratchet`` runs this script, which counts the findings inside
the official debt scope (``forgeflow/ dashboard/ tests/``) restricted to
**tracked** files and fails if that count rose above the total frozen in
``docs/quality/ruff-baseline.json``.

Why "tracked only"?
    A raw directory scan also sees throwaway scratch files (``qa_*.py`` at the
    repo root, ``tests/qa_independent/*``) that are deliberately *not* part of
    the repository. Those must never be able to trip — or mask — the ratchet, so
    the scope is intersected with ``git ls-files``. If ``git`` cannot be run the
    script **fails loudly** (non-zero) instead of silently counting zero.

Why gate on the *total* and not on individual findings?
    Debt is paid down file by file across many increments. A ratchet on the
    total lets any single rule or file improve *or* regress as long as the sum
    does not grow; the per-rule / per-area breakdown is kept in the baseline and
    printed by ``--report`` for humans, but is never enforced. When the total
    *does* grow, the per-entry "what changed" report compares ``entries`` as a
    multiset (Counter), so a new duplicate of an already-known anchor is still
    located (see ``_multiset_added``).

Usage::

    python scripts/ruff_debt.py                        # check: fail if debt grew
    python scripts/ruff_debt.py --report               # print total / by_rule / by_area
    python scripts/ruff_debt.py --update               # lower the baseline (normal)
    python scripts/ruff_debt.py --update --force --reason "..."   # raise it (warned)

The script always runs from the application root (the parent of ``scripts/``) so
it never depends on the caller's working directory, and it invokes Ruff as
``<this interpreter> -m ruff`` so a stray ``ruff`` on ``PATH`` cannot change the
verdict.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

#: The official debt scope (spec §一): these three trees, tracked files only.
SCOPE: tuple[str, ...] = ("forgeflow", "dashboard", "tests")

#: Machine-readable baseline (spec D2).
BASELINE_PATH = Path("docs/quality/ruff-baseline.json")

#: The date the ratchet was introduced. Frozen on first write; never changes.
BASELINE_DATE = "2026-10-04"

#: Baseline file schema version.
SCHEMA_VERSION = 1

#: Application root = the parent of this ``scripts/`` directory.
_APP_ROOT = Path(__file__).resolve().parent.parent


class RatchetError(RuntimeError):
    """Raised when the ratchet cannot reach a trustworthy verdict."""


def _ruff_findings(scope: list[str]) -> list[dict[str, Any]]:
    """Return Ruff's JSON diagnostics for ``scope`` (ordered as Ruff reports them).

    Args:
        scope: Paths to scan (relative to the application root).

    Returns:
        The parsed ``--output-format json`` payload.

    Raises:
        RatchetError: If Ruff is missing or exits with an internal error (>1).
    """
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", *scope, "--output-format", "json"],
        cwd=str(_APP_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode > 1:
        raise RatchetError(f"ruff check failed (exit {proc.returncode}): {proc.stderr.strip()}")
    if not proc.stdout.strip():
        return []
    parsed = json.loads(proc.stdout)
    if not isinstance(parsed, list):
        raise RatchetError("unexpected ruff JSON payload (not a list)")
    return parsed


def _ruff_version() -> str:
    """Return the Ruff version string, e.g. ``"0.16.10"``."""
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "--version"],
        cwd=str(_APP_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.stdout.strip().replace("ruff ", "").strip()


def _tracked_paths() -> set[str]:
    """Return the set of git-tracked paths, relative to the application root.

    Returns:
        Tracked paths with forward slashes (e.g. ``"forgeflow/agent/budget.py"``).

    Raises:
        RatchetError: If ``git`` is unavailable or ``git ls-files`` fails.
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=str(_APP_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError as exc:
        raise RatchetError(
            "git is not available on PATH — cannot restrict the scope to tracked files"
        ) from exc
    if proc.returncode != 0:
        raise RatchetError(f"git ls-files failed (exit {proc.returncode}): {proc.stderr.strip()}")
    return {line.strip().replace("\\", "/") for line in proc.stdout.splitlines() if line.strip()}


def _normalize(filename: str) -> str:
    """Normalize a Ruff filename to an application-root-relative POSIX path."""
    path = filename.replace("\\", "/")
    root = str(_APP_ROOT).replace("\\", "/")
    if path.startswith(root):
        path = path[len(root) :].lstrip("/")
    return path


def _area(path: str) -> str:
    """Return the reporting area for ``path`` (e.g. ``"forgeflow/agent"``)."""
    parts = path.split("/")
    if len(parts) >= 2 and parts[0] in SCOPE:
        return f"{parts[0]}/{parts[1]}"
    return parts[0] if parts else path


def _entry(path: str, row: int, code: str) -> str:
    """Render a single finding as a stable ``file:line:code`` anchor."""
    return f"{path}:{row}:{code}"


def _snapshot(scope: list[str]) -> tuple[dict[str, Any], list[str]]:
    """Run a full scan and reduce it to a baseline-shaped snapshot.

    Args:
        scope: The debt scope to scan (defaults to :data:`SCOPE`).

    Returns:
        A ``(snapshot, all_entries)`` pair. ``snapshot`` holds the tracked-only
        ``total`` / ``by_rule`` / ``by_area`` / ``entries`` plus the raw and
        whole-repo reference totals; ``all_entries`` is the tracked
        ``file:line:code`` list used for the "what changed" report.

    Raises:
        RatchetError: If ``git`` is unavailable or Ruff errors out.
    """
    tracked = _tracked_paths()
    if not tracked:
        raise RatchetError("git ls-files returned no files — refusing to report a count of 0")

    scope_findings = _ruff_findings(scope)
    whole_findings = _ruff_findings(["."])

    tracked_rows: list[tuple[str, int, str]] = []
    for finding in scope_findings:
        path = _normalize(str(finding["filename"]))
        if path in tracked:
            tracked_rows.append((path, int(finding["location"]["row"]), str(finding["code"])))

    tracked_rows.sort()
    entries = [_entry(path, row, code) for path, row, code in tracked_rows]

    snapshot: dict[str, Any] = {
        "scope": list(scope),
        "scope_mode": "tracked-only",
        "total": len(tracked_rows),
        "raw_command_total": len(scope_findings),
        "whole_repo_raw_total": len(whole_findings),
        "by_rule": dict(sorted(Counter(code for _, _, code in tracked_rows).items())),
        "by_area": dict(sorted(Counter(_area(path) for path, _, _ in tracked_rows).items())),
        "entries": entries,
    }
    return snapshot, entries


def _now_iso() -> str:
    """Return the current local time as an ISO-8601 string with offset."""
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _read_baseline() -> dict[str, Any]:
    """Load the baseline JSON.

    Raises:
        RatchetError: If the baseline is missing or unreadable.
    """
    path = _APP_ROOT / BASELINE_PATH
    if not path.is_file():
        raise RatchetError(f"baseline not found: {path} (create it with --update)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RatchetError(f"cannot read baseline {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RatchetError(f"baseline {path} is not a JSON object")
    return data


def _multiset_added(
    baseline_entries: list[str], current_entries: list[str]
) -> list[tuple[str, int]]:
    """Return findings that occur *more often* now than in the baseline.

    Multiset (``Counter``) semantics, not set semantics. A finding is anchored by
    ``file:line:code``, and the same anchor can legitimately appear several times
    (the same rule on the same line reported for several statements — the frozen
    baseline really does contain duplicates). The former set-based diff
    (``[e for e in entries if e not in baseline_entries]``) silently dropped a
    *new duplicate* of an already-known anchor, so growth degraded into the
    unhelpful "no per-entry diff available" message. Counting occurrences
    locates the growth precisely.

    Args:
        baseline_entries: The ``entries`` list frozen in the baseline.
        current_entries: The ``entries`` of the current scan.

    Returns:
        Sorted ``(entry, extra_count)`` pairs; ``extra_count`` is how many more
        occurrences the current scan has than the baseline.
    """
    baseline_counts = Counter(baseline_entries)
    current_counts = Counter(current_entries)
    return sorted(
        (entry, count - baseline_counts.get(entry, 0))
        for entry, count in current_counts.items()
        if count - baseline_counts.get(entry, 0) > 0
    )


def cmd_check() -> int:
    """Gate: fail when the tracked debt total rose above the baseline."""
    baseline = _read_baseline()
    baseline_total = int(baseline.get("total", 0))
    baseline_entries = list(baseline.get("entries", []) or [])

    snapshot, entries = _snapshot(list(SCOPE))
    current = int(snapshot["total"])

    print(
        f"[RATCHET] tracked-file lint debt: current={current} baseline={baseline_total} "
        f"(scope: {' '.join(SCOPE)})"
    )
    if current <= baseline_total:
        headroom = baseline_total - current
        print(f"[RATCHET] OK — debt did not grow (headroom {headroom}).")
        return 0

    added = _multiset_added(baseline_entries, entries)
    print(f"[RATCHET] FAIL — lint debt grew by {current - baseline_total}.")
    if added:
        print("[RATCHET] new findings:")
        for entry, extra in added:
            suffix = f" (x{extra})" if extra > 1 else ""
            print(f"[RATCHET]   + {entry}{suffix}")
    else:
        print("[RATCHET] (no per-entry diff available — the baseline has no 'entries' list)")
    print("[RATCHET] pay the debt down, or fix the offending change — do not raise the baseline.")
    return 1


def cmd_report() -> int:
    """Print the current total and the by-rule / by-area breakdown. Never gates."""
    snapshot, _ = _snapshot(list(SCOPE))
    print(f"[RATCHET] scope: {' '.join(SCOPE)} (mode: {snapshot['scope_mode']})")
    print(f"[RATCHET] total (tracked): {snapshot['total']}")
    print(f"[RATCHET] raw scan total: {snapshot['raw_command_total']}")
    print(f"[RATCHET] whole-repo raw total: {snapshot['whole_repo_raw_total']}")
    print("[RATCHET] by_rule:")
    for rule, count in snapshot["by_rule"].items():
        print(f"[RATCHET]   {rule:8s} {count}")
    print("[RATCHET] by_area:")
    for area, count in snapshot["by_area"].items():
        print(f"[RATCHET]   {area:28s} {count}")
    return 0


def cmd_update(force: bool, reason: str | None) -> int:
    """Rewrite the baseline, refusing to *raise* it unless ``--force --reason``.

    Args:
        force: Allow raising the baseline above its previous total.
        reason: Mandatory justification when ``force`` is set.

    Returns:
        ``0`` on success; ``1`` when an upward move was refused.
    """
    snapshot, _ = _snapshot(list(SCOPE))
    new_total = int(snapshot["total"])

    old_total: int | None = None
    old_date = BASELINE_DATE
    try:
        previous = _read_baseline()
    except RatchetError:
        previous = None
    if previous is not None:
        old_total = int(previous.get("total", new_total))
        old_date = str(previous.get("baseline_date", BASELINE_DATE))

    # The INITIAL anchors (spec §三) are frozen once and preserved forever: a
    # reader must be able to tell the original debt (484/504/515) from the live,
    # post-cleanup numbers. Only a brand-new baseline derives them from a scan.
    initial_totals: dict[str, Any]
    if previous is not None and isinstance(previous.get("initial_totals"), dict):
        initial_totals = dict(previous["initial_totals"])
    else:
        initial_totals = {
            "tracked": new_total,
            "raw_command": int(snapshot["raw_command_total"]),
            "whole_repo_raw": int(snapshot["whole_repo_raw_total"]),
            "measured_at": _now_iso(),
        }

    if old_total is not None and new_total > old_total:
        if not force:
            print(
                f"[RATCHET] REFUSED — new total {new_total} > baseline {old_total}. "
                "The baseline may only go down."
            )
            print('[RATCHET] (override deliberately with --update --force --reason "...")')
            return 1
        if not reason:
            print('[RATCHET] REFUSED — --force requires --reason "...".')
            return 1
        print(
            f"[RATCHET] WARNING: raising the baseline {old_total} -> {new_total} "
            f"(force-reason: {reason})"
        )
    elif old_total is not None and new_total < old_total:
        print(f"[RATCHET] lowering the baseline {old_total} -> {new_total}.")

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "baseline_date": old_date,
        "measured_at": _now_iso(),
        "ruff_version": _ruff_version(),
        "python_version": sys.version.split()[0],
        "scope": list(SCOPE),
        "scope_mode": "tracked-only",
        "total": new_total,
        "raw_command_total": int(snapshot["raw_command_total"]),
        "whole_repo_raw_total": int(snapshot["whole_repo_raw_total"]),
        "by_rule": snapshot["by_rule"],
        "by_area": snapshot["by_area"],
        "entries": snapshot["entries"],
        "initial_totals": initial_totals,
        "notes": (
            "Ruff lint-debt baseline. 'total' counts findings in the official scope "
            "(forgeflow/ dashboard/ tests/) restricted to git-tracked files — it is the "
            "ratchet threshold and may only decrease. 'raw_command_total' is the raw "
            "`ruff check forgeflow dashboard tests` count; 'whole_repo_raw_total' is the "
            "raw `ruff check .` count; both are informational. See "
            "docs/quality/ruff-baseline-2026-10-04.md and docs/quality/ruff-debt.md. "
            "'total' / 'raw_command_total' / 'whole_repo_raw_total' are LIVE post-cleanup "
            "measurements (regenerated by --update). The INITIAL baseline anchors "
            "(484 tracked / 504 raw / 515 whole-repo) are frozen in 'initial_totals' and "
            "in docs/quality/ruff-debt.md."
        ),
    }
    out = _APP_ROOT / BASELINE_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[RATCHET] wrote {out} (total={new_total})")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to check / report / update."""
    parser = argparse.ArgumentParser(
        prog="python scripts/ruff_debt.py",
        description="Ruff lint-debt ratchet: fail when the tracked debt total grows.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="check",
        choices=["check", "report", "update"],
        help="what to do (default: check)",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="print total / by_rule / by_area and exit 0 (never gates)",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="rewrite the baseline from a fresh scan (only ever lowers it, unless --force)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --update, allow raising the baseline (requires --reason); warns loudly",
    )
    parser.add_argument(
        "--reason",
        default=None,
        help="justification required together with --update --force",
    )
    args = parser.parse_args(argv)

    os.chdir(_APP_ROOT)
    try:
        if args.update or args.command == "update":
            return cmd_update(args.force, args.reason)
        if args.report or args.command == "report":
            return cmd_report()
        return cmd_check()
    except RatchetError as exc:
        print(f"[RATCHET] ERROR: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
