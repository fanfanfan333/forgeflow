# INC25 企业级验证计划（QA / S4）

> 文档类型：验证计划 + 矩阵 + 执行清单（**只出计划**；真实模型重活待主理人放行）
> 上游：`docs/sop/INC25-PRD.md`（AC-1~AC-24）、`docs/sop/INC25-DESIGN.md`
> 代码基线：`alembic` head = **`015_resources.py`**（INC25-W1 新增）；代码执行面已落地（`forgeflow/codeplane/*`）
> 运行档位：**A 离线**（`storage_backend=memory` + `llm_provider=mock`，引擎注入假 runner）/ **B 真实**（PG5433 + 本机 Ollama `qwen3:8b`）
> 状态：A 档可立即跑；**B 档真实模型重活未执行**（避免与工程师 P0-C 可靠性跑抢同一台 Ollama）

---

## 0. 一句话目标

用**可机械判定的证据**证明 INC25 的资源层与代码执行面满足 AC-1~AC-24，并在企业级七维度（重复可靠性 / 诚实性 / 降级逐字 / 隔离 / HITL / 证据可见性 / RBAC）上不留假绿。

---

## 1. 判定总纪律（最重要，先立规矩）

1. **成败判据 = `codeplane.tests.verdict`（叠加真实 pytest 退出码与 diff 非空），绝不是会话状态。**
   - 依据（团队实测既成事实）：`execution_status=STUCK` 的运行**也可能已经改对代码**（`temperature=0` 的 3 次里 2 次 STUCK 但 `pytest rc=0`）。
   - ⇒ 会话状态只作为**失败签名**记录，不作为通过/失败判据。
2. **降级绝不伪装 ok**：`codeplane.degraded` 非空 ⇒ 运行状态不得为 `completed`；`degraded` 值与其 `reason` 原文必须逐字可见。
3. **未测量 ≠ 通过**：`tests.measured=false` / `verdict="unmeasured"` 一律视为**未通过**（`tests_verdict.evaluate_test_output` 的既有口径）。
4. **未测量耗时 = `None`**（前端渲染「—」），绝不写 `0`；沿用既有 `latency_ms: float | None` 口径。
5. **junit XML 为准**：本机裸跑 pytest 常只有 `EXIT=1` 而无汇总行；一切计数以 `--junitxml` 落盘的 XML 为准，并给出 `tests/failures/errors/skipped` 摘要。
6. **反事实自查（falsifiability）**：每个「守卫型」断言都要有一次「把它改坏 ⇒ 必须转红」的反事实证据，否则不算证明。

---

## 2. 运行档位与证据规范

### 2.1 测试执行宿主（本机既定范式）

依赖装在 `ff-test:inc20` 镜像（`alembic 1.20.0 / asyncpg / fastapi / pytest 9.1.1`）；PG 在宿主 `127.0.0.1:5433`，容器用 **host 网络**直达。

**A 档（离线，可立即跑；无 PG / 无 Ollama 依赖）**

```bash
docker run --rm --network host \
  -e TESTRELIC_API_KEY= -e LLM_PROVIDER=mock -e EMBEDDING_PROVIDER=mock -e STORAGE_BACKEND=memory \
  -v "<repo>":/app -w /app ff-test:inc20 \
  python -m pytest <TEST_PATHS> -p no:cacheprovider --junitxml=/app/qa_tmp/inc25/<NAME>.xml -q
```

**B 档（真实 PG + Ollama；**待放行**，先不跑）**

```bash
docker run --rm --network host \
  -e TESTRELIC_API_KEY= -e STORAGE_BACKEND=postgres -e LLM_PROVIDER=ollama \
  -e CODEPLANE_TEMPERATURE=0 \
  -e CODEPLANE_INTERPRETER=<OpenHands venv python> \
  -v "<repo>":/app -w /app ff-test:inc20 \
  python -m pytest <TEST_PATHS> -p no:cacheprovider --junitxml=/app/qa_tmp/inc25/<NAME>.xml -q
```

> 前置：B 档代码任务需要 OpenHands venv（`Settings.codeplane_interpreter` / `FORGEFLOW_CODEPLANE_PYTHON`）可达；`engine.available()` 为 `False` 时表现为结构化降级（`engine_unavailable`），不是崩溃。

