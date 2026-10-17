"""Minimal environment for control-plane Git subprocesses."""

from __future__ import annotations

import os

from forgeflow.codeplane.runner.run_code_task import safe_process_environment


def git_environment() -> dict[str, str]:
    env = safe_process_environment()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    return env
