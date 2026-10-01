# INC39 QA 报告 —— 结果层「Agent 对话式完成态」（纯前端）

- 验收人：严过关（QA Engineer）
- 被验收人：寇豆码（Engineer，自报 `IS_PASS: YES`）
- 仓库根：`D:\Agentxm\Multi-Agent\ForgeFlow-main`（前端 `frontend/`，Vite + React + TS）
- 验收日期：2026-10-01
- 验收方式：**独立撰写新 e2e 钉子 + 自建视觉抽检脚本 + 全量回归**，全部「自己写、自己跑、自己读原始产物」；
  未采信工程师的自测脚本结论（其 `qa_tmp/inc39_*_check.cjs` 仅作对照，不作证据）。

---

## 0. 结论（智能路由判定）

| 项 | 结果 |
|---|---|
| 路由判定 | **NoOne（全绿，无需打回工程师，无测试缺陷遗留）** |
| 源码 Bug | **0 个** |
| 测试自身缺陷 | **2 个（均 QA 自修，非源码问题）**：① 首轮新钉子 ⑥ 误把「段⑥ 默认折叠」的诚实空态断言成 `visible`；② 主理人复核发现的**真空断言** `result-ctx-region`（该 testid 在 src 中不存在 ⇒ 恒为 0 的假绿）→ 详见 **§8 复核整改记录** |
| 新增 e2e 钉子 | `frontend/e2e/inc39_agent_completion.spec.ts`，**12 个用例** |
| 全量 e2e 四列 | tests **63** / passed **63** / failed **0** / skipped **0**（判据：`test-results/.last-run.json`） |
| 复核整改 | 主理人复核指出 1 处真空断言 → 判**测试缺陷** → QA 自修 + 全 spec testid 普查；整改后**单 spec 12/12、全量 63/63** 仍全绿（详见 §8） |
| `tsc -b` | exit **0**（无输出） |
| `vite build` | exit **0**，`✓ built in 8.74s` |
| 视觉抽检 | 明暗两档 `.res-env` 全部文本对比度 **≥ 5.28:1**（阈值 4.5:1）；新块横向溢出 **0 命中**（各档扫描 19 个元素） |

---

## 1. 交付物清单

| 交付物 | 路径 |
|---|---|
| 新 e2e 钉子（12 用例） | `frontend/e2e/inc39_agent_completion.spec.ts` |
| 视觉抽检脚本 | `frontend/qa_tmp/inc39_visual_check.cjs`（复用既有 `D:/Temp/_contrast_audit2.cjs` 的 canvas 对比度内核） |
| 视觉抽检原始报告 | `frontend/qa_tmp/inc39_visual_report.json` + `qa_tmp/inc39_visual.txt` |
| **复核用 testid 普查脚本** | `frontend/qa_tmp/inc39_testid_audit.cjs`（+ 原始输出 `qa_tmp/inc39_testid_audit.txt`） |
| **复核整改重跑原始日志** | `frontend/qa_tmp/inc39_rerun.log`（build → 单 spec → 全量 e2e + 两轮 `.last-run.json` mtime） |
| e2e 原始输出 | `frontend/qa_tmp/inc39_e2e_new.txt`（单 spec）、`qa_tmp/inc39_e2e_full.txt`（全量） |
| `vite build` 原始输出 | `qa_tmp/inc39_build.txt`（`tsc -b` 无任何输出，仅 `TSC_EXIT=0`） |
| 本报告 | `docs/sop/INC39-QA-REPORT.md` |

> 未提交 git（遵主理人指示）。未改动任何后端文件。

---

## 2. 环境与方法（含**偏差声明**）

### 2.1 运行环境铁律适配（如实声明，不影响结论效力）

1. `npm run` 在本机被拦 → 全程走 `node node_modules/...` 直接路径。
2. `playwright.config.ts::webServer.command` 是 `npm run build && npm run preview -- ...`；
   该脚本内部为 `tsc -b && vite build`，而 **`tsc` 不在 PATH**（原始错误：
   `[WebServer] 'tsc' 不是内部或外部命令` ⇒ `Error: Process from config.webServer was not able to start. Exit code: 1`）。
   **故无法使用仓库默认 webServer。**
3. 替代口径（**只改「谁来起服务器」，不改任何断言/测试范围**）：
   - 先手动构建 dist：`node node_modules/vite/bin/vite.js build`（**每次 e2e 前都重建**，杜绝 `reuseExistingServer` 复用旧 dist 的假结果）；
   - 再于**同一条 bash 进程树内**自起 `node node_modules/vite/bin/vite.js preview --port 4173 --strictPort`，
     经就绪探针（`http://localhost:4173/` HTTP 200）确认后，让 Playwright 的 `reuseExistingServer`（未设 CI ⇒ `true`）**复用**它；
   - 运行 `node node_modules/@playwright/test/cli.js test`，`testDir`/`reporter`/`projects` 全部沿用仓库 `playwright.config.ts`，未覆盖。
   - 结果：`.last-run.json` 的 mtime 在跑测后**前进**（`13:00:51.673Z → 13:01:41.116Z`），证明新一轮真的跑过（非旧结果）。

### 2.2 判红绿的唯一判据

`frontend/test-results/.last-run.json`（看 mtime + `failedTests`），**未**使用 per-test 目录残留。
本次全量回归后：

```json
{
  "status": "passed",
  "failedTests": []
}
```

### 2.3 禁用项

未使用 `toHaveScreenshot`（本轮禁用）。e2e 一律在 `/api/**` 边界打桩（stub 后端），
骨架与 payload 构造函数**照抄** `e2e/inc36_conversation.spec.ts`，未另造一套。

### 2.4 反证纪律

每条「不存在」断言都配**阳性对照**（`expectAbsentWithControl`：临时注入同 `data-testid` 元素 →
断言 `count=1` → 证明选择器真能命中，否则 `count=0` 可能是**假绿**）；
「≤3」「互不相同」「逐字 placeholder」「唯一输入框」都配**判别力对照**（注入第 4 个按钮 /
断言排除近似串 / 注入第 2 个 input）。

