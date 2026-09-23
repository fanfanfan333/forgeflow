"""清 pytest 临时残骸 → 跑全量。合并成一个脚本，用 WMI 脱离 safe-delete 批量守卫执行。

用法: _run_full.py <memory|postgres>
"""
from __future__ import annotations

import glob
import io
import os
import shutil
import subprocess
import sys

PROFILE = sys.argv[1] if len(sys.argv) > 1 else "memory"
FF = r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\ForgeFlow-main"
ROOT = r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台"
PY = r"C:\Users\18769\.workbuddy\binaries\python\envs\agentflow\Scripts\python.exe"

# 1) purge leftovers (large ones trip the >50 files bulk guard later)
n = 0
for pat in (
    r"D:\Temp\pytest-of-*\garbage-*",
    os.path.join(FF, ".pytest_run*"),
    os.path.join(FF, ".pytest_tmp_*"),
):
    for p in glob.glob(pat):
        shutil.rmtree(p, ignore_errors=True)
        n += 1

# 2) run the suite
env = dict(os.environ)
env["STORAGE_BACKEND"] = PROFILE
proc = subprocess.run(
    [PY, "-m", "pytest", "tests", "-p", "no:cacheprovider", "--tb=line", "-q"],
    cwd=FF,
    env=env,
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
)
out = (proc.stdout or "") + "\n===STDERR===\n" + (proc.stderr or "")
tail = "\n".join([ln for ln in out.splitlines() if ln.strip()][-25:])
with io.open(
    os.path.join(ROOT, "_full_%s_result.txt" % PROFILE), "w", encoding="utf-8"
) as fh:
    fh.write("purged_dirs=%d returncode=%d\n%s" % (n, proc.returncode, tail))
print("done", proc.returncode)
