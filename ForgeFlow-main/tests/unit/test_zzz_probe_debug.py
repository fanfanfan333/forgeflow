"""TEMPORARY diagnostic — deleted after use."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _resolve_make() -> str | None:
    for candidate in (os.environ.get("QA_MAKE"),):
        if candidate and Path(candidate).exists():
            return candidate
    fallback = Path.home() / ".workbuddy" / "binaries" / "make" / "bin"
    for name in ("make.exe", "make"):
        candidate = fallback / name
        if candidate.exists():
            return str(candidate)
    return None


def test_probe_debug(capsys):
    make = _resolve_make()
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "memory"
    env["LLM_PROVIDER"] = "mock"
    p = subprocess.run(
        [make, f"PY={sys.executable}", "__check-interpreter"],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
    )
    with capsys.disabled():
        print("\n=== PROBE DEBUG ===")
        print("make:", make)
        print("sys.executable:", sys.executable)
        print("cwd:", os.getcwd())
        print("rc:", p.returncode)
        print("out:", repr(p.stdout))
        print("err:", repr(p.stderr))
        print("BASH_ENV:", os.environ.get("BASH_ENV"))
        print("ENV:", os.environ.get("ENV"))
        print("SHELL:", os.environ.get("SHELL"))
        print("PATH head:", os.environ.get("PATH", "")[:300])
    assert True
