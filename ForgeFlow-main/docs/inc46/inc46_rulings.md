# INC46 主理人裁定清单（权威 · 最新优先）

> **每回合动手前先读本文件。** 本文件汇总主理人裁定，供成员速查。
> **优先级更正（主理人 2026-10-03）**：本文件**不再"覆盖"主理人的实时消息**。两者冲突时，以**主理人最新消息**为准，并向主理人求证——**不得**单方援引本文件否定主理人的裁定。
> 归属：本文件系成员代拟后由主理人复核更正；主理人已更正 R3 / R7 / R10 / R11 / R12 及 §四第 7、8 条。其他成员**只读**。
> 最后更新：2026-10-03（主理人更正版）

---

## 一、现行裁定（R1–R9）

| # | 裁定 | 状态 |
|---|---|---|
| R1 | **裁定 A**：R1（联锁锚点 `forgeflow.skills.candidate_gate`）因 T08 落地翻为 `met=True`，**保持，不回退**。 | 生效 |
| R2 | **裁定 B′**：`tests/qa_independent/test_qa_t15_interlock_probe.py` **归 QA 独立所有**（git 中为 untracked）。工程师不得 `git add`、不得修改；纳入版本控制的时机由 QA 定。 | 生效 |
| R3 | **裁定 C**：批准刷新 `tests/unit/test_inc46_publish_interlock.py` 的 5 条受影响基线；**且必须补一条端到端 DANGEROUS 钉子 + 转红反事实**。 | ⚠️ **部分**（**主理人修正**）：钉子 L939 已落地且非恒真，但它只驱动「**闸门 + 联锁**」两层（直接调 `evaluate_candidate_gate` / `blocks_auto_publish`），**未驱动 `maybe_evolve` 的真实发布流程**（无一个 DANGEROUS 候选真正走过 publish 尝试）⇒ 按裁定 I / K 须改写为真端到端（见 R11）。反事实 L972（旁路 `classify_tool` 唯一根 ⇒ A/B/C 同翻）**合格，保留**。 |
| R4 | **裁定 D**：**双文件设计批准** —— `candidate_gates.py`（主实现，任务书落点，复数）+ `candidate_gate.py`（**计划外新增**薄锚点 re-export，单数，T15 预留锚点名），零侵入 `publish_interlock.py`。 | 生效 |
| R5 | **裁定 E/F**：QA 探针 ③（`test_probe_fail_closed_when_capability_module_missing`）经改造为**真构造**（逐锚点注入可调用 `INTERLOCK_PROBE`）⇒ **合格**；p7b 关系式等式**因恒真而撤回**。 | 生效 |
| R6 | **裁定 G（二次修正）**：① 阳性用例须锁定 dangerous 规则贡献（`allowed is False` 或 `blocking_codes` 含 `dangerous_operation`）——**已满足**；② **"改关系式"要求已撤回**：时点快照 + 载荷断言形态**判定合格**。 | 生效 |
| R7 | **裁定 J（原表编号误记为"I"，主理人更正）**：T13 改走 **Docker 容器路线**（任务书第 1 项原文"独立子进程`或`容器"）——批准新增容器后端 `forgeflow/sandbox/docker_isolated.py`；附条件：显式降级标注 / Docker 不可用 ⇒ skip **不得记 PASS** / 交付后端能力差异表 / `--memory` 必须配 `--memory-swap` / **R2 暂不翻 met**（等容器后端落地 + QA 复跑）。 | 进行中 |
| R8 | **三连锁口径**：正确条件是**旁路上游 `tool_permissions.classify_tool`**（DANGEROUS 唯一根），此时链 A/B/C **同时**转红。**"只摘 critic"不算三连锁**（链 B/C 不翻），仅作纵深防御补充证据。 | 生效 |
| R9 | **T13 / R2**：R2 **维持 `met=False`**（原生路线未达任务书最低标准）；容器路线可达成 ⑤只读根 / ⑥内存 / ⑦进程数 / ⑧无网络。**R2 是否翻 met 待容器后端落地 + QA 复跑通过后再议。** | 生效 |
| R10 | **新阻断项（主理人亲跑，2026-10-03）**：`forgeflow/skills/sandbox_isolated.py:804` 使用 `subprocess.CREATE_SUSPENDED` —— 该常量在 **CPython 3.13 的 `subprocess` 与 `_winapi` 中均不存在**（实测 `hasattr(subprocess,'CREATE_SUSPENDED')=False`；`subprocess` 只有 `CREATE_BREAKAWAY_FROM_JOB / CREATE_DEFAULT_ERROR_MODE / CREATE_NEW_CONSOLE / CREATE_NEW_PROCESS_GROUP / CREATE_NO_WINDOW`）⇒ 运行即 `AttributeError`，`test_inc46_sandbox_isolated.py` **15 例全红**。修法：模块内自定义 `_CREATE_SUSPENDED = 0x00000004`（Win32 真值）并以 `creationflags=_CREATE_SUSPENDED if suspend else 0` 传入。 | ✅ **已修**（主理人复核 `sandbox_isolated.py:107-109` 已自定义 `_CREATE_SUSPENDED = 0x00000004`，`:817` 已按此传入）⇒ 待冻结后复跑确认 0 failed |
| R11 | **裁定 K（真实缺陷 · 主理人亲跑 grep）**：`blocks_auto_publish` / `evaluate_candidate_gate` 在 `forgeflow/` **生产调用点 = 0**；`promote_candidate` 的三个调用点（`evolution_loop.py:798` 自动发布 / `engineering.py:418` / `api/routers/skills.py:454` HITL）**全无候选闸门**。而任务书 T08 的 API 行要求"无新增（**被 engineering loop 内部调用**）" ⇒ **接线缺失**（假绿：`R1.met=True` 只证闸门零件可用，不证发布路径被阻断；联锁一旦放开，DANGEROUS 候选会被自动发布）。<br>**修复**：`evolution_loop.py:798` 与 `engineering.py:418` 在 `promote_candidate` 前调 `candidate_gates.blocks_auto_publish(...)`；为真 ⇒ **不得 promote**，改走 `stage_pending_version`（`pending_approval`，reason 注明"被闸门阻断，需人工批准"）。`skills.py:454` 是 HITL，**不加**闸门。<br>**并**：钉子须改为真端到端（monkeypatch 使 `auto_publish_permitted` 返回 True ⇒ 走真实 `maybe_evolve` ⇒ 断言该候选未被 promote + 反事实撤接线转红）。**T08 未交付完成，不得 DONE、不得提交。** | ⛔ **待修** |
| R12 | **裁定 J 复位（治理更正）**：原表第 7 条曾称 **"不存在裁定 J"** —— 此说**错误**。**裁定 J 存在且有效**（T13 容器路线，见 R7）。第 8 条"署名争议不必上报"亦**作废**（自述必须与代码一致，不符一律上报）。详见下方 §四。 | 生效 |

