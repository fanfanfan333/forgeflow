"""INC30 —— 客户端/薄服务与**官方** ``openhands-agent-server`` 的线路契约一致性钉子。

背景：``forgeflow/codeplane/engine.py::AgentServerOpenHandsEngine`` 号称「对官方实现
只改 base URL 即可切换」，但四处契约曾经漂移（全部有官方源码锚点）：

  1. ``after_seq`` **不是**官方 REST 参数 —— 官方 ``GET /api/conversations/{id}/events/search``
     只接受 ``page_id, limit, kind, source, body, sort_order, timestamp__gte,
     timestamp__lt``（``openhands-agent-server/openhands/agent_server/event_router.py::
     search_conversation_events``）；``after_seq`` 只存在于会话 socket
     （``session_socket.py::session_socket`` / ``session_protocol.py``）。
  2. 官方 ``limit`` 上限 ``le=100``，客户端却发过 ``200`` ⇒ 官方直接 ``422``。
  3. 官方 ``Event``（``openhands-sdk/openhands/sdk/event/base.py::Event``）**没有**
     ``seq`` 字段，只有 ``id`` + ``timestamp``；用 ``item.get("seq")`` 推进游标对官方
     永远拿不到。
  4. 官方 ``workspace`` / ``agent`` 都是带判别字段的模型
     （``openhands-sdk/openhands/sdk/utils/models.py::DiscriminatedUnionMixin`` 的
     ``@computed_field kind`` = 类名）；裸 ``{"working_dir": ...}`` 会被官方 ``422``。

本文件把上面 4 条写成**可证伪的常量 + 断言**，并对**真实函数**取值（不是文本启发式）：

  (a) 薄服务声明的**路径**集合 ⊆ 官方路径集合（两侧用**同一归一化规则**处理）；
  (a') 薄服务声明的 **``(method, path)`` 对** ⊆ 官方 operations ∪ 一张**显式声明的 501
      方法桩表**（``_DECLARED_METHOD_EXTENSIONS``）；starlette 为每个 ``GET`` 自动补的
      ``HEAD``，仅在「同路径存在被允许的 ``GET``」时才放行（判据见
      ``_method_violations``）；
  (b) 客户端 ``_poll_events`` 实际发出的查询参数**逐个**在官方白名单内，或属于一张
      **显式声明的扩展表**（非白名单且未声明 ⇒ 失败）；
  (c) 客户端 ``limit <= 100``；
  (d) ``_start_payload`` 的 ``workspace`` / ``agent`` 都带非空 ``kind``。

**catch-all 的排除判据（INC31 T3 修复）**：``_declared_paths_from_routes`` /
``_declared_operations_from_routes`` 只在**「那个 501 handler」挂载在「那个精确模板」**时
才把一条 route 当作薄服务的 catch-all 排除 —— 即 ``path`` **整体等于** ``/api/{rest:path}``
或 ``/sockets/{rest:path}``，**且** ``route.endpoint is not_implemented`` /
``ws_not_implemented``（starlette 不暴露 ``endpoint`` 时以路径整体相等为准）。**绝不**再按
路径里的字面量 ``"{rest"`` 做子串匹配 —— 旧写法会把任何**把路径参数命名为 ``rest``** 的路由
静默丢弃（INC31 QA 曾用 ``/api/v2/totally_made_up/{rest}`` 实证放行）。反向也成立：把
``not_implemented`` 挂到自造路径（如 ``/api/v2/xxx/{rest}``）上仍会被判违规。

并带**反事实注入**：把 ``limit`` 改成 200、把 ``kind`` 摘掉、塞一个不存在的路径 / 一个
未声明的 ``(method, path)`` / 一条**仅名字**为 ``rest`` 的伪造路由 —— 对应钉子必须**变红**；
同时合成真 catch-all 必须被排除、且不得连带排除非 catch-all 的 ``rest`` 参数路由（证明判据
是按端点身份、而非字面子串）。

官方权威集合来自**入库 fixture**（不再是人工手打的字面量表）：
``tests/fixtures/inc30/official_agent_server_contract.json`` —— 138 条归一化路径
（135 条 HTTP + 3 条 websocket），是「静态 AST 扫描官方 server 源码」∪「运行时
``app.openapi()``」的并集（两者各有盲区，单独任一都不是全集）。fixture 由
``scripts/regenerate_official_contract.py`` 重生成，方法/命令见其
``_provenance.how_to_regenerate`` 块。归一化规则：仅剥 starlette 转换器后缀
（``{x:path}`` → ``{x}``）。

引文一律 ``file.py::symbol``，不用行号。
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from forgeflow.codeplane import engine as engine_module
from forgeflow.codeplane.engine import (
    AgentServerOpenHandsEngine,
    CodeJob,
    SubprocessOpenHandsEngine,
    _EventCursor,
    _build_engine,
)
from forgeflow.codeplane.runner.agent_server.app import (
    create_app,
    not_implemented,
    ws_not_implemented,
)

# --------------------------------------------------------------------------- #
# 官方契约 fixture（**唯一事实源**；不再是内联字面量表）                          #
# --------------------------------------------------------------------------- #
_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "inc30"
    / "official_agent_server_contract.json"
)
_FIXTURE: dict[str, Any] = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))

#: starlette 路径转换器后缀（``{x:path}`` → ``{x}``）—— 两侧比较前统一施加。
_CONVERTER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[a-zA-Z]+)?\}")


def _normalize(path: str) -> str:
    """官方与薄服务两侧共用的**同一归一化规则**。"""
    return _CONVERTER.sub(r"{\1}", path) or "/"


#: 官方 server 暴露的**路径集合**（``openhands-agent-server/openhands/agent_server``）：
#: 静态 AST 扫描 ∪ 运行时 OpenAPI 的并集，来自入库 fixture。
_OFFICIAL_PATHS: frozenset[str] = frozenset(_FIXTURE["http_paths"]) | frozenset(
    _FIXTURE["socket_paths"]
)

#: 官方 ``search_conversation_events`` 接受的查询参数（逐字取自 fixture）。
_OFFICIAL_SEARCH_PARAMS: frozenset[str] = frozenset(_FIXTURE["search_query_params"])

#: **显式声明的扩展参数表** —— 客户端会发、但官方 REST **不定义**的参数。只因为本仓
#: 薄服务需要它而保留；官方 server 忽略未知查询参数。任何不在「官方白名单 ∪ 本表」里的
#: 参数都算漂移，钉子会红。
_DECLARED_EXTENSION_PARAMS: frozenset[str] = frozenset({"after_seq"})

#: 官方 ``limit`` 的上界（``event_router.py::search_conversation_events`` 声明 ``le=100``）。
_OFFICIAL_LIMIT_CAP: int = int(_FIXTURE["limit_cap"])

#: 官方 ``(METHOD, path)`` operations（逐条取自 fixture，施加同一归一化）。
_OFFICIAL_OPERATIONS: frozenset[tuple[str, str]] = frozenset(
    (str(method).upper(), _normalize(path)) for method, path in _FIXTURE["http_operations"]
)

#: **显式声明的方法扩展表** —— 薄服务**故意**为其自用而声明的一批 501 方法桩，好让未实现的
#: verb 得到干净的 ``501``（而非 ``405``）。官方 API **不定义**这些对；与
#: ``_DECLARED_EXTENSION_PARAMS`` 同一套「显式声明、可审计」范式（**不是**隐式豁免）。
#: 任何落在「官方 operations ∪ 本表」之外的对都必须判违规。
#:
#: 逐条来自实测 route dump（``create_app(token="")``）：``/api/conversations`` 多声明
#: DELETE/PATCH/PUT；``/api/conversations/{conversation_id}`` 多声明 POST/PUT；
#: ``/api/conversations/{conversation_id}/run`` 多声明 GET。``HEAD`` **不在此表** —— 它是
#: starlette 对 ``GET`` 的自动补全，由 :func:`_method_violations` 的一条窄规则处理。
_DECLARED_METHOD_EXTENSIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("DELETE", "/api/conversations"),
        ("PATCH", "/api/conversations"),
        ("PUT", "/api/conversations"),
        ("POST", "/api/conversations/{conversation_id}"),
        ("PUT", "/api/conversations/{conversation_id}"),
        ("GET", "/api/conversations/{conversation_id}/run"),
    })

#: 薄服务真正的 501 catch-all **端点对象**。声明路径/方法抽取按**对象同一性**排除它们，
#: 而不是按路径里的字面量。
_CATCH_ALL_ENDPOINTS: frozenset[Any] = frozenset({not_implemented, ws_not_implemented})

#: catch-all 的**精确**路径模板，仅在 starlette 不暴露 ``route.endpoint`` 时作回退。
#: 判据是**整体相等**（``path in 集合``），绝不是子串。
_CATCH_ALL_PATH_TEMPLATES: frozenset[str] = frozenset({"/api/{rest:path}", "/sockets/{rest:path}"})


# --------------------------------------------------------------------------- #
# 测试夹具 / 取值helper（都对**真实函数**取值）                                  #
# --------------------------------------------------------------------------- #
def _settings(**overrides: Any) -> SimpleNamespace:
    base = dict(
        codeplane_enabled=True,
        codeplane_agent_server_url="",
        codeplane_agent_server_token="",
        codeplane_interpreter=sys.executable,
        codeplane_agent_server_allow_external=False,
        codeplane_agent_server_startup_s=2,
        codeplane_timeout_seconds=5,
        codeplane_max_rounds=1,
        codeplane_test_command="",
        codeplane_reasoning_effort="none",
        codeplane_num_ctx=1024,
        codeplane_temperature=0.0,
        ollama_base_url="http://127.0.0.1:11434",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _job(**overrides: Any) -> CodeJob:
    base = dict(
        run_id="r-inc30",
        task_intent="noop",
        workspace_path="",
        model="ollama_chat/qwen3:8b",
        base_url="http://127.0.0.1:11434",
        api_key="",
        max_rounds=1,
        wall_timeout_s=5,
        test_command="",
        language_hint="python",
        reasoning_effort="none",
        num_ctx=1024,
        temperature=0.0,
    )
    base.update(overrides)
    return CodeJob(**base)


class _FakeResponse:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        return self._body


class _CapturingClient:
    """A minimal async HTTP client that records every GET's query params verbatim."""

    def __init__(self, body: Any = None, status_code: int = 200) -> None:
        self.body = body if body is not None else {"items": [], "next_page_id": None}
        self.status_code = status_code
        self.calls: list[dict[str, Any]] = []

    async def get(self, url: str, params: Any = None) -> _FakeResponse:
        self.calls.append({"url": url, "params": dict(params or {})})
        return _FakeResponse(self.status_code, self.body)