### 2.2 junit 摘要提取（证据规范）

```bash
python - <<'PY'
import xml.etree.ElementTree as ET, glob, sys
for f in sorted(glob.glob("qa_tmp/inc25/*.xml")):
    r = ET.parse(f).getroot(); s = r if r.tag == "testsuite" else r.find("testsuite")
    print(f"{f} => tests={s.get('tests')} failures={s.get('failures')} "
          f"errors={s.get('errors')} skipped={s.get('skipped')} time={s.get('time')}s")
    for tc in s.iter("testcase"):
        st = "PASS"
        if tc.find("failure") is not None: st = "FAIL"
        if tc.find("error") is not None: st = "ERROR"
        if tc.find("skipped") is not None: st = "SKIP"
        print(f"   [{st}] {tc.get('classname').split('.')[-1]}::{tc.get('name')}")
PY
```

### 2.3 夹具约定（`tests/fixtures/inc25/`）

固定夹具，字节数写死以便逐字节比对：`sample.csv`（自带主键列 + 已知空值列）、`sample.txt`、`sample.pdf`（页数已知）、`mini_repo/`（迷你 git 仓：一个**故意失败**的测试 + `mathlib.py` 断言错误）、`unsupported.bin`。

---

## 3. 企业级七维度验证矩阵（D1–D7，**必须显式覆盖**）

### D1. 重复可靠性（真实 qwen3:8b，N ≥ 5）

| 项 | 内容 |
|---|---|
| 目标 | 固定夹具代码任务（修复失败测试）在真实模型下**稳定**跑通 |
| 前置 | 工程师 P0-C 落地：`CODEPLANE_TEMPERATURE=0` + 真正生效的 `max_rounds`（逐字 `degraded="max_rounds"`）+ 对话墙钟 + terminal 超时 |
| 方式 | 驱动脚本 `qa_tmp/inc25/run_reliability.sh`（或 `tests/integration/test_inc25_codeplane_reliability.py`，`@pytest.mark.slow`）连跑 **N≥5** 次真实代码任务；每次落一份 junit + 一份 `codeplane` JSON 快照 |
| 判据（**每次**） | ① `codeplane.tests.verdict == "passed"`；② 真实 `pytest rc == 0`；③ `codeplane.diff` 非空 |
| 判据（汇总） | **成功率 5/5 为发布门禁**；否则报告成功率 + **失败签名**（`degraded` 值 / `codeplane.timeline` 尾部 / 是否出现 `"Stuck pattern detected"` / 墙钟秒数 / 重试次数） |
| 记录 | 每次的 `execution_status`（仅作签名，**不作判据**）、墙钟、`diff` 行数 |
| 反事实 | 以默认温度（不加 `CODEPLANE_TEMPERATURE=0`）复跑同夹具，应观察到成功率显著下降或出现 `max_rounds`/`timeout` 降级 ⇒ 证明该门禁有鉴别力 |

> **红线**：本维度是 B 档重活，**当前不执行**，待主理人放行后与工程师协调串行跑（避免 Ollama 争用污染）。

### D2. 诚实性（模型话术不得覆盖 tests verdict）

| 项 | 内容 |
|---|---|
| 文件 | `tests/unit/test_inc25_codeplane_honesty.py`（新，纯单元，A 档可跑） |
| 场景 | 注入假 runner：timeline 出现「执行完成 / 修改成功」类 message，但测试输出为 `2 failed` 或退出码非 0 |
| 判据 | `codeplane.tests.verdict == "failed"`、`measured is True`、`failed_cases` 逐字含失败用例名；**绝不被模型话术覆盖成 passed** |
| 截断逐字 | 构造超限 payload，断言 `*_truncated` / `*_original_length` / `timeline_omitted` 逐字可见：`codeplane.truncated`、`detail_truncated`/`detail_original_length`、`diff_truncated`/`diff_original_length`、`tests.raw_stdout_truncated`/`raw_stdout_original_length`、`timeline_omitted` |
| 反事实 | 把 `evaluate_test_output` 的失败分支改回「默认 passed」，断言必须转红 |

