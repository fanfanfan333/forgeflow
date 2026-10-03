"""INC46 T13 — isolation backends.

* :mod:`forgeflow.sandbox.docker_isolated` — the **container** backend (裁定 I),
  the landed route for kernel-level isolation (read-only rootfs / no network /
  memory / processes).
* The **native** pure-Python backend stays in
  :mod:`forgeflow.skills.sandbox_isolated` (degraded on Windows without admin).

The T15 publish-interlock requirement **R2** anchors on
``forgeflow.sandbox.real_isolation``; that module is deliberately **not** created
here (R2 is kept ``met=False`` per ruling R9 until the container backend lands and
QA re-verifies end to end).
"""

from __future__ import annotations

__all__: list[str] = []