def _real_search_params() -> dict[str, Any]:
    """The query params the **real** ``_poll_events`` actually emits (not a guess)."""
    engine = AgentServerOpenHandsEngine(settings=_settings())
    client = _CapturingClient()
    asyncio.run(engine._poll_events(client, "cid", _EventCursor(), []))
    assert client.calls, "`_poll_events` 未发出任何请求 —— 取值失败"
    return client.calls[0]["params"]


def _real_start_payload() -> dict[str, Any]:
    """The body the **real** ``_start_payload`` actually builds."""
    engine = AgentServerOpenHandsEngine(settings=_settings())
    return engine._start_payload(_job())


def _is_catch_all_route(route: Any) -> bool:
    """Whether ``route`` is the thin server's 501 catch-all (NOT a real endpoint claim).

    真正的 catch-all 定义为**「那个端点对象」挂载在「那个精确模板」上**，两者**同时**成立：

    1. 路径模板**整体等于** :data:`_CATCH_ALL_PATH_TEMPLATES` 之一（``/api/{rest:path}`` /
       ``/sockets/{rest:path}``）—— 判据是整体相等，**绝不是**子串（旧写法 ``"{rest" in
       path`` 会把任何把参数命名为 ``rest`` 的路由静默丢弃，正是绕过口）；
    2. 端点是那个 catch-all handler（``route.endpoint is not_implemented`` /
       ``ws_not_implemented``）；若该 starlette 版本不暴露 ``route.endpoint``，则以第 1 条
       为准。

    只满足其一都**不算** catch-all：例如把 ``not_implemented`` 挂到 ``/api/v2/xxx/{rest}``
    这类自造路径上，仍是一条「声明了官方不存在路径」的路由，必须被保留并判违规。
    """
    path = getattr(route, "path", "") or ""
    if path not in _CATCH_ALL_PATH_TEMPLATES:
        return False
    endpoint = getattr(route, "endpoint", None)
    if endpoint is None:
        return True
    return any(endpoint is handler for handler in _CATCH_ALL_ENDPOINTS)


