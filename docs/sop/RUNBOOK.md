# AgentFlow · 运行手册（RUNBOOK）

> 适用：ForgeFlow-main / AgentFlow「企业级 Multi-Agent 智能工作与技能资产平台」改造版
> 目标：让任何人照着本手册就能把平台跑起来，并验证「任务 → 经验 → Skill」闭环与首页六大区块。

---

## 0. 两种运行模式

| 模式 | 依赖 | 用途 | 关键环境变量 |
|---|---|---|---|
| **A. 全离线（推荐先用）** | 仅 Python + Node | 无 PG / 无 Ollama / 无 API Key，跑通闭环与首页 | `STORAGE_BACKEND=memory` `LLM_PROVIDER=mock` `EMBEDDING_PROVIDER=mock` |
| **B. PostgreSQL** | 本机 PG（端口 5433） | 持久化仓储、Alembic 迁移、pgvector 语义检索 | `STORAGE_BACKEND=postgres` + `POSTGRES_URL` |

> 本机运行约束：内存约 2.2GB，**不要**启动 docker-compose / Ollama 全家桶。用模式 A 即可完成全部 P0 验收。

---

## 1. 环境准备（Windows · Git Bash / PowerShell）

### 1.1 Python 虚拟环境

本机 Python 为托管版本，虚拟环境必须建在托管目录内（不要建在仓库里）：

```bash
PY=C:/Users/18769/.workbuddy/binaries/python/versions/3.13.12/python.exe
"$PY" -m venv C:/Users/18769/.workbuddy/binaries/python/envs/agentflow
VENV_PY=C:/Users/18769/.workbuddy/binaries/python/envs/agentflow/Scripts/python.exe
```

### 1.2 安装依赖

```bash
"$VENV_PY" -m pip install --upgrade pip
"$VENV_PY" -m pip install -r requirements.txt
# 若个别可选包在 Py3.13 无 wheel（如 tiktoken/pandas），可单独重试或跳过：
"$VENV_PY" -m pip install pyjwt argon2-cffi pyotp cryptography python-multipart \
  prometheus-client tenacity beautifulsoup4 langgraph-checkpoint-postgres
```

### 1.3 后端环境变量

仓库根 `ForgeFlow-main/ForgeFlow-backend`… 实际以 `forgeflow/` 所在目录为准，创建 `.env`：

```dotenv
# —— 全离线模式 ——
STORAGE_BACKEND=memory
LLM_PROVIDER=mock
EMBEDDING_PROVIDER=mock
DEV_LOGIN_ENABLED=true
DEV_LOGIN_PASSWORD=forgeflow-dev
CORS_ALLOW_ORIGINS=http://localhost:5173
```

> 所有新增变量与默认值见 `.env.example`（`STORAGE_BACKEND` / `EMBEDDING_PROVIDER` / `EMBEDDING_DIMENSION` /
> `SKILL_CANDIDATE_MIN_EXPERIENCES` / `SKILL_CANDIDATE_SIMILARITY_THRESHOLD` / `SKILL_FEATURED_LIMIT` /
> `TENANT_ISOLATION_LEVEL` / `DEFAULT_TENANT_ID` / `DLP_ENABLED` / `MAX_REPLAN_ATTEMPTS`）。

---

## 2. 启动后端（模式 A）

```bash
cd ForgeFlow-main
STORAGE_BACKEND=memory LLM_PROVIDER=mock EMBEDDING_PROVIDER=mock \
  "$VENV_PY" -m uvicorn forgeflow.api.main:app --reload --port 8000
```

期望日志：`ForgeFlow API ready` + `Seeded 4 featured skills (memory backend)`。

自检：

```bash
curl -s localhost:8000/health          # {"status":"healthy",...}
curl -s localhost:8000/skills?featured=true   # 需带 Bearer token（见 §4）
```

---

## 3. 启动前端

```bash
cd ForgeFlow-main/frontend
NODE_DIR=C:/Program\ Files/nodejs        # 按本机实际路径替换
node "$NODE_DIR/node_modules/npm/bin/npm-cli.js" install
node "$NODE_DIR/node_modules/npm/bin/npm-cli.js" run dev   # http://localhost:5173
```

