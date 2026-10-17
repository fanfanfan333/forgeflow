# 前后端代码质量加固（2026-10-10）

## 本轮目标与实际范围

本轮按“保持现有能力，增加校验，错误真实可见”的方向执行。检查前端编译配置、CI、Make 门禁、上下文检索/压缩/同步、文档预览与 A2A 协作代码。未降低既有测试或 lint 基线，未通过增加 ignore 或替换成 mock 消除问题。

参考 FastAPI 官方 [full-stack-fastapi-template 的质量配置](https://github.com/fastapi/full-stack-fastapi-template/blob/master/backend/pyproject.toml)与前后端分工，采用明确且可重复运行的类型/格式/测试门禁；[TanStack Query 官方重试说明](https://tanstack.com/query/latest/docs/framework/react/guides/query-retries)用于核对错误重试语义。本轮未替换项目框架或盲目升级依赖。

## 已完成

1. 前端 `tsconfig.app.json` 开启 `strict: true`，让空值、隐式 any、函数参数等严格检查随每次构建执行。严格预检、生产构建均通过，没有为通过而添加类型屏蔽。
2. Make 的受管范围从 62 扩到 78 个文件，纳入上下文组件、A2A、二进制预览、技能编译。现有 lint、format-check、typecheck 门禁对这些模块生效，CI 使用同一入口。
3. 修正扩大检查发现的类型问题：心跳年龄的可空比较、缺失租户的技能同步、同名 list 方法遮蔽返回类型、远端任务列表可能为 None 的迭代。非法远端任务列表现在显式失败，不假装完成。
4. 修复上下文队列日志写入：独立临时文件、flush/fsync 后原子替换，Windows 同进程替换互斥；失败保持旧记录并清理临时文件。
5. 上下文模块修复导入/枚举/不变量检查表达式并统一格式，受保护内容的完整性检查仍保留。
6. GitHub CI 前端新增 Chromium 安装和 Playwright 行为回归。代码已写入工作流，本轮没有推送或宣称远端 CI 已执行；本地实际验证完整浏览器测试。

## 验证与证据

- 重点后端回归 33 passed，覆盖消息分派、技能编译、原子落盘、失败保留和临时文件清理。
- 前端完整 157 passed（35.1 秒）；严格 TypeScript、eslint、生产构建通过。
- 扩展后 Make lint、format-check、typecheck、lint-ratchet 全部通过；78 个受管源文件，历史 lint debt 降至 447，冻结上限 460 没有增加。
- 完整后端结果追加在本文件末尾，日志 `_audit/quality-20261010-backend-full.txt`。
- 其余证据：`_audit/quality-20261010-focused.txt`、`_audit/quality-20261010-make-final.txt`、`_audit/quality-20261010-frontend-e2e.txt`、`_audit/quality-20261010-build.txt`。

## 未冒称完成的部分

存量全仓 lint/type 债务仍存在；78 个受管文件通过不等于全仓零缺陷。当前 mypy 不是全仓 strict，前端仍按既有设置跳过第三方声明文件检查；本轮没有增加这类豁免。旧的开发/测试 mock 路径和规则回退仍是明确的开发与可用性机制，生产模型耗尽不得以 mock 冒充成功。

原子写入保证单条日志不会被部分内容覆盖，Windows 互斥只限同进程，不能以此宣称多节点队列已有事务一致性。目录 fsync、跨进程锁/租约与全仓严格类型迁移应分别验证。此次优化保护原有功能，并扩大持续检查，而非以降低规则获得通过。

前端构建工具配置 `tsconfig.node.json` 同步启用 strict，完整构建再次通过，运行行为无变更。

## 最终完整结果

后端全量：3662 passed、7 skipped、1 xfailed（245.43 秒）。前端完整 157 passed，应用/构建工具 strict 构建通过；最终 eslint 复查清理了上轮删除首页功能后留下的未使用测试常量，未增加豁免。Make 四项门禁全部通过，78 个受管文件、lint debt 447。旧日志完整保留；最终 lint 日志为 `quality-20261010-frontend-lint-final.txt`。

服务恢复核验：8010 后端、5173 前端、1933 OpenViking、1934 模型适配服务均返回 200。证据 `_audit/quality-20261010-health.json`。