def _declared_paths_from_routes(routes: Iterable[Any]) -> set[str]:
    """The paths ``routes`` declare, dropping the real 501 catch-alls by identity."""
    paths: set[str] = set()
    for route in routes:
        path = getattr(route, "path", "") or ""
        if path and not _is_catch_all_route(route):
            paths.add(path)
    return paths


def _declared_operations_from_routes(routes: Iterable[Any]) -> set[tuple[str, str]]:
    """The ``(METHOD, path)`` pairs ``routes`` declare (same catch-all exclusion)."""
    operations: set[tuple[str, str]] = set()
    for route in routes:
        path = getattr(route, "path", "") or ""
        if not path or _is_catch_all_route(route):
            continue
        for method in getattr(route, "methods", None) or []:
            operations.add((str(method).upper(), path))
    return operations


def _thin_server_declared_paths() -> set[str]:
    """The concrete paths the thin server declares, excluding the 501 catch-alls."""
    return _declared_paths_from_routes(create_app(token="").routes)


def _thin_server_declared_operations() -> set[tuple[str, str]]:
    """The ``(METHOD, path)`` pairs the thin server declares, excluding the 501 catch-alls."""
    return _declared_operations_from_routes(create_app(token="").routes)


# --------------------------------------------------------------------------- #
# 机械判定函数（钉子的核心；反事实注入复用同一函数，证明它真的会红）            #
# --------------------------------------------------------------------------- #
def _path_violations(paths: Mapping[str, Any] | set[str]) -> set[str]:
    """Declared paths (normalised by the shared rule) that are not official."""
    return {_normalize(p) for p in paths} - _OFFICIAL_PATHS


