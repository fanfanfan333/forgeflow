# INC39 —— 结果层从「任务审批面板」重构为「Agent 对话式完成态」（纯前端）

## 1. 目标

用户在 `/tasks` 结果面板底部看到**三个固定按钮**（「让智能体处理」/「我自己处理」/「查看变更」），
判定：① 「让智能体处理」与「用户本身已在用 Agent」重复；② 「我自己处理」无产品价值；
③ 「查看变更」只应在**确有 Diff**时出现；④ 固定三按钮让页面像**审批 / 工作流系统**；
⑤ 与已定的 **ChatGPT 式极简交互**冲突。

目标完成态：

```
✓ 已完成 → AI 自然语言结果 → 产物 / 报告（如有）→ 上下文快捷操作 → 底部统一续聊
```

硬约束：**后端一行不改**；只改展示层级，不删后端能力；不新增 Tab / 面板；空态必须诚实。

## 2. 病灶（三处，均已修）

1. `ResultNextActions.tsx`（段⑤）固定渲染三档按钮 + 「下一步」标题 + 重复的续聊输入框
   `result-continue`；生产者为 `realRun.ts::deriveNextActions`（注释写死「恒三档」）。
2. `ResultHeadline.tsx`（段②）无真实结论时**降级为 `degrade.label`** ⇒ 「本次运行未启用模型驱动」
   被当成 **Agent 的一句话结论**渲染（伪装）；同一句又在段④ `ResultBlockers` 印一遍。
3. 页面上有**两个续聊输入框**（结果面板 `result-continue` + 中列底部 `conv-followup`）。

## 3. 实现方案（逐文件）

| 文件 | 改动 |
|---|---|
| `src/views/runs/resultActions.ts` | 删写死的 `QUICK_ACTIONS` 与 `AGENT_RESUME_INSTRUCTION`；新增纯函数 `deriveContextualActions(ctx)`（0～3 条，见 §5）。 |
| `src/views/runs/ResultContextActions.tsx` | **新增**，替代 `ResultNextActions`。渲染 `result-contextual-actions`（`actions>0` 才进 DOM）+ 保留的结果操作折叠（`result-ctas` 等，testid 不删不改）。不渲染「下一步」标题 / 三档固定按钮 / 输入框。 |
| `src/views/runs/ResultNextActions.tsx` | **删除**。 |
| `src/views/runs/ResultEnvStatus.tsx` | **新增** 执行环境状态条（`result-env-status` + `result-env-rerun`），仅 `degrade.kind === 'env'` 时进 DOM。 |
| `src/views/runs/realRun.ts` | `DegradeNotice` **新增 `kind: 'env' \| 'degraded' \| null`**（既有字段与分支语义逐字不变）；`codeplaneDegradeNotice` 归 `'degraded'`；删除 `deriveNextActions`。 |
| `src/views/runs/ResultBlockers.tsx` | 段④ 只保留 `degrade.present && degrade.kind === 'degraded'`；`env` 档由段① 专责，段④ 不再重复。 |
| `src/views/runs/ResultHeadline.tsx` | 无真实结论时改为**诚实空态**（`本次运行没有生成自然语言结论`）；**删掉对 `degrade` / `deliveryLabel` 的依赖**。 |
| `src/views/runs/ResultPanel.tsx` | 用 `ResultContextActions` 替换 `ResultNextActions`；删 `onAgentAction` / `derivation` / `result-continue` 输入态；**`result-intent` 下移到段② 之后**并收紧单行；段① 内 `result-status-line` 之后插入 `ResultEnvStatus`。 |
| `src/views/runs/FollowUpComposer.tsx` | placeholder 改 `继续告诉 AI 你想怎么处理……`；保留 `conv-followup` / `#conv-followup-input` / 提交通路 / 错误诚实展示。 |
| `src/views/LiveRunsView.tsx` | 包一层 `onFollowUp`：中列底部 follow-up **补上** `continued_from_artifact_ref`（能力不减少）；移除已失效的 `continueError` 透传。 |
| `src/styles/runs.css` | 新增 `.res-env` / `.res-ctx-region` / `.res-ctx`；清掉已无引用的 `.res-next*` / `.res-action-row` / `.res-continue*` / `.res-quick*`；`.res-intent` 收紧为单行。 |

## 4. testid 清单

**新增**：`result-contextual-actions`、`result-ctx-<key>`（见 §5）、`result-env-status`、`result-env-rerun`。

**有意退役**（本文件外的既有 testid，需明列解释）：

| testid | 理由 |
|---|---|
| `result-action-agent` | 「让智能体处理」与用户已在用 Agent 重复；三档固定按钮病灶之一。 |
| `result-action-self` | 「我自己处理」无产品价值（结果编辑能力保留在 `result-edit`）。 |
| `result-action-view-diff` | 「查看变更」改为**仅当确有 Diff** 时以 `result-ctx-code-diff` 动态出现。 |
| `result-continue`（+ `#res-continue-<id>` 输入框） | 结果面板内续聊输入框退役；续聊统一到中列底部 `conv-followup`。 |
| `result-quick` / `result-quick-<key>` | 4 条写死快捷项随 `QUICK_ACTIONS` 退役；改由 `deriveContextualActions` 按真实产物派生。 |
| `result-next-actions` | 段⑤ 容器改名 `result-contextual-actions`（语义由「下一步动作」变为「上下文快捷操作」）。 |

**保留（一个都不删或改名）**：`result-headline` / `result-headline-fallback` / `result-delivery` /
`result-blockers` / `result-rerun` / `result-no-deliverable` / `result-degrade-note` /
`result-missing-inputs` / `result-partial` / `result-ctas` / `result-edit|save|cancel|export|copy|print|store`
/ `result-export|store|print|copy-note` / `result-save-note` / `result-tab-*` / `workspace-exec-detail` /
`conv-*` / `artifact-*` / `code-*`。

