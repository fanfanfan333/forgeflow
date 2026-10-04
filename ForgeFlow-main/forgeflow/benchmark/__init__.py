"""INC46 T36 — 端到端基准（冻结语料 + 参考执行器）。

``cases/e2e_corpus.json`` 是**冻结**的基准语料（≥ 50 例，见 :mod:`.runner`
的 ``FROZEN_CORPUS_SHA256``）；:mod:`.runner` 用**真实规则**从用例字段派生
verdict（注入检测器 / 危险指令扫描 / 平台能力矩阵 / 歧义判定），再与用例的
``expect`` 断言比对。基准集被改动（哈希不符）时拒绝运行（任务书 T36 §阴性探针）。
"""

from __future__ import annotations

__all__: list[str] = []
