---
name: docx-exact-replace
description: 在 Word（.docx）文档中按精确匹配的文本执行替换，仅改动命中片段、保留其余内容与格式。当用户需要精确替换术语、修正错别字或批量替换指定字串时使用；关键词：Word、docx、精确替换、查找替换、术语统一。
metadata:
  display_name: Word 文档精确替换
  version: 1.0.0
allowed-tools: document.inspect document.edit artifact.save
---

# Word 文档精确替换

## 步骤

1. 读取文档结构，确认待替换文本确实存在
2. 对精确匹配的文本片段执行替换，仅改动命中处
3. 校验替换结果并登记交付产物

## 约束

- 仅替换精确匹配的文本，不得做模糊或语义替换
- 未命中的内容一字不改，保留原有格式
- 替换失败或无命中时不得伪造成功，须如实报告

## 工具绑定

- 工具：document.inspect
- 工具：document.edit
- 工具：artifact.save

## 示例

- 把全文中的「甲方」精确替换为「采购方」，其余文字与格式保持不变。
- 将「2023 年度」精确替换为「2024 年度」，只改命中片段。

## 参考

- [精确替换的匹配与替换规则：跨 run 文本、大小写与全半角处理的边界说明](references/docx-replace-rules.md)

## 评估

评估集见 assets/evals/（上游规范未涵盖评估段；权威评估记录在 ForgeFlow DB）。
权威评估记录存于 ForgeFlow DB；此处仅声明评估集引用
