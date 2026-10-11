# 工作台升级与真实链路验收（2026-10-07）

本轮基于现有 ForgeFlow 完成整改，保留任务、工具、审批、技能和存储框架。修改未提交或部署到外部环境，工作区原有修改未回滚。

## 完成的整改

- 工作台采用左侧会话导航、居中对话、底部输入框，支持明暗主题和手机侧栏；移除外部字体依赖。
- 新对话重置上下文；连续提问传递会话及父运行标识，刷新恢复持久化历史，支持会话搜索与历史链接。
- 输入支持 Enter 发送、Shift+Enter 换行、中文输入法保护、自动增高。失败保留草稿和附件，阻止重复提交。
- 运行中实际停止任务；回复支持 Markdown、复制、错误、缺少输入及文件产物。失败和取消不会显示成成功。
- 退出登录清理私有查询缓存，顶栏读取真实健康状态。
- 修复通用 ReAct 文档工具未获得类型化上传路径的缺陷；平台注入真实资源路径和名称，保护显式 edits/spec，拒绝模型伪造路径。新增 Word、表格、文本、PDF 参数及真实 Word 工具闭环回归。

资源隔离、有界上传/排队、技能进程内并发保护和单实例持久卷整改见 [前一阶段审查](enterprise-review-2026-10-07.md)。本报告补充该审查之后的前端重构和真实模型验证。

## 验证

- 后端全量：3272 passed、6 skipped、1 xfailed。日志：`D:/Agentxm/Multi-Agent/_audit/workspace-backend-full.txt`。
- 前端 lint、生产构建通过；全量浏览器 139 passed。日志：`D:/Agentxm/Multi-Agent/_audit/workspace-e2e-delivery.txt`；覆盖上传、审批、回放、连续对话、恢复、新对话、停止、搜索、主题、手机布局和退出后的缓存清理。
- 后端受管 lint、format、typecheck 通过；静态债务保持既有基线 460 项，不代表全仓库零问题。
- 真实 PostgreSQL 与 Ollama qwen3:8b：健康状态 healthy、数据库 connected、图 compiled；真实模型普通问答成功。
- 上传新建 Word，只给自然语言修改要求，实际执行读取、修改、保存、下载，重新打开下载文件验证修改内容。产物 36649 字节；运行 ID `5476b7eb-cfe0-4e26-bbaa-5c7fe9c7f671`。日志：`D:/Agentxm/Multi-Agent/_audit/workspace-live-natural.txt`。
- 真实复验入口：`scripts/verify_workspace_live.py --natural-language`，依赖运行中的服务和项目配置开发账号，会新建验收资源与任务，不输出密码或令牌。
- 项目 `_audit/workspace-desktop.png`、`workspace-mobile.png`、`workspace-dark.png` 为界面预览。截图使用浏览器接口替身；真实模型证据来自上述 HTTP 验证。

## 能力与部署边界

本轮确认核心工作台和简单 Word 内容修改可用，未验证任意损坏文档恢复、复杂排版完全保真、所有外部连接器或集群容灾。

通用聊天使用 ReAct 工具执行；业务 LangGraph 保留 Supervisor/Researcher/Analyzer/Executor 协作。技能沿用现有沉淀和治理链路，增加进程内并发保护；跨实例事务治理和真实业务质量评估仍需完成。

当前按单 API 进程运行。调度、停止、事件订阅仍有进程内状态；多副本生产化需要持久队列、执行租约、跨进程控制及恢复/容量验收，因此不宣称已完成分布式企业级认证。

本机前端：http://127.0.0.1:5173/；后端：http://127.0.0.1:8010/。使用项目已有配置账号登录。
