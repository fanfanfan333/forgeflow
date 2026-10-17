"""Keep container Git metadata separate from the control-plane repository."""

from __future__ import annotations

import os
import runpy
import shutil

if __name__ == "__main__":
    if os.environ.get("FORGEFLOW_CODEPLANE_CONTAINER") != "1":
        raise RuntimeError("Container bootstrap requires the managed container")
    shutil.copytree("/workspace/.git", "/tmp/forgeflow-git")
    os.environ["GIT_DIR"] = "/tmp/forgeflow-git"
    os.environ["GIT_WORK_TREE"] = "/workspace"
    runpy.run_path("/opt/forgeflow-runner/run_code_task.py", run_name="__main__")