### D3. 降级逐字（六值与 reason 原文可见，绝不伪装 ok）

| 项 | 内容 |
|---|---|
| 文件 | `tests/unit/test_inc25_codeplane_degrade.py`（新）+ `tests/integration/test_inc25_codeplane_http.py`（新，HTTP 面） |
| 覆盖值（`forgeflow/codeplane/protocol.py::DEGRADED_VALUES`） | `engine_unavailable`、`model_unavailable`、`timeout`、`runner_crashed`、`parse_failed`、`max_rounds` |
| 构造方式 | 假 runner 注入：解释器/runner 缺失⇒`engine_unavailable`；runner 上报模型不可达⇒`model_unavailable`；挂起超墙钟⇒`timeout`；非零退出无 result 行⇒`runner_crashed`；stdout 非 JSONL⇒`parse_failed`；回合预算耗尽⇒`max_rounds` |
| 判据（每个值） | `codeplane.degraded == <值>`（**逐字**）；对应 `reason` 原文可见；`engine_unavailable/model_unavailable` ⇒ 步骤 `unavailable`，其余 ⇒ `error`；**运行 status ≠ `completed`**（`codeplane.degraded` 非空即不得完成） |
| 未登记值 | 传入未登记的 `degraded` 值 ⇒ 原值透传、只做中性说明、**不编造原因** |
| 反事实 | 对每个值断言「若把它当 `ok` 处理则运行会 `completed`」——用现有 `_UNAVAILABLE_DEGRADES` 分支证明其不会 |

> ⚠️ 与工程师 P0-C 的接口：`max_rounds` 已登记为 **error**（非 unavailable），必须逐字出现在 `degraded` 且不得渲染为 `ok`。

### D4. 隔离（源仓库任何路径下都不被改动，含 approve 后）

| 项 | 内容 |
|---|---|
| 文件 | `tests/unit/test_inc25_codeplane_isolation.py`（新，A 档可跑，`WorkspaceManager(root=tmp)` 注入项目外根） |
| 基准 | 任务**前**捕获源仓库 `git status --porcelain`（字节快照）+ `git rev-parse HEAD` |
| 流程 | 完整代码任务（execute → 产出 diff/tests → approve → commit）；approve 用 `code_version_prior` 复用工作区 |
| 判据 | 源仓库 `git status --porcelain` **前后字节相等**；`git rev-parse HEAD` 不变；工作区路径**不在**项目根目录下（`not Path(ws.path).is_relative_to(_ROOT)`） |
| 反事实 | 令工作区根误设到项目内，断言隔离检查转红 |

> 提交落在**任务工作区分支**，绝不 push 主线、不自动开 PR（PR 属 P2）。

### D5. HITL 三动作（approve / reject / replan + 审计 + 未批准前不写分支）

| 项 | 内容 |
|---|---|
| 文件 | `tests/unit/test_inc25_codeplane_approval.py`（新，A 档）+ 复用 `tests/unit/test_inc25_codeplane_commit_idempotent.py` |
| approve | `POST /codeplane/runs/{id}/approve` → `codeplane.approval.status="approved"`，resume run 产出 `code_diff`/`code_test_report` 产物；`decided_by`/`decided_at` 非空 |
| reject | `POST /codeplane/runs/{id}/reject` → `status="rejected"`、工作区 `destroy=True`、**不留任何改动** |
| replan | 复用 `POST /runs/{id}/replan` → 新 run / 新工作区 / 新一轮 |
| 审计 | 三动作各写 `middleware/audit.py::write_audit_entry`（`codeplane.approval.requested` / `approved` / `rejected`），**可按 run 检索** |
| 未批准前 | 运行停在 `awaiting_approval`（`code.commit` 步骤 `executed=False`、`latency_ms=None`）；目标分支**未被写入**（对照 D4 快照） |
| 反事实 | 删掉 approve 的审计写入 ⇒ 审计断言转红 |

### D6. 证据可见性（`GET /runs/{id}` 的 `codeplane` 六键齐全）

