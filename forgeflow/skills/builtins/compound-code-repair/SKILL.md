---
name: compound-code-repair
description: 用户要求同时修复代码并增加返回值等多个修改时使用，逐项验证需求。
allowed-tools: textfile.inspect textfile.edit artifact.save code.lint code.execute evidence.read report.render
---

# 组合代码修复

1. 把用户全部要求列为独立条目，先读取完整相关函数与调用方。不得用一个拼写修复替代其他要求。
2. 修改前检查文件版本；出现冲突重新读取，禁止覆盖用户新修改。保留修改前快照。
3. 按代码结构修改，C/C++ 的 return 必须在目标函数闭合括号之前；保留辅助函数、字符串和注释。
4. 使用真实编辑工具生成文件并回读最终版本，运行项目已有检查。工具执行成功不能替代需求验收。
5. 返回补丁或文件、每项需求的证据、实际检查结果与限制；环境缺少编译器时标为未验证。

仅在需要验收说明时读取 [验收约定](references/acceptance.md)。合成回归案例位于 assets/evals/cases.json，不作为人工业务保留集，也不证明真实模型通过。