---

## 3. 逐条 AC 验证

### AC-1（红线①）结果面板不得再出现三个固定按钮

- **判据**：`result-action-agent` / `result-action-self` / `result-action-view-diff` / `result-continue` /
  `result-next-actions` 均 `toHaveCount(0)`；`[data-testid^="result-quick-"]` `count=0`。
- **手段**：e2e 用例 `①`（富 payload = 结论 + 关键发现 + 真实来源），5 个 testid 逐个走阳性对照。
- **原始输出**：`ok 52 ... ① 三固定按钮已消失 · 结果面板无自有输入框 · 唯一续聊输入是 conv-followup (3.5s)`
- **源码核验**：`Grep` 全 `src/` 后，退役 testid **0 个活 `data-testid` 用法**，仅出现在注释/文档引述中
  （`LiveRunsView.tsx:299,522`、`FollowUpComposer.tsx:8,35`、`resultActions.ts:74`、
  `ResultContextActions.tsx:2,14`、`ResultPanel.tsx:332`、`types.ts:291`、`runs.css:1207,1579`）。
  `src/views/runs/ResultNextActions.tsx` = **ABSENT**（`git status` 显示 ` D`）。
- **结论**：✅ 通过。

### AC-2（红线②）上下文快捷操作由任务类型/真实产物决定，最多 1～3 条

- **判据**：代码档 / 数据档 / 知识档三组 payload，各自 `result-contextual-actions` 内按钮数 `1 ≤ n ≤ 3`，
  且三组 **testid 集合两两不同**，并与 `deriveContextualActions` 的规则表**逐字相符**。
- **手段**：e2e 用例 `②`（单用例内三次 boot）；`≤3` 附判别力对照（注入第 4 个按钮 ⇒ 计数 4 > 3）。
- **原始输出**：`ok 53 ... ② 快捷操作 ≤3 且按任务类型派生：三组 testid 集合互不相同 (5.4s)`
- **三组集合实测**（用例内硬断言，逐字）：
  - code = `["result-ctx-code-fix","result-ctx-code-diff","result-ctx-code-rerun"]`
  - data = `["result-ctx-data-deep-dive","result-ctx-data-chart","result-ctx-data-export"]`
  - knowledge = `["result-ctx-kb-follow-up","result-ctx-kb-sources","result-ctx-kb-summary"]`
- **结论**：✅ 通过（三组互异，非写死）。

### AC-3（红线③）「查看变更/Diff」只在真实存在变更时出现（双向对照）

- **判据 A（反）**：同一代码 payload，`codeplane.diff.present === false` ⇒ `result-ctx-code-diff` `count=0`，
  且面板内无任何「差异」措辞；同时 `code-diff-empty` 可见（后端确无变更）。
- **判据 B（正）**：同 payload 仅把 `codeplane.diff` 设为真实 unified diff ⇒ `result-ctx-code-diff` 出现、
  可见、逐字 `查看差异`，且 `code-diff-empty` 消失。
- **手段**：e2e 用例 `③a` / `③b`（双向对照；`③a` 另带阳性对照证明选择器敏感）。
- **原始输出**：
  - `ok 54 ... ③a Diff 红线（反）：diff.present=false ⇒ 无任何 Diff 动作 (2.9s)`
  - `ok 55 ... ③b Diff 红线（正）：diff.present=true ⇒ 查看差异出现且可见 (2.0s)`
- **结论**：✅ 通过。

### AC-4（红线④）「本次运行未启用模型驱动」不得伪装成正常 Agent 完成态/一句话结论，须显式作为执行环境状态

- **判据**：
  1. 非模型驱动档（`runtime_mode:'deterministic'` 无 `llm`，与 `llm.degraded:'no_model'` 两种）
     ⇒ `result-env-status` 可见，文本含「平台编排」与「未连接模型服务」；
  2. **核心红线**：`#res-panel-result` 子树 `innerText` **不含** `本次运行未启用模型驱动`；
  3. 段② 为**诚实空态** `result-headline-fallback`，逐字 `本次运行没有生成自然语言结论`，
     且其文本 `!== 「本次运行未启用模型驱动」`；真结论位 `result-headline` `count=0`（二者互斥）；
  4. 段④ 不再重复（env 档不产生 `result-degrade-note`）；
  5. **反证**：模型驱动档（`runtime_mode:'react'` + 非空 `llm.final_answer`）⇒ `result-env-status` `count=0`，
     且 `result-headline` 逐字 = 真实 `final_answer`。
- **手段**：e2e 用例 `④a` / `④b` / `④c`。
- **原始输出**：
  - `ok 56 ... ④a 环境状态不伪装：deterministic ⇒ result-env-status 可见且不进 Agent 结论位 (1.9s)`
  - `ok 57 ... ④b 环境状态不伪装：llm.degraded=no_model ⇒ 同上口径 (1.8s)`
  - `ok 58 ... ④c 反证：模型驱动档（react + 真实结论）⇒ result-env-status 不存在 (2.2s)`
- **源码核验**：`ResultEnvStatus.tsx:38`（`if (!degrade.present || degrade.kind !== 'env') return null`）、
  `realRun.ts:1287-1296`（`no_model → kind:'env'`）、`realRun.ts:1274-1284`（档位判定 → `kind:'env'`）、
  `ResultHeadline.tsx:51-58`（无 `degrade` 依赖的诚实空态）、`ResultBlockers.tsx:40`（仅 `kind==='degraded'`）。
- **结论**：✅ 通过（红线守住了）。

### AC-5（红线⑤）底部统一一个 follow-up 输入；结果面板内不再有自己的输入框