Vite 代理把 `/api/*` 转发到 `http://localhost:8000`（见 `vite.config.ts`），前端始终请求 `/api/...`。

---

## 4. 登录拿 Token（本地密码登录）

`DEV_LOGIN_ENABLED=true` 时会种入 4 个演示用户：`admin` / `manager-1` / `rep-1` / `viewer-1`，密码为 `DEV_LOGIN_PASSWORD`。

```bash
TOKEN=$(curl -s localhost:8000/auth/login -H 'content-type: application/json' \
  -d '{"user_id":"admin","password":"forgeflow-dev"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
echo "Bearer $TOKEN"
```

后续受保护接口都加 `-H "Authorization: Bearer $TOKEN"`。

> 角色权限：`admin=*:*`；`manager` 可读/写/批准 skill 与 policy；`sales_rep` 可执行任务；`viewer` 只读。
> **未登记 `ROUTE_PERMISSION_MAP` 的路由一律 403（fail-closed）**——新增路由务必同步登记。

---

## 5. 验证「任务 → 经验 → Skill」闭环（HTTP）

```bash
# ① 成功任务
RUN=$(curl -s localhost:8000/tasks -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"intent":"分析华东地区销售数据并生成报告"}' | python -c "import sys,json;print(json.load(sys.stdin)['run_id'])")

# ② 查看 run 详情（含 steps）
curl -s localhost:8000/runs/$RUN -H "Authorization: Bearer $TOKEN"

# ③ SSE 实时事件流（应看到 run.started → run.step... → run.completed → [DONE]）
curl -N localhost:8000/runs/$RUN/events -H "Authorization: Bearer $TOKEN"

# ④ 自动沉淀的经验
curl -s "localhost:8000/experiences" -H "Authorization: Bearer $TOKEN"

# ⑤ 失败任务：意图含「失败」或 context.simulate_failure=true → 触发 Replan → 达上限转 HITL
curl -s localhost:8000/tasks -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"intent":"模拟失败任务"}'

# ⑥ 从经验编译候选（跳③）→ 评估 → 固化
CAND=$(curl -s localhost:8000/skill-candidates -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"mode":"auto"}' | python -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -s -X POST localhost:8000/skill-candidates/$CAND/evaluate -H "Authorization: Bearer $TOKEN"
curl -s -X POST localhost:8000/skill-candidates/$CAND/promote  -H "Authorization: Bearer $TOKEN"
```

> 经验不足（< `SKILL_CANDIDATE_MIN_EXPERIENCES`）时，编译返回 `status="insufficient"`（HTTP 200），先多跑几个任务即可。

---

## 6. 前端页面验收清单

打开 <http://localhost:5173/console>（先点右上「Sign in」用 `admin` / `forgeflow-dev` 登录）。

| 区块 | 期望（全部来自真实 API） |
|---|---|
| 侧边栏 10 项导航 | 首页 / 智能任务 / Agent 工作台 / 技能中心 / 知识库 / 记忆管理 / 数据分析 / 安全与权限 / 运维监控 / 系统设置；选中态左侧高亮条 + 渐变底 |
| Hero | 大标题 + 副标题 + 任务输入框 + 4 个快捷 chip + 装饰方块 |
| 4 KPI | 总任务数 / 成功率 / 累计成本 / 平均响应时间 ← `/api/metrics` |
| 我的 Agent（6） | ← `/api/agents/catalog` |
| 技能中心（4） | ← `/api/skills?featured=true` |
| 近期任务 | ← `/api/runs` |
| 智能执行日志 | ← `/api/runs/{id}/events`（SSE 实时追加，断线降级轮询） |
| 安全与资源概览 | ← `/api/security/overview` |

其余页面：`/console/skills`（技能中心：检索 / 版本 / 回滚 / 候选固化）、`/console/knowledge`（五层记忆 + 经验）、
`/console/security`（策略 + 待审批决策）。

---

## 7. 测试与回归

```bash
cd ForgeFlow-main
STORAGE_BACKEND=memory LLM_PROVIDER=mock "$VENV_PY" -m pytest tests/ -q
```

