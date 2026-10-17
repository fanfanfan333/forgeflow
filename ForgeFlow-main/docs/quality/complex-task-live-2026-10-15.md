# 复杂组合任务实测 — 2026-10-15

使用本地 API、真实上传接口、配置的 Ollama 模型、异步任务执行及产物下载。每轮创建独立 QA 租户与合成文件，结束后禁用 QA 账户。没有修改用户原始文件或历史任务。

## 最终结果

| 任务 | 真实文件验收 | 状态 |
| --- | --- | --- |
| C 代码组合修改 | hello word → hello world；main 内加入 return 0；保留 helper 的 return 7；下载后 gcc 严格编译，运行输出 hello world、退出码 0 | 通过 |
| Word 清洗与数据保留 | 清理空格；姓名、日期、金额、表格行列保持；下载文件可重新解析 | 通过 |
| Excel 多工作表清洗 | 销售与说明工作表完成清洗；金额、税率、SUM 与乘法公式、数字格式及工作表顺序保持 | 通过 |

三项最终运行均为 completed/success，实际验收证据 complete=true。检查既不依赖最终回复，也不以下载成功代替文件内容正确。

## 发现并修复

1. **增加与保留要求冲突**：此前意图解析器看到“保留 helper 的 return 7”便停止 main 的 return 0 补全。现在区分保留条款与新增条款；新增非零返回仍不会被擅自改成 return 0。
2. **Office 清洗没有需求证据**：此前 Word/Excel 文件实际正确，但平台仍标记未验证。现在对明确的空白清洗请求，比较实际产物与规范化后的完整 Office 包。公式、样式、表格、属性等意外差异不能凭工具成功绕过；仅忽略保存时变化的修改时间。
3. **Excel 漏清洗非当前表**：复测发现模型只选择销售表，遗漏说明表。整份工作簿清洗且操作仅为空白整理时，平台保证覆盖所有表；明确限定范围的请求不会被扩大。校验没有因失败而放宽。

失败、部分完成和最终通过的结果分别保留：

- [首次结果](evidence/2026-10-15/complex-tasks/before-fix-results.json)
- [中间复测](evidence/2026-10-15/complex-tasks/intermediate-results.json)
- [最终逐项证据](evidence/2026-10-15/complex-tasks/results.json)
- [真实编译运行证据](evidence/2026-10-15/complex-tasks/code-compile.json)
- [C 文件](evidence/2026-10-15/complex-tasks/code-download.txt)
- [Word 文件](evidence/2026-10-15/complex-tasks/word-download.docx)
- [Excel 文件](evidence/2026-10-15/complex-tasks/excel-download.xlsx)

## 自动回归

相关专项回归 **120 passed**；覆盖原文件不能冒充已完成产物、金额损坏、公式丢失、组合代码要求及跨工作表遗漏。代码检查通过。完整后端回归 **4065 passed，32 skipped，1 xfailed**，耗时 318.10 秒。日志：`D:/Temp/forgeflow-complex-full-regression.log`。跳过与预期失败没有计作通过。已有测试警告仍存在。

复现入口：`scripts/verify_complex_tasks_live.py`。需要本地真实 API、模型与数据库；脚本会实际产生 QA 任务和模型调用，不属于离线测试。

## 验证边界

这是三项组合任务的真实抽样回归，不是三领域各 10 项、每项重复三次的基准对比，也没有验证任意复杂业务指令。通用任务恢复、Skill 保留集对照评测等剩余计划仍不能标记完成。