| 项 | 内容 |
|---|---|
| 文件 | `tests/integration/test_inc25_codeplane_http.py`（新，A 档；无需真实 PG） |
| 断言 | `RunDetailResponse.codeplane` **键不得缺失**：`engine` / `workspace` / `timeline` / `tests` / `diff`（+ 有审批时 `approval`、commit 后 `committed`） |
| 老记录 | 无 codeplane 的记录 ⇒ `codeplane == {}` 且 `GET /runs/{id}` **不 500**（additive 默认 `{}`） |
| 反事实 | 从 `_assemble_codeplane` 的 `merged` 里删掉 `"tests"` 键 ⇒ 断言转红 |

### D7. RBAC（viewer 不可 approve）

| 项 | 内容 |
|---|---|
| 文件 | `tests/unit/test_inc25_rbac.py`（新）+ 复用 `tests/unit/test_rbac.py` |
| 断言 | `POST /codeplane` 路由映射为 `approve:skills`（`rbac/policies.py::ROUTE_PERMISSION_MAP`）；**viewer** 调用 `POST /codeplane/runs/{id}/approve` → **403**；缺权限时审批**未发生**、审计记为拒绝 |
| 反事实 | 把路由映射临时放宽为 `read:*` ⇒ viewer 断言转红 |

---

## 4. AC-1 ~ AC-24 验证矩阵

> 记法：**已存在** = 现成测试文件；**新** = S4 待创建。命令默认 A 档（离线），B 档单列。判据皆为机械可判。

