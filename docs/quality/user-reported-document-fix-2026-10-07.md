# 用户截图问题的复现、修复与复验

此前简单 Word 样例成功，不能证明所有上传流程都正确。用户截图中的运行 `2ee3c9e1-df20-4a93-a89b-6e816857c6c5` 实际失败：文件为普通 `.txt` 说明，文本读取成功后被当作 Python 送入 `code.lint`，得到 `invalid decimal literal`，随后重复三次，只生成运行报告，没有修改文件。

## 本轮修复

ReAct 按平台注册的附件原始名称和类型筛选工具。普通文字、Word、表格、PDF 附件不再提供代码执行/检查工具；真实代码附件或显式代码仓库保留相应能力。正文提到 lint 或代码路径，不改变附件类型。系统指令明确要求修改请求落实到编辑工具和真实文件产物，无法确定的数据需澄清，不能捏造修改。

最近对话入口改为 `/conversations`，提供搜索和简洁列表，点击进入 `/chat/{session_id}` 对话。旧 `/tasks`、`/runs` 及其运行深链仍用于详细执行记录。回复 Markdown 不再继承 pre-wrap，修复段落异常留白；运行报告默认折叠，真实修改文件保持直接可下载。

侧栏和最近对话列表新增删除按钮，确认后调用已有租户隔离的会话软删除接口。失败保留记录并显示错误；删除当前会话清除保存的会话标识和界面，刷新不恢复已删记录。新对话从其他页面进入也清除旧会话。

技能列表提供删除按钮，调用新增 `/skills/{id}/archive`，将技能移出发现列表和运行选择；历史版本、评估和运行引用保留。仅经理及管理员显示此动作，后端继续执行 write:skills 权限检查。原有草稿硬删除、退役和恢复入口保留。

## 真实复验

1. 同一个用户附件、同一句“检查该文件是否正确，并进行优化”重新执行：新运行 `7691c605-3999-4692-be60-3b31787098be` completed，实际调用 textfile.inspect、textfile.edit；三行文本有修改，返回 `新建 文本文档.edited.txt`。原始失败历史保留，没有篡改其状态。
2. 真实 qwen3:8b + PostgreSQL：Word 自然语言修改名称及销售额 100万元 → 120万元，下载并重新打开确认两项内容。新运行 `2319589e-01ae-45c7-9a82-0de18b8d8776`，产物 36675 字节。
3. 在本机 5173 前端登录 manager-1，经真实 API 打开修复会话，点击下载并核对修改后的文本。浏览器没有页面运行错误。
4. 新建临时技能和临时对话，通过实际前端删除按钮操作；技能从服务端列表消失，会话详情返回 404。未删除原有用户对话或技能。

真实日志位于 `D:/Agentxm/Multi-Agent/_audit/user-failure-retry.txt`、`user-word-data-repair.txt`、`user-live-browser.txt`。真实界面截图及下载文件在项目 `_audit/live-repaired-chat.png`、`live-conversations.png`、`live-skills.png`、`repaired-user-document.txt`。

## 回归

后端全量 3278 passed、6 skipped、1 xfailed；前端全量 141 passed。日志：`D:/Agentxm/Multi-Agent/_audit/user-followup-backend-full.txt`、`user-followup-e2e-final.txt`。后续界面细调的专项复验见 `user-followup-ui-final.txt`；受管 lint、format、typecheck 及前端构建通过。新增用例覆盖普通文件不可误用代码检查、代码文件能力保留、技能移出列表但保留版本、会话删除失败/取消/刷新，以及技能删除实际请求。

这些证据覆盖本次用户报告的路径，不构成任意文档修复、跨实例企业级容灾或全局数据正确性的保证。
