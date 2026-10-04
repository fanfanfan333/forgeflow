"""Real-stack frontend boot — ``scripts/run_frontend_real.py`` (Vite dev on :5173).

Version-controlled successor of the former root-level ``_run_frontend_real.py``
(that file lived outside Git under the ``/_*`` ignore rule, so the exact proxy /
port behaviour could not be reviewed, diffed or reproduced on another machine).
Keeping it beside ``run_backend_real.py`` in ``scripts/`` means the whole local
launch path — backend *and* frontend — is now reviewable and relocatable.

Why a wrapper instead of calling vite directly:
  ``vite.config.ts`` defaults its proxy target to ``http://localhost:8000``,
  which on this host is a DIFFERENT project (the RAG stack). ``VITE_API_TARGET``
  must be set to point the ``/api`` proxy at our backend on :8010.

Why ``node <vite/bin/vite.js>`` instead of ``npx vite``:
  there is no ``node_modules/.bin/vite`` on this checkout, and npx would
  re-resolve the package over the network (proxy!). The local entrypoint is
  hermetic.

Why ``subprocess`` instead of ``os.execv``:
  ``os.execv`` on Windows joins argv WITHOUT quoting, so any argument containing
  a space gets truncated. ``subprocess`` uses ``list2cmdline`` and quotes
  correctly, so this stays robust even if a future checkout path contains a
  space.

Log -> ``<repo>/qa_tmp/_run_frontend_real.log`` (path kept stable; the local
health check ``verify_forgeflow.py`` reads it).
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

# Derive the repo root from this file's own location instead of hardcoding it.
# This file lives at ``<repo>/ForgeFlow-main/scripts/run_frontend_real.py``, so
# ``parents[2]`` is the repository root. (The project has already been moved
# once; a hardcoded root silently breaks every future move.)
ROOT = pathlib.Path(__file__).resolve().parents[2]
FE = ROOT / "ForgeFlow-main" / "frontend"
LOG = ROOT / "qa_tmp" / "_run_frontend_real.log"

# Overridable for other machines/CI images; the default is this box's node.
NODE_CANDIDATES = [
    os.environ.get("FORGEFLOW_NODE_EXE", "")
    or r"C:\Users\18769\.workbuddy\binaries\node\versions\22.22.2-3\node.exe",
]

HOST = "127.0.0.1"
PORT = "5173"


def main() -> int:
    """Prepare the environment, then exec the local Vite entrypoint."""
    node = next((p for p in NODE_CANDIDATES if pathlib.Path(p).is_file()), None)
    if node is None:
        sys.stderr.write("no node.exe found in candidates\n")
        return 2
    vite = FE / "node_modules" / "vite" / "bin" / "vite.js"
    if not vite.is_file():
        sys.stderr.write(f"vite entrypoint missing: {vite}\n")
        return 2

    for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(_k, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1"
    # THE fix: vite.config.ts would otherwise proxy /api -> :8000 (another project).
    os.environ["VITE_API_TARGET"] = "http://127.0.0.1:8010"

    os.chdir(FE)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(f"\n===== boot frontend REAL pid={os.getpid()} cwd={FE} =====\n")
        log.flush()

        proc = subprocess.Popen(
            [node, str(vite), "--port", PORT, "--strictPort", "--host", HOST],
            cwd=str(FE),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        return proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())
