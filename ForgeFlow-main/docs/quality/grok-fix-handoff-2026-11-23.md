# Grok 修复交接 — 2026-11-23

当前主项目是 `ForgeFlow-main/`。仓库根目录的 `forgeflow-test-*`、`forgeflow-handoff-*` 是历史版本副本，不是本轮修复目标，不要将它们当作当前源码或重复修改。

代码基线：`aee434b`。以下是此基线检查发现、尚未修复的问题；请先写失败测试，再修复并运行回归测试。

1. **MCP 写入审批未绑定申请人。** `forgeflow/mcp/approvals.py::find_approved_write` 接收 actor，但未检查审批 requester。最小复现：审批 requester=alice、工具及 payload_digest 相同，在相同租户用 actor=bob 查询仍返回 Alice 的审批。Bob 若也有工具权限，可进入后续授权。应限制申请人匹配，并保留审批人独立审核、租户和 payload 校验。
2. **满队列仍能接受需要重新启动的幂等任务。** `forgeflow/runtime/dispatcher.py::dispatch` 达到容量时只检查 prior 是否存在。已有 interrupted 且 detail.dispatch_started=False 的任务会继续进入重新调度。应让需要新执行的重试遵守容量限制；只返回已有任务结果的重试仍可放行，不能破坏防止副作用重放的标记。
3. **HubSpot 备注失败没有进入动作失败列表。** `forgeflow/agents/executor.py::_sync_to_crm` 中 add_note 异常仅记录日志，没有追加失败动作。其他 CRM 操作成功时，会漏报备注失败。应记录失败并让结果如实显示部分完成，保留已成功动作。

最近验证：本轮此前全量单元测试 3679 passed / 7 skipped；最后连接退出异常补强后，相关回归 109 passed / 1 skipped。这些结果不代表上述三个新问题通过验收，也不代表真实邮件、CRM 或多实例部署已验收。

本次上传包含用户要求保留的历史项目副本和目标文档。`tmp/`、`output/`、被 Git 忽略的密钥、依赖与运行数据不属于可交付源码，未公开上传。
