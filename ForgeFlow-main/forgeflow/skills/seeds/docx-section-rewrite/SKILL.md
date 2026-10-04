---
name: docx-section-rewrite
description: 改写 Word（.docx）文档中指定章节的正文，并保持原有样式与结构。当用户需要重写某一章节、调整段落文字或保持排版不变地更新 Word
  内容时使用；关键词：Word、docx、章节、改写、保持格式。
metadata:
  display_name: 改写 Word 指定章节
  version: 1.0.0
allowed-tools: document.inspect document.edit artifact.save
---

# 改写 Word 指定章节

## 步骤

1. 读取文档结构，定位目标标题及其章节区间
2. 仅改写该章节正文，保留原有段落样式、编号与层级
3. 校验改写结果并登记交付产物

## 约束

- 只改写指定章节，章节外的任何内容不得改动
- 保留原始样式与格式，不得降级为纯文本
- 改写失败时不得伪造成功，须如实报告未修改

## 工具绑定

- 工具：document.inspect
- 工具：document.edit
- 工具：artifact.save

## 示例

- 把「第三章 付款方式」整章改写为分期付款表述，其余章节与样式保持不变。
- 在保持标题层级与字体不变的前提下，重写「风险提示」章节的正文。

## 参考

- [Word 文档的标题层级与段落/样式模型，说明为何改写章节时必须保留 run 级样式](references/docx-structure.md)

## 评估

评估集见 assets/evals/（上游规范未涵盖评估段；权威评估记录在 ForgeFlow DB）。
权威评估记录存于 ForgeFlow DB；此处仅声明评估集引用