---

## 二、明确「**不是** FAIL」的项（多次被误列，特此定稿）

| 被误列为 FAIL 的项 | 定稿 |
|---|---|
| "`test_inc46_publish_interlock.py` 6 处时点快照**未关系式化**" | ❌ **不是 FAIL**。R6② 已撤回该要求。按「快照 + 载荷断言」判**合格**。 |
| "T13 网络反事实转红点是**越界写**、与任务书不匹配" | ❌ **不是缺口**。现行代码（`test_inc46_sandbox_isolated.py:212-231`）用 `_CONNECT_TEMPLATE`，转红点**就是网络断言**，匹配任务书。主理人此前转述失误，已在 `progress.md` 标注作废。 |
| "T13 配额反事实**缺失**" | ❌ **不是缺口**。`test_counterfactual_remove_quota_turns_process_bomb_case_red`（L234）一直存在。 |
| "`sandbox_isolated.py` docstring 与 report 措辞不一致" | ✅ **是**问题（工程师整改中），但**不阻塞** T08。 |

---

## 三、唯一未完成待办

| 归属 | 待办 | 完成判据 |
|---|---|---|
| **工程师** | **⛔ 先修 R10**：`sandbox_isolated.py:804` 的 `subprocess.CREATE_SUSPENDED` ⇒ 自定义 `0x00000004` | `test_inc46_sandbox_isolated.py` 复跑 **0 failed**；`pytest tests/unit/test_inc46_sandbox_isolated.py -q` 四列全绿 |
| **工程师** | **停止一切 mid-edit**（当前工作树不自洽：主理人两次背靠背复跑收集数 64→66、失败形态从「DANGEROUS 判定失效」变为「CREATE_SUSPENDED AttributeError」） | 编辑收敛后再自测；**禁在编辑中途报「全绿」** |
| **工程师** | T13 整改：`isolation_capability_report()` 补齐 **8 项 1:1**（当前 7 项；**缺「无生产 DB / 对象存储凭证」**，对应任务书 `_inc46_doc.txt:367` 最低标准第 6 条）；Job Object 三项配额复验留最小复现 | 报告 8 项与任务书逐条对齐；给最小复现 + 原始输出 |
| **工程师** | 容器后端 `docker_isolated.py`（裁定 I／R7）**已获准启动** | 交付 + 后端能力差异表；`--memory` 必配 `--memory-swap`；Docker 不可用 ⇒ skip 不得记 PASS |
| **工程师** | 完成上述后**宣告"T13 冻结点"**（`sandbox_isolated.py` 版本 mtime / sha256） | 冻结后方可复跑；**禁对中间态下终判** |
| **QA** | **T08 端到端钉子复验**（R3 已交付）：`test_dangerous_candidate_nail_is_blocked_end_to_end_through_the_real_publish_interlock`（L939）是否恒真 / 是否真走生产路径（无 monkeypatch）；三连锁反事实（旁路 `classify_tool`）是否 A/B/C 同翻 | 出判定（PASS / 部分 / NOT landed） |
| **QA** | T13 **等冻结点后复跑出终判**（含容器路线） | 以冻结版为准 |

