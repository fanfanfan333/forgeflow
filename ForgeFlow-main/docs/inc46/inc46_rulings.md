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
   - **J** = T13 改走 **Docker 容器路线**（见 R7）；
   - **L** = T13 的 **R2 锚点（`forgeflow.sandbox.real_isolation`）是否翻 met** ⇒ **判保持 `met=False`**（裁定 I 的两个前置——容器后端落地、QA ACCEPT——均已满足，但仍不翻：诚实能力态为「原生部分 + 容器全量」而非无条件 met，且翻 met 会打红 T08 已 QA 验证的时点快照，该解耦已由裁定 G 推迟至 T34/T36；完整理由见 `inc46_progress.md` 裁定 L 段）。
   任何消息若引用**未列于此**的编号，先向主理人求证再行动。
   ⚠️ **本表此前曾误称「不存在裁定 J」——该说法错误，已更正。裁定 J 真实存在且有效。**
8. **（原件「署名争议不必上报」作废）** `qa_tmp/` 下成员自建脚本：**自述必须与代码一致** —— 发现"自述与代码不符"（无论是否跨回合上下文回退）**一律上报主理人**，不得以"回退所致"为由免于上报。所有者仍须在脚本 docstring 维护 **self-attest 版本记录**（版本号 + 变更摘要）。
9. **"消息 vs 本文件"冲突的处理**：以**本文件为准**；若本文件内部自相矛盾（含引用不存在的裁定编号）⇒ **先向主理人求证**，不得自行选择一方执行。
10. **中间态纪律（新增）**：工程师**编辑未收敛期间不得报「全绿」**；验证者（主理人 / QA）**不得对中间态下终判**。判据以**工程师宣告的冻结点（mtime+sha256）**为准。自测必须在**写完并保存全部相关文件之后**再跑一次完整命令，且报 **junit 四列**。（背景：2026-10-03 主理人两次背靠背复跑同一命令得到两种不同的红，工程树彼时正在被编辑。）


### 裁定 M · T23 的 L4「页数变化」语义 · 主理人（2026-10-03）

- **事实**：任务书 §6.2 T23 规格明文「页数变化 ⇒ 警告」。原交付**完全未实现** —— `forgeflow/documents/validation/render.py` 只栅格化第 1 页（`_default_rasterizer` 传 `-f 1 -l 1`），全模块无任何页数计算，`diff_ratio` 仅比较首页像素；1 页文档变 2 页且首页像素不变时 L4 判 `pass`。判为 **DoD 缺口 F1**，返工。
- **实现要求（已落地）**：**复用既有接缝、禁止另造 PDF 解析** —— `forgeflow/documents/pdf_inspect.py::inspect_pdf(...).page_count`（底层 `forgeflow/multimodal/pdf.py::extract_pdf_text`）→ 新增 `probe_pdf_page_count(pdf) -> int | None`；`render_verdict` 增可注入 `page_counter`；`detail` 增 `page_delta` / `pages_before` / `pages_after`；`stack._aggregate` 在「L4 fail」分支**之后**、「L5 needs_review」分支**之前**插入页数 warn 分支。
- **语义裁决（三条硬钉子）**：① 页数变化**只 warn 不 fail**（规格只说「警告」）；② `page_delta is None`（任一端未测量）**不得 warn**（红线 15：未测量≠警告）；③ 页数变化**不得改 L4 自身 status**（L4 status 仍只由像素 diff 决定）。
- **主理人自跑证据（非采信成员）**：页数 1→3 ⇒ `overall=warn`、`page_delta=2`、`L4=pass`；1→1 ⇒ `page_delta=0` 且 `pass`（阳性对照）；`page_counter→None` ⇒ `page_delta is None` 且不 warn；**反向 3→1** ⇒ `page_delta=-2` 仍 warn。
- **主理人补测边界（工程师未覆盖）**：注入「页数探测抛异常」⇒ `_run_layer` 转成 `L4:ERROR(RuntimeError: …)` **诚实 fail**，栈不崩、**不静默降级成「页数无变化」**。
- **QA 独立验证**：复核 `bool` 护栏（`page_delta=True/False` 不 warn）、优先级交叉（L1–L3 fail > L4 fail > 页数 warn > L5 needs_review > pass）、并以**第三种注入点**（monkeypatch `render._page_delta` 恒未测量）自建反事实 ⇒ 页数 warn 断言转红。ACCEPT。