def _method_violations(operations: Iterable[tuple[str, str]]) -> set[tuple[str, str]]:
    """Declared ``(METHOD, path)`` pairs that are neither official nor a declared 501 stub.

    官方集合 = ``_OFFICIAL_OPERATIONS``；显式扩展 = ``_DECLARED_METHOD_EXTENSIONS``。
    **唯一的窄放行规则**：starlette 会为每个 ``GET`` 自动注册对应 ``HEAD``，故 ``(HEAD, p)``
    当且仅当 ``(GET, p)`` 本身被允许（在官方 ∪ 扩展内）时才放行 —— 这不是对 ``HEAD`` 的一揽子
    豁免（官方无 ``GET`` 的路径上的 ``HEAD`` 仍会判违规）。
    """
    allowed = _OFFICIAL_OPERATIONS | _DECLARED_METHOD_EXTENSIONS
    violations: set[tuple[str, str]] = set()
    for method, path in operations:
        pair = (str(method).upper(), _normalize(path))
        if pair[0] == "HEAD" and ("GET", pair[1]) in allowed:
            continue
        if pair not in allowed:
            violations.add(pair)
    return violations


def _search_param_violations(params: Mapping[str, Any]) -> set[str]:
    allowed = _OFFICIAL_SEARCH_PARAMS | _DECLARED_EXTENSION_PARAMS
    return {name for name in params if name not in allowed}


def _limit_violations(params: Mapping[str, Any]) -> list[str]:
    limit = params.get("limit")
    if isinstance(limit, bool) or not isinstance(limit, int):
        return [f"limit 非整数：{limit!r}"]
    if not (0 < limit <= _OFFICIAL_LIMIT_CAP):
        return [f"limit={limit} 超出官方范围 (gt=0, le={_OFFICIAL_LIMIT_CAP})"]
    return []


def _kind_violations(payload: Mapping[str, Any]) -> list[str]:
    violations: list[str] = []
    workspace = payload.get("workspace") or {}
    if not str(workspace.get("kind") or "").strip():
        violations.append("workspace 缺少非空 kind 判别字段")
    agent = payload.get("agent") or {}
    if not str(agent.get("kind") or "").strip():
        violations.append("agent 缺少非空 kind 判别字段")
    return violations


# --------------------------------------------------------------------------- #
# (a) 薄服务声明路径 ⊆ 官方路径                                                  #
# --------------------------------------------------------------------------- #
def test_a_thin_server_declared_paths_are_a_subset_of_the_official_api():
    declared = {_normalize(p) for p in _thin_server_declared_paths()}
    assert declared, "薄服务未声明任何端点 —— 钉子失去意义"
    violations = _path_violations(declared)
    assert violations == set(), f"薄服务声明了官方不存在的路径：{sorted(violations)}"