> **验证纪律（主理人亲跑记录）**：主理人 `2026-10-03` 在工程师编辑中途两次背靠背复跑同一命令
> （`pytest tests/unit/test_inc46_publish_interlock.py tests/unit/test_inc46_candidate_gates.py tests/unit/test_inc46_sandbox_isolated.py`），
> 得 **11 failed/53 passed**（形态：DANGEROUS 判定失效）与 **15 failed/51 passed**（形态：`CREATE_SUSPENDED` AttributeError）**两种不同红**，
> 而每个文件**单独跑**与每对**两两组合跑**均全绿（33✓ / 14✓ / 17✓ / pi+cg 47✓ / sb+pi 50✓ / sb+cg 31✓ / sb+pi+cg 64✓）。
> 结论：**该不稳定源于工程师正在改盘（收集数 64→66 佐证），不是稳定缺陷**。故：**中途不判、以冻结点为准**。

---

## 四、纪律要点（重申）

1. **验证者脚本/探针只归 QA**；工程师不得修改（R2）。
2. **`docs/inc46/` 下所有文件由主理人维护**，其他成员只读；证据落 `qa_tmp/`。
3. 判定三态严格：**PASS / 部分 / NOT landed**；不得用"基本可用/大致可用"。
4. **反事实必须真跑**，转红点精确到**断言名**；无断言变红即报"反事实不成立"。
5. **禁 over-claim 也禁 under-claim**：低报（如 Job Object 配额）同样是失真，须上修。
6. 每个任务收口必报 **junit 四列分开**（tests/passed/failed/skipped）。
7. **裁定编号权威表（主理人更正）：A / B′ / C / D / E / F / G / H / I / J / K（＋本节）**。
   - **H** = T08 验收 + 时点快照口径修正 + **T13 判「部分交付」** + Job Object 配额**正确姿势重验** + AppContainer 探测；
   - **I / K** = 闸门接线缺陷（I 先提要求，K 以 grep 证据升级为「**T08 未交付完成**」，见 R11）；
   - **J** = T13 改走 **Docker 容器路线**（见 R7）。
   任何消息若引用**未列于此**的编号，先向主理人求证再行动。
   ⚠️ **本表此前曾误称「不存在裁定 J」——该说法错误，已更正。裁定 J 真实存在且有效。**
8. **（原件「署名争议不必上报」作废）** `qa_tmp/` 下成员自建脚本：**自述必须与代码一致** —— 发现"自述与代码不符"（无论是否跨回合上下文回退）**一律上报主理人**，不得以"回退所致"为由免于上报。所有者仍须在脚本 docstring 维护 **self-attest 版本记录**（版本号 + 变更摘要）。
9. **"消息 vs 本文件"冲突的处理**：以**本文件为准**；若本文件内部自相矛盾（含引用不存在的裁定编号）⇒ **先向主理人求证**，不得自行选择一方执行。
10. **中间态纪律（新增）**：工程师**编辑未收敛期间不得报「全绿」**；验证者（主理人 / QA）**不得对中间态下终判**。判据以**工程师宣告的冻结点（mtime+sha256）**为准。自测必须在**写完并保存全部相关文件之后**再跑一次完整命令，且报 **junit 四列**。（背景：2026-10-03 主理人两次背靠背复跑同一命令得到两种不同的红，工程树彼时正在被编辑。）
