"""真实栈（real stack）验收测试包 — Ollama + Docker/PostgreSQL。

这一包里的每一条用例都**真的**调用本机服务：

  * 真 Ollama（``qwen3:8b``，:11434）做规划与执行 —— 不是 mock、不是录制的
    离线档；
  * 真 PostgreSQL（Docker 容器，:5433）做持久化 —— 写入后用**独立的
    asyncpg 连接**回查，不复用应用自己的 store/pool。

默认不执行
----------
``pytest tests/``（离线快测）必须**不受影响**：所有用例都被双重门控挡住，
skip 时给出明确 reason（不是静默跳过）：

  1. 环境变量 ``FORGEFLOW_REAL_STACK=1``；
  2. 探测到 Ollama :11434 **和** PostgreSQL :5433 都真的可达。

启用方式::

    FORGEFLOW_REAL_STACK=1 pytest tests/realstack/ -v

为什么放在独立的目录而不是 ``tests/integration/``:
    集成目录里的 PG 用例（``test_a7_metrics_store_pg.py`` 等）在``STORAGE_BACKEND``
    切换时会参与离线跑；真实栈用例会**真的烧 token**（一次 4k+）并且**真的写
    开发库**，必须与它们物理隔离，避免有人在 CI 上无意触发。
"""
