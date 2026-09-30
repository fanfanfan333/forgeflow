# INC34 · PRD + 设计（合并档）

> 状态：已裁决（主理人代笔；PM 两次 `502 TLS` 网络失败，按「连续两次同因失败停止硬重试」纪律由主理人接管 PM/架构师职责）
> 日期：2026-09-30 · 前置：INC33（`cd9cd4f`）/ E2E 收口（`57e0650`，22/22 绿）
> 用户原话：**「前端要以中文为主，而且首页左上角去掉标志，不要使用表情，前端要呈现高级感，白色与灰色为主」**

---

## 0. 用户已裁决、不在本增量范围

| 项 | 结论 |
|---|---|
| TestRelic AI 改名 | **不做** —— 产品源码里根本没有该字符串（仅存在于 TestRelic 自动生成的 `analytics-timeline.html` 与注释/配置） |
| 删除某个按钮 | **不做** —— 用户截图未能取到，用户答复「没有就不用改」 |

## 1. 产品目标

把 ForgeFlow 前端从「英文营销腔 + 蓝紫渐变」转成「**中文为主 + 白灰高级感**」的企业级控制台观感：
中性色主导、强调色克制、无表情符号、品牌图形标记移除（文字保留）。

**成功判据**：打开任何一页，第一观感是「白与灰的克制排版」；UI 上找不到表情/装饰符号；除专有术语外无英文文案。

## 2. 用户故事

- 作为**使用者**，我看到的所有说明、按钮、空态、提示都是中文，只有 Agent/RBAC/MCP 这类术语保留英文。
- 作为**使用者**，界面是白灰为主的安静配色，主按钮是克制的深色，而不是抢眼的蓝紫渐变。
- 作为**使用者**，界面上没有任何 emoji / 装饰符号（✓ ✕ ☰ ➤ ✦ 之类）。
- 作为**老用户**，深色主题切换仍然可用（`theme-toggle` 不删）。

## 3. 需求池

### P0（本轮必做）
| # | 需求 | 落点 |
|---|---|---|
| P0-1 | **主题白灰化**：中性面/边框/前景去蓝偏；主按钮改石墨深色；信号色降饱和；装饰渐变收敛 | `src/styles/tokens.css`（light + dark 同步） |
| P0-2 | **中文字体栈**：`--font-sans` 补中文优先回退（PingFang SC / Microsoft YaHei），中文排版才谈得上高级感 | `src/styles/tokens.css` |
| P0-3 | **移除品牌图形标记**（6 处）：Sidebar:62 / Topbar:57 / ArchitecturePage:38 / DesignHubPage:35 / DesignSystemPage:35 / DocsPage:27；`.brand-mark*` 样式与 `home.css:15` 一并清理；**品牌文字 `ForgeFlow` 保留** | 上述 6 个 tsx + 2 个 css |
| P0-4 | **去表情/符号（仅 UI 可见）**：`DocMarkdown.tsx:74`「已复制 ✓」、`DocsPage.tsx:24` `✕`/`☰`、`HomeView.tsx:155` `➤`、`HomeView.tsx:195` `✓`、`HomeView.tsx:389` `✦`、`ArchitecturePage.tsx:878` SVG `<text>✕</text>` → 一律改用 `src/components/icons.tsx` 的 SVG（缺的**新增**：IconClose / IconMenu / IconSend 等） | 上述文件 |
| P0-5 | **中文化 · 第一批（日常动线）**：`index.html`（title / meta description / keywords）、壳层、`HomeView`、`LiveRunsView` + `views/runs/*`（ResultPanel / ResourcePicker / ArtifactPanel / RunListPanel / CodeTaskTimeline / ResultNextActions / ResultBlockers / ResultDetails / roles.ts） | 见左 |

### P1（后续批次）
| # | 需求 | 落点 |
|---|---|---|
| P1-1 | 中文化 · 第二批：`LandingPage.tsx`（101 条候选英文文案，营销腔最重） | `src/views/LandingPage.tsx` |
| P1-2 | 中文化 · 第三批：`ArchitecturePage.tsx`（170 条，最重）/ `DesignHubPage.tsx` / `DesignSystemPage.tsx` | 同左 |
| P1-3 | 中文化 · 第四批：`DocsPage.tsx` + `src/docs/content.ts` + `src/docs/manifest.ts`（内置文档正文） | 同左 |

