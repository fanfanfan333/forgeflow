# 中文返回语句需求执行修复

问题：此前结构修复要求用户明确写出 `return 0`，且只在模型操作违反括号检查时触发。用户说“在该代码中增加一个返回语句”时，拼写修改属于合法编辑，绕过了需求核对。

改动是执行逻辑而非提示词：ReAct 将原始用户需求通过平台注入的 `_user_intent` 传给编辑工具，模型不能用“修正拼写”替换原始需求。对可识别的 C/C++ main 缺少返回语句场景，中文“返回语句/返回的语句”直接定位函数结束位置，新增 `return 0;` 并保留其余内容。保存前比较真实字节是否符合该结构修改，不符合则拒绝产出。validation.requirement_ok 来自实际字节核对，并注明未进行编译验证。

边界：这是 main 返回语句的确定性处理，并非所有语言/函数的通用语义理解。void main、不明确代码目标、删除返回语句、明确指定其他返回值，不强行改成 main return 0。既有完整代码代理、工具权限、原文件保护不变。

真实复验使用原话“在该代码中增加一个返回语句”，原文件为 main()、printf("hello word")，实际下载产物增加函数内 return 0，原字符串保留。Run a9570ef3-87ea-4c11-9cb7-37b7332d9dd5；证据 `_audit/contextual-code-live.json`、`contextual-main-result.txt` 和父工作区 `_audit/return-intent-live.txt`。

最终加载保存前需求核对后的真实复验：Run 0febb10e-cc0a-4104-a60e-d02dd1f03f22，再次确认 return 位于函数内且原逻辑保留；日志 `_audit/return-intent-live-final.txt`（父工作区）。

相关文本/技能/上下文回归 40 passed，ReAct 相关回归 36 passed。新增测试直接调用真实编辑 handler，传入模型仅拼写修改的操作和原始中文返回需求，验证返回语句出现、拼写保留、原文件未覆盖、requirement_ok=true。Make 四项门禁通过，历史 lint debt 未增加。
