# 对话布局与上下文代码编辑修复

## 问题与修改

旧工作台三栏位于 860px 容器，两侧占据大部分空间，空首页和聊天均变成窄列。现在默认只呈现宽聊天区域，上下文/文件通过按钮展开，对象详情在选择后打开并可关闭。原有产物、执行记录、审批入口保留。用户消息右对齐，灰色人形头像位于右侧；AI 消息左对齐，使用灰色机器人头像。

原有文本编辑可能将 TXT 内的 C 源码按文字追加，生成函数外 return，或用 return 替换结束大括号。指令现在明确要求读取结构与作用域，关键歧义先澄清。工具在写入前检查 C/C++ 内容中的 return 作用域与大括号；检查失败不生成产物。对明确的“main 缺少 return 0，增加该语句”，提供保留原函数逻辑的结构定位修复；有效模型操作优先保留，不把所有代码请求都套成同一改动。检查不等于完整编译器，未执行编译时不得宣称编译通过。

## 验证

- 前端完整 Playwright：164 passed（34.1 秒）。布局测试改为验证默认收起、展开联动、宽聊天、详情关闭以及移动端无溢出；没有删除数据真实性、权限与变更审批检查。
- 后端相关回归：39 passed，包括上下文代码作用域、错误替换结束括号、字符串/注释中的 return、指令技能及原有文本编辑。
- 前端 lint、TypeScript/生产构建通过；Make lint、format-check、typecheck、lint-ratchet 通过（32 个受管文件，历史债务 456 ≤ 460）。
- 实际浏览器：未登录首页和已登录真实历史对话的聊天宽度大于 700px；用户头像在 AI 头像右侧；390px 无横向溢出；上下文入口可展开；0 pageerror。
- 真实 Ollama + PostgreSQL 执行已修改 TXT 内 C 源码并下载核对 return 位于函数内、printf 保留。初次复验发现漏掉结束括号，记录失败后补齐检查；不以模型回复声称成功替代实际产物核对。

证据：`_audit/contextual-code-live.json`、`_audit/contextual-main-result.txt`、`_audit/chat-home-desktop.png`、`_audit/chat-conversation-desktop.png`、`_audit/chat-conversation-mobile.png`。完整日志在父工作区 `_audit/chat-*.txt`、`contextual-code-*.txt`。
