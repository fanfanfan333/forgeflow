# Skill 运行与评测：一手资料核对

核对日期：2026-10-17。只记录设计依据，不将外部成绩视为 ForgeFlow 的效果；本报告不含业务案例或答案。

## 已核实的机制

| 来源与固定版本 | 代码或说明中实际存在的机制 | 本项目可采用的部分 |
| --- | --- | --- |
| Anthropic `skills`，`dbd4588f9e1033efb41dad4bef2f7947c8993d44`，提交时间 2026-10-09 UTC | `skill-creator/SKILL.md` 将包分为元数据、正文、按需资源；支持沉淀重复使用的脚本。创建技能时比较无技能，修改技能时比较旧版；保存断言、产物、耗时与 Token，并检查逐例轨迹。 | 评测完整固定版本，不能只复制正文而丢弃资源与流程；区分触发率与任务完成率。 |
| Claude Code 官方 Skills 文档，2026-10-17 读取，滚动文档没有固定发布日期 | 正文按需加载，附带资源可继续读取。内置技能也可以是提示词驱动；`run-skill-generator` 将实际成功的启动步骤保存为项目配方，减少重复探索。 | 问题不在“用了提示词”，而在是否复用了经验证的方法、有没有可观测增益。 |
| ByteDance `deer-flow`，`169002a4c995881ae046f34a540bb83b6ac828d6`，提交时间 2026-10-17 UTC | `skill_activation_middleware.py` 将显式激活的正文接入运行上下文；`skill_context.py` 跟踪实际读取的技能文件；`tool_policy.py` 区分未声明工具范围与显式空白名单。 | 未声明应继承父任务范围；显式空列表不能误变成不限制。加载不等于执行，更不等于业务验收通过。 |
| ACE，arXiv `2510.04618v3` | 首次提交 2025-10-06，v3 修订于 2026-03-29。通过生成、反思、整理及结构化增量修改维护上下文，避免反复整体重写导致知识丢失。 | 从实际执行反馈提出有来源的局部修订；保留失败边界和旧版，不凭文字合理性自动发布。 |

一手链接：

- [Anthropic skill-creator 固定文件](https://github.com/anthropics/skills/blob/dbd4588f9e1033efb41dad4bef2f7947c8993d44/skills/skill-creator/SKILL.md)
- [Claude Code Skills 官方文档](https://code.claude.com/docs/en/skills)
- [DeerFlow 工具范围逻辑](https://github.com/bytedance/deer-flow/blob/169002a4c995881ae046f34a540bb83b6ac828d6/backend/packages/harness/deerflow/skills/tool_policy.py)
- [DeerFlow 激活中间件](https://github.com/bytedance/deer-flow/blob/169002a4c995881ae046f34a540bb83b6ac828d6/backend/packages/harness/deerflow/agents/middlewares/skill_activation_middleware.py)
- [DeerFlow 已读技能上下文](https://github.com/bytedance/deer-flow/blob/169002a4c995881ae046f34a540bb83b6ac828d6/backend/packages/harness/deerflow/agents/middlewares/skill_context.py)
- [ACE 固定论文版本](https://arxiv.org/abs/2510.04618v3)

## 对当前修复的判断

本地 `package_import.package_spec` 保留“未声明／显式空”的差异，与上游工具策略的关键语义一致；父任务权限仍必须是上限。`functional_evaluation.execute_case` 私有固定完整 spec，并由原有 `ProgressiveLoader` 回读资源，修复了正文评测与完整包运行不一致的问题。评测身份限制不能替代租户鉴权，完整包也不能自动执行上传脚本。

`document.inspect` 的实际处理器已支持 `task_id/field` 时，模型工具 schema 也必须暴露这两个字段；否则技能给出正确流程，模型仍可能无法发出正确调用。这是接口契约修复，不能单独宣称为 Skill 独有收益。

最小的后续价值方向：检查重复轨迹，将固定的资源定位、字段选择和检查步骤沉淀为已有 `procedure` 或受控支持资源，减少反复探索；复用现有 LangGraph Skill subgraph，不引入新运行时。只有生产和评测走相同执行路径、保留真实工具证据、预算与权限限制，并在同输入对照中改善质量或成本，才计作技能增益。不能为获得收益而削弱无技能基线、缓存测试答案或自动开启任意脚本执行。

## 评测边界

优先验证资源确实读取、工具确实按配方执行、失败能如实报告；随后比较无技能／候选／旧版的任务完成率、错误完成率和实际开销。已用于调试的案例应标为回归集，新增独立来源的保留案例才用于泛化结论。简单读取任务的无技能基线已经全对时，应寻找真实成本收益或承认该技能不适用，不能把两组全对解释为额外价值。

限制：本次核对不是对所有开源项目的排名，也未复制或运行其代码；外部模型及论文指标不构成本地收益证明。
