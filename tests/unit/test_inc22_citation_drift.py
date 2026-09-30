"""INC22 收口补丁 —— 防「引文漂移」钉子（citation-drift guard）。

背景
----
本项目大量注释 / docstring / **证据串（运行时字符串字面量）** 用 ``file.py:行号``
形式指真值来源。这类引用一旦目标文件在其上方插入 / 删除哪怕一行，**全部**指向该
文件的 ``file:line`` 就会静默漂移成错误坐标（W1 在 ``orchestrator.py`` 顶部插入 3 段
后，全仓 63 处引用集体失真）。本测试把这种脆弱写法**钉死**：域内一律不得再出现裸
行号引用，须改成**符号锚点**（``file.py::symbol`` / ``file.py`` 中的语句片段），
符号不随行号漂移。

设计（四段，缺一不可）
----------------------
1. ``SCAN_DOMAIN``：显式列**扫描域** —— INC22 的 9 个文件 ∪ INC23 本轮新增引文的
   5 个文件（``forgeflow/experience/extractor.py``、``frontend/src/api/client.ts``、
   ``forgeflow/runtime/planning.py``、``forgeflow/runtime/react_executor.py``、
   ``tests/unit/test_inc17_plan_from_records.py``）= **14** 个；并显式声明
   ``docs/sop/``、``.workbuddy/memory/`` **不在域内**（前者是项目红线，任何人不许改；
   后者是会话记忆，非交付物）——避免"漏网"被误当成已覆盖。
   ⚠️ 域必须随每期**真正被改 / 新增引文**的文件扩展：不把本轮改动的文件并进来，
   守卫对本轮修复就**没有覆盖**——"声称已覆盖"与"实际未扫描"背离，正是本钉子要防的病。
2. 域内断言：扫描面 = **注释 + docstring + 字符串字面量**（.py 用 tokenize/ast 取
   ``COMMENT``、``ast.Constant`` 字符串与 f-string 文本片段；.ts/.tsx 取 ``//`` /
   ``/* */`` 注释与 ``"`` / ``'`` / 模板串内容；.env 取整行，含注释与值），任一文本里
   **不得存在** ``file.py:行号`` 形式（正则见 ``BARE_LINE_REF``）；命中则打印
   ``file:line:原文``。
   ⚠️ 运行时字符串字面量（如 trust 报告写进 witness 的证据串）**同样在扫描面内**：
   它不是"不可改"的借口——证据串漂移一样误导审计，故本题一并钉死。
3. 阳性对照（非空、非假绿）：① 合成裸引用串必须被 ``BARE_LINE_REF`` 命中；② 合成
   ``.py`` 源码里的注释 / docstring / 字符串三处裸引用，必须被**同一提取器**取到；
   ③ 合成 ``.ts`` 源码里注释与字符串内的裸引用要被取到，而**正则字面量**里的
   ``orchestrator`` 不得被当成文本取出（验证 `.ts` 扫描器真的区分注释 / 字符串 / 正则）。
4. 符号锚点存在性：域内 ``file.ext::symbol`` 锚点的**符号名**（含点号的每一段）必须在目标
   文件里真实存在（目标文件按**路径后缀**解析，避免 basename 撞名如多个 ``client.ts``）。
   ⚠️ 这是**启发式**：判定 = "名字作为子串出现在目标文件"，**只能抓「查无此名」**
   （如臆造 ``StepPlan``），**抓不到「名字存在但指错对象」**，**不**证明锚点语义正确。

覆盖边界（已知未覆盖形态，声明式记录）
--------------------------------------
本钉子**只**吃 ``file.ext:NNN`` 一种形态，且 ``file.ext`` 必须带 ``.py`` / ``.ts`` /
``.tsx`` 扩展名、``:``/``：`` 后紧跟数字。以下形态**不在**覆盖内，属**已知盲区**：
* **裸续行坐标**：如 ``* :1166``、``* :1168-1169`` —— 省略文件名、仅留 ``:NNN`` 的
  续行写法。钉子**故意不覆盖**：能吃掉它的宽正则会同时误命中端口 ``localhost:5433``、
  时间 ``10:30``、模型 tag ``qwen3:8b`` 等正常文本（均为 ``:数字`` 家族），造成假阳性。
  故此类形态靠**人工**在域内清零（本期已清零），**钉子不保证其非回归**。
* 其它无扩展名的裸 ``:NNN``，及 ``LNNN`` / ``行 NNN`` 等变体。

扩钉前置条件：须先给出能**区分**上述"正常 ``:NNN``"的判据；**禁止**使用会误命中端口 /
时间 / 模型 tag 的宽正则，也**禁止**为凑覆盖而加任何可能产生假阳性的断言。
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

# ForgeFlow-main/  ← tests/unit/test_inc22_citation_drift.py
REPO_ROOT = Path(__file__).resolve().parents[2]

#: 扫描域（INC22 的 9 个 ∪ INC23 本轮被改 / 新增引文的 5 个）= **14** 个文件；
#: **只有**这些文件里的文本会被检查。
SCAN_DOMAIN: tuple[str, ...] = (
    ".env.example",
    "forgeflow/api/hub_schemas.py",
    "forgeflow/api/routers/runs.py",
    "frontend/src/views/runs/ResultPanel.tsx",
    "frontend/src/views/runs/realRun.ts",
    "frontend/src/views/runs/roles.ts",
    "tests/integration/test_inc12_trust_loop.py",
    "tests/integration/test_inc12_trust_report.py",
    "tests/unit/test_inc20_usage_null.py",
    # --- INC23：本轮真正被改、且新增引文的文件（并入后实测域内裸 ``file:行号`` = 0）---
    "forgeflow/experience/extractor.py",
    "forgeflow/runtime/planning.py",
    "forgeflow/runtime/react_executor.py",
    "frontend/src/api/client.ts",
    "tests/unit/test_inc17_plan_from_records.py",
)

#: 显式**排除**在扫描域之外（红线 / 非交付物）。列在此以"声明式"记录覆盖边界。
OUT_OF_DOMAIN: tuple[str, ...] = (
    "docs/sop/",           # 项目红线：任何人不许改
    ".workbuddy/memory/",  # 会话记忆：非交付物
)

#: 裸行号引用：``<path>.<py|ts|tsx>`` 紧跟 ``:``/``：`` 再跟数字。符号锚点用 ``::``，
#: 不会命中（``:`` 后不是数字）。
BARE_LINE_REF = re.compile(r"[A-Za-z0-9_./-]+\.(py|ts|tsx)\s*[:：]\s*\d+")

#: 符号锚点：``<path>.<py|ts|tsx>::<symbol>``；``symbol`` 允许点号（``A.b.c``）。
SYMBOL_ANCHOR = re.compile(r"([A-Za-z0-9_./-]+\.(?:py|ts|tsx))::([A-Za-z_][A-Za-z0-9_.]*)")

#: 正则字面量可能的**起点前导**：这些"非值"字符之后，``/`` 是正则而非除号。
_REGEX_PREFIX_CHARS = set("(,=:[!&|?{};+-*%<>^~")
#: 这些关键字之后，``/`` 也是正则起点（``return /re/``、``typeof /re/`` …）。
_REGEX_PREFIX_KEYWORDS = frozenset(
    {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
     "do", "else", "case", "yield", "await"}
)


# --------------------------------------------------------------------------- #
# 文本提取（注释 / docstring / 字符串字面量 —— 全部在扫描面内）                  #
# --------------------------------------------------------------------------- #
def _py_units_from_src(src: str) -> list[tuple[int, str]]:
    """(.py 源码) 返回 (行号, 文本)：(a) ``#`` 注释，(b) 所有**字符串字面量**。

    字符串字面量经 ``ast.Constant`` 覆盖：既含 docstring（它本身就是 str 常量），
    也含普通 ``str`` / 拼接段；f-string 用 ``ast.JoinedStr`` 逐段取其中的文本常量。
    取的是**字面量值**（已解转义），故 ``"orchestrator.py:1"`` 与
    ``"orchestrator.py\\x3a1"`` 都能被还原命中。
    """
    units: list[tuple[int, str]] = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            units.append((tok.start[0], tok.string))
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            units.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    units.append((part.lineno, part.value))
    return units


def _regex_can_start(prev: str) -> bool:
    """``prev``（上一个有意义 token）之后，``/`` 是否应被当作**正则起点**。"""
    if prev == "":
        return True
    if prev in _REGEX_PREFIX_CHARS:
        return True
    return prev in _REGEX_PREFIX_KEYWORDS


def _ts_units_from_src(src: str) -> list[tuple[int, str]]:
    """(.ts/.tsx 源码) 返回 (行号, 文本)：(a) ``//`` 与 ``/* */`` 注释文本，
    (b) ``"..."`` / ``'...'`` / `` `...` `` **字符串 / 模板串的内容**。

    正则字面量 ``/.../flags`` 会被**识别并跳过**：它既不是注释也不是字符串，其内容
    （可能含引号或 ``//`` / ``/*`` 序列）不得污染结果。``//`` 与 ``/*`` 只有在
    "非正则位置"才当注释起点；``/`` 在"值"之后（除号）同样不当作正则。
    """
    units: list[tuple[int, str]] = []
    i, n, line = 0, len(src), 1
    prev = ""  # 上一个有意义 token：单字符，或标识符 / 关键字整体
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c in ('"', "'", "`"):
            quote, start_line = c, line
            i += 1
            buf: list[str] = []
            while i < n:
                ch = src[i]
                if ch == "\\":
                    if i + 1 < n:
                        buf.append(src[i + 1])
                    i += 2
                    continue
                if ch == "\n":
                    line += 1
                if ch == quote:
                    i += 1
                    break
                buf.append(ch)
                i += 1
            units.append((start_line, "".join(buf)))
            prev = quote
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/" and not _regex_can_start(prev):
            start = i + 2
            j = start
            while j < n and src[j] != "\n":
                j += 1
            units.append((line, src[start:j]))
            i = j
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*" and not _regex_can_start(prev):
            i += 2
            seg_start, seg_line = i, line
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                if src[i] == "\n":
                    units.append((seg_line, src[seg_start:i]))
                    line += 1
                    seg_line, seg_start = line, i + 1
                i += 1
            units.append((seg_line, src[seg_start:i]))
            i += 2
            prev = "*"
            continue
        if c == "/" and _regex_can_start(prev):
            # 正则字面量：跳到闭合 ``/``（字符类 ``[...]`` 内的 ``/`` 不算闭合）。
            i += 1
            in_class = False
            while i < n:
                ch = src[i]
                if ch == "\\":
                    i += 2
                    continue
                if ch == "\n":
                    break
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    i += 1
                    break
                i += 1
            while i < n and src[i].isalpha():  # 跳过 flags
                i += 1
            prev = "/"
            continue
        if c.isspace():
            i += 1
            continue
        if c.isalnum() or c in "_$":
            j = i
            while j < n and (src[j].isalnum() or src[j] in "_$"):
                j += 1
            prev = src[i:j]
            i = j
            continue
        prev = c
        i += 1
    return units


def _env_units_from_src(src: str) -> list[tuple[int, str]]:
    """(.env 等) 返回 (行号, 整行文本)：注释与**值**都在扫描面内（整行最省事且不漏）。"""
    return [(ln, raw) for ln, raw in enumerate(src.splitlines(), 1)]


def _units_from_src(path: Path, src: str) -> list[tuple[int, str]]:
    if path.suffix == ".py":
        return _py_units_from_src(src)
    if path.suffix in (".ts", ".tsx"):
        return _ts_units_from_src(src)
    return _env_units_from_src(src)


def _units(path: Path) -> list[tuple[int, str]]:
    return _units_from_src(path, path.read_text(encoding="utf-8"))


def _scan_violations() -> list[tuple[str, int, str]]:
    violations: list[tuple[str, int, str]] = []
    for rel in SCAN_DOMAIN:
        path = REPO_ROOT / rel
        for ln, text in _units(path):
            if BARE_LINE_REF.search(text):
                violations.append((rel, ln, text.strip()))
    return violations


# --------------------------------------------------------------------------- #
# 符号锚点存在性（启发式 —— 只抓「查无此名」，不证明语义）                       #
# --------------------------------------------------------------------------- #
_ANCHOR_FILES_CACHE: list[Path] | None = None


def _all_source_files() -> list[Path]:
    """仓库内所有 ``.py`` / ``.ts`` / ``.tsx`` 文件（缓存一次，供后缀解析）。"""
    global _ANCHOR_FILES_CACHE
    if _ANCHOR_FILES_CACHE is None:
        _ANCHOR_FILES_CACHE = [
            p for p in REPO_ROOT.rglob("*")
            if p.is_file() and p.suffix in (".py", ".ts", ".tsx")
        ]
    return _ANCHOR_FILES_CACHE


def _resolve_anchor_target(target: str) -> Path | None:
    """``api/client.ts`` → 以路径后缀 ``/api/client.ts`` 结尾的文件（取最短者）。

    按**路径后缀**解析而非 basename：本仓有重名文件（多个 ``client.ts``），
    只比 basename 会解析到错文件，进而误判锚点。
    """
    suffix = target.replace("\\", "/").lstrip("./")
    hits = [
        p for p in _all_source_files()
        if str(p).replace("\\", "/").endswith("/" + suffix)
        or str(p).replace("\\", "/") == suffix
    ]
    if not hits:
        return None
    return sorted(hits, key=lambda p: len(str(p)))[0]


def _anchor_missing_parts(target: str, sym: str) -> list[str] | None:
    """返回 ``sym`` 在 ``target`` 文件里**缺失的段**；目标文件不存在则返回 ``None``。

    点号分隔的**每一段**都必须出现：``StepPlan.to_payload`` 的头段 ``StepPlan`` 缺失即算
    错，**不**因尾段 ``to_payload`` 真实存在而放行（那正是"头错尾对"漏网的根因）。
    """
    path = _resolve_anchor_target(target)
    if path is None:
        return None
    body = path.read_text(encoding="utf-8", errors="replace")
    return [part for part in sym.split(".") if part not in body]


def _anchor_scan() -> tuple[int, list[tuple[str, int, str, str, str]]]:
    """扫域内每一行的 ``file.ext::symbol`` 锚点，返回 ``(检查数, 违规列表)``。

    违规 = ``(源文件, 行, 目标, 符号, 原因)``；原因含 ``TARGET_FILE_NOT_FOUND`` 或缺失段。
    锚点只出现在注释 / docstring / 字符串里，故直接按**整行**扫（是其超集，避免漏网）。
    """
    checked = 0
    out: list[tuple[str, int, str, str, str]] = []
    for rel in SCAN_DOMAIN:
        path = REPO_ROOT / rel
        for ln, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in SYMBOL_ANCHOR.finditer(line):
                target, sym = m.group(1), m.group(2)
                checked += 1
                missing = _anchor_missing_parts(target, sym)
                if missing is None:
                    out.append((rel, ln, target, sym, "TARGET_FILE_NOT_FOUND"))
                elif missing:
                    out.append((rel, ln, target, sym, "缺失段=" + ",".join(missing)))
    return checked, out


# --------------------------------------------------------------------------- #
# 钉子本体                                                                     #
# --------------------------------------------------------------------------- #
def test_domain_files_exist_and_are_scanned():
    """域枚举健全：14 个文件（INC22 的 9 ∪ INC23 的 5）都在，且每个都真的取到了文本（非空扫）。"""
    assert len(SCAN_DOMAIN) == 14
    for rel in SCAN_DOMAIN:
        path = REPO_ROOT / rel
        assert path.is_file(), "SCAN_DOMAIN 成员缺失: %s" % rel
        assert _units(path), "未取到任何文本（扫描面为空）: %s" % rel


def test_domain_declaration_excludes_docs_and_memory():
    """声明式边界：``docs/sop/`` 与 ``.workbuddy/memory/`` 明确**不在**扫描域。"""
    joined = "|".join(SCAN_DOMAIN)
    for out_of in OUT_OF_DOMAIN:
        assert out_of.rstrip("/") not in joined, "越界：%s 不应在 SCAN_DOMAIN 内" % out_of


def test_no_bare_line_refs_in_domain_text():
    """域内注释 / docstring / 字符串字面量均不得出现 ``file.py:行号`` 式裸引用。"""
    violations = _scan_violations()
    assert not violations, (
        "域内文本仍存在易漂移的裸行号引用，请改成符号锚点"
        "（`file.py::symbol` 或 `file.py` 中的语句片段）：\n"
        + "\n".join("%s:%d: %s" % (f, ln, text) for f, ln, text in violations)
    )


def test_positive_control_bare_line_ref_is_detected():
    """阳性对照 ①：合成串必须被同一正则命中 —— 证明钉子非空、不是假绿。"""
    synthetic = "see orchestrator.py:515 for the degrade write point"
    match = BARE_LINE_REF.search(synthetic)
    assert match is not None, "阳性对照失败：正则未命中合成裸行号引用"
    assert match.group(0) == "orchestrator.py:515"


def test_positive_control_py_scanner_catches_comment_docstring_and_string():
    """阳性对照 ②：喂合成 ``.py`` 源码，注释 / docstring / 字符串三处裸引用都要被取出。"""
    src = (
        '"""mod doc: see orchestrator.py:515"""\n'
        "x = 1  # inline: gate.py:60\n"
        'y = "literal: runs.py:93"\n'
    )
    joined = " || ".join(t for _, t in _py_units_from_src(src))
    assert "orchestrator.py:515" in joined, "docstring 中的裸引用未被取出"
    assert "gate.py:60" in joined, "注释中的裸引用未被取出"
    assert "runs.py:93" in joined, "字符串字面量中的裸引用未被取出"


def test_positive_control_ts_scanner_separates_comment_string_and_regex():
    """阳性对照 ③：注释与字符串内的裸引用要被取出；正则字面量里的 ``orchestrator`` 不得被取出。"""
    src = (
        "const a = 1 // ref: orchestrator.py:515\n"
        "const b = 'ref: gate.py:60'\n"
        "const RE = /orchestrator[.]py:\\d+/  // regex, not extracted\n"
    )
    units = _ts_units_from_src(src)
    hits = [t for _, t in units if "orchestrator.py" in t]
    # 只有第 1 行的注释含 ``orchestrator.py``；正则里的不是文本、不被取出。
    assert len(hits) == 1, "正则字面量被误当文本取出（或注释未被取出）: %r" % (hits,)
    assert "orchestrator.py:515" in hits[0]
    assert any("gate.py:60" in t for _, t in units), "字符串内容未被取出"


def test_symbol_anchor_form_does_not_match():
    """反向对照：符号锚点写法（``::``）不得被正则误伤。"""
    for ok in (
        "orchestrator.py::run_task",
        "orchestrator.py::_llm_executor 写 `runtime_meta[\"degraded\"]` 处",
        "gate.py::PLATFORM_TOOL_CATALOGUE",
        "planning.py::PlanStep 中 step_id 约定 ``{run_id}:{attempt}:{index}``",
    ):
        assert BARE_LINE_REF.search(ok) is None, "符号锚点被误判为裸行号: %r" % ok


def test_domain_symbol_anchors_point_at_existing_names():
    """域内所有 ``file.ext::symbol`` 锚点的符号名在目标文件里**真实存在**。

    ⚠️ **启发式**（非语义证明）：判定 = "符号名（含点号的**每一段**）作为**子串**出现在
    目标文件中"。因此它**只能抓「查无此名」**（如臆造的 ``StepPlan``），**抓不到**
    「名字存在但指错对象」，也**不**证明锚点语义正确。目标文件按**路径后缀**解析。
    """
    checked, violations = _anchor_scan()
    assert checked > 0, "锚点扫描为空（假绿风险）：未取到任何 file.ext::symbol 锚点"
    assert not violations, (
        "域内存在指向**目标文件中不存在符号名**的锚点（疑似臆造）：\n"
        + "\n".join("%s:%d → %s::%s  %s" % v for v in violations)
    )


def test_positive_control_nonexistent_anchor_symbol_is_detected():
    """阳性对照 ④：臆造符号名必须被检出；且点号需**逐段**校验（头错尾对不放行）。"""
    # 仓库里不存在 ``StepPlan`` ⇒ 必须报出缺失段。
    assert _anchor_missing_parts("planning.py", "StepPlan") == ["StepPlan"]
    # ``StepPlan.to_payload``：尾段 ``to_payload`` 真实存在，但头段臆造 ⇒ 仍须报错。
    assert _anchor_missing_parts("planning.py", "StepPlan.to_payload") == ["StepPlan"]
    # 真实符号名 ⇒ 无缺失段（证明对照不是恒真）。
    assert _anchor_missing_parts("planning.py", "PlanStep.to_payload") == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