def test_a_counterfactual_invented_path_is_flagged():
    """反向对照：一个官方不存在的路径必须被判违规（钉子非恒真）。"""
    assert _path_violations({"/api/v2/invented_endpoint"}) == {"/api/v2/invented_endpoint"}
    # 正向：官方真实路径不得被误判。
    assert _path_violations({"/api/conversations/count", "/api/git/diff"}) == set()
    # 归一化规则同向生效：带转换器后缀的官方路径也必须被接纳。
    assert _path_violations({"/api/conversations/{conversation_id}/workspace/{file_path:path}"}) == set()


def _fake_route(path: str, endpoint: Any, methods: list[str] | None = None) -> Any:
    """A route duck-type carrying just what the declared-path/op extractors read."""
    return SimpleNamespace(path=path, endpoint=endpoint, methods=methods or [])


def _invented_endpoint(request: Any) -> None:  # pragma: no cover — never called
    raise AssertionError("invented endpoint must never be served")


def test_a_catch_all_is_excluded_by_identity_not_by_substring():
    """**INC31 T3 绕过口回归**：真 catch-all 按端点身份排除，仅**名字**为 ``rest`` 的普通
    路由必须被保留并判违规（旧子串判据会让它逃逸）。

    这里合成一批 route 喂给**抽取逻辑**本身（不依赖真 app），两侧都断：
    排除侧 —— 两个真 catch-all 端点必须被丢弃；
    保留侧 —— 一条 ``/api/v2/totally_made_up/{rest}``（端点不是 catch-all）必须留下并被判违规。
    """
    routes = [
        _fake_route("/api/{rest:path}", not_implemented, ["GET"]),  # 真 catch-all → 排除
        _fake_route("/sockets/{rest:path}", ws_not_implemented, ["GET"]),  # 真 catch-all → 排除
        _fake_route("/api/v2/totally_made_up/{rest}", _invented_endpoint, ["GET"]),  # 必须保留
        # 同一 catch-all handler，但挂在**自造路径**上 ⇒ 仍是一条越界声明，必须保留。
        # （这正是 INC31 QA 原始的绕过输入，只是当时用了 not_implemented 作 handler。）
        _fake_route("/api/v2/original_bypass/{rest}", not_implemented, ["GET"]),
        _fake_route("/api/conversations/count", _invented_endpoint, ["GET"]),  # 官方真实路径
    ]
    declared = {_normalize(p) for p in _declared_paths_from_routes(routes)}

    # 排除侧：真 catch-all（handler 身份 **且** 精确模板）不得进入声明集。
    assert "/api/{rest}" not in declared
    assert "/sockets/{rest}" not in declared
    # 保留侧：参数名恰好是 ``rest`` 的伪造路由**不被**连带排除，且被判违规；
    # 把 catch-all handler 挂到自造路径上同样必须被判违规。
    assert "/api/v2/totally_made_up/{rest}" in declared
    assert "/api/v2/original_bypass/{rest}" in declared
    assert _path_violations(declared) == {
        "/api/v2/totally_made_up/{rest}",
        "/api/v2/original_bypass/{rest}",
    }

    # 方法维度同源：catch-all 的 verbs 也不得被当成声明。
    ops = {(_m, _normalize(p)) for _m, p in _declared_operations_from_routes(routes)}
    assert not any(path == "/api/{rest}" for _m, path in ops)


def test_a_catch_all_fallback_uses_exact_template_not_substring():
    """starlette 不暴露 ``route.endpoint`` 时的回退判据必须是**精确模板**，而非子串。"""
    routes = [
        _fake_route("/api/{rest:path}", None),  # 精确 catch-all → 排除
        _fake_route("/sockets/{rest:path}", None),  # 精确 catch-all → 排除
        _fake_route("/api/v2/{rest}", None),  # 非 catch-all 的 rest 参数路由 → 保留
        _fake_route("/api/{rest:path}/extra", None),  # 只是**子串**含 rest → 保留
    ]
    declared = {_normalize(p) for p in _declared_paths_from_routes(routes)}
    assert "/api/{rest}" not in declared
    assert "/sockets/{rest}" not in declared
    assert "/api/v2/{rest}" in declared
    assert "/api/{rest}/extra" in declared


