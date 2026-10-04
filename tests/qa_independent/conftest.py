"""INC46 QA-independent 测试的进程级隔离（**仅新增**，不改任何既有测试文件）。

缺陷登记 · 2026-10-04 · 非回归（与 INC46 T25 版本链改动无关）
--------------------------------------------------------------
``forgeflow.evaluation.golden_registry._FROZEN_INDEX`` 是一个**进程级**字典，
**任何 registry 读**（含 :class:`PostgresGoldenRegistry` 的 ``list_sets`` /
``get_set`` / ``latest_frozen_set``）都会 ``register_frozen_index(...)`` 写入，
而它**从不在测试之间被清理**。

因此，只要 PG 可达，``tests/integration/test_inc46_golden_pg.py`` 就会在本进程
留下 ``(tenant, set_id)`` 残留项；随后同进程运行的本目录独立探针用例
（``golden_regression.INTERLOCK_PROBE`` → ``frozen_set_count()``）会**误报
``ok=True``**，使本来应“锁死”的 R4 被判定为已满足：

* ``test_qa_t13_independent::test_r2_remains_unmet_no_real_isolation_anchor``
* ``test_qa_t15_interlock_probe::test_probe_fail_closed_when_capability_module_missing``

单进程可证伪实验（gate 干净 ⇒ 注入一次 registry 残留 ⇒ 清除后复原）::

    frozen_set_count() == 0                        ⇒ R4 unmet  missing=[R2..R8]
    register_frozen_index("t-x", "set-y", 4)       ⇒ frozen_set_count() == 1
    evaluate_interlock(...)                        ⇒ R4 变 met missing=[R2,R3,R5..R8]
    clear_frozen_index()                           ⇒ R4 复原 unmet missing=[R2..R8]

其中 ``missing=[R2,R3,R5,R6,R7,R8]`` 与用例失败信息**字节一致**；最小复现命令::

    pytest tests/integration/test_inc46_golden_pg.py \
           tests/qa_independent/test_qa_t13_independent.py \
           tests/qa_independent/test_qa_t15_interlock_probe.py

处置
----
本 conftest **只清理进程级全局态**，不缩小任何断言、不改动任何既有测试文件，
以恢复用例「全新进程 ⇒ 联锁锁死」的既定语义（红线 1：仅新增）。

注意：T15 自带的 ``_clean_qa_state`` autouse fixture 重置了 interlock / evolution /
skill-store，但**漏掉了 golden 冻结索引**；T13 原本没有任何隔离 fixture。本文件
在目录层统一补齐，覆盖两者。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_process_global_interlock_state():
    """每个用例前后清除 golden 冻结索引残留，让 R4 探针看到干净的进程态。"""
    from forgeflow.evaluation import golden_registry as _gr

    _gr.clear_frozen_index()
    yield
    _gr.clear_frozen_index()
