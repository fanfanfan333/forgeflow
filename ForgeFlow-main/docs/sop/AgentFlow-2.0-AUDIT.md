# AgentFlow 2.0 对象与调用方审计

审计日期：2026-10-08。依据用户明确授权的《AgentFlow_2.0_执行指令.md》，按 A → H 顺序实施；不新增 Agent、数据库或 UI 框架。工作区已有大量未提交改动，保留其内容与历史证据。

## Phase A：真实对象映射

| 对象 | 事实来源与生命周期 | 当前生产调用方 | 复用点与缺口 |
|---|---|---|---|
| Workspace | `/workspace/sessions`、`/workspace/tasks`，会话串联父运行 | HomeView、useChatThread、ContextPanel | 已有上传、历史恢复、跟进；三栏 WorkbenchShell 已接线，须重新实测 |
| File / Data | `/resources/files`、`/resources/{id}/preview`；租户资源、原始 blob、真实 summary | ChatComposer、ResourcePicker | CSV/Excel 有行列统计；工作台尚缺数据对象 Inspector；Parquet 不能假称支持 |
| Run | `/runs/{id}`、SSE、持久运行记录、declared_inputs | ChatWorkspace、ObjectInspector、Runs | 输入、工具、技能、产物已有事实源；历史对象须锚定正确所属运行 |
| Change Set | 代码 codeplane diff + decision/replan；文档版本链 approve/reject/revert | useChangeSet、ChangeSetDrawer | 已有真实审批机制；普通文本/表格新产物尚不等于完整审批变更集，不得假称完成 |
| Skill | `/skills`、版本库、instructions、readiness、rules、experience、lifecycle | SkillsLibraryView、SkillsView 高级管理、显式 skill_ids 注入执行器 | 指令可实际传入模型和编辑工具；新建直接发布需审查是否绕开现有发布联锁 |
| Experience | `/experiences?run_id=`、`/experiences/{id}/lineage` | 运行 Inspector、技能高级管理 | 可追溯来源运行；待组织为直观学习时间线 |
| Evaluation / Evolution | candidate evaluate/promote、skill rollout/canary、版本演进 | 原有高级技能组件 | 复用现有评估与发布机制，禁止另造质量评分或发布规则 |
| Knowledge | 既有资源、知识检索与证据链 | KnowledgeView、Run 来源面板 | 仅组织证据体验，不新增 RAG 算法 |

## 已存在的能力

FastAPI、PostgreSQL、LangGraph、Ollama、Policy / RBAC、HITL、Skill Registry / Versioning / Evaluation / Evolution / Canary、Memory 生命周期、租户隔离、原文件保留、产物预览下载。它们是复用对象，存在代码不等于本轮端到端验收通过。

## 实施与风险

1. B：先核验既有三栏工作台及历史归属。
2. C：核验真实代码 diff、验证、审批、应用；修正无源状态，不伪造审批与测试成功。
3. D：以真实资源 summary / preview 组织数据检查与分析证据。
4. E：在上述验收后完善自然语言草案、试运行、保存技能；使用既有版本/发布门禁。
5. F/G：组织来源运行、经验、评估、候选、版本与演进记录；不可计算指标明确为空。
6. H：按用户参考图整理技能列表与右侧详情，统一安静、浅色、细分隔线视觉。

现有事实源 `目标.md` 的首段仍引用 2026-09 的 1078 条历史测试，不能代表当前树。新基线必须实际重跑双档取得；禁止据旧日志宣称本轮成功。当前服务的历史进程已结束，先恢复现有启动入口，再核验健康、依赖和真实运行。测试报告与阶段证据将继续补入本文件。

## 2026-10-08 实施结果（覆盖上面的审计时状态）

| 阶段 | 实际改动与调用方 | 真实验证及证据 |
|---|---|---|
| B | 复用三栏工作台；历史产物 Inspector 锚定所属 runId；Files 入口使用既有 ResourcePicker 并携带资源到 Composer | 工作台/历史/租户测试；`_audit/skills-library-desktop.png`、实际浏览器 Files → Workbench |
| C | OpenHands 使用真实 Python；传入测试命令；成功修改后通过既有 code.commit 门禁生成 Change Set；审批恢复应用已验证 diff | 原始运行 bdec1331-b0ab-459e-ad8a-4eb8b620059f；批准恢复 f40ff443-baec-4fde-b39a-b4c6f3d14717；准确 ID 以 `_audit/code-verification-d7969db6/applied.json` 为准；原始仓库不覆盖 |
| D | 当前输入来自 declared_inputs；Inspector 展示实际预览、字段、行列、缺失值与分析来源 | Run 0580ede0-4ea2-4df2-8c4f-2822dbf6bf7b：4 行、25% 缺失、实际求和 70；`_audit/data-workbench-live.json`、`data-workbench-desktop.png` |
| E | 自然语言 → 真实模型草案 → 上传文件试运行 → 保存不可变版本；后端核对试运行成功且指令完全一致；显式选择技能传入模型和文件编辑工具 | Run 12af5b50-1c02-484c-ace0-1c21a3a31601；`_audit/skill-studio-live.json`；失败保留表单、越权/异租户拒绝、保存失败回滚测试 |
| F/G | 复用 Experience、版本、readiness、lifecycle、rollout；显示来源运行和真实评估，现有 forgeSkill 生成候选入口；usage 只统计最近 1000 条工作区记录中的显式选择 | 实际浏览器读取来源经验、导出真实 SKILL.md；已有 evolution/canary/evaluator 闭环测试 49 条通过；未测量成本和成功率显示未知 |
| H | 技能列表和右侧详情，使用说明/运行与学习/版本；创建、编辑、导出、删除技能和候选；高级工程管理移至次级入口 | 浏览器实际删除仅验收创建的技能，归档后 404；390px 无横向溢出、深色模式、0 console error；`_audit/skills-library-*.png` |

### 文档清洗真实验证

技能 74bf854d-442c-4b61-945c-d3ab8356ab93 / 1.0.0，真实 Ollama 模型与 PostgreSQL：TXT Run 764a88b7-afbc-48e6-83de-f0572c418dca；Word Run db4c3db5-449e-4f33-8c48-5ae20b8e44e3；Excel Run 2419bd78-f768-4608-805b-da853a564444。三个产物均清理多余空白并保留姓名/数字。`_audit/skill-cleaning-result.txt/.docx/.xlsx` 为实际下载结果。补充测试保护 Word 加粗和表格、Excel 数值和公式、代码缩进与字符串内容；字面替换未命中时不再输出伪成功文件。

### 明确边界

- 自然语言技能创建、实际使用和来源经验已实测；完整真实 Learning → Canary → Promote 尚未端到端证明，不宣称自主持续学习已全面验收。
- 代码变更详情的完整跨服务重启持久恢复仍有缺口；运行头、声明输入与文件产物可恢复。不能据此宣称所有企业级持久性要求已满足。
- TXT/Word/Excel 有真实清洗结果；不承诺 PDF 原位编辑、Parquet 或所有复杂 Office 对象均受支持。
- 自动生成候选的发布仍走已有评估/审批门禁；用户手工确认保存的指令技能不是自动演进发布，不伪造评估分数。
- 未增加 Agent、数据库、向量库或 UI 框架；PostgreSQL 全量验收使用原数据库的隔离 QA schema，避免清空用户表。

最终测试结果与交付说明见 `docs/quality/AgentFlow-2.0-delivery-2026-10-08.md`；旧 SSOT 全文保存在 `docs/sop/AgentFlow-2.0-SSOT-history-2026-10-08.md`。