def test_a_catch_all_handlers_are_the_real_thin_server_stubs():
    """``_CATCH_ALL_ENDPOINTS`` 必须真的是真 app 里的那两个 501 端点对象。"""
    endpoints = {getattr(route, "endpoint", None) for route in create_app(token="").routes}
    missing = [h for h in _CATCH_ALL_ENDPOINTS if h not in endpoints]
    assert not missing, f"catch-all 端点对象未出现在 app.routes：{missing}"


# --------------------------------------------------------------------------- #
# (a') 薄服务声明的 (method, path) ⊆ 官方 operations ∪ 显式 501 桩表             #
# --------------------------------------------------------------------------- #
def test_a_thin_server_operations_are_official_or_declared_stubs():
    """薄服务真实声明的每个 ``(METHOD, path)`` 必须官方有，或落在显式 501 桩表内。"""
    operations = _thin_server_declared_operations()
    assert operations, "薄服务未声明任何 (method,path) —— 钉子失去意义"
    violations = _method_violations(operations)
    assert violations == set(), (
        f"薄服务声明了官方未定义、且未显式声明的 (method,path)：{sorted(violations)}"
    )


def test_a_counterfactual_undeclared_method_is_flagged():
    """反向对照：官方无此方法、且未在桩表内 ⇒ 必须判违规。"""
    # 官方 ``/api/git/diff`` 只有 GET ⇒ DELETE 未声明。
    assert _method_violations({("DELETE", "/api/git/diff")}) == {("DELETE", "/api/git/diff")}
    # 官方真实 (method,path) 不得被误判。
    assert _method_violations({("GET", "/api/git/diff"), ("POST", "/api/conversations")}) == set()
    # 显式声明的 501 桩放行（薄服务自用），但同一路径上**未声明**的另一个 verb 仍判违规。
    assert _method_violations({("DELETE", "/api/conversations")}) == set()
    assert _method_violations({("DELETE", "/api/init")}) == {("DELETE", "/api/init")}


def test_a_counterfactual_head_is_only_allowed_where_a_get_is_allowed():
    """``HEAD`` 是 starlette 对 ``GET`` 的自动补全：同路径有被允许的 ``GET`` 才放行。"""
    # 官方 ``GET /api/init`` 存在 ⇒ 自动 ``HEAD`` 放行。
    assert _method_violations({("HEAD", "/api/init")}) == set()
    # 官方 ``/api/git/diff`` 只有 GET ⇒ HEAD 放行；但官方**没有** HEAD 定义本身。
    assert _method_violations({("HEAD", "/api/git/diff")}) == set()
    # 官方**无 GET**（也无桩）的路径上出现 HEAD ⇒ 违规（不是一揽子豁免）。
    assert _method_violations({("HEAD", "/api/llm/models/verified")}) == set()  # 官方该路径有 GET
    assert _method_violations({("HEAD", "/api/v2/invented")}) == {("HEAD", "/api/v2/invented")}


def test_declared_method_extensions_are_disjoint_from_official_operations():
    """元钉子：方法桩表与官方 operations 必须互斥（桩表的语义是『官方没有』）。"""
    assert _DECLARED_METHOD_EXTENSIONS.isdisjoint(_OFFICIAL_OPERATIONS), (
        "方法桩表与官方 operations 必须互斥（桩表的语义是『官方没有』）"
    )


# --------------------------------------------------------------------------- #
# (b)(c) 客户端真实请求参数 ∈ 官方白名单 ∪ 已声明扩展；limit ≤ 100              #
# --------------------------------------------------------------------------- #
def test_b_client_search_params_are_all_official_or_declared_extensions():
    params = _real_search_params()
    violations = _search_param_violations(params)
    assert violations == set(), (
        f"客户端发出了官方未定义、且未显式声明的查询参数：{sorted(violations)}"
        f"（真实参数：{params}）"
    )


def test_c_client_search_limit_is_within_the_official_cap():
    params = _real_search_params()
    assert _limit_violations(params) == [], f"真实 limit 越界：{params}"
    assert params["limit"] <= _OFFICIAL_LIMIT_CAP


def test_client_declares_after_seq_as_an_explicit_extension():
    """``after_seq`` 必须存在，且**只能**来自显式扩展表（官方 REST 无此参数）。"""
    params = _real_search_params()
    assert "after_seq" in params, "薄服务档依赖 after_seq 续订，扩展缺失"
    assert "after_seq" not in _OFFICIAL_SEARCH_PARAMS
    assert "after_seq" in _DECLARED_EXTENSION_PARAMS


