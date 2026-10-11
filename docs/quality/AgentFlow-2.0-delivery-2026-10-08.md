# AgentFlow 工作台与技能升级验收 — 2026-10-08

依据用户指定执行文档，复用 ForgeFlow 现有框架实施。审计、调用方、真实运行与边界详见 [阶段审计](../sop/AgentFlow-2.0-AUDIT.md)。未新增 Agent、数据库、UI 框架或外部上传测试服务。

## 产品验收八问

| 问题 | 当前结论 | 证据 |
|---|---|---|
| 任务自然进入工作台？ | 已验证 | 三栏工作台与 Files → Composer 实际浏览器验证 |
| 代码生成可审查变更集？ | 已验证 | OpenHands 真修改、pytest、diff、awaiting_approval |
| 验证、审批、应用、审计？ | 本轮真实审批应用通过；完整跨重启详情恢复仍需补齐 | `_audit/code-verification-d7969db6/run.json`、`applied.json`；审批/拒绝集成测试 |
| 真实数据分析和证据？ | 已验证 CSV 示例 | `_audit/data-workbench-live.json`：实际 4 行、空值 25%、合计 70 |
| 自然语言创建技能？ | 已验证 | `_audit/skill-studio-live.json`：真实模型草案、实际试运行、保存第一版本 |
| 运行、使用和查看表现？ | 已验证 TXT/Word/Excel 清洗；未知指标不造数 | 实际三个下载产物，skills_used 指向选定版本；usage 明确统计窗口与成本未知 |
| Run → Experience → Skill 来源可见？ | 已验证手动确认沉淀的真实来源 | 试运行经验与来源运行链接、版本来源；实际浏览器读取学习详情 |
| Evaluation/Evolution/Canary/Promote 持续改进？ | 既有机制测试通过，完整真实端到端尚未证明 | 49 条相关机制测试；默认门禁保持，不冒称自主学习已验收 |

因此本轮可交付技能和工作台升级，但不将整个 AgentFlow 2.0 宣称为全部八项最终验收完成。

## 验证记录

- 后端 memory 首次全量：3315 passed、1 failed、6 skipped、1 xfailed。字段查询缺少 schema 条件已修复，4 条相关测试全过。修复后再次完整复验：3315 passed、1 failed、6 skipped、1 xfailed，208.99 秒；这次唯一失败是 Windows 沙箱 os_link 阳性对照被 CPU 配额终止（0.045 秒）。随后整组沙箱实际重跑 84 passed、4 skipped、28.16 秒。保留该间歇失败，未据分组重跑冒称 memory 全量零失败；CPU 配额波动根因尚未确定。
- PostgreSQL 首次同样 3315 passed、1 failed、6 skipped、1 xfailed，命中同一测试查询缺陷；修复后隔离 schema `agentflow2_qa_c32ed9222a` 完整复验：3316 passed、6 skipped、1 xfailed，276.95 秒。
- 前端全量 Playwright：164 passed。两条过期文案断言已改为本轮真实数据/变更集行为，仍检查入口可见和未知数据空态。
- 前端 eslint、TypeScript 与生产构建通过。构建仍提示部分包超过 500kB，属于后续拆包优化项。
- Make lint、format-check、typecheck、lint-ratchet 通过：31 个受管文件；历史 lint debt 456 ≤ 基线 460，未扩大门禁范围掩盖旧债。
- 依赖检查通过。真实浏览器：技能列表、来源经验、SKILL.md 导出、实际删除、文件进入工作台、390px 无溢出，console error 为 0。
- 真模型清洗：TXT / Word / Excel 均 passed、data_preserved=true、skill_used=true。测试补充格式/公式/数值/缩进保护及替换零命中失败。
- 异常测试覆盖越权、异租户、无效/空指令、试运行与指令不一致、模型生成失败、版本保存回滚、归档后不可继续使用，不以假响应替代真实验证。

完整日志在父工作区 `_audit/agentflow2-*.txt`，真实产物与截图在项目 `_audit/`。旧 SSOT 已完整归档，当前事实与历史不混写。
