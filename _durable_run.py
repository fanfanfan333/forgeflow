"""依次拉起两个独立进程，验证真 PG checkpointer 的跨进程恢复能力。

write 进程跑图并落 checkpoint → 进程退出（模拟崩溃/重启）→ read 进程用同一
thread_id 读回。只有跨进程才算真恢复证明。
"""
from __future__ import annotations

import io
import os
import subprocess
import uuid

ROOT = r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台"
PY = r"C:\Users\18769\.workbuddy\binaries\python\envs\agentflow\Scripts\python.exe"

THREAD = "durable-" + uuid.uuid4().hex[:12]
lines: list[str] = [f"thread_id = {THREAD}", ""]

for phase in ("write", "read"):
    proc = subprocess.run(
        [PY, "_durable_phase.py", phase, THREAD],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    lines.append(f"########## {phase.upper()} (rc={proc.returncode}) ##########")
    lines.append(
        "\n".join(
            [
                l
                for l in io.open(
                    os.path.join(ROOT, f"_durable_{phase}.txt"), encoding="utf-8"
                ).read().splitlines()
            ]
        )
        if os.path.exists(os.path.join(ROOT, f"_durable_{phase}.txt"))
        else "(no output file)"
    )
    if proc.returncode != 0:
        lines.append(f"--- stderr ---\n{(proc.stderr or '')[-2000:]}")
    lines.append("")

io.open(os.path.join(ROOT, "_durable_run.txt"), "w", encoding="utf-8").write(
    "\n".join(lines)
)
print("durable run done")
