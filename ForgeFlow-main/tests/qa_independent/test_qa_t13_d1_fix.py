"""QA independent re-verification — INC46 T13 D1 fix (frozen sandbox_isolated.py
sha256 5c2c9c…; docker_isolated.py sha256 4c9111…).

Owned by QA (严过关). Added only. Verifies that the retargeted write-guard really
blocks the previously-escaped vectors, with a HOST-SIDE file-existence check made
from the parent process (never trusting the child's own return value).

Self-attest: v1 2026-10-03 — round-2 re-verification after the D1 fix.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forgeflow.skills.sandbox_isolated import IsolationLimits, run_isolated

ESCAPE_DIR = Path(__file__).resolve().parents[2] / "qa_tmp" / "escape_targets"
ESCAPE_DIR.mkdir(parents=True, exist_ok=True)


def _attempt(body: str, name: str, *, write_guard: bool = True):
    """Run ``body`` (with ``T`` bound to an OUTSIDE path) and check the host.

    Returns ``(result, host_file_exists)`` — ``host_file_exists`` is measured in
    the parent process, independent of the sandbox return value.
    """
    target = ESCAPE_DIR / name
    if target.exists():
        target.unlink()
    src = "import os, io, pathlib\n" f"T = {str(target)!r}\n" + body
    result = run_isolated(src, write_guard=write_guard)
    leaked = target.exists()
    if leaked:
        target.unlink()
    return result, leaked


# --------------------------------------------------------------------------- #
# A. D1 escape vectors — each must fail AND leave no host file                  #
# --------------------------------------------------------------------------- #
_ESCAPES = {
    "v1_os_open": "fd = os.open(T, os.O_WRONLY | os.O_CREAT); os.write(fd, b'x'); os.close(fd)\nprint('WROTE')\n",
    "v2_io_open": "f = io.open(T, 'w'); f.write('x'); f.close()\nprint('WROTE')\n",
    "v3_pathlib_write_text": "pathlib.Path(T).write_text('x')\nprint('WROTE')\n",
    "v4_fdopen_osopen": "fd = os.open(T, os.O_WRONLY | os.O_CREAT); f = os.fdopen(fd, 'w'); f.write('x'); f.close()\nprint('WROTE')\n",
    "v5_builtins_open": "open(T, 'w').write('x')\nprint('WROTE')\n",
}


@pytest.mark.parametrize("name,body", list(_ESCAPES.items()))
def test_A_escape_vector_blocked_and_no_host_residue(name, body):
    result, leaked = _attempt(body, name + ".txt")
    assert result.verdict == "fail", (name, result.to_dict())
    assert "write_outside_workdir" in result.violations, (name, result.violations)
    assert "WROTE" not in result.stdout, name
    assert leaked is False, f"{name}: host-side file was created (escape!)"


def test_A6_os_open_dir_fd_is_fail_closed():
    """``os.open(..., dir_fd=<not None>)`` with write intent ⇒ fail-closed.

    The guard must fire (proved by the ``write_outside_workdir`` violation marker
    in stderr — a platform error would carry no sentinel), never re-base the path.
    """
    # dir_fd is deliberately a non-None int; the guard must reject *before* the
    # real os.open (so it holds regardless of platform dir_fd support).
    target = ESCAPE_DIR / "v6_dirfd.txt"
    if target.exists():
        target.unlink()
    src = (
        "import os\n"
        "try:\n"
        "    os.open('escaped.txt', os.O_WRONLY | os.O_CREAT, dir_fd=0)\n"
        "    print('WROTE')\n"
        "except PermissionError as e:\n"
        "    print('DENIED', str(e)[:40])\n"
    )
    result = run_isolated(src)
    assert result.verdict == "fail", result.to_dict()
    assert "write_outside_workdir" in result.violations, result.violations


# --------------------------------------------------------------------------- #
# B. positive control — INSIDE-workdir writes must pass and physically land     #
# --------------------------------------------------------------------------- #
def test_B_inside_workdir_writes_pass_and_physically_land():
    src = (
        "import os\n"
        "wd = os.environ['FF_SANDBOX_WORKDIR']\n"
        "p1 = os.path.join(wd, 'p1.txt')\n"
        "open(p1, 'w').write('ALPHA')\n"
        "p2 = os.path.join(wd, 'p2.txt')\n"
        "fd = os.open(p2, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)\n"
        "os.write(fd, b'BETA'); os.close(fd)\n"
        "print('R1', open(p1).read())\n"
        "print('R2', open(p2).read())\n"
        "print('LIST', sorted(n for n in os.listdir(wd)))\n"
    )
    result = run_isolated(src)
    assert result.verdict == "pass", result.to_dict()
    assert "R1 ALPHA" in result.stdout
    assert "R2 BETA" in result.stdout
    assert "p1.txt" in result.stdout and "p2.txt" in result.stdout
    assert result.violations == []


# --------------------------------------------------------------------------- #
# C. counterfactual — guard off ⇒ vectors 1–4 are allowed and DO land on host   #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name", ["v1_os_open", "v2_io_open", "v3_pathlib_write_text", "v4_fdopen_osopen"]
)
def test_C_counterfactual_guard_off_allows_and_lands(name):
    result, leaked = _attempt(_ESCAPES[name], "cf_" + name + ".txt", write_guard=False)
    assert "write_outside_workdir" not in result.violations, result.violations
    assert leaked is True, f"{name}: with the guard OFF the write must land (guard is causal)"


# --------------------------------------------------------------------------- #
# D. report honesty (machine-readable rows)                                    #
# --------------------------------------------------------------------------- #
def test_D_report_rows_exactly_eight_and_row5_is_degraded():
    from forgeflow.skills.sandbox_isolated import isolation_capability_report

    report = isolation_capability_report()
    assert len(report) == 8, [r["requirement"] for r in report]
    row5 = next(r for r in report if "越界写" in r["requirement"])
    assert row5["status"] == "degraded", row5
    assert row5["means"] and row5["probe"]
    # no field of row5 may claim "landed"
    assert all(str(v) != "landed" for k, v in row5.items() if k != "requirement")
    # statuses are within the strict three-state set
    assert {r["status"] for r in report} <= {"landed", "degraded", "unlanded"}


def test_D_module_docstring_declares_derivation_from_seven_clauses():
    import forgeflow.skills.sandbox_isolated as mod

    doc = mod.__doc__ or ""
    assert "7 条" in doc, "docstring must state the task book has 7 clauses"
    assert "派生展开" in doc, "docstring must declare the 8 rows are a derivation"
    assert "不是*任务书原生" in doc or "不是任务书原生" in doc, "must deny a native 8-item list"


# --------------------------------------------------------------------------- #
# E. D2 — diff table aligned to the 8-row report, [附加] rows after            #
# --------------------------------------------------------------------------- #
def test_E_diff_table_aligned_and_appendix_after_eight():
    from forgeflow.sandbox.docker_isolated import backend_capability_diff_table
    from forgeflow.skills.sandbox_isolated import isolation_capability_report

    rows = backend_capability_diff_table()
    keys = [r["能力"] for r in rows]
    assert len(rows) == 10, keys

    eight = keys[:8]
    native = [r["requirement"] for r in isolation_capability_report()]

    # (1) first 8 rows carry the ①…⑧ prefixes, in order.
    for i, marker in enumerate("①②③④⑤⑥⑦⑧", start=0):
        assert eight[i].startswith(marker), (i, eight[i])

    # (2) each of the 8 is the SAME requirement, in the SAME order, as the
    #     machine-readable native report (the real "aligned" proof; equal text
    #     or one a prefix of the other — e.g. ① differs by a trailing 「执行」).
    for i in range(8):
        assert eight[i].startswith(native[i]) or native[i].startswith(eight[i]), (
            i,
            eight[i],
            native[i],
        )

    # (3) EVERY row after the eight is an appendix row, and NONE inside is.
    assert all(k.startswith("[附加]") for k in keys[8:]), keys[8:]
    assert not any(k.startswith("[附加]") for k in eight), eight

    # (4) the two capabilities folded into ⑥ appear as appendix rows …
    appendix = keys[8:]
    assert any("文件大小配额" in k for k in appendix), appendix
    assert any("CPU 配额" in k for k in appendix), appendix
    # … while ⑥ (inside the eight) legitimately folds 文件大小配额 into its text.
    assert "文件大小配额" in eight[5], eight[5]