- **判据**：`#conv-followup-input` `count=1`、`[data-testid="conv-followup"]` `count=1`；
  **会话列**（`workspace-col-conversation`）内 `input,textarea` `count=1`（即唯一就是 follow-up）；
  结果面板 `[data-testid="result-layer"]` 内 `input,textarea` `count=0`；`result-continue` `count=0`。
- **手段**：e2e 用例 `①`；「会话列恰 1 个 input」附判别力对照（再注入 1 个 input ⇒ 计数 2）。
- **原始输出**：`ok 52 ... ① ... (3.5s)`（含上述全部断言）
- **口径澄清（如实声明）**：主理人原话「页面上输入框只有 1 个」在**全页**层面**不成立于本设计**——
  左列 `RunListPanel` 自带 3 个任务声明输入（`#run-intent` / `#run-declare-table` / `#run-declare-paths`）+ `ResourcePicker`；
  这是**既有**设计（`RunListPanel.tsx:103,119,131`），非本轮引入。因此我把该红线钉在**语义正确的范围**上：
  「**会话列（结果所在列）恰有 1 个输入框 = conv-followup**，且**结果面板内 0 个**」。本报告**未**把「全页输入框数 = 1」写成结论。
- **结论**：✅ 通过（在正确范围内；偏差已在 AC-5 口径澄清中披露）。

### AC-6（红线⑥）保留 Agent / Skill / Tool / Memory / Evidence / Trace / Artifact / Cost / SSE 能力

- **判据**：`result-capture-skill`、`code-plane`、`code-timeline`、`code-entry-*`、`artifact-*`、`conv-*`、
  `result-tab-*`、`result-ctas`/`result-edit|save|cancel|export|copy|print|store` 等 **testid 一个不消失**。
- **手段**：（a）`Grep` 源码确认 testid 仍存在；（b）**全量 e2e 回归**中，既有 spec 对这些 testid 的既有钉子全部绿。
- **原始输出（源码存在性）**：
  - `result-capture-skill` → `SkillCapture.tsx:1`
  - `code-plane|code-timeline|code-entry-` → `CodeTaskTimeline.tsx:8`、`ResultPanel.tsx:3`、`runs.css:5`
  - `artifact-card|artifact-empty|artifact-preview|artifact-download` → `ArtifactPanel.tsx:10`、`InlineArtifacts.tsx:3`
  - `conv-followup|conv-user-turn|conv-run-summary|conv-sources|conv-exec-` → `ExecDetailPanel.tsx:10`、`FollowUpComposer.tsx:5`、`SourcesDisclosure.tsx:5`、`ResultPanel.tsx:4` 等
  - `result-tab-` → `ResultPanel.tsx:358`
  - `result-ctas`/`result-edit|save|cancel|export|copy|print|store`/`result-*-note` → `ResultContextActions.tsx:120,130,135,146,154,165,175,185,195,207,219,224,229`（**原样迁移**进「更多操作」折叠组）
- **e2e 覆盖（既有钉子，全绿）**：`inc34`（`result-capture-skill` 可见/门控 2 例）、
  `inc29`（`code-plane`/`code-timeline`/`code-entry-*`）、`inc32`（`artifact-*`）、`inc36`（`conv-*`/`result-tab-*`）。
- **结论**：✅ 通过。

### AC-7（红线⑤ 续）底部 follow-up placeholder 逐字

- **判据**：`#conv-followup-input` 的 `placeholder` **逐字** = `继续告诉 AI 你想怎么处理……`；
  且近似串（去掉省略号）**不**等值（判别力对照）。
- **手段**：e2e 用例 `⑦`。
- **原始输出**：`ok 63 ... ⑦ 底部 follow-up placeholder 逐字且真发请求（含 continued_from_artifact_ref） (2.0s)`
- **结论**：✅ 通过。

### AC-8（红线⑥ 续 / 能力不减少）follow-up 真发请求，且 `continued_from_artifact_ref` 不丢

- **判据**：提交后真发 `POST /api/workspace/tasks`，body 带 `parent_run_id` **且**
  `context.continued_from_run_id` **且** `context.continued_from_artifact_ref`（逐字 = 主产物 `result_ref`）。
- **手段**：e2e 用例 `⑦`（payload 含主产物 `result_ref = 'inc39-primary-ref'`）。
- **原始输出**：`ok 63 ...(2.0s)`（断言 `intent`、`parent_run_id`、`continued_from_run_id`、`continued_from_artifact_ref` 全逐字）
- **源码核验**：`LiveRunsView.tsx:301-306`（`onFollowUp` 补 `continued_from_artifact_ref`）。
- **结论**：✅ 通过。

### AC-9（红线⑤）快捷操作点击有真实可见效果（不许装饰按钮）

| 档 | 判据 | 用例 | 结果 |
|---|---|---|---|
| `continue` | 点击 `result-ctx-kb-follow-up` ⇒ `POST /workspace/tasks`，body.intent = 逐字业务指令、`parent_run_id` + `context.continued_from_run_id` | `⑤a` | `ok 59 (3.6s)` ✅ |
| `export` | 点击 `result-ctx-fallback-export` ⇒ 真实下载锚点（`download = run-<runId>-result.md`、`href` 以 `blob:` 开头）；点击前 0 个、点击后 1 个 | `⑤b` | `ok 60 (2.1s)` ✅ |
| `sources` | 点击 `result-ctx-kb-sources` ⇒ `#res-panel-evidence` 由不可见变可见 | `⑤c` | `ok 61 (1.8s)` ✅ |

### AC-10 不空转：无任何特征产物 ⇒ 整段不进 DOM

- **判据**：`result-ctx-region` `count=0`、`result-contextual-actions` `count=0`（带阳性对照）、
  无「无后续动作」话术；同时面板确已渲染（`result-layer` 可见 + `result-conclusions` 可见）。
- **手段**：e2e 用例 `⑥`。
- **原始输出**：`ok 62 ... ⑥ 不空转：无特征产物 ⇒ result-contextual-actions 整段不进 DOM (2.5s)`
- **结论**：✅ 通过。

