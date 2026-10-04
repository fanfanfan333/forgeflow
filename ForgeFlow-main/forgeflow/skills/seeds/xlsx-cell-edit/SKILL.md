---
name: xlsx-cell-edit
description: 编辑 Excel（.xlsx）工作簿中指定单元格的值或文本，保留工作表结构与其余数据。当用户需要修改某个单元格内容、修正表内数据或更新台账单元格时使用；关键词：Excel、xlsx、单元格、表格、改值。
metadata:
  display_name: 编辑 Excel 单元格
  version: 1.0.0
allowed-tools: sheet.inspect sheet.edit artifact.save
---

# 编辑 Excel 单元格

## 步骤

1. 读取工作簿结构，确认目标单元格/工作表
2. 编辑指定单元格的值或文本，保留其余数据
3. 校验编辑结果并登记交付产物

## 约束

- 仅编辑目标单元格，不得改动其它工作表与数据
- 保留工作簿结构、公式与格式
- 定位失败或编辑失败时不得伪造成功，须如实报告

## 工具绑定

- 工具：sheet.inspect
- 工具：sheet.edit
- 工具：artifact.save

## 示例

- 把「销售额」工作表的 B2 单元格改为 4200，其余单元格不变。
- 更新台账中 A1 标题单元格的文字，保留其它行数据。

## 参考

- [XLSX 的工作表/单元格模型与 set_cell / replace_text 的语义边界](references/xlsx-cell-model.md)

## 评估

评估集见 assets/evals/（上游规范未涵盖评估段；权威评估记录在 ForgeFlow DB）。
权威评估记录存于 ForgeFlow DB；此处仅声明评估集引用
