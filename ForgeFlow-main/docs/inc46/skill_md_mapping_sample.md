---
name: docx-section-rewrite
description: Rewrites a specified DOCX section while preserving the original formatting.
  Use when a document section must be reworded, condensed, or expanded without touching
  the rest of the file.
license: Proprietary
compatibility: Requires python-docx and a local filesystem workspace
metadata:
  display_name: 文档章节改写
  version: 1.0.0
allowed-tools: docx.read docx.write fs.read
---

# 文档章节改写

## 步骤

1. 读取目标章节定位参数（章节标题或索引）
2. 用 docx.read 提取章节原文与格式信息
3. 按改写指令生成新文本（保持样式锚点）
4. 用 docx.write 写回并校验格式未漂移

## 约束

- 不得改动非目标章节
- 不得引入原文之外的引用来源

## 工具绑定

- docx.read — 章节提取
- docx.write — 格式化写回
- fs.read — 模板/样式读取

## 示例

- 输入：report.docx 第 3 章「太长，压缩一半」→ 输出：压缩后同格式章节

## 参考

- [DOCX 样式锚点速查](references/docx-styles.md)

## 评估

评估集见 assets/evals/（上游规范未涵盖评估段；权威评估记录在 ForgeFlow DB）。
评估集物化于 assets/evals/；权威评估记录在 ForgeFlow DB。