### AC-11 段序（`result-env-status` / 一句话结论 均在 `result-intent` 之前）

- **判据（DOM 顺序）**：`result-status-line(0) < result-env-status(1) < result-headline-fallback(3) < result-intent(4) < result-conclusions(5) < result-contextual-actions(6)`。
- **手段**：视觉脚本 `ORDER` 探针（明暗两档各一次）。
- **原始输出**：`=== light  段序(DOM index)={"statusLine":0,"env":1,"headline":null,"fallback":3,"intent":4,"conclusions":5,"contextual":6}`（dark 同值）
- **结论**：✅ 通过（`result-intent` 确已下移到段② 之后）。

### AC-12 被改动文件清单（机械核验工程师声称）

`git status --porcelain`（仓库根 = `D:/Agentxm/Multi-Agent`，项目在 `ForgeFlow-main/`）：

```
 M ForgeFlow-main/frontend/src/styles/runs.css
 M ForgeFlow-main/frontend/src/views/LiveRunsView.tsx
 M ForgeFlow-main/frontend/src/views/runs/FollowUpComposer.tsx
 M ForgeFlow-main/frontend/src/views/runs/ResultBlockers.tsx
 M ForgeFlow-main/frontend/src/views/runs/ResultHeadline.tsx
 D ForgeFlow-main/frontend/src/views/runs/ResultNextActions.tsx
 M ForgeFlow-main/frontend/src/views/runs/ResultPanel.tsx
 M ForgeFlow-main/frontend/src/views/runs/realRun.ts
 M ForgeFlow-main/frontend/src/views/runs/resultActions.ts
 M ForgeFlow-main/frontend/src/views/runs/types.ts
?? ForgeFlow-main/frontend/src/views/runs/ResultContextActions.tsx
?? ForgeFlow-main/frontend/src/views/runs/ResultEnvStatus.tsx
```

与工程师声称**一致**（并额外包含 `types.ts` 的 **additive** 类型改动，属实现必需、未破坏既有类型）。
`ResultNextActions.tsx` = ` D`（已删），`ResultContextActions.tsx` / `ResultEnvStatus.tsx` = 新增。
- **结论**：✅ 通过（声称与事实一致）。

---

## 4. 全量回归

### 4.1 类型检查

```
$ node node_modules/typescript/bin/tsc -b
TSC_EXIT=0        （无任何输出）
```

### 4.2 构建

```
$ node node_modules/vite/bin/vite.js build
... 
✓ built in 8.74s
BUILD_EXIT=0
```

### 4.3 全量 e2e（**重建 dist 后**运行）

```
$ node node_modules/@playwright/test/cli.js test
...
ok 63 [chromium] › e2e\inc39_agent_completion.spec.ts:736:3 › ... ⑦ ... (2.0s)
ok 62 [chromium] › e2e\inc39_agent_completion.spec.ts:720:3 › ... ⑥ ... (2.5s)

  63 passed (30.1s)

┌──────────────────────────────────────────────┐
│  TestRelic AI - Playwright Test Report        │
│  Tests:       63 total (63 ✓)                 │
│  Navigations: 282 visits across 22 unique URLs│
│  Actions:     1146 steps                      │
└──────────────────────────────────────────────┘
PW_EXIT=0
```

**四列数字**：

| tests | passed | failed | skipped |
|---|---|---|---|
| **63** | **63** | **0** | **0** |

判据文件（跑测后 mtime 前进 + `failedTests` 为空）：

```
PRE_MTIME=2026-10-01T13:00:51.673Z
POST_MTIME=2026-10-01T13:01:41.116Z
{ "status": "passed", "failedTests": [] }
```

**新增 INC39 钉子 12/12 全绿**（test 编号 52–63，逐条见 §3）。
> 说明：单 spec 首轮曾 1 红——用例 `⑥` 把段⑥ 的诚实空态 `result-empty` 断言为 `visible`，
> 而它位于**设计上默认折叠**的 `<details data-testid="result-details">` 内（`ResultDetails.tsx:86,129-133`），
> 故 `hidden`。判定为**测试自身缺陷**（断言口径错误），QA **自修**为 `toHaveCount(1)`（判「在 DOM」），
> 非源码 Bug。修正后单 spec 与全量均全绿。

---

## 5. 视觉抽检（明暗两档）

脚本：`frontend/qa_tmp/inc39_visual_check.cjs`（对比度内核**逐字复用** `D:/Temp/_contrast_audit2.cjs`；
差异：补 `/api/**` 打桩 + 会话注入，以便 `.res-env` / `.res-ctx` 真的进 DOM）。
payload 用「环境档」（`deterministic` + 无交付产物）⇒ 同页同时出现 `.res-env` 与 `.res-ctx`。

### 5.1 主题切换的**阳性对照**（证明两档真的换了色板，否则「两档都 0 命中」不可信）

```
light: bodyBg=oklch(0.995 0.002 250)  envBg=oklch(0.972 0.003 250)  mainColor=oklch(0.2 0.004 250)  noteColor=oklch(0.36 0.004 250)
dark : bodyBg=oklch(0.145 0.004 250)  envBg=oklch(0.125 0.004 250)  mainColor=oklch(0.965 0.003 250) noteColor=oklch(0.82 0.004 250)
```

两档前景/背景色值**完全不同** ⇒ 主题确实生效。

### 5.2 对比度（目标 ≥ 4.5:1；1×1 canvas 合成前景 + 逐层祖先背景）

**根元素存在性**（两档一致）：`.res-env`=1、`.res-ctx`=1、`.res-ctx-region`=1。

| 主题 | 扫描元素 | 已评估 | 无法解析 | 低对比命中 | 最小比值（最差元素） |
|---|---|---|---|---|---|
| light | 19 | 13 | 0 | **0** | **6.47**（`.res-note`「编辑仅在本地生效」，需 4.5） |
| dark | 19 | 13 | 0 | **0** | **5.28**（同上） |

