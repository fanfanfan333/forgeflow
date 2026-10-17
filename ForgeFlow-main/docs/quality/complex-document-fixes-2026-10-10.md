# 复杂文档任务整改 2026-10-10

针对 `complex-document-test-2026-10-10.md` 中的真实失败，在现有运行器、文档编辑层和前端对话组件内修复，没有替换框架或提高失败容忍阈值。

## 执行与保护

新增 `documents/requested_edits.py` 从运行器提供的原始用户需求解析明确的引号文字替换，支持嵌套中文引号。对于封闭的纯替换任务，使用明确请求生成操作，保留标题周围的项目名、版本以及非目标正文。标题替换限定段落 index，复用 run 级替换以保留周围粗体、斜体等属性。

保存前独立计算用户期望的逐段文本，与实际产物比较。写入器若不执行或漏掉任一替换，requirement_ok=false，不保存成功制品。数字不变量依据原始用户请求建立，模型的整段替换不能豁免“数字、金额或日期不变”；数字 token 的顺序和重复次数也须相同。

这条确定性路径适用于可解析的明确文字替换，不把自由改写、摘要生成等需求伪装成已获得语义证明。缺失目标或标题歧义显式失败，不猜测位置。

## 正文与表格读取

新增 `documents/content_read.py`，按原文顺序读取顶层正文和表格行，并提供 paragraph/table/row 位置及所属标题。检索同时覆盖原始问题与模型关键词，按编号问题分配证据，避免一项问题吞掉所有上下文。

内容保持既有 4,000 字符工具预算：压缩结构摘要，内容片段约 2,200 字符，显式报告 next_offset/next_char_offset。长段落或表格行截断后可继续读取未读尾部。原文经现有不可信工具输出防护包装，不能变更工具权限或作为执行指令。

## 状态与对话

轮次耗尽记录 budget_exceeded 错误，运行不再以 completed/success 收场。模型在工具失败后自述成功，最终可见答复改为未完成验收；原始模型文字单独保留在运行元数据供审计。重试仅返回文字不能清除未解决的工具错误；真实执行了授权的恢复操作或替代工具，仍可按现有机制重新验收。

前端终态优先显示经过执行结果核对的答复，避免流式草稿覆盖失败结论。新增浏览器用例证明终态失败会替换“所有要求都已完成”的流式回复。

真实复验还发现：连续读取后，模型可能只总结最后一项问题，却丢失原始五项要求。新增 `documents/question_contract.py` 为明确的只读编号问答建立逐题契约：实际读取后用聚焦的证据上下文生成结构化答案，编号必须完整且唯一，每题必须有来源位置与逐字引用；引用须属于那个实际来源。缺项、空项、错误来源或伪造引用都不作为最终答案，修复仍计入既有轮次预算。原始模型 JSON 留在元数据供审计，面向用户的答复按问题编号展示原文依据。

该契约验证的是逐题覆盖与引用一致性，不将此标记为通用语义正确性证明。本次任务簿五项答案另由独立原文期望值检查，包含“不能宣称达标”的明确结论。

## 回归过程中发现的 Windows 进程身份风险

全量回归两次出现不同合法沙箱操作在约 0.04 秒内误报 CPU 配额超限，专项 25 个合法操作与额外 100 次真实动作均未复现。没有放宽 CPU 配额或跳过失败测试。

Windows 进程快照记录的是创建者 PID，PID 重用可能使旧的、无关进程被认作当前根进程的后代；这属于代码审查发现的风险，尚不能声称是上述偶发失败的唯一原因。现在通过 GetProcessTimes 的创建时间过滤能确认早于父进程的假亲缘。时间未知时仍保守纳入配额计量，不用未知数据掩盖资源消耗。原 Job Object 和配额设置保持原值。创建时间真实测量与 PID 重用反事实回归均通过，沙箱专项 86 passed、4 skipped。

## 参考方式与边界

- [python-docx 官方 Document 接口](https://python-docx.readthedocs.io/en/latest/api/document.html)：确认 iter_inner_content 的段落/表格顺序，以及 paragraphs/tables 对修订内容和嵌套表格的边界。本轮读取顶层内容，未声称完整读取修订中的隐藏文本或嵌套表格。
- [LangGraph 官方项目](https://github.com/langchain-ai/langgraph)及[步数耗尽说明](https://docs.langchain.com/oss/python/langgraph/errors/GRAPH_RECURSION_LIMIT)：对照有限循环与终止条件；本轮修复本项目错误传播，没有以无限提高轮次掩盖循环。
- [Microsoft PROCESSENTRY32](https://learn.microsoft.com/en-us/windows/win32/api/tlhelp32/ns-tlhelp32-processentry32)与[GetProcessTimes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes)：确认父 PID、创建时间和 CPU 计量字段。

本轮没有宣称 Word 逐页视觉渲染或通用 LLM 语义评审已经通过；这些仍为未测量。桌面原件保留，测试均返回独立产物。

## 验证证据

证据位于 `_audit/complex-document/`。修复前、各次复验和最终 verified 记录分别保存，不覆盖原始失败事实。可用该目录的 `verify_fixed.py` 重复三项真实模型测试。

- 前端完整浏览器回归 158 passed；生产构建与 ESLint 通过。
- 81 文件受管 lint、格式、类型门禁通过，lint debt 443≤460；没有增加忽略或抬高基线。
- 关键编辑与重试回归含实际工具失败、空写入器反事实、数字越改、标题混合 run、长段落续读、原始多问题检索等。
- 最后完整后端：3689 passed、22 skipped、1 xfailed，240.17 秒，证据 `final-backend.txt`。其中未启用的外部 realstack 用例按既有环境条件跳过，没有计为通过；真实模型三场景独立执行。先前失败的两轮全量日志保留，最终门禁无失败，没有删除用例或放宽沙箱预算。

## 最终真实场景

1. 三处跨位置编辑：运行 `2c652e04-a80f-4d93-b439-084b8dd3ec63`，全部正文逐段等于期望，项目名与版本保留、数字 token 不变、43 表 XML 不变、段落属性不变。
2. 五项表格问答：最终运行 `65eef49d-dfdc-4ab2-b167-045702e05911`，全部字段和配置值正确，最小样本 30、时长 24h、待校准且不能宣称达标均明确回答，逐项带真实引用。前两次复验仍有遗漏与目标漂移，分别保存，不计通过。
3. 历史续改：运行 `82803e17-9f51-4b10-9a42-7293935b90b4`，原会话和 parent 链保持，基于第一轮正确产物完成两项替换，全部其余段落、数字、表格和段落属性保持。

三场景桌面原件 SHA256 均与最初上传一致。证据：`verified-edit-checks.json`、`verified-analysis-checks.json`、`verified-followup-checks.json` 及对应运行快照；最后一次问答日志为 `verified-analysis-live.txt`。
