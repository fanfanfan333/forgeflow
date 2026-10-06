"""Development-environment self-check — ``scripts/check_dev_env.py``.

Answers one question fast: *is this machine's environment complete enough to
build, lint, type-check and test ForgeFlow?* It is a **read-only reporter** — it
never installs, upgrades or otherwise mutates the environment.

    python scripts/check_dev_env.py            # report, exit 0
    python scripts/check_dev_env.py --strict   # exit 1 if a required tool is missing

Output is one line per tool, so it is greppable and CI-friendly::

    [ENV] Python      PASS   3.13.14 (C:\\...\\envs\\agentflow\\Scripts\\python.exe)
    [ENV] uvicorn     PASS   0.53.0
    [ENV] fastapi     PASS   0.141.1
    [ENV] psycopg     PASS   3.3.6
    [ENV] pytest      PASS   9.1.1
    [ENV] ruff        MISSING
    [ENV] mypy        MISSING

The *required* set is the runtime/toolchain the repository declares for the
Windows + Uvicorn + psycopg stack plus the two static analysers. Declared
extras that are commonly absent on a dev box (``pytest-cov``,
``pytest-asyncio``, ``testrelic-pytest``) are reported separately as ``EXTRA`` so
a missing one never masquerades as a broken installation, and vice versa. Core
runtime dependencies (e.g. ``streamlit``) are NOT extras: they are required, and
their absence fails ``--strict``.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import platform
import sys

# name -> (distribution name, import name). The distribution name is what
# `pip install` / `importlib.metadata` knows; the import name is a sanity check.
REQUIRED: tuple[tuple[str, str, str], ...] = (
    ("Python", "", ""),
    ("uvicorn", "uvicorn", "uvicorn"),
    ("fastapi", "fastapi", "fastapi"),
    ("psycopg", "psycopg", "psycopg"),
    ("pytest", "pytest", "pytest"),
    ("ruff", "ruff", "ruff"),
    ("mypy", "mypy", "mypy"),
    # Core runtime dependency ([project.dependencies]): the Streamlit dashboard
    # needs it, so a missing streamlit means the environment is NOT complete. It
    # used to live in EXTRAS ("never a failure"), which let a core dependency go
    # missing while `make env` still reported COMPLETE.
    ("streamlit", "streamlit", "streamlit"),
)

# Declared in pyproject.toml / requirements-dev.txt but not needed to lint or to
# run the test suite; reported for visibility only, never a failure.
EXTRAS: tuple[tuple[str, str], ...] = (
    ("pytest-cov", "pytest-cov"),
    ("pytest-asyncio", "pytest-asyncio"),
    ("testrelic-pytest", "testrelic_pytest"),
)

INSTALL_HINT = "python -m pip install -e '.[dev]'   # or: pip install -r requirements-dev.txt"


def _version(dist: str, module: str) -> str | None:
    """Return the installed version, or ``None`` when the tool is absent."""
    try:
        return md.version(dist)
    except md.PackageNotFoundError:
        pass
    # Fall back to an import probe: a `pip install --target` layout (or an
    # editable install) can be importable without distribution metadata.
    if module and module in sys.modules:
        return "importable (no metadata)"
    import importlib.util

    try:
        if importlib.util.find_spec(module) is not None:
            return "importable (no metadata)"
    except (ImportError, ValueError):
        pass
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/check_dev_env.py",
        description="Report whether this environment has the toolchain ForgeFlow declares.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when a required tool is missing (for CI)",
    )
    args = parser.parse_args(argv)

    print(f"[ENV] interpreter {sys.executable}")
    print(f"[ENV] platform    {platform.platform()}")

    missing: list[str] = []
    for label, dist, module in REQUIRED:
        if label == "Python":
            print(f"[ENV] Python      PASS   {platform.python_version()}")
            continue
        version = _version(dist, module)
        if version is None:
            print(f"[ENV] {label:<9} MISSING")
            missing.append(label)
        else:
            print(f"[ENV] {label:<9} PASS   {version}")

    print()
    for label, module in EXTRAS:
        version = _version(label, module)
        state = "PRESENT" if version else "ABSENT "
        print(f"[ENV] {label:<17} EXTRA  {state} {version or ''}".rstrip())

    print()
    if missing:
        print(f"[ENV] result      INCOMPLETE — missing: {', '.join(missing)}")
        print(f"[ENV] install     {INSTALL_HINT}")
        print("[ENV] note        this script never installs anything for you.")
        return 1 if args.strict else 0
    print("[ENV] result      COMPLETE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