`.res-env` 子树**逐元素**对比度（红线要求「明暗两档文本均可读」）：

| 元素 | 文本 | light | dark |
|---|---|---|---|
| `.res-env-main` | 执行环境：平台编排（未连接模型服务） | **16.71** | **18.27** |
| `.res-env-note` | 平台只记录执行过程…启用模型服务后重新运行可获得完整交付物 | **10.04** | **11.53** |
| `.res-env .btn`（`result-env-rerun`） | 重新运行 | **16.39** | **16.26** |

⇒ 明暗两档 `.res-env` 全部文本 **≥ 5.28:1 > 4.5:1**，无低对比命中。
（「扫描 19 / 评估 13 / 低对比 0」三数同时给出，满足「报 0 命中须同时报扫描元素数」。）

### 5.3 横向溢出（`scrollWidth > clientWidth`，**排除** `overflow-x ∈ {auto,scroll,hidden}` 自带/祖先）

| 主题 | 扫描元素（`.res-env`+`.res-ctx`+`.res-ctx-region`+`.res-more` 子树） | 溢出命中 |
|---|---|---|
| light | 19 | **0** |
| dark | 19 | **0** |

⇒ 新增块**不引入横向溢出**（0 命中 + 同时报扫描元素数 19）。

---

## 6. 未能验证 / 残留风险（如实列出，不含糊）

1. **`degraded` 档（真实模型失败）未用 e2e 独立钉住**：AC-4 的「env 档不进段④」由 e2e `④a/④b` 反证
   （`result-degrade-note` `count=0`），但「`provider_degraded_to_mock` / `exception:` 等 `degraded` 档
   **仍**在段④ 渲染 `result-degrade-note`」这一半**仅由源码核验**（`ResultBlockers.tsx:40`），
   **未**新增 e2e（受「最多 2 轮」纪律约束，本轮已用满 2 轮，选择不为它再开一轮）。
   风险面：低（分支为单一 `&&` 条件，且 `env` 侧已被双向钉住）。
2. **`deriveContextualActions` 的纯函数级边界未单测**：4 档优先级（代码 > 数据 > 知识 > 兜底）
   中，「代码档压过数据/知识」这一互斥性**仅由源码核验**（`resultActions.ts:129-164` 的 `return` 早退）；
   e2e 三组 payload 各自只命中一档，未构造「同时含 codeplane 与 sources」的交叉 payload。
   风险面：极低（早退结构 + 三档集合实测互异）。
3. **后端语义不在范围**：全部在 `/api/**` stub 边界验证前端；`POST /workspace/tasks` 的后端父子关系落库、
   SSE 真实流、artifacts 真实字节 **未** 验证（属后端集成测试范围，沿用 inc32/inc36 既有口径）。
4. **视觉抽检的范围**：只测 `.res-env*` / `.res-ctx*`（+`.res-ctx-region` / `.res-more`）子树，
   未做全站对比度扫描（`_contrast_audit2.cjs` 的全站模式需真实后端，本环境未起后端）。
5. **仓库遗留杂物（非本次 QA 产生，请主理人处置）**：`git status` 显示工程师过程产物未清理——
   `frontend/build_out.txt`、`frontend/tsc_out.txt`、`diffstat.txt`、`sidebar_head_testid.txt`、
   `testid_diff.txt`、`testid_diff2.txt`（均 `??` 未跟踪）。建议提交前删除或加 `.gitignore`。

---

## 7. 复现命令（一键）

```bash
cd D:/Agentxm/Multi-Agent/ForgeFlow-main/frontend

# 1) 类型检查 + 构建
node node_modules/typescript/bin/tsc -b
node node_modules/vite/bin/vite.js build

# 2) 全量 e2e（同进程树内自起 preview，供 playwright 复用）
node node_modules/vite/bin/vite.js preview --port 4173 --strictPort > qa_tmp/inc39_preview.txt 2>&1 &
PREV=$!
node -e "/* 探针 http://localhost:4173/ 就绪 */"
node node_modules/@playwright/test/cli.js test
kill $PREV
# 判红绿：读 test-results/.last-run.json（mtime + failedTests）

# 3) 视觉抽检（明暗两档）
node node_modules/vite/bin/vite.js preview --port 4173 --strictPort > qa_tmp/inc39_preview.txt 2>&1 &
PREV=$!
node qa_tmp/inc39_visual_check.cjs http://localhost:4173 qa_tmp/inc39_visual_report.json
kill $PREV
```

---

## 8. 复核整改记录（主理人复核 → QA 自修）

> 本节记录主理人对**首轮验收**的复核意见（指出 1 处真空断言）及我的整改与复跑证据。
> 上文 §0–§7 为**首轮**验收结果；**本节数字为整改后的复跑结果**（两者均已全绿，互为印证）。

### 8.1 问题（主理人指出，属实）

主理人逐条读 spec 时发现**一处真空断言**（永真、不可能变红 = 假绿）：

- 位置：`frontend/e2e/inc39_agent_completion.spec.ts`，用例 ⑥（改前 `:729` → 改后 `:739`）
  ```ts
  await expect(page.getByTestId('result-ctx-region')).toHaveCount(0)
  ```
- 根因：`result-ctx-region` **在 `frontend/src` 里根本不存在**。它其实是 **className**——
  `ResultContextActions.tsx::<div className="res-ctx-region">` 这个 wrapper **没有** `data-testid`。
  因此 `page.getByTestId('result-ctx-region')` **恒返回 0 个元素**，这条断言**无论实现对错都不会红**，
  是典型的**真空/假绿**断言（同 §2.4「反证纪律」要防的就是它）。
- 主理人原话核实：已 grep `frontend/src` —— 只有 `className="res-ctx-region"`，无任何 `data-testid="result-ctx-region"`。**属实。**

