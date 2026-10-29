# 代码执行重复与最终结果丢失修复（2026-10-29）

## 本机现象（8857e4b，B 运行 8a34c87d）

交接本身已通：Docker 容器启动，`read_handoff` 被调用，服务端
提取的交接数据含 A 的 DOCX 正文，容器内新建 `calc.py` /
`test_calc.py` 且 `pytest -q` 1 passed。但整条任务未通过验收：

- 代码工具记录出现两次成功执行，之后再次执行降级；
- 协调器最终因模型调用达到上限 24 而失败；
- 最终运行详情没有保留完整的代码平面结果。

这三点不是交接内容的问题，是执行与结果组装的问题。

## 原因（代码里可证）

1. **结果组装是 last-wins。** `orchestrator._assemble_codeplane`
   遍历全部 `code.execute` 调用，后一个覆盖前一个。一次降级
   的空 payload 就能把前面成功的 diff/测试证据全部抹掉。
2. **重复执行没有任何幂等。** `tool_handlers.code_execute`
   每次调用都新建工作区、跑一次完整 Docker 引擎、占一次预算。
   模型重试一次就是一次完整容器运行；引擎在重试时不可用，
   就产生一次降级记录。审批恢复分支已有“复用不重跑”的先例，
   但普通重试没有。
3. **B 项目既有代码未被用作工作区来源，与上述两点无关。**
   `_codeplane_source` 只认 `repo_path` / `paths[0]`，两者都
   没有时就是空工作区——引擎在空工作区新建文件是正确行为，
   不是它改错了代码。复验既有代码必须在任务里声明 B 项目
   路径（见下文），不能靠空工作区任务证明“修改了既有代码”。

## 修复

- `code_execute`：在 Docker 门和引擎可用检查之前，若本运行
  的 `tool_invocations` 里已有 `ok=True` 且
  `codeplane.outcome="succeeded"` 的调用，原样复用该 payload
  （加 `reused: True`，文案说明未重新执行引擎），不建新
  工作区、不跑引擎、不占预算。审批/工作区恢复分支优先；
  真需要重跑的调用方传 `force_rerun=True`。
- `_assemble_codeplane`：成功执行优先于之后任何降级/失败
  执行；多个成功执行取最后一个（当前工作区的结果），但
  diff/tests 按字段回填——后一个成功执行若没报测试，不抹
  掉前一个成功执行的测试证据。无成功执行时仍保留最后一次
  的诚实降级结果，不伪造成功。
- Docker 门、租户/用户校验、审批门均未动。

## 验证

- 新测试 `tests/unit/test_code_execute_idempotency.py`（4 个）：
  成功+降级组装保留成功 diff/测试、无成功时保留降级、
  二次调用复用且不触引擎、`force_rerun` 绕过复用。
- 反向验证：撤掉修复跑新测试，2 个失败（组装丢成功、
  二次调用无 `reused` 键），与本机现象一致。

## 复验（既有代码）

1. `git pull` 到新提交后重启 API；
2. B 任务必须声明 B 项目路径：任务 context/输入里给
   `repo_path` 或 `paths` 指向 B 项目的本地路径（前端工作区
   任务的“选择工作区”即此含义），否则仍是空工作区；
3. 任务 context 只含 `{"handoff_from_run_id": "<A run id>"}`，
   意图写清“按 A 文档实现/修改 B 项目既有文件 X，不要新建
   同名文件”；
4. 通过标准：只应有一次真实 Docker 执行（后续重试若有，
   记录里应是 `reused: True` 而非新容器），最终运行详情的
   `codeplane.diff` / `codeplane.tests` 是成功那次的结果，
   且 diff 是对 B 既有文件的修改，不是空工作区新建。