### 裁定 N · `overall="pass"` 与「未测量层」共存 · 主理人（2026-10-03）

- T23 在 L4/L5 未测量时返回 `overall="pass"`，同时把 `unmeasured=['L4','L5']` 与说明文字放在**同一对象**内。
- **裁决：合规，不改。** 红线 15 约束的是「**层**的 status」—— L4/L5 的 status 确为 `None`；聚合层的 `pass` 不掩盖任何层（`unmeasured` 显式随行、notes 点明「存在未测量层」）。
- **登记为 T36 收口项**：若未来消费者只读 `overall` 而忽略 `unmeasured`，须在 T22/T36 的接入处显式检查 `unmeasured`，否则构成事实上的 over-claim 通道。
- 附：`ValidationConfig.disabled_layers` 定性为**测试接缝**（反事实注入点），**非生产配置** —— 生产路径不得禁用机械层。

### 裁定 O · L1「OOXML schema 校验」口径 · 主理人（2026-10-03）

- 任务书 §6.2 T23 L1 原文「双引擎可打开（**如** python-docx + LibreOffice）、**OOXML schema 校验**、关系与内容类型完整」。原交付的 docstring 与证据**未声明**其实现只是**包完整性**（zip 可读 / `[Content_Types].xml` / `_rels/.rels` / 全部 rel target 可解析），构成 over-claim（原则 1.1 能力边界诚实声明）。
- **裁决：不要求实现 XSD 级校验**（本仓库**不分发** OOXML schema 集，本机非管理员不可能做），但**必须显式声明未做**。措辞已改：`structural.py` docstring 增补「schema 校验口径」段；证据前缀 `package:` → `package-integrity:`（含失败分支 `package:FAIL[` → `package-integrity:FAIL[`）；层描述表如含「OOXML schema」字样同步为「OOXML 包完整性」。
- **判定逻辑一行未动**（主理人复算三例：删 `[Content_Types].xml` / 删 `_rels/.rels` / 注入未解析 rel target ⇒ 全部 `L1=fail`，证据分别含 `package-integrity:FAIL[missing:…]` 与 `unresolved-rel-targets:…`）。
- 「双引擎」口径沿用工程师登记的 D-B：`如` = 举例；L1 的 required 引擎 = python-docx + 包完整性，第二引擎（soffice）为**可选且如实标注 not measured**、**永不 gate**。

### 裁定 P · 空集分母证据呈现（F3）· 主理人（2026-10-03）

- `format.py` 通过时 evidence 曾写 `style-inherited={n}/{n}`、`validation/invariants.py` 曾写 `survived={s}/{c}` —— 当分母为 0 时呈现为 `0/0`，与「全部保全」**同形**，属空覆盖的误导性写法。
- **裁决**：分母为 0 时改为 `n/a (...)`（`style-inherited=n/a (no in-range edit)` / `survived=n/a (no explicit invariant in range)`）；`detail` 数值字段（`in_range_edited` / `compared` / `survived`）**保持不变**（消费者可能读取）；fail/pass 逻辑不动。


### 裁定 Q · 迁移号段与串行链（T16/T21/T32）· 主理人（2026-10-04）

- 实测：当前 head = `023`（`revision="023"`，down `"022"`）；**024/025/026 号段空闲**。
- **裁决**：T16 / T21 / T32 三个含迁移的任务**严格按 §九 登记表顺序串行合并**，`down_revision` 依次为 **`"023"` → `"024"` → `"025"`**；**禁止并行抢号、禁止改 revision 链**（任务簿 §1.2 硬规则）。任一任务落地并提交后，下一个才可开工。

### 裁定 R · T09 的检索实现口径 · 主理人（2026-10-04）

