# 项目体验与文件预览整改（2026-10-08）

## 优化依据

本轮梳理了项目入口、前后端路由、资源登记/存储/预览链路、技能候选/指令/版本模块、上下文同步和主要测试入口；重点修复用户截图反映的真实问题。没有把所有文件逐行阅读或企业级认证当作本轮成果。

参考 Open WebUI 的[文件管理](https://docs.openwebui.com/features/chat-conversations/data-controls/files/)与[工作区](https://docs.openwebui.com/features/workspace/)、Dify 的[知识资源接口](https://github.com/langgenius/dify/blob/main/api/controllers/service_api/dataset/document.py)，采用清晰的资源列表、按需读取详情、独立预览和真实状态等方式；未复制上游实现或引入另一套框架。

## 发现与实际整改

| 问题 | 改动 | 用户收益 |
|---|---|---|
| 文件页复用资源组件，缺少样式加载 | 明确加载共享样式并建立独立文件页样式 | 去掉挤在一起的原生列表，统一字体、行间距、边界与按钮 |
| 预览插在整个列表之后 | 原生模态窗口，支持 Escape、焦点约束、滚动内容 | 点击后立即看到内容，键盘操作可用 |
| Word、Excel、PDF、PPTX 预览接口直接拒绝 | 实际读取段落/表格/单元格/文字；后台线程处理二进制 | 直接查看上传内容，Excel 公式按原文显示 |
| 文件列表固定前 50 条 | 后续批次加载、每页 12 项、名称搜索、类型筛选 | 控制页面密度，并能访问后续资源 |
| 文件页同时高亮工作区 | 修正侧栏当前入口判断 | 导航位置明确 |
| 候选技能全是相同的大卡片 | 紧凑列表显示实际指令摘要、状态、日期、来源经验数、编号 | 区分经验，查看原文后再整理；保留删除入口 |
| 全局同步计数抢占技能页面首屏 | 同步说明移到页面末尾 | 首屏聚焦技能管理 |

预览不会修改原文件。文本输出有长度限制，表格最多 50 列，PDF 最多读取前 5 页；缺失依赖、损坏/加密文件、扫描件显示明确说明。数据库、远端接口等登记项仍不会凭空显示内容。搜索当前已加载的资源，更多资源通过“加载更多资源”获取。现有重复上传和候选记录保留，不自动删除用户数据。

## 验证记录

- 文档预览、资源登记、删除重点后端回归：50 passed。
- 前端全量回归：166 passed（含新增分页/搜索/模态窗口与手机布局检查）。
- TypeScript/生产构建、eslint 通过。
- Make lint、format-check、typecheck、lint-ratchet 通过；61 个受管文件，历史 lint debt 456 ≤ 460。
- 真实服务读取已有 TXT、Word 和 Excel：预览内容正确返回；实际浏览器无 pageerror，390px 下无横向溢出。
- 证据：`_audit/files-binary-preview-live.json`、`_audit/files-skills-browser-live.json`、`_audit/files-preview-backend.txt`、`_audit/files-upgrade-frontend-full.txt`。
- 界面截图：`_audit/files-ui-live.png`、`_audit/files-preview-live.png`、`_audit/skill-candidates-live.png`、`_audit/skill-candidates-mobile.png`。

## 整体框架判断

现有框架具备独立执行器、真实工具调用、权限、审计、技能评估门禁、文档修改校验和 OpenViking 上下文服务，值得继续迭代。README 已明确部分业务工作流仍是模板，不能将模板数量视为生产能力。历史 lint/type 债务、单进程上下文同步队列、多节点一致性与模型提炼效果仍需独立工程投入；本轮围绕资源与技能的可用性整改，未冒称已完成分布式高可用建设。

完整后端回归记录：3653 passed、7 skipped、1 xfailed、1 failed（261.45 秒）。失败项为沙箱符号链接拒绝测试，实际返回 quota_exceeded=cpu，导致预期 fail 与实际 error 分类不一致；本轮保留该全量失败日志，未将分组通过冒充全量通过。独立复验记录在 `_audit/files-upgrade-sandbox-retest.txt`。
独立沙箱复验：84 passed、4 skipped（26.69 秒），包括此前失败的符号链接目标拒绝测试。未修改安全门禁或隐藏首次全量结果。
