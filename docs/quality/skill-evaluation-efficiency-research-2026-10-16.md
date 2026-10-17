# Skill 正式评测与研发效率：核实后的整改依据

核实日期：2026-10-16。结论：先缩短反馈回路、固定业务验收和证据，再考虑换框架。以下是适配建议，不是已经测出的 ForgeFlow 提升。

## 1. 将日常回归与最终验收分开，先用现有 pytest 能力

pytest 官方支持 `--lf` 只重跑上次失败，`--ff` 失败优先但仍跑全量；无失败缓存时 `--lf` 默认全跑。日常修改应通过现有 `scripts/run_tests.py` 传入明确受影响测试路径，修失败时传 `--lf`，一次稳定后再全量。输出必须分别记录执行、未选、跳过数量，不能把局部通过叫作全量通过。[pytest 官方说明](https://docs.pytest.org/en/stable/how-to/cache.html)

`pytest-testmon` 用执行覆盖关系选择依赖测试，首次需采集完整关系；官方明确不跟踪静态文件与外部服务。因此 SKILL.md、资产、SQL 迁移、Postgres 配置、HTTP 行为变化必须显式追加对应测试或全量，不能仅靠自动依赖图。先不新增依赖；只有测出路径选择维护成本高，再在独立环境试点。缓存应绑定 Python、依赖锁、平台、配置及迁移/资产指纹，不能用每次随机数据库名作为唯一环境键。[testmon 官方机制与限制](https://www.testmon.org/)

CI 如需进一步加速，可用按历史耗时分组的 pytest-split；它是分片，不是省略测试。每组仍走现有 UUID 数据库、NOSUPERUSER/NOBYPASSRLS 角色和迁移，资源目录、缓存及端口必须分开；汇总全部分片 nodeids、失败与缺失后才算完整。异步 fixture 和共享外部服务尚需本地试点，不能直接并发现有全部套件。[pytest-split 官方仓库](https://github.com/jerry-git/pytest-split)

**省时点**：小改不再反复支付约 4,140 例的整套运行成本；全量兜底与数据库安全边界保留。实际收益必须测量，不承诺倍数。

## 2. 正式业务集先确认来源和验收，再做成对比较

Anthropic 将任务、重复尝试、评分器、执行轨迹、环境结果分开，建议组合程序、模型与人工评分，检查最终环境结果而非相信回复，并用人工校准评分器。正式评测沿用 ForgeFlow 的冻结 GoldenSet、资源哈希、实际文件断言及候选发布门禁。[Anthropic，2026-01-09](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)

当前合成烟测继续明确标为合成。业务保留集必须来自可核实业务任务，由人确认预期、资源权属与保留用途；生成器扩写不等于人审，也不能为凑够最少 20 例自动填写 human 标签。没有这些来源时，应交付待审核数据集和实际烟测结果，正式发布保持阻止。发现评分器误判时保留原始成绩，记录评分规则版本后重新评分，不改写历史。

**省时点**：提前排除错误验收、路径假设和词面误判，避免修复本来正确的能力；一套真实业务契约同时服务回归和发布。

## 3. Skill 对照分阶段运行，复用重复确定性工作

Anthropic 的 skill-creator 明确比较无技能/有技能或旧版/新版，保存实际产物、耗时、Token 与逐项证据；先少量案例迭代，再扩大。它还建议从轨迹中发现反复编写的同类脚本，将稳定部分沉淀为技能资源。DeerFlow 的公开 skill-creator 同样采用这一流程。[Anthropic 固定版本说明](https://github.com/anthropics/skills/blob/dbd4588f9e1033efb41dad4bef2f7947c8993d44/skills/skill-creator/SKILL.md)、[DeerFlow 固定版本说明](https://github.com/bytedance/deer-flow/blob/ec07de9618b4194533fea3d8bf8a2e4fbcca5385/skills/public/skill-creator/SKILL.md)

ForgeFlow 可先跑改动相关失败案例及代表性烟测，满足后再跑正式完整保留集；不降低现有正式发布样本门槛。文档解析、哈希、断言等确定性阶段可按输入和实现哈希复用；模型输出、搜索时效、远程副作用不因缓存而跳过实际测量。本地 Ollama 默认单并发，不照搬官方云端“全部同时跑”，避免争用污染耗时。报告应区分命中缓存和真实执行，保留对照顺序与固定参数。

**省时点**：不在明显失败候选上消耗整套模型评测；稳定解析和产物检查不重复劳动，避免把缓存误算成能力。

## 4. 复用已验证启动配方与小上下文，避免反复环境发现

Claude Code 官方记录按需加载技能，并描述保存经实际运行验证的项目启动配方供后续验证复用。DeerFlow 的元数据→指令→资源按需加载也用于保持上下文精简。[Claude Code Skills](https://code.claude.com/docs/en/skills)、[DeerFlow 固定版本 README](https://github.com/bytedance/deer-flow/blob/ec07de9618b4194533fea3d8bf8a2e4fbcca5385/README.md)

ForgeFlow 应复用已有 codeplane、依赖锁和启动检测，将实际成功的安装、启动、检查方式及环境版本作为项目证据保存；配置或锁文件变化即失效重检。技能只先展示名称用途，选定后固定版本加载，不能把整包和全部历史轨迹塞给 8B 模型。包中脚本仍是非可信资源，不能读到就执行。

**省时点**：减少每次探索如何启动、重装依赖及无关上下文消耗；不引入第三套主运行时。

## 5. 按轨迹定位框架瓶颈，单变量验证，避免盲目迁移

DeepSeek 官方 Harness 仓库已核实存在，其架构区分持久会话事实和瞬时事件，从追加日志推导恢复、转录及遥测。这适合参考为 ForgeFlow 现有检查点补齐可重建证据，不意味着复制其插件树或改写主循环。[固定版本架构](https://github.com/deepseek-ai/deepseek-harness/blob/86a402200d95fecef4529d969836f1fe13c6cb0d/docs/architecture.md)

一手论文 *Finding the Right Fit: Model-Harness Interactions across Agent Tasks*，arXiv v1 为 2026-10-01，研究模型、框架和任务组合；其外部结果不能当本项目成绩。对 ForgeFlow 应分别记录规划、模型、工具、等待、验证时间和重复失败次数，每轮只改一个瓶颈，使用同模型参数和独立任务比较。[论文与版本记录](https://arxiv.org/abs/2610.00917)

**省时点**：用真实轨迹确定到底是模型推理、工具重试还是重复整套测试慢；避免为“专业性”增加未经证明的框架开销。

## 版本与未验证边界

本次通过 `git ls-remote ... HEAD` 核实：anthropics/skills `dbd4588f9e1033efb41dad4bef2f7947c8993d44`；bytedance/deer-flow `ec07de9618b4194533fea3d8bf8a2e4fbcca5385`；deepseek-ai/deepseek-harness `86a402200d95fecef4529d969836f1fe13c6cb0d`；langchain-ai/deepagents `7f474e1527dd8b014d53ebb6aa66321a54798ced`。DeepAgents README 未给出足以支持“自动评测缓存”的实现证据，本报告不宣称它有该机制，也不据此改造。动态官网只记录本次读取日，不猜发布日期。本报告仅调研，没有安装插件、运行模型或修改业务代码。