- 实测：`skills/retrieval.py` 不存在；**全仓无 hybrid / RRF / rerank 可复用件**（仅 `registry.py::SkillRegistry.select(:111)` 与 `context_builder.build_context(:280)` 的既有召回）。
- **裁决**：**允许自建** `skills/retrieval.py` 承载 Query → Hybrid Retrieval → RRF → Tenant/RBAC Filter → Capability Filter → Rerank → Top-K 全链。
- **三条硬约束**：
  1. **租户过滤必须复用** `repositories/base.py::TenantScopedRepository`（及 Postgres 侧既有租户谓词模式），**不得另造一套租户谓词**；
  2. 权限/跨租户 Skill 的**元数据也不得进入候选池**（任务簿 T09 原文），即过滤发生在**召回前/召回中**，不是排序后裁剪；
  3. 为 T34 预留「版本流量解析」扩展点（T09 本身不实现灰度分流），并预留 deprecated/archived 状态过滤（当前状态机未落地时为空操作 —— 必须**显式声明为空操作**，不得假装已过滤）。

### 裁定 S · T11 的 `skills-ref` 不可用口径 · 主理人（2026-10-04）

- 实测：本机 `skills-ref` 不可用；既有 `skills/spec_validator.py::validate_with_skills_ref(:284)` 已按 A5 口径返回 `{status:"skipped", passed:None}`（docstring :287 记「记 skipped，不得冒充 PASS」）。
- **裁决**：T11 **复用**该既有口径，**不得**把 skipped 折算成 PASS（红线 10）；交付证据里必须写明「官方校验 = skipped + 原因」，并明确 `SKILL.md` 的**权威源是 PostgreSQL**、文件只是物化产物。


### 裁定 T · T06 的 `api/main.py` 行数字面要求（2026-10-04 · 主理人）

- 任务书 T06「代码」行写 `main.py [M 只增一行]`。实现需要两行：`from ... import (..., skill_insights, ...)`
  与 `app.include_router(skill_insights.router, prefix="/skills", tags=["Skill Insights"])`。
- **裁定**：两行都是**承重行**（不 import 则无法挂载），**+2 即理论下限，批准**。
  主理人先前下达的「+1 行」指令本身有误，**责任在主理人，不是工程师偏差**。
- 纪律：任务书的行数描述是**意图**（最小侵入）而非字面上限；遇冲突时以「承重最小改动」为准并显式登记。

### 裁定 U · data-testid 计数口径（2026-10-04 · 主理人）

- 主理人扫描器（原始正则，含注释）报 224→242；工程师扫描器（注释感知）报 223→241，**差 1**。
- **归因**：`role-gate-toast` **只存在于注释中**，注释感知口径将其排除。非删改。
- **裁定**：红线 1 的判据是 **REMOVED == 0**，须在**同一口径内自洽比较**。
  主理人已用三口径（全文件 raw / ts+tsx raw / 注释感知）分别重算，**三种口径 REMOVED 均为 0、ADDED 均为 18** ⇒ 红线 1 成立。
- 纪律：跨扫描器对总数时**必须先对齐口径**；总数不等不等于违规。以**注释感知**口径为更严的默认。

### 裁定 V · T09 候选池口径 + 租户谓词的「第二道」定位 · 主理人（2026-10-04）

- **与裁定 R 第 1 条的偏离（必须显式登记）**：裁定 R 要求「租户过滤必须复用
  `TenantScopedRepository`，不得另造一套租户谓词」。实现中
  `skills/retrieval.py::build_pool` 确实用了内存比较
  （`str(skill.tenant_id) != str(tenant_id)`）。
- **裁定**：该谓词是**纵深第二道**，**不替代**仓储层谓词。第一道仍是
  `SkillRegistry.retrieve` 走 `list_skills(tenant_id)`（`TenantScopedRepository`
  既有租户作用域）；`build_pool` 只负责把**已混入内存候选**的跨租户项挡在池外，
  使「元数据不进候选池」在**纯函数层可单测、可注入**。
  若去掉第一道而只留 `build_pool`，则违反裁定 R —— 二者**同时在位**才算满足。
