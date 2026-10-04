---
name: pptx-slide-text-edit
description: 编辑 PowerPoint（.pptx）演示文稿中指定幻灯片/形状的文本，保留版式与主题。当用户需要修改幻灯片标题、替换正文文本框或更新备注时使用；关键词：PPT、pptx、幻灯片、文本、标题、备注。
metadata:
  display_name: 编辑 PPT 幻灯片文本
  version: 1.0.0
allowed-tools: document.inspect document.edit artifact.save
---

# 编辑 PPT 幻灯片文本

## 步骤

1. 读取演示文稿结构与幻灯片数量
2. 编辑指定幻灯片/形状的文本，保留版式与主题
3. 校验编辑结果并登记交付产物

## 约束

- 仅编辑目标幻灯片/形状的文本，不得改动其它幻灯片
- 保留版式、主题与形状几何，不改变呈现结构
- 定位失败或编辑失败时不得伪造成功，须如实报告

## 工具绑定

- 工具：document.inspect
- 工具：document.edit
- 工具：artifact.save

## 示例

- 把第 1 页幻灯片标题改为「2024 年度经营回顾」，版式不变。
- 更新第 3 页标题文本框文字，其余形状与备注保持原样。

## 参考

- [PPTX 的幻灯片/形状/文本框模型，说明按索引定位形状并仅改文本的边界](references/pptx-shape-model.md)

## 评估

评估集见 assets/evals/（上游规范未涵盖评估段；权威评估记录在 ForgeFlow DB）。
权威评估记录存于 ForgeFlow DB；此处仅声明评估集引用
