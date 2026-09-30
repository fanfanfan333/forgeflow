"""INC28 W6 —— 代码面/控制面两条 import 红线的**机械门禁**（AST 扫描）。

## 为什么会有这条钉子（留存的经验）

`forgeflow/codeplane/runner/README.md` 一度声称两条隔离红线「pinned by a drift
test (`tests/test_inc25_contracts.py`, S4)」，而**该文件并不存在**，全仓 `tests/`
也**没有任何**测试对越界 import 做扫描 —— 两条红线只靠纪律维持。一条只靠纪律的
红线会在某次重构里被无声突破（这正是「假绿」的来源）。本文件把两条红线变成
**会变红的钉子**：

  1. `forgeflow/**`（**排除** `forgeflow/codeplane/runner/**`）不得 `import
     openhands`：控制面所在的 venv 没有（也不该有）OpenHands SDK（`openai<3` /
     `litellm` 与 ForgeFlow venv 的 `openai==3.19.0` 冲突）。
  2. `forgeflow/codeplane/runner/**` 不得 `import forgeflow`：runner 跑在
     OpenHands venv 里，那里没装 ForgeFlow。

## 方法纪律

* 用 **AST**（`ast.Import` / `ast.ImportFrom`）判定，**不是**字符串匹配 —— 文档/
  docstring 里出现 `openhands` 一词不算越界（本文件自己的 docstring 就出现了）。
* 相对 import（`from . import x`）不是跨包 import，跳过。
* 钉子必须**可被证伪**：本文件带**反事实注入**（对合成源码扫描必须判定违规），
  证明扫描器真的会红，而不是「自证恒真」。

引文一律 `file.py::symbol`，不用行号。
"""

from __future__ import annotations

import ast
from pathlib import Path

#: 扫描根：仓库内的 ``forgeflow`` 包（本文件在 ``tests/unit/`` 下）。
_PACKAGE = Path(__file__).resolve().parents[2] / "forgeflow"
_RUNNER_DIR = _PACKAGE / "codeplane" / "runner"

#: 两条红线：{被扫描的文件集合描述: (扫描函数, 禁止的顶层模块名)}
_FORBIDDEN_IN_CONTROL_PLANE = "openhands"
_FORBIDDEN_IN_RUNNER = "forgeflow"


def _imported_top_levels(source: str) -> set[str]:
    """源码里所有 **绝对** import 的顶层模块名（AST，非文本匹配）。"""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # ``level > 0`` ⇒ 相对 import（包内引用），不是跨包 import。
            if node.level and node.level > 0:
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def _is_runner_file(path: Path) -> bool:
    return _RUNNER_DIR in path.parents


def _scan(files: list[Path], forbidden_root: str) -> list[tuple[str, str]]:
    """返回 ``[(文件, 违规模块名), ...]``（空列表 = 通过）。"""
    violations: list[tuple[str, str]] = []
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover — unreadable ⇒ skip
            continue
        if forbidden_root in _imported_top_levels(source):
            violations.append((str(path), forbidden_root))
    return violations


# --------------------------------------------------------------------------- #
# 红线 1 —— 控制面不得 import openhands                                          #
# --------------------------------------------------------------------------- #
def test_control_plane_does_not_import_openhands():
    files = [p for p in _python_files(_PACKAGE) if not _is_runner_file(p)]
    assert files, "扫描域为空 —— forgeflow 包未找到 .py 文件，钉子失去意义"
    violations = _scan(files, _FORBIDDEN_IN_CONTROL_PLANE)
    assert violations == [], (
        f"红线①被突破：forgeflow/**（不含 runner）出现 import openhands：{violations}"
    )


# --------------------------------------------------------------------------- #
# 红线 2 —— runner 不得 import forgeflow                                        #
# --------------------------------------------------------------------------- #
def test_runner_does_not_import_forgeflow():
    files = _python_files(_RUNNER_DIR)
    assert files, "扫描域为空 —— codeplane/runner 未找到 .py 文件，钉子失去意义"
    violations = _scan(files, _FORBIDDEN_IN_RUNNER)
    assert violations == [], (
        f"红线②被突破：codeplane/runner/** 出现 import forgeflow：{violations}"
    )


# --------------------------------------------------------------------------- #
# 反事实注入 —— 钉子真的会红（不是自证恒真）                                    #
# --------------------------------------------------------------------------- #
def test_counterfactual_scanner_flags_a_violating_import(tmp_path):
    """合成一段越界源码 ⇒ 扫描器必须判定为违规（否则钉子恒真、毫无价值）。"""
    bad_control = tmp_path / "bad_control.py"
    bad_control.write_text("import openhands\nfrom openhands.sdk import LLM\n", encoding="utf-8")
    assert _scan([bad_control], _FORBIDDEN_IN_CONTROL_PLANE), (
        "扫描器漏判了控制面的 import openhands —— 钉子恒真"
    )

    bad_runner = tmp_path / "bad_runner.py"
    bad_runner.write_text("from forgeflow.runtime import tool_handlers\n", encoding="utf-8")
    assert _scan([bad_runner], _FORBIDDEN_IN_RUNNER), (
        "扫描器漏判了 runner 的 import forgeflow —— 钉子恒真"
    )


def test_counterfactual_scanner_ignores_text_and_relative_imports(tmp_path):
    """反向对照：只有**真 import** 才判违规 —— 文本提及与相对 import 都不算。"""
    benign = tmp_path / "benign.py"
    benign.write_text(
        '"""This docstring mentions openhands and forgeflow but imports neither."""\n'
        "import os\n"
        "from . import sibling\n"
        "from ..runtime import helper\n",
        encoding="utf-8",
    )
    for forbidden in (_FORBIDDEN_IN_CONTROL_PLANE, _FORBIDDEN_IN_RUNNER):
        assert _scan([benign], forbidden) == [], (
            f"扫描器把文本/相对 import 误判为越界（{forbidden}）"
        )
