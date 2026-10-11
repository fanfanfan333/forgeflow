# OpenViking 融合与运行说明（2026-10-08）

本文件更新此前仅只读桥接的交付状态。OpenViking 已作为独立上下文服务部署并完成真实联调；ForgeFlow 保留既有 LangGraph 执行、PostgreSQL、权限、文件编辑校验和技能发布门禁。

## 已完成的闭环

- 上传资源、任务产物和本地登记代码自动进入持久化同步队列。支持 TXT、Word、Excel、PDF、PPTX 的有界内容提取；远端数据库/API 仅同步登记元数据，避免复制凭证。
- 会话记录与带来源的情景记忆持久化，后续新任务能够检索、读取并引用历史内容。成功任务和明确记忆请求才触发额外提炼。
- 已发布技能通过官方技能包接口同步，各用户拥有独立副本；版本变更、撤回、删除均同步。经验进入本地候选编译和评估流程，候选不会未经评估自动发布。
- 删除历史或资源时立即屏蔽相应检索来源，并后台撤销远端副本；恢复资源会撤销旧删除任务。保留原生审计。
- 前端文件页和技能页展示实际同步状态及重试入口；回答保留可展开的来源记录。已可检索但仍刷新目录摘要的项目单独显示。
- 持久化队列支持失败重试、任务状态轮询、取消、重复提交去重、断点续传；提交成功不会冒称索引完成。

## 服务与模型

本机前端 http://127.0.0.1:5173，后端 8010，OpenViking 1933，模型适配服务 1934。实际服务健康检查均返回 200。

OpenViking 镜像固定为 v0.4.23 对应摘要：
`sha256:fb4cd789fd3303509edd1c10437ff63d4fab56b622494ac621f1fda042540ed4`。

使用独立 Docker 数据卷；旧卷保留为备份。应用用户分别使用自身 API Key，ROOT 仅用于账号初始化。配置在被 Git 忽略的 `.runtime/openviking/`，不得提交密钥文件。远端调用校验租户、用户根、URI、响应大小和读取预算，资料按不可信输入处理。

主 Agent 显式分配 8192 上下文；记忆工具调用使用 qwen3:4b 的 16384 上下文别名；普通摘要使用 qwen2.5vl:3b。适配层通过程序关闭思考输出、限制输出长度并排队执行，保留实际模型名和真实用量。嵌入使用 nomic-embed-text（768 维）。

## 启动和维护

使用已安装项目依赖的 Python 运行，工作目录为仓库根：

```powershell
python scripts/setup_openviking_local.py
python scripts/provision_openviking_users.py
python scripts/run_backend_real.py
# 另一终端
python scripts/run_frontend_real.py
```

准备脚本会检查并安装所需本地模型，使用固定镜像。后端启动脚本加载本地集成配置并启动模型适配服务。历史回填使用 `python scripts/backfill_openviking.py`；真实验收使用 `python scripts/verify_viking_assets_live.py`。重建脚本只适用于明确需要重建派生索引的维护操作，不是日常启动步骤。

## 实际验收

- 新对话无附件查询历史偏好：Run `636cc9a0-f5c8-495e-aba2-c94985f846c4`，正确回答“简体中文、三条结论、再列依据”，来源指向 OpenViking memories 下实际文件。
- 真实技能包创建、读取、更新版本和删除后 404 全部通过。
- 真实上传资料到远端检索再进入 Agent 的验证：Run `a56ba39d-4e1e-42bf-9d73-1c10fe616175`。
- 证据在 `_audit/openviking-assets-live.json`、`_audit/openviking-agent-memory-recall-live.json`、`_audit/openviking-fusion-live.json`；完整运行日志保留在父目录 `_audit/`。
- 交付检查时队列已完成 113 个资源投影、51 个会话、50 个情景来源和 8 个技能副本；还有 1 个资源索引和 1 个新会话来源等待完成。数量是队列快照，不代表不同用户文档数量。

## 质量与边界

后端 memory 和 PostgreSQL 隔离 schema 全量测试分别为 3652 passed、7 skipped、1 xfailed；最后配置、撤销/恢复与模型适配回归 18 passed。前端全量 164 passed，eslint、TypeScript/生产构建通过。Make lint、format-check、typecheck、lint-ratchet 通过：61 个受管文件，历史 lint debt 456 ≤ 460。

本地小模型在一次记忆提炼验收中返回零新增事实；因此本轮证明的是持久化的来源记忆和真实召回，不将其计为已学会推断事实。候选技能的质量仍需实际样本评估。后台迁移可观察和重试，未宣称每条历史数据均提炼成功。当前文件队列适用于本机单工作进程，尚不等于多节点高可用服务或企业级认证。

上游参考：[OpenViking](https://github.com/volcengine/OpenViking)，源码审计快照 `55c34934e7b61a8755303e0df1da7a4d4cffcc53`。通过独立 HTTP API 集成。