- 基线：`tests/` 下 43 个既有文件必须全绿（含 `test_graph.py` / `test_rbac.py` / `test_multi_tenant.py` / `test_memory.py`）。
- 新增单测：`tests/unit/test_experience_extraction.py`、`test_validation.py`、`test_candidate_compiler.py`、
  `test_skill_versioning.py`、`test_skill_evaluator.py`、`test_governance_gate.py`、`test_policy_engine.py`、
  `test_tenant_isolation.py`、`test_memory_scopes.py`、`test_security.py`；集成：`tests/integration/test_runs_sse.py`。

零依赖自检：

```bash
STORAGE_BACKEND=memory LLM_PROVIDER=mock "$VENV_PY" -c "import forgeflow.api.main; print('import ok')"
```

---

## 8. 模式 B（PostgreSQL 5433，可选）

> ⚠️ **本机凭据更正（2026-09-23 主理人实测）**：本文档早期版本写的 `postgres:postgres` **在本机连不上**
> （`FATAL: password authentication failed for user "postgres"`）。本机唯一可用账号是 **`forgeflow`**。
> 实测可用的 DSN（逐候选试连得出）：
>
> ```
> postgresql://forgeflow:forgeflow@127.0.0.1:5433/forgeflow     ← 本项目库（39 张 public 表）
> postgresql://forgeflow:forgeflow@localhost:5433/forgeflow    ← 等价写法
> ```
>
> 另注：同账号连 `/postgres` 维护库也能通，但那里**没有本项目表**，别用错库。

```bash
export POSTGRES_URL="postgresql://forgeflow:forgeflow@127.0.0.1:5433/forgeflow"
# 同步驱动必须带 +psycopg（Windows 下尤其容易漏）
export POSTGRES_SYNC_URL="postgresql+psycopg://forgeflow:forgeflow@127.0.0.1:5433/forgeflow"
export STORAGE_BACKEND=postgres
"$VENV_PY" -m alembic upgrade head        # 001 → 011
"$VENV_PY" -m uvicorn forgeflow.api.main:app --port 8000
```

- 迁移 `010_agentflow_hubs`：`revision="010"`, `down_revision="009"`，全部 `IF NOT EXISTS`，可重复执行；
  语义检索列需要 `CREATE EXTENSION IF NOT EXISTS vector`（pgvector）。
- 迁移 `011_cost_slo_evolution`：`revision="011"`, `down_revision="010"`，新增 `cost_budgets` /
  `skill_ratings` / `context_build_stats` 三表 + 加列；**只增不破，严禁改 010 及更早**。
- `downgrade`：`alembic downgrade 010` 可回退 011。
- ✅ **实测状态**：`experiences` 表已确认含 011 的四列（`conflict_with` / `merged_from` / `dedup_key` /
  `confidence`），证明 011 在本机真库**已实际升级到位**，不是纸面迁移。

---

## 9. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 接口 401 `missing bearer token` | 未登录或 token 过期，重新 `POST /auth/login` |
| 接口 403 | 角色权限不足，或该路由未登记 `ROUTE_PERMISSION_MAP`（fail-closed） |
| 首页某些区块空白 | 该角色缺少对应读权限（如 `viewer` 无 `read:audit`）；改用 `admin` 登录 |
| `ModuleNotFoundError: langchain_*` | 选装依赖未装；离线模式核心路径不 import 这些包，装齐即可 |
| `POST /tasks` 403 | 当前角色无 `execute:workflows`（`manager`/`viewer` 无）；用 `admin` 或 `rep-1` |
| 候选编译一直 `insufficient` | 相似经验 < `SKILL_CANDIDATE_MIN_EXPERIENCES`，多跑几个同类任务 |

---

## 10. 关键约定速查

- 后端路由挂在根路径（`/skills`），前端调用 `/api/skills`（Vite 代理去掉 `/api`）。
- 仓储按 `STORAGE_BACKEND` 选择 `memory` / `postgres`，两者 **签名一致**。
- 离线 LLM = `MockChatModel`（确定性 JSON），离线嵌入 = 特征哈希（1536 维，无 numpy）。
- 租户隔离：`resolve_tenant(workspace_id)` → workspace 或 `DEFAULT_TENANT_ID`；跨租户读=404，写=403。
- SSE 用 `fetch` + `ReadableStream`（`EventSource` 无法带 Bearer 头），`X-Accel-Buffering: no`。