### 8.2 判为「测试自身缺陷」的理由（而非源码 Bug）

- 被断言**行为**本身是**正确的**：「无特征产物 ⇒ 段⑤ 整段不进 DOM」既是设计（`INC39-DESIGN.md`）也是实测事实；
  源码也**确实正确实现**了它（`ResultContextActions.tsx:38` `if (actions.length === 0) return null`）。
- 有缺陷的只是**断言的定位方式**——用了一个**不存在的 testid**，而非被测行为有问题。
- 依 §「智能路由」判据：**断言写法错、被测行为对 ⇒ 测试缺陷 ⇒ QA 自修，不打回工程师**。
  （若「用不存在的 testid 去断言」被当作源码 bug 打回，工程师将无从修改，属误判。）

### 8.3 改法（保留强断言，换成真实选择器 + 判别力对照）

采用主理人给的**第一种口径**（保留「整段不进 DOM」这条强断言，只把定位方式换成**真实 DOM 选择器**），
并补**判别力对照**（注入一个 `.res-ctx-region` ⇒ 计数 1，证明该 class 选择器**真能命中**这个 wrapper，
从而把 0 断言变成**可证伪**）：

修改后（用例 ⑥，`:739–:750`）：

```ts
await expect(page.locator('.res-ctx-region')).toHaveCount(0)
await page.evaluate(() => {
  const d = document.createElement('div')
  d.className = 'res-ctx-region'
  d.setAttribute('data-ff-probe', 'inc39')
  document.querySelector('[data-testid="result-layer"]')?.appendChild(d)
})
await expect(page.locator('.res-ctx-region')).toHaveCount(1)   // 阳性对照：证明选择器真能命中
await page.evaluate(() => {
  for (const el of Array.from(document.querySelectorAll('[data-ff-probe="inc39"]'))) el.remove()
})
await expect(page.locator('.res-ctx-region')).toHaveCount(0)
```

**为何此处不复用 `expectAbsentWithControl`**：该 helper 注入的是**同 `data-testid` 探针**；
而 `.res-ctx-region` 是 **wrapper class**（无 testid），注入同 testid 探针会把「阳性对照元素」与「目标元素」混为一体
（无法区分命中来自目标还是探针）。故改为**手动注入同类 class 元素 + 断言翻转 0 → 1 → 0**，等效达成可证伪。

**同类整改（举一反三）**：用例 ① 中 `[data-testid^="result-quick-"]` 的 0 断言，同样补了前缀选择器的阳性对照
（注入 `result-quick-probe` ⇒ 计数 1 ⇒ 移除 ⇒ 计数 0），确保该前缀断言**可证伪**（重新引入写死的快捷项即会红）。

### 8.4 全 spec testid 普查（自查是否还有其它「src 查无此名」）

- 脚本：`frontend/qa_tmp/inc39_testid_audit.cjs`（原始输出 `qa_tmp/inc39_testid_audit.txt`）。
- 口径：提取 spec 中每个 `getByTestId('X')` / `expectAbsentWithControl(page,'X')` / `[data-testid="X"]` 的
  **精确 testid**，以及 `[data-testid^="X"]` 的**前缀选择器**，逐一 grep `frontend/src/**/*.{tsx,ts,css}`。
- **精确 testid：共 18 个**，分类如下（`字面命中` = 在 src 里能直接 grep 到该字符串的次数）：

  | 分类 | 个数 | testid |
  |---|---|---|
  | ✅ `ok(字面存在)`（命中 ≥1） | **11** | `code-diff-empty`(1)、`conv-followup`(19)、`result-conclusions`(7)、`result-contextual-actions`(4)、`result-degrade-note`(4)、`result-empty`(1)、`result-env-status`(6)、`result-headline`(11)、`result-headline-fallback`(4)、`result-layer`(5)、`workspace-col-conversation`(3) |
  | ✅ `ok(运行期模板值)`（字面命中 0，但属**合法模板键集合**） | **6** | `result-ctx-code-diff`、`result-ctx-code-fix`、`result-ctx-code-rerun`、`result-ctx-fallback-export`、`result-ctx-kb-follow-up`、`result-ctx-kb-sources` |
  | ❌ `src查无此名`（**假绿候选**） | **1** | `result-ctx-region` ← **本轮修复对象** |

- **运行期模板值为何算合法**：这些 testid 由 `resultActions.ts` 的 `ctxAction(key, ...)` 在**运行时拼接**成
  `result-ctx-<key>`；脚本从源码**枚举**了全部合法 `key`（11 个：`code-{fix,diff,rerun}`、`data-{deep-dive,chart,export}`、
  `kb-{follow-up,sources,summary}`、`fallback-{trace,export}`），上述 6 个**均在该集合内** ⇒ 合法，非假绿。
  （`result-ctx-region` **不在**该集合内 ⇒ 判为查无此名，与主理人结论一致。）
- **前缀选择器：1 个**（`result-quick-`）——前缀 0 断言**可证伪**（8.3 已补阳性对照），非真空。
- **普查结论**：**除已修复的 `result-ctx-region` 外，全 spec 无其它「testid 在 src 中不存在」的假绿候选。**
  （首轮 v1 版普查曾因用**粗粒度前缀规则**误把 `result-ctx-region` 当作「模板拼接-已知」而放过；
  本版改为**从 `ctxAction(...)` 精确枚举合法键集合**后，才把它正确揪出——这也反证了本轮修复确有必要。）

### 8.5 整改后复跑数字（**先重建 dist**）

复跑脚本：`frontend/qa_tmp/inc39_rerun.log`（一条进程树内：build → 起 preview → 单 spec → 全量 e2e，
杜绝 `reuseExistingServer` 复用旧 dist）。