### P2（可选）
- 「UI 文件不得含 emoji」的静态检查脚本（防回潮）。

## 4. 术语白名单与判定规则（**本 PRD 的核心裁决**）

**判定规则（一句话）**：**产品概念名词、协议名、技术专有名词保留英文原样；叙述句、按钮、标题、空态、提示、表头、说明走中文**；中英混排时以中文为主、英文术语原样嵌入、术语两侧不加引号不加空格强调。

**保留英文（白名单，非穷举）**：`ForgeFlow`（品牌名）、`Agent`、`RBAC`、`MCP`、`KPI`、`SSE`、`API`、`Token`、`LLM`、`Prompt`、`Trace`、`Diff`、`JSON`、`YAML`、`SQL`、`id`、`Slug`、`Webhook`、`OpenAPI`、`Kubernetes`、`Grafana`、`Playwright`、`pytest`。

**必须中文**：`Run→运行/任务`（按语境）、`Workflow→工作流`、`Skill→技能`、`Memory→记忆`、`Artifact→产物`、`Session→会话`、`Approval→审批`、`Audit→审计`、`Cluster→集群`、`Cost→成本`、`Dashboard→看板`、`Overview→概览`、`Tools→工具`、`Knowledge→知识库`、`Evaluations→评测`、`Marketplace→市场`、以及一切整句文案。

**禁止**：机翻式直译（如把 `Run` 译成「奔跑」）、中英夹杂的口语腔（「这个 Agent 会帮你搞定哒」）、感叹号堆叠。

## 5. UI 设计方向（白灰高级感的具体口径）

1. **中性面**：light 主题所有 surface / border / foreground 的 oklch 色相 `250` 保留但 **chroma 压到 ≤0.004**（近乎纯灰，去掉现在的冷蓝偏色）。
2. **主按钮**：`.btn.primary` 由「蓝色渐变 + 蓝色 glow」改为**石墨近黑实色 + 白字**（`--fg-primary` 反白），hover 仅提亮 4%；这是「高级感」的最大单点。
3. **信号色降饱和**：blue / purple / emerald / amber / red 五组 step 的 chroma 整体下调约 1/3，`*-glow` 减半或移除；**语义不变**（success/warn/danger 仍可辨），badge/tint 同步。
4. **装饰收敛**：`--nav-active-bg` / `--foot-bg` / `--hero-glow-*` / `--spotlight-*` / `--brandmark-ring` 全部中性化或大幅调淡；`.grid-bg`、`.spotlight` 保留但更隐。
5. **深色主题同步中性化**（同一条规则套用），**不删 dark**。
6. **留白与字重**：不动 spacing/字号 scale（已是完整体系），只动颜色。

## 6. 硬性约束

1. **`data-testid` 只增不改不删**（`REMOVED=0` 是项目红线，两区间审计口径沿用 INC33 的）。
2. **E2E 冲击管理**：`frontend/e2e/` 4 spec / 22 用例，大量**逐字中文断言**。凡改动会命中断言的文案，必须在**同一次提交**里同步更新断言；**明令禁止弱化断言**（逐字比对不得改成模糊匹配、不得删用例）。
3. 禁 MUI / Tailwind / 任何 UI 组件库；只改手写 CSS 与 tsx。
4. 代码注释里的 `⚠️`、`★` 等**不属于本增量范围**（不是 UI）。
5. 后端不动 —— 本增量是纯前端 + 文案。

## 7. 验收（每批提交都要过）

- `node node_modules/typescript/bin/tsc -b` 零错（cwd = `frontend`）
- `node node_modules/vite/bin/vite.js build` 成功
- 全量 playwright（判红**只**看 `frontend/test-results/.last-run.json`）
- 双档 pytest 后端数字与 INC33 基线一致（1522/1521/0/0/1，ADDED=0/REMOVED=0）
- `data-testid` REMOVED=0（区间 `cd9cd4f…HEAD` 与 `c806715…HEAD` 各报一次，口径照 INC33 声明）
- UI 符号抽查：上列 6 处符号在 UI 中不再出现