def test_bc_counterfactual_limit_200_turns_the_cap_nail_red(monkeypatch):
    """反事实注入：把客户端 ``limit`` 改成 200 ⇒ **真实函数**发出 200 ⇒ 钉子变红。"""
    monkeypatch.setattr(engine_module, "_EVENTS_PAGE_LIMIT", 200)
    params = _real_search_params()
    assert params["limit"] == 200, "注入未生效 —— 反证无效"
    assert _limit_violations(params) != [], "limit=200 竟未触发钉子 —— 钉子恒真"


# --------------------------------------------------------------------------- #
# 客户端分页/去重同时兼容官方 ``id`` 与薄服务 ``seq``                           #
# --------------------------------------------------------------------------- #
def test_client_uses_page_id_and_dedups_by_official_id():
    engine = AgentServerOpenHandsEngine(settings=_settings())
    first = _CapturingClient(body={"items": [{"id": "e1", "type": "ActionEvent"}], "next_page_id": "P2"})
    cursor, frames = asyncio.run(engine._poll_events(first, "cid", _EventCursor(), []))
    assert "page_id" not in first.calls[0]["params"], "首页不应带 page_id"
    assert cursor.page_id == "P2"
    assert [f["id"] for f in frames] == ["e1"]

    second = _CapturingClient(
        body={"items": [{"id": "e1"}, {"id": "e2"}], "next_page_id": None})
    cursor, frames = asyncio.run(engine._poll_events(second, "cid", cursor, frames))
    assert second.calls[0]["params"].get("page_id") == "P2", "未回传官方 opaque 游标"
    assert _search_param_violations(second.calls[0]["params"]) == set()
    assert [f["id"] for f in frames] == ["e1", "e2"], "官方 id 去重失败"


def test_client_advances_the_seq_cursor_for_the_thin_server():
    engine = AgentServerOpenHandsEngine(settings=_settings())
    client = _CapturingClient(
        body={
            "items": [{"seq": 0, "type": "ActionEvent"}, {"seq": 1, "type": "ObservationEvent"}],
            "next_page_id": None,
        })
    cursor, frames = asyncio.run(engine._poll_events(client, "cid", _EventCursor(), []))
    assert cursor.after_seq == 1, "薄服务 seq 游标未推进"
    assert len(frames) == 2


# --------------------------------------------------------------------------- #
# (d) ``_start_payload`` 的 workspace / agent 都带非空 kind                     #
# --------------------------------------------------------------------------- #
def test_d_start_payload_declares_workspace_and_agent_kind():
    payload = _real_start_payload()
    assert _kind_violations(payload) == [], payload
    assert payload["workspace"]["kind"] == "LocalWorkspace", (
        "官方 workspace 的判别 kind 必须是具体类名 LocalWorkspace"
    )
    assert payload["agent"]["kind"] == "Agent", (
        "官方 agent 的判别 kind 必须是具体类名 Agent（AgentBase 的唯一常规实现）"
    )


def test_d_counterfactual_dropping_kind_turns_the_discriminator_nail_red():
    """反事实注入：分别摘掉 ``workspace.kind`` / ``agent.kind`` ⇒ 钉子必须变红。"""
    payload = _real_start_payload()
    assert _kind_violations(payload) == []

    without_workspace = copy.deepcopy(payload)
    without_workspace["workspace"].pop("kind", None)
    assert _kind_violations(without_workspace) != [], "摘掉 workspace.kind 竟未触发钉子"

    without_agent = copy.deepcopy(payload)
    without_agent["agent"].pop("kind", None)
    assert _kind_violations(without_agent) != [], "摘掉 agent.kind 竟未触发钉子"


# --------------------------------------------------------------------------- #
# 默认 ``subprocess`` 行为不受影响（自证：默认档根本不走 agent-server 线路）     #
# --------------------------------------------------------------------------- #
#: A real child that reads the job off stdin and echoes a valid ``result`` line.
_RESULT_SCRIPT = """\
import sys, json
sys.stdin.read()
print(json.dumps({
    "seq": 0, "ts": "2026-01-01T00:00:00+00:00",
    "phase": "done", "kind": "result", "status": "ok", "data": {"exit_code": 0},
}), flush=True)
"""