| 项 | 结果 |
|---|---|
| `vite build`（重建 dist） | `✓ built in 8.78s`，**exit 0** |
| **单 spec** `inc39_agent_completion.spec.ts` | `12 passed (20.2s)`，**exit 0** |
| 单 spec 四列 | tests **12** / passed **12** / failed **0** / skipped **0** |
| 单 spec `.last-run.json` mtime | **`2026-10-01T13:11:05.487Z`** |
| **全量 e2e** | `63 passed (29.9s)`，**exit 0** |
| 全量四列 | tests **63** / passed **63** / failed **0** / skipped **0** |
| 全量 `.last-run.json` mtime | **`2026-10-01T13:11:36.844Z`** |
| 全量 `.last-run.json` 内容 | `{ "status": "passed", "failedTests": [] }` |

- 两轮 `.last-run.json` mtime **均前进**（单 spec `13:11:05` → 全量 `13:11:36`，且都晚于本轮 `13:10` 启动）
  ⇒ 证明是**真跑**而非读旧结果。
- 用例 ⑥ 声明行 `:720 → :726`（因在用例 ① 中新增前缀阳性对照插入若干行，使其整体下移；同理用例 ⑦ `:736 → :758`）；
  单 spec 与全量中该用例（编号 11 / 62）**均为 `ok`**，
  且其内部阳性对照 `toHaveCount(1)` 亦通过 ⇒ 证明新的 `.res-ctx-region` 断言**可命中、非真空**。

### 8.6 结论

- 主理人复核意见**属实**：确为一处真空断言；已判为**测试自身缺陷**并 **QA 自修**（未打回工程师，符合路由判据）。
- 全 spec testid 普查：**除该 1 处外无其它假绿候选**（18 个精确 testid 中，其余 17 个均验证合法）。
- 整改后**单 spec 12/12、全量 63/63 双双全绿**，`.last-run.json` 两轮 mtime 均前进。
- **路由判定不变：NoOne（0 源码 Bug）**。
- 未提交 git（遵主理人指示）；未改动任何后端文件。

---

## 9. 第二轮：去重收敛后的复核

> 本轮由**主理人**在复核中抓到「同一屏两个『重新运行』」的**源码级重复**，判为源码 Bug 并交工程师修复；
> QA 据此**更新钉子并重跑**。上文 §0–§8 为上一轮结论；本节记录本轮变化与复核证据。

### 9.1 改前问题（主理人指出）

代码任务档下，段⑤「上下文快捷操作」会再产出一个**「重新运行」**动作（旧 `result-ctx-code-rerun`），
与同屏**专责入口**的「重新运行」构成**信息重复**：**同守卫**（`canRerun`）、**同标签**（「重新运行」）、
**同回调**（`POST /runs/{id}/replan`）⇒ **零信息增益**，只会让页面看起来像「审批 / 工作流系统」。

### 9.2 判为**源码 Bug**的理由（非测试缺陷）

- **存在性推导**（`resultActions.ts` 顶部「去重规则」）：`canRerun = missingInputs>0 || !hasDeliverable`；
  ① `missingInputs>0`、② `!hasDeliverable` 且无降级、③ `!hasDeliverable` 且 `degrade.kind='degraded'`
  三种情形**段④ 必已渲染 `result-rerun`**；④ `!hasDeliverable` 且 `degrade.kind='env'` 时段①
  `ResultEnvStatus` 已给 `result-env-rerun` ⇒ **只要 rerun 真可用，段④ 或段① 必有且只有一个入口**。
  段⑤ 再加一个必然是**重复**。
- 与上一轮 `result-ctx-region`（测试写错选择器）**性质不同**：这里是**产品行为重复**（源码问题），
  按钮真的会渲染、真的有 2 个同义入口 ⇒ 判源码 Bug，交工程师修。
- 工程师改动（源码现态，已核验）：`resultActions.ts` 代码档**只剩** `code-fix` +（仅 `diff.present`）
  `code-diff`；`types.ts::ContextActionKind` **移除 `'rerun'`**，`ContextActionInput` 收敛为
  `{ codeplane, metrics, findings, sources, artifacts }`；`ResultContextActions.tsx` 删 `case 'rerun'`
  与 `onRerun`/`rerunPending` props。

### 9.3 新的**可证伪**判据（关键：绝不制造真空断言）

⚠️ **铁律**：`result-ctx-code-rerun` 这个 testid **现在整个 `frontend/src` 里彻底不存在了**
（已随收敛删除）。所以 **绝不**写 `await expect(page.getByTestId('result-ctx-code-rerun')).toHaveCount(0)`
—— 那又是**永远绿的假绿**（上一轮刚修掉的那类）。本轮改用**可证伪的事实**：

1. **唯一入口计数**（`count=1` 天生可证伪：变 2 或变 0 都红）：
   ```ts
   const rerun = page
     .getByTestId('result-layer')
     .getByRole('button', { name: '重新运行', exact: true })
   await expect(rerun).toHaveCount(1)
   ```
   —— 旧行为下段⑤ 会再给一个 ⇒ 计数 2 ⇒ **变红**。
2. **判别力对照**（证明「按可见名定位」的选择器真能命中）：断言 `rerun.first()` 可见 + 逐字
   `toHaveText('重新运行')`；再临时注入一个**同标签** `<button>重新运行</button>` ⇒ 计数变 2 ⇒
   移除 ⇒ 回到 1（证明「=1」真的会红，不是选择器失灵）。
3. **段⑤ 动作集合逐项相等**（用「容器内按钮的 testid 序列」，而非「某 testid 不存在」）：
   - 代码档**有** diff（`codeRun(true)`，用例 ②）⇒ 恰 `['result-ctx-code-fix','result-ctx-code-diff']`；
   - 代码档**无** diff（`codeRun(false)`，用例 ③a）⇒ 恰 `['result-ctx-code-fix']`。
   `toEqual` 为逐项相等 ⇒ 多一条（例如把 rerun 加回来）即**变红**。

### 9.4 ⚠️ 存疑 / **纠正主理人**（如实上报，不为全绿含糊）

