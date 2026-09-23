"""Independent QA pytest runner: runs pytest in a subprocess and writes
combined stdout/stderr + exit code to an output file (UTF-8)."""
import os
import subprocess
import sys

PY = r"C:\Users\18769\.workbuddy\binaries\python\envs\agentflow\Scripts\python.exe"
ROOT = r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\ForgeFlow-main"
OUTDIR = r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\qa_tmp"

env = dict(os.environ)
env.update({
    "STORAGE_BACKEND": "memory",
    "LLM_PROVIDER": "mock",
    "EMBEDDING_PROVIDER": "mock",
    "DEV_LOGIN_ENABLED": "true",
    "DEV_LOGIN_PASSWORD": "forgeflow-dev",
})

args = sys.argv[1:-1]
outfile = sys.argv[-1]

proc = subprocess.run(
    [PY, "-m", "pytest", *args],
    cwd=ROOT,
    env=env,
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
)
tail = (
    "\n\n=== CAPTURED SUMMARY TAIL (stderr last 5 lines) ===\n"
    + "\n".join(proc.stderr.splitlines()[-5:])
)
with open(outfile, "w", encoding="utf-8") as fh:
    fh.write(proc.stdout)
    fh.write(tail)
    fh.write(f"\n\n=== EXIT CODE: {proc.returncode} ===\n")
print(f"wrote {outfile} exit={proc.returncode}")