## 5. 上下文动作派生规则表

`deriveContextualActions(ctx)` 只吃**已派生**的真实业务值（`ContextActionInput`，不摸原始 `RunDetail`）。
**按序判定、互斥**，先命中即定档，**最多 3 条**；不满足前置条件的一律不进列表。

| 档位 | 前置条件 | 动作（key → label → kind） | 附加前置 |
|---|---|---|---|
| 代码档 | `codeplane.present` | `code-fix` → 继续修复 → continue | — |
| | | `code-diff` → 查看差异 → diff | **仅 `codeplane.diff.present`**（红线：无 diff 绝不出现） |
| | | `code-rerun` → 重新运行 → rerun | 仅 `canRerun` |
| 数据档 | `metrics>0 \|\| findings>0` | `data-deep-dive` → 继续深入分析 → continue | — |
| | | `data-chart` → 生成图表 → continue | — |
| | | `data-export` → 导出报告 → export | 仅 `artifacts>0` |
| 知识档 | `sources>0`（非代码档） | `kb-follow-up` → 继续追问 → continue | — |
| | | `kb-sources` → 查看来源 → sources | 仅 `sources>0` |
| | | `kb-summary` → 生成摘要 → continue | — |
| 兜底 | `artifacts>0`（无以上特征） | `fallback-export` → 导出报告 → export | — |
| | | `fallback-trace` → 查看执行过程 → trace | — |
| — | 皆不满足 | **返回空数组**（段⑤ 整段不进 DOM） | — |

**执行机制（全部真实可执行，无装饰按钮）**：`continue → onContinue(instruction)`（真调
`POST /workspace/tasks`，带 `parent_run_id`）；`rerun → onRerun()`（真调 `POST /runs/{id}/replan`）；
`diff → scrollToId(document,'code-diff')`（滚动到真实变更）；`export → edit.exportResult()`
（真实本地下载 + 可见确认）；`sources → onTabChange('evidence')`；`trace → onTabChange('trace')`。

## 6. 自测（机械证据，原文关键行）

```
$ node node_modules/typescript/bin/tsc -b      → TSC_OK (exit 0)
$ node node_modules/vite/bin/vite.js build     → ✓ built in 8.73s (exit 0)

$ node qa_tmp/inc39_derive_check.cjs
PASS  code tier w/ diff + rerun
PASS  code tier w/o diff → no diff action        ← 红线：无 diff 的代码档**不出现**「查看差异」
PASS  code tier w/o diff (keys)  ["result-ctx-code-fix","result-ctx-code-rerun"]
PASS  data tier w/ artifacts
PASS  data tier w/o artifacts (no export)
PASS  knowledge tier
PASS  fallback tier
PASS  empty ⇒ []
PASS  code tier wins over data/knowledge
PASS  every continue has instruction
PASS  no engineering words in instruction/label
PASS  max 3 actions
ALL PASS

$ node qa_tmp/inc39_degrade_check.cjs
PASS  no_model → env
PASS  provider_degraded_to_mock → degraded
PASS  exception: → degraded
PASS  unregistered → degraded
PASS  no degraded, no runtimeMode → present false / kind null   ← 向后兼容语义不变
PASS  no degraded, deterministic runtimeMode → env
PASS  no degraded, llm runtimeMode → present false
PASS  no degraded, graph runtimeMode → present false
PASS  codeplane degraded → degraded
PASS  no_model label unchanged
PASS  exception diagnostic verbatim
ALL PASS
```

## 7. 偏差与未决项

1. **`执行环境状态` 的 DOM 位置**：需求 D 明写「段① 内、`result-status-line` 之后、
   `result-intent` 之前」；而需求 F 的「首屏读序」列表把执行环境状态排在**关键结论之后**。
   两处冲突。**本实现采用 D 的段① 定位**（组件级显式 DOM 锚点，且「执行环境」置顶更符合其
   提示语义）。因 F 要求 `result-intent` 下移到段② 之后，段① 内 env 自然位于 intent 之前，
   与 D 的「intent 之前」一致。
2. **动作可见标签**：需求 A 示例写「查看 Diff」，但硬约束 4 禁止可见中文含工程术语，
   且仓库既有入口已用业务词「查看差异」（`code-entry-diff`）。**本实现用「查看差异」**，
   key/testid 仍为 `code-diff` / `result-ctx-code-diff`。
3. **`trace` 机制（新增）**：需求给的机制清单只有 5 种（无 `trace`），但兜底档要求
   「查看执行过程 (trace/sources)」；而 `sources` 档有「仅 `sources>0`」前置，兜底档恰无来源。
   故**新增 `trace` 机制**（`onTabChange('trace')`，真实可见态变化），兜底档用它承载「查看执行过程」。
4. **保留「更多操作」折叠组**：硬约束 3 要求 `result-ctas` / `result-edit|save|cancel|export|copy|print|store`
   **不得删除**。它们原本在 `ResultNextActions` 的「更多操作」`<details>` 内。本实现将该折叠组
   **原样迁移**进 `ResultContextActions`（默认折叠，维持极简首屏），以同时满足「testid 不删」
   与「极简」两个约束。未决：若后续产品决定彻底移除结果编辑能力，可再单独评审。
5. **`result-save-note`**：不在硬约束 3 的必留清单，但为避免丢失「保存 / 编辑」反馈而**保留**。
6. **未删后端能力**：Agent / Skill / Tool / Memory / Evidence / Trace / Artifact / Cost / SSE
   能力全部保留，仅改展示层级。
