---
name: document-cleaning
description: 用户上传文本、Word 或 Excel，需要规范多余空白并保留原始数据时使用。
allowed-tools: skills.read textfile.inspect textfile.edit document.inspect document.edit sheet.inspect sheet.edit artifact.save evidence.read report.render
---

# 文档清洗

1. 逐文件确认真实类型和清洗范围，读取文档或工作簿结构；禁止把 Office 二进制当作代码执行。
2. 只规范用户要求的空白或重复内容。保留姓名、编号、金额、日期、有效文本、Word 表格和 Excel 公式、格式、工作表。
3. 用户说整份工作簿时覆盖所有工作表，不擅自缩减为当前页；保留合并单元格及有效数据。
4. 保存独立输出文件并实际回读；比较数据和结构。任何必要字段或公式丢失均不算完成。
5. 逐文件返回可预览下载的结果及检查证据；不能处理的文件单独说明，不把其他文件的成功计为整批成功。

仅在需要验收说明时读取 [验收约定](references/acceptance.md)。合成回归案例位于 assets/evals/cases.json，不作为人工业务保留集，也不证明真实模型通过。