| AC | 验证方式 | 具体测试文件 / 命令 | 通过判据 |
|---|---|---|---|
| **AC-1** | 五类资源登记入口各自可寻址（API 契约）+ 前端五 `data-testid`（`resource-kind-*`）存在 | `tests/unit/test_inc25_resources_api.py`（新）；`tests/unit/test_inc25_contracts.py`（新，testid 快照） | 五入口各自返回可寻址资源 id；五 testid 全部存在 |
| **AC-2** | 上传夹具 CSV → 真实字节数逐字节相等、status=parsed | `tests/unit/test_inc25_resources.py`（新，夹具 `tests/fixtures/inc25/`） | `locator.bytes == len(fixture)` 且 `status == "parsed"` |
| **AC-3** | 摘要 rows/columns/fields 与夹具真实内容逐项相等 | 同上 | 三项逐项 `==`（非模糊匹配） |
| **AC-4** | 数据质量真实统计（空值列占比 / 重复主键） | 同上（夹具自带主键列 + 已知空值） | 空值列及占比一致；重复主键「有则报、无则不报」 |
| **AC-5** | 文本 `chars==len(text)`；PDF `pages==page_count`；无来源无 `keywords` | 同上（+ `multimodal/pdf.py::extract_pdf_text`） | 两等式成立；夹具无关键词来源时 `"keywords" not in summary` |
| **AC-6** | 超限上传 → HTTP 413，detail 同时含两数字 | 同上（构造 > `multimodal_max_bytes`） | `status==413` 且 detail 逐字含实际字节数与上限值两数字 |
| **AC-7** | 不支持类型 → 400 逐字原因，不产生 parsed 条目 | 同上 | `status==400`；资源列表无「已解析」条目 |
| **AC-8** | 解析依赖缺失 → 降级态 + 原因，HTTP 非 5xx | 同上（monkeypatch 令解析依赖缺失） | 状态 ∈ {`metadata_only`,`ignored`}；`status < 500` |
| **AC-9** | 不选资源 ⇒ payload 无资源键、`declared_inputs=={}` | `tests/unit/test_inc25_codeplane_runtime.py`（新）；前端 `RunListPanel.tsx::submit` 行为断言 | 提交 payload `"resources" not in ctx`；`declared_inputs == {}` |
| **AC-10** | 声明资源 ⇒ `declared_inputs` 逐字等于所声明，无多余键；`paths`/`repo_path` 步骤不再受阻 | 同上 | `declared_inputs == {"resources":[...]}`（无多余键）；相关步骤 `status != blocked`（或给真实执行结果） |
| **AC-11** | 代码任务前后源仓库 `git status` 一致 | `tests/unit/test_inc25_codeplane_isolation.py`（新）；B 档 e2e 复证 | 前后 `git status --porcelain` 字节相等 |
| **AC-12** | 工作区创建/回收记录 + 路径不在项目目录内 | 同上 | 生命周期记录存在；`not ws.path.is_relative_to(_ROOT)` |
| **AC-13** | 时间线项数 ≥ 关键步骤；默认不露原文，展开可见 | 后端：`test_inc25_codeplane_runtime.py`（timeline 的 `label`/`detail` 分离）；前端：testid 快照 + `tsc --noEmit` | timeline 非空且每项含 `label`；原始文本只在 `detail`；`code-trace` testid 存在 |
| **AC-14** | 未测量耗时「—」不出 `0 ms`；`blocked` 显示受阻且不计 failed | 后端：`test_inc25_codeplane_runtime.py`（`latency_ms is None`）；前端：`realRun.ts` 静态派生断言（**不新增 vitest**） | 未测量步骤 `latency_ms is None`；`blocked` 不计入 `errors`/`failed` |
| **AC-15** | Diff 按文件分组、与工作区真实改动逐行一致；无改动诚实空态 | `test_inc25_codeplane_runtime.py` + isolation | diff 与工作区 `git diff` 逐行一致；无改动 `degraded`/diff 为空态不伪造 |
| **AC-16** | 测试计数与真实输出一致；失败则结论非「已完成」；不可解析记未测量 | `tests/unit/test_inc25_codeplane_honesty.py`（新）+ `test_inc25_codeplane_runtime.py` | 计数 `==` 真实输出；失败 ⇒ 运行非 completed；不可解析 ⇒ `measured=False` |
| **AC-17** | 产出 Diff 后 `status=awaiting_approval`；目标分支工作区外未写入 | `test_inc25_codeplane_runtime.py` + isolation | 运行 `status == "awaiting_approval"`；源仓库工作树无变化 |
| **AC-18** | 仅三入口；各自状态变化；拒绝不留改动 | `tests/unit/test_inc25_codeplane_approval.py`（新） | 恰好 approve/reject/replan；各自产生可观测状态；reject 后无改动残留 |
| **AC-19** | 审批动作写审计，可按 run 检索 | 同上 | 每条审计可按 `run_id` 检索到 |
| **AC-20** | 批准后 artifacts 含代码产物；既有报告 artifacts 不回归 | `test_inc25_codeplane_approval.py` + `tests/unit/test_inc25_contracts.py`（新，回归） | `artifacts` ≥1 条代码产物（含 diff + 测试结论）；`report_markdown` 既有投影数量/内容不回归 |
| **AC-21** | 引擎不可用：非完成 + 显式降级 + 受影响步骤点名 | `tests/unit/test_inc25_codeplane_degrade.py`（新） | `degraded=="engine_unavailable"`；`affected_steps` 点名；运行非 completed |
| **AC-22** | 模型不可用：同上；确定性结果不包装成模型产出；未登记值中性 | 同上 | `degraded=="model_unavailable"`；`llm.degraded` 与 `codeplane.degraded` 各自如实；未登记值只中性说明 |
| **AC-23** | AC-2/AC-6/AC-9/AC-13/AC-17 在 A/B 两档判定一致；A 档不 5xx | A 档 + B 档各跑 `test_inc25_resources.py` / `test_inc25_codeplane_runtime.py` | 同一 AC 在两档判定一致（除真实内容差异）；A 档无 5xx |
| **AC-24** | 旧 testid `run-declare-table`/`run-declare-paths` 仍可寻址且行为不变；新 testid 不覆盖同名 | `tests/unit/test_inc25_contracts.py`（testid 快照）+ `tsc --noEmit` | 两旧 testid 仍在且 `submit` 空不传键语义不变；无同名覆盖 |

---

## 5. 待创建测试文件清单（S4 交付物）