def test_default_transport_is_the_unchanged_subprocess_engine():
    assert isinstance(
        _build_engine(_settings(codeplane_transport="subprocess")), SubprocessOpenHandsEngine
    )
    # An unknown / empty value must default to the byte-identical subprocess path.
    assert isinstance(_build_engine(_settings(codeplane_transport="")), SubprocessOpenHandsEngine)
    assert not isinstance(
        _build_engine(_settings(codeplane_transport="subprocess")), AgentServerOpenHandsEngine
    )


def test_default_subprocess_run_is_byte_identical(tmp_path):
    """Run the default transport end-to-end and assert the unchanged wire contract."""
    runner = tmp_path / "runner_ok.py"
    runner.write_text(_RESULT_SCRIPT, encoding="utf-8")
    engine = SubprocessOpenHandsEngine(
        settings=_settings(codeplane_interpreter=sys.executable), runner_argv=[str(runner)]
    )
    result = asyncio.run(engine.run(_job()))
    assert result.status == "ok"
    assert result.transport == "subprocess"
    assert result.fell_back_from is None
    assert result.raw.get("exit_code") == 0


# --------------------------------------------------------------------------- #
# 元钉子 —— fixture 自身必须可信                                                #
# --------------------------------------------------------------------------- #
_FIXTURE_REQUIRED_KEYS = {
    "http_paths",
    "http_operations",
    "socket_paths",
    "search_query_params",
    "limit_cap",
    "known_blind_spots",
    "_provenance",
}
_PROVENANCE_REQUIRED_KEYS = {
    "package",
    "sdk_version",
    "method",
    "normalization",
    "how_to_regenerate",
    "counts",
}


def test_official_fixture_is_complete_and_provenanced():
    """fixture 的键齐备、来源可追溯（静态扫描 ∪ 运行时 OpenAPI 的并集）。"""
    assert _FIXTURE_REQUIRED_KEYS <= set(_FIXTURE), f"fixture 缺键：{sorted(_FIXTURE_REQUIRED_KEYS - set(_FIXTURE))}"
    provenance = _FIXTURE["_provenance"]
    assert _PROVENANCE_REQUIRED_KEYS <= set(provenance), (
        f"_provenance 缺键：{sorted(_PROVENANCE_REQUIRED_KEYS - set(provenance))}"
    )
    assert provenance["package"] == "openhands-agent-server"
    assert provenance["how_to_regenerate"].strip(), "必须写明确切的重生成命令"
    assert "regenerate_official_contract.py" in provenance["how_to_regenerate"]


def test_official_fixture_covers_the_whole_authoritative_set():
    """权威集规模：≥130 条 HTTP 路径、无 501 catch-all、含此前缺失的路由族。"""
    assert len(_FIXTURE["http_paths"]) >= 130, "官方路径表可疑地小（应含静态 ∪ 运行时的并集）"
    assert len(_OFFICIAL_PATHS) >= 133
    assert not any("{rest" in p for p in _OFFICIAL_PATHS), "官方表不得含薄服务 501 catch-all"
    # 旧的人工表漏掉的官方族必须已在 fixture 内（抽样断言）。
    assert any(p.startswith("/api/settings/") for p in _OFFICIAL_PATHS), "缺 /api/settings/... 族"
    assert any(
        p.startswith("/api/conversations/{conversation_id}/workspace") for p in _OFFICIAL_PATHS
    ), "缺 /api/conversations/{conversation_id}/workspace... 族"
    assert any(
        "/{runtime_conversation_id}/" in p for p in _OFFICIAL_PATHS
    ), "缺运行时 workspace 代理族"


def test_official_table_and_param_whitelist_are_internally_sane():
    """元钉子：扩展表与官方白名单必须互斥（扩展的语义是『官方没有』）。"""
    assert _OFFICIAL_SEARCH_PARAMS.isdisjoint(_DECLARED_EXTENSION_PARAMS), (
        "扩展表与官方白名单必须互斥（扩展的语义是『官方没有』）"
    )
    assert len(_OFFICIAL_SEARCH_PARAMS) == 8, "官方 events/search 查询参数应为 8 个"
    assert _OFFICIAL_LIMIT_CAP == 100, "官方 limit 上界应为 100"
    # http_paths 内不得混入 socket 路径（两者必须分列，避免口径模糊）。
    assert not (set(_FIXTURE["http_paths"]) & set(_FIXTURE["socket_paths"]))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
