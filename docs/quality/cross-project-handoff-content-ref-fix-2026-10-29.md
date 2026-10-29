# 跨项目交接 content_ref 修复（2026-10-29）

## 问题：真实交付物在交接时被静默丢弃

`runtime/artifacts.py::_deliverable_artifact` 明确写着：真实交付物
的 `content` 是**故意为空**的，字节只存在 `content_ref` 背后的文档
存储里（base64 正文会被 payload 上限截断，所以不能放进正文）。

但 `runtime/result_handoff.py::build_handoff_results` 只读内联
`content`，`runtime/orchestrator.py::_continued_context_text` 也只读
内联 `content`。于是：

- 单元测试用内联正文的合成产物，全部通过；
- 真实栈里一个成功交付了文档的源任务，派生结果却是 `[]`，
  续聊段落里印着“（无正文）”——交接看起来“未通过”，而代码本身
  没有报错。

这是本机 c9015cb 验收里“跨项目交接未通过”的一部分原因（另一
部分是代码任务的 Docker 门，见下文）。

## 修复

- `result_handoff._artifact_text`：先读内联正文，没有则按
  `content_ref` 到 `DocArtifactStore` 取字节，按格式提取文本
  （文本类直接解码；docx/xlsx/pdf/pptx 与预览端点同口径、
  同表/行/列/页上限），再由原有 `_append` 做 12000 字符/总字节
  上限。读不出或格式不支持的产物跳过，绝不编造占位正文。
- `_continued_context_text`：同一产物改用 `_artifact_text`，
  续聊段落不再对真实文档印“（无正文）”，截断仍是原来的
  每产物 1500 字符。
- 租户/用户 fail-closed、validator 上限、Docker 隔离门**全部未动**。

## 仍未变的边界（重测时必须分清）

1. **代码交接仍只走 Docker。** `tool_handlers.code_execute`
   在 `handoff_results` 非空时要求 `DockerOpenHandsEngine`，
   默认 transport 是 subprocess。重测代码交接必须先：
   ```powershell
   docker build -f Dockerfile.codeplane -t forgeflow-codeplane:1.49.6 .
   ```
   并在 `.env` 里设 `CODEPLANE_TRANSPORT=docker`、
   `CODEPLANE_DOCKER_IMAGE=forgeflow-codeplane:1.49.6`，重启 API。
   未配 Docker 时的拒绝是设计内行为，不是本次修复要消除的。
   详情见 `docs/codeplane-docker.md`。
2. **`handoff_from_run_id` 是代码平面交接。** 非代码任务的
   文档续聊请用 `continued_from_run_id` / `parent_run_id`，
   它走 `_continued_context_text`（本次同样已修）。
3. **源任务本身必须有可提取的交付物。** 若源任务只有
   `final_answer` 或 diff，交接只含那些；若三者皆无，
   派生结果仍是诚实的 `[]`，不会凭空出现文档。

## 验证

- 新测试 `tests/unit/test_result_handoff_content_ref.py`（4 个）：
  文本、docx 的 `content_ref` 交付物到达交接，缺失/不支持
  格式诚实为空，续聊段落解析 `content_ref`。
- 反向验证：撤掉修复跑新测试，3 个失败，续聊段落原样印出
  “（无正文）”，与真实栈现象一致。
- 隔离门未放宽：非 Docker 下带 `handoff_results` 的代码任务
  仍被拒绝，文案见 c9015cb 的审计 #6 修复。