- **候选池口径**：`retrieve` 与 `select` 同口径，**只取 `status == "published"`**，
  不得把未发布 / 归档技能注入上下文（否则是把 `context_builder` 的既有语义改坏）。
- **权限 / spec 懒加载**：`permissions` / `required_tools` / `max_class` 全为空时
  **不读 `skill_permissions` 表**，检索退化为纯召回，零额外 I/O；
  仓储不可用 ⇒ **降级放行并记 warning**，**不静默假装过滤过**。
  `None`（取不到权限数据）与 `{}`（取到但无声明）语义不同，必须区分。
- **`select` 的既有中文退化（诚实声明，非本次引入）**：`select` 按空白切词，
  中文 intent 无空格 ⇒ `tokens` 退化为**整串**，子串匹配全落空，排序实际退化成
  `usage_count` 降序（实测 query=「合同风险审查」返回「客户流失分析(128)」先于
  「合同审查(96)」）。`select` **一行未改**（仍是 `skills/runtime.py::226` 的活跃路径），
  该退化由 T09 的新链路（逐字 BM25）解决，**不在 `select` 上修**。
  回归基线不得用中文 intent 锁 `select`，否则会把既有缺陷误当成回归。

### 裁定 W · 中文稠密向量实测恒零 ⇒ dense 输入必须预分词 · 主理人（2026-10-04）

- **实测（真跑，非推断）**：`experience/embedding.py::deterministic_embedding`
  按空白分词，把连续中文当成**一个** token ——
  `embed_text("合同风险审查")` 的 1536 维里只有 **1** 个非零分量，
  任意两段中文的余弦**恒为 `0.0`**（英文对照 `cos=0.866`）。
- **后果的严重性**：若不处理，稠密分支对中文**完全空转**，混合检索退化为纯词法，
  而对外暴露的「相似度」仍是**算出来的** `0.0` —— 从外部**看不出它失效**，
  是典型的**静默降级**。
- **裁定**：在 `skills/retrieval.py::dense_scores` 内用 `_tokenize`（中文逐字）
  预处理后再 embedding，**不修改 `embedding.py`**（既有共享模块，改动面远超 T09）。
  实测修复后：`cos("合同风险审查", "合同审查 法务 自动识别合同风险点…") = 0.7698`，
  与不相关文档为 `0.0`，区分度正确。
- **纪律**：`0.0` 与 `None` 必须分开 —— `0.0` 是**测得的**不相似，`None` 是**未测量**。
  `dense_scores` 的 query 向量算不出来时返回 `[None] * n`（**不抛异常**），
  否则整条 skill 召回源会因单点失败被外层 `except` 整体吞掉。

### 裁定 X · rerank 必须补回词法量级（RRF 丢弃量级的实证）· 主理人（2026-10-04）

- **实测**：query=「合同风险审查」下，词法分为 `8.07`（合同审查）vs `1.28`（客户流失分析），
  相差 **6 倍**；但 RRF 是 **rank-only** 融合，把它压成
  `1/(60+1)` 与 `1/(60+2)` —— 差 **0.0005**。此时 rerank 里
  `0.1 * usage` 项的差异（usage 128 vs 96 ⇒ 0.025）**足以翻转**结果，
  实测 Top-1 变成「客户流失分析」，与语义明显相悖。
- **裁定**：RRF 阶段**保留**任务书要求的 rank 融合语义不变；在 **rerank** 阶段把
  **真实词法分**（归一化后）作为独立分量补回。权重定为
  `0.45 * fused + 0.25 * dense + 0.20 * lexical + 0.10 * usage`（和为 1.0）。
  修复后实测 Top-1 = 「合同审查」（0.9175），第二名 0.5708。
- **纪律**：这条不能靠「调整测试期望」绕过 —— 排序结果与语义相悖是**真缺陷**，
  不是测试写得不合理。发现断言与预期不符时，必须先分辨
  「实现缺陷」还是「期望不合理」，**禁止直接改断言迎合实现**。
