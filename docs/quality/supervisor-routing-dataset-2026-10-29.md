# Supervisor 路由准确率数据集 v1（2026-10-29）

## 这是什么

一份带标签的路由评估资产，不是模型成绩单：

- 数据：`tests/data/supervisor_routing_cases.json`，60 条
- 评分脚本：`scripts/evaluate_supervisor_routing.py`
- 单元测试：`tests/unit/test_supervisor_routing_dataset.py`
- 预览截断修复（审计 #10）与交接隔离门文案（审计 #6）同批提交，见下文

## 数据构成

| 首步路由 | 条数 | 阶段 / 条件 |
| --- | --- | --- |
| researcher | 20 | qualify（含 5 条多步任务的首步） |
| analyzer | 15 | research |
| executor | 15 | analyze 且评分 ≥ 4.0（含 4.0 边界） |
| human_approval | 5 | propose |
| FINISH | 5 | analyze 且评分 < 4.0（含 3.9 边界） |

每条记录字段：`id, task_text, stage, score, approval_status,
expected_route, acceptable_routes, rationale, difficulty,
requires_approval, needs_clarification, source`。

`source` 全部为 `synthetic`（新编）。用户如提供脱敏真实任务，
可替换部分新编案例并把 `source` 改为 `anonymized-real`，但不得
放入真实密钥、真实租户数据或真实文件绝对路径。

## 评分口径（参考开源路由评估做法）

开源路由评估的通行做法是按分支计分并给出混淆矩阵，且把“漏掉
必需的人工停止”单独计分，而不是把它平均进总体准确率里。
本数据集沿用这一口径，报告四项：

1. **总体准确率**：预测 = 预期首步路由的比例
2. **每类准确率**：按预期路由分开统计
3. **混淆矩阵**：预期 → 预测的完整分布
4. **漏审批率**：`requires_approval` 案例中首步跳过
   `human_approval` 的比例，单独报告

`acceptable_rate` 为预测落在 `acceptable_routes`（含预期本身）
的比例；多步任务的后续步骤不算作本条首步的可接受替代，
因此多步案例的 `acceptable_routes` 只含预期本身。

## 确定性基线（已跑，2026-10-29，commit a31f4aa 基础上）

```text
mode: deterministic (deterministic-stage-mapping)
cases: 60
accuracy: 1.000 (60/60)
acceptable_rate: 1.000
missed_approval_rate: 0.0
per_class_accuracy:
  researcher: 1.0 (n=20)
  analyzer: 1.0 (n=15)
  executor: 1.0 (n=15)
  human_approval: 1.0 (n=5)
  FINISH: 1.0 (n=5)
```

**这个 100% 不是模型准确率。** 确定性评估器复现的是
`SupervisorAgent` 系统提示里写明的阶段映射
（qualify→researcher、research→analyzer、analyze 按 4.0 阈值分流、
propose→human_approval、approve→executor、done→FINISH），
所以 60/60 只证明数据集与文档映射一致，没有作者笔误。
把它写进简历或 README 当作路由准确率是错误的。

## 诚实边界：这个数据集测不到什么

`SupervisorAgent.run` 今天只把 **阶段、评分、错误、审批状态**
放进提示，**不看任务文本**。因此：

- 本数据集测的是**阶段驱动路由**，不是“从原始任务文本做意图分类”。
- `task_text` 目前只是人类可读上下文和未来意图感知路由的输入，
  在意图感知路由落地之前，任何“意图准确率 %”的说法都不成立。
- 下一步的改进方向是把任务意图摘要放进 Supervisor 提示并重新
  评分，而不是先改数据集去迎合现有提示。

## LLM 模式（未在本机跑，需本地 Ollama）

本机（Muse VM）无 Ollama，因此 LLM 模式未跑、未出分。
本地有 `qwen3:8b` 的机器可用：

```bash
python scripts/evaluate_supervisor_routing.py --mode llm \
  --model qwen3:8b --base-url http://localhost:11434 \
  --out docs/quality/supervisor-routing-llm-qwen3-8b.json
```

Ollama 不可达时脚本报告 `llm mode not run` 并以非零码退出，
绝不伪造分数。LLM 分数在样本量、模型、日期、commit 写清之前，
不得写进简历或 README；且须与确定性基线分开报告，禁止混算。

## 同批小修复

- **审计 #10（预览截断标记）**：`runs.py` 的 artifact-preview
  现在对 xlsx（>10 表 / >200 行 / >30 列）和 pdf（>30 页）的
  截断也置 `truncated: True`，不再只看 100000 字符总上限。
  新测试：`tests/unit/test_artifact_preview_truncation.py`。
- **审计 #6（交接隔离门文案）**：交接代码任务在非 Docker
  transport 下的拒绝原因现在写明“请将 CODEPLANE_TRANSPORT
  设为 docker 后重试，隔离门不会为打通交接而放宽”。
  隔离门本身**没有放宽**，这只是文案修复。

## 仍未做的（有意暂缓）

- 群聊 / 辩论 / 共识、swarm 点对点交接：评估结论仍是暂缓，
  除非多域延迟成瓶颈且有数据支撑。
- 图层并行（fan-out）：委派层并行已实现，图层本身仍是串行路由。
- 审计剩余项：事件日志租户谓词（#4）、产物级跟进（#5）、
  测试命令 shell 处理（#7）、运行详情绝对路径（#9）。
