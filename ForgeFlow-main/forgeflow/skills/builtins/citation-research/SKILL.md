---
name: citation-research
description: 用户需要调查官方资料、比较方案或生成有来源支持的研究报告时使用。
allowed-tools: research.search research.read research.verify evidence.read report.render artifact.save
---

# 引用研究

1. 拆解研究问题与判断标准，优先官方文档、原始论文和项目源代码。实际搜索服务未配置时明确说明。
2. 逐个读取真实原文，保存 URL、读取时间、来源引用和内容哈希；大内容分页读取，不能用摘要代替关键证据。
3. 每个关键结论绑定原文片段，通过 research.verify 核对；source_url 可引用本任务已成功读取的 URL，禁止编造来源引用或哈希。
4. 交叉核对冲突资料，区分事实、推断和资料不足；记录版本日期，不把项目宣传效果当作本项目实测结果。
5. 引用校验失败时修订或删除结论，无法支持时标为未验证；交付含实际链接与限制的报告。

仅在需要验收说明时读取 [验收约定](references/acceptance.md)。合成回归案例位于 assets/evals/cases.json，不作为人工业务保留集，也不证明真实模型通过。