主理人指示「该唯一『重新运行』的 testid 是 **`result-rerun`（段④那个）**」。**实测不符**：

- 本轮 code 档 payload（`deterministic` + 无 artifacts ⇒ `degrade.kind='env'`、`hasDeliverable=false`）
  下，**段④ 因无任何阻塞来源而不渲染**（`ResultBlockers.tsx:43-47` `hasBlockers=false ⇒ return null`），
  故**没有** `result-rerun`；真正提供 rerun 的是**段① 的 `result-env-rerun`**。
- **证据**（临时探针，跑后即删）：dump 代码档 `result-layer` 内全部按钮 ⇒ 标签恰为「重新运行」者
  **只有 `result-env-rerun` 一个**（`withDiff` 真/假两态一致）；无 `result-rerun`，无 `result-ctx-code-rerun`。
- 因此新钉子断言的是 `data-testid === 'result-env-rerun'`（本 payload 的**实际**专责入口），
  并在注释里写明「此处**不是** `result-rerun`（段④ 仅在**有阻塞**时渲染）」。
- **对本轮红线无影响**：无论专责入口落在段④ 还是段①，「同屏『重新运行』重复入口」都已消除
  （计数恒 = 1）。主理人描述的「段④ + 段⑤」重复**确实可能存在**——但需在**有阻塞**的代码档
  （如 `missingInputs>0`）才会命中段④；本 spec 的 payload 走的是**段① 分支**。两者属同一 bug 的不同子分支。

> 结论：主理人的**问题定性正确**（确有重复源码 Bug），仅**具体 testid 归属**需按 payload 分支区分；
> 已按实测修正断言，未盲从。

### 9.5 复跑数字（**先 `tsc -b` + 重建 dist**；日志 `qa_tmp/inc39_rerun2.log`）

| 项 | 结果 |
|---|---|
| `tsc -b`（源码已变，QA **自行复验**，不采信转述） | **exit 0**（无输出） |
| `vite build`（重建 dist） | `✓ built in 8.78s`，**exit 0** |
| **单 spec** `inc39_agent_completion.spec.ts` | tests **12** / passed **12** / failed **0** / skipped **0**（`12 passed (13.6s)`，**exit 0**） |
| 单 spec `.last-run.json` mtime | **`2026-10-01T13:25:53.567Z`** |
| **全量 e2e** | tests **63** / passed **63** / failed **0** / skipped **0**（`63 passed (27.6s)`，**exit 0**） |
| 全量 `.last-run.json` mtime | **`2026-10-01T13:26:22.487Z`** |
| 全量 `.last-run.json` 内容 | `{ "status": "passed", "failedTests": [] }` |

- 两轮 mtime **均前进**（单 spec `13:25:53` → 全量 `13:26:22`，且均晚于本轮 `13:25` 启动）
  ⇒ 证明是**真跑**而非读旧结果。

**本轮改用例数**：修改 **3** 个（② / ③a / ③b 复核；实际改动数值在 ② 与 ③a）、新增**可证伪钉子 1 条**
（③a 内「唯一『重新运行』」+ 判别力对照），删除临时探针 1 个；**用例总数仍 12**（未增删用例）。
行号变化（因增删行）：② `:541`、③a `:610→:616`、③b `:625→:669`、⑥ `:770`、⑦ `:802`。

### 9.6 testid 与 src 对照**普查复算**（脚本 `qa_tmp/inc39_testid_audit.cjs` v3）

- 脚本 v3 升级：**先剥注释再匹配**（v2 会把注解里写的 `getByTestId('result-ctx-code-rerun')` 当成真引用，
  既凭空多一个 testid，又因 src 注释也提到它而**误判为 ok**）；并追加采集 `toHaveAttribute('data-testid', 'X')`。
- **精确 testid 共 16 个**（已剥注释）：**全部 `ok`**——
  - `ok(字面存在)`：`code-diff-empty`、`conv-followup`、`result-conclusions`、`result-contextual-actions`、
    `result-degrade-note`、`result-empty`、**`result-env-rerun`**、`result-env-status`、`result-headline`、
    `result-headline-fallback`、`result-layer`、`workspace-col-conversation`；
  - `ok(运行期模板值)`：`result-ctx-code-diff`、`result-ctx-fallback-export`、`result-ctx-kb-follow-up`、`result-ctx-kb-sources`。
- **运行期模板合法键集合**（从 `ctxAction(...)` 枚举）已由 11 → **10**（`code-rerun` 随收敛移除），
  与源码现态一致：`code-{fix,diff}`、`data-{deep-dive,chart,export}`、`kb-{follow-up,sources,summary}`、
  `fallback-{trace,export}`。
- **前缀选择器 1 个**（`result-quick-`，可证伪）。
- **候选假绿（src 查无此名）：（无）** —— 上一轮的 `result-ctx-region` 已修、本轮新代码**未再引入**任何
  「对不存在 testid 的断言」。

### 9.7 结论与遗留

- 主理人复核**定性正确**（确有重复源码 Bug）；本轮的**测试侧**改动**未制造新的真空断言**
  （判据全部落在可证伪事实上：`count=1` / 动作集合逐项相等 / 判别力对照）。
- **路由判定：NoOne**（源码 Bug 已由工程师修复并验证；QA 侧无遗留测试缺陷）。
- 复跑：`tsc -b` exit 0、`vite build` exit 0、单 spec 12/12、全量 63/63 全绿。
- 未提交 git；未改动任何后端文件。
- **遗留（如实）**：本轮**未**为「段④ 分支（有阻塞的代码档）」单独加「唯一 rerun」钉子——因为该分支的
  rerun 归属（`result-rerun`）与本 payload（段①）不同，属**不同子分支**；受「最多 2 轮」纪律约束，
  未再开一轮。风险面：低（段④ 的 `result-rerun` 在既有 inc32/inc33 套件中已有存在性钉子）。