| 文件 | 覆盖 | 档位 |
|---|---|---|
| `tests/unit/test_inc25_resources.py` | AC-2/3/4/5/6/7/8（`force_memory_backend`） | A（PG 复证） |
| `tests/unit/test_inc25_resources_api.py` | AC-1（五入口契约） | A |
| `tests/unit/test_inc25_codeplane_runtime.py` | AC-9/10/13/14/15/16/17（注入假 runner） | A |
| `tests/unit/test_inc25_codeplane_honesty.py` | **D2** 诚实性 + 截断逐字 | A |
| `tests/unit/test_inc25_codeplane_degrade.py` | **D3** 六降级值逐字 | A |
| `tests/unit/test_inc25_codeplane_isolation.py` | **D4** 隔离（AC-11/12/15/17） | A |
| `tests/unit/test_inc25_codeplane_approval.py` | **D5** AC-18/19/20 + 审计 | A |
| `tests/unit/test_inc25_rbac.py` | **D7** AC-7（viewer 不可 approve） | A |
| `tests/unit/test_inc25_contracts.py` | AC-20 回归 / AC-24 testid 快照 / drift（`forgeflow.*` 不 import `openhands.*`） | A |
| `tests/integration/test_inc25_codeplane_http.py` | **D6** AC-1/C 面证据可见性 + 老记录不 500 | A（+ PG 复证） |
| `tests/integration/test_inc25_codeplane_reliability.py` | **D1** 真实模型 N≥5（`@pytest.mark.slow`） | **B（待放行）** |

> 现成可复用：`tests/unit/test_inc25_codeplane_payload_bound.py`（截断存活）、`tests/unit/test_inc25_runner_agent_config.py`（LLM 三守卫）、`tests/unit/test_inc25_codeplane_commit_idempotent.py`（approve→commit 幂等 + 反事实）、`tests/integration/test_offline_boot.py`（A 档离线）。

---

## 6. 执行顺序与门禁（最多 2 轮）

1. **Round 1（A 档，离线，须全绿）**：先跑既有回归 + 全部新增 A 档文件，以 junit 为准。
   - 绿 ⇒ 进入 Round 2；红 ⇒ 按 Smart Routing 判定（源码 bug → 工程师；测试 bug → QA 自修）。
2. **Round 2（B 档，真实，待放行）**：PG 复证 + D1 可靠性 N≥5 + 端到端证据可见性/RBAC。
   - 绿 ⇒ 出最终报告；仍有红 ⇒ **立即退出**，把残留写入「Known Issues」，**不进入 Round 3**。
3. 每轮产出：junit XML 路径 + `tests/failures/errors/skipped` 摘要 + 反事实证据。

---

## 7. 我认为最可能抓出问题的 3 条（Top-3 预警）

1. **D1 重复可靠性（真实模型）** —— 已知 qwen3:8b 默认温度 2/3 失败、软超时→重试环（实测重试 15 次 + `Stuck pattern detected`）。即便 `temperature=0` 也仅实测 3/3，**N≥5 会暴露尾部不稳定**；且「STUCK 但已改对代码」会诱使误判，正是本计划第 1 条纪律的靶心。
2. **D3 降级逐字（尤其 `max_rounds`）** —— P0-C 刚把 `max_rounds` 登记为一等 `degraded` 值；最易出现的偏差是「回合耗尽却渲染成完成」。逐字断言（值 + reason 原文 + 运行非 completed）是最可能抓到假绿的一环。
3. **D4/D5 隔离与未批准不写分支** —— 工作区一旦误根到项目目录、或 approve 前误写目标分支，破坏性最大且最难回滚；用「前后 `git status --porcelain` 字节快照 + `HEAD` 快照」做机械判据，抓漏率高。

---

## 8. 已知风险 / 未决 / 非目标

- **Ollama 争用**：D1 与工程师的可靠性跑共享同一台 Ollama，**必须串行**，否则互相污染。
- **前端验证手段受限**：设计明令**禁止新增前端单测设施**（无 vitest/jsdom）；AC-13/14/24 的前端面以 **testid 快照 + `tsc --noEmit` + 既有 `realRun.ts` 派生函数静态断言 + 构建后实测**为证据，不新增测试框架。
- **A 档数据库资源**：`data.query` 是永久 development stub；资源摘要层**绝不**为数据库资源捏造行数/字段。
- **B 档解释器可达性**：代码任务需 OpenHands venv；不可达时为 `engine_unavailable` 结构化降级（预期非崩溃）。
- **非目标**：不做自动 merge / 自动 PR（P2）；不改既有四层契约与 `report.render` 语义。
