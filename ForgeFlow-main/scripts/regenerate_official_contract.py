"""Regenerate the **official** ``openhands-agent-server`` contract fixture.

This is a **development tool** (not imported by tests). It extracts the official
route contract by two independent means and writes the union as a committed
fixture consumed by
``tests/unit/test_inc30_agent_server_official_conformance.py``:

1. **Static AST scan** of the official server source (``--sdk-root``): a router-graph
   resolver walks ``NAME = APIRouter(prefix=...)`` definitions, ``@router.<method>``
   decorators, ``<parent>.include_router(<child>)`` mount edges and module imports,
   composing the absolute ``/api`` (or root) prefix for every route. This is the only
   source that sees routes FastAPI omits from the schema (``include_in_schema=False``)
   and websocket routes (never in OpenAPI).
2. **Runtime OpenAPI** (``--openapi-json``): ``app.openapi()`` dumped from a process
   that has ``openhands-agent-server`` importable. This is the only source that sees
   the dynamically-assembled router families (the ``create_runtime_router()`` factory
   and the ``/conversations/{runtime_conversation_id}/...`` workspace proxy).

Neither alone is complete, so the fixture is the **union** (see ``known_blind_spots``).

The authoritative rule (also asserted by the nail's meta-test)::

    official paths = static AST scan ∪ runtime OpenAPI  ∪  websockets (static only)

Normalisation: route template parameters keep the official spelling except the
starlette path-converter suffix is stripped (``{x:path}`` -> ``{x}``); a path is
stored in its **absolute** form (``/api/...`` when mounted under the api router).

**Fail-loud, not fail-open.** A previous revision silently *fell back* to a phantom
parent whenever a mount edge could not be resolved, which let a semantically
equivalent source rename (e.g. renaming the ``add_execution_routes`` receiver
parameter, or a router property) emit *ghost* paths missing their ``/api`` prefix
while still exiting ``0``. This tool now refuses to guess:

* every ``include_router(...)`` edge must have a **resolvable** parent and child;
  the only tolerated exceptions are (a) a ``Call`` child — a recognised
  **dynamic factory** such as ``create_runtime_router()`` that is *recorded and never
  fabricates a path* — and (b) wiring internal to a **router factory** (a function
  that returns a locally-built router), which is not part of the static mount graph.
  Everything else is a hard error with the offending symbol printed;
* a decorated router that is the endpoint of **no** ``include_router`` edge is a
  **conditional alternate** (reachable only via a subclass property override such as
  the docker-runtime workspace/sockets routers). It is *not* dropped silently: it is
  accepted **only** when every route it emits is already covered by a mounted router
  (same path, or ``/api`` + path, or ``/sockets`` + path); an uncovered route is a
  hard error (a ghost path);
* the **name maps** that remain (the ``router`` receiver, the registry property
  routers, the sockets composite) are explicit and a **miss is loud**;
* every decorated router must trace a full mount chain to the app root whose
  top-level prefix is one of ``""`` / ``"/api"`` / ``"/sockets"``;
* every router factory must be reachable (via a composite attribute or a dynamic
  ``Call`` mount) — an unreferenced factory is a hard error;
* the assembled contract is validated (ops ⊆ paths, no duplicates, sockets disjoint
  from http, every path rooted in the known surface) **before** anything is written.

Usage::

    python scripts/regenerate_official_contract.py \
        --sdk-root /path/to/software-agent-sdk-main \
        [--openapi-json /path/to/openapi.json] \
        [--out tests/fixtures/inc30/official_agent_server_contract.json] \
        [--check]

``--check`` extracts + validates and compares against ``--out`` **without writing**
(exit non-zero on any mismatch). The ``_provenance.how_to_regenerate`` block echoes the
exact command lines used.
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re
import sys
from typing import Any

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
_ALL_METHODS = _HTTP_METHODS | {"websocket"}

#: Path-template normaliser: strip a starlette converter suffix (``{x:path}`` -> ``{x}``).
_CONVERTER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[a-zA-Z]+)?\}")

#: The execution-routes method whose ``router`` parameter receives the api router.
_EXECUTION_METHOD = "add_execution_routes"
_EXECUTION_ROUTER_RECEIVER = "api.py::api_router"
_RECEIVER_PARAM_NAME = "router"

#: Attribute-routers exposed as ``@property`` on the conversation registry object.
#: A referenced property that is **not** in this map is a hard error.
_PROPERTY_ROUTERS: dict[str, str] = {
    "workspace_router": "workspace_router.py::workspace_router",
    "session_sockets_router": "session_socket.py::session_router",
    "conversation_sockets_router": "sockets.py::conversation_sockets_router",
}

#: Composite routers assembled inline (``router = APIRouter(); router.include_router(...);
#: return router``). Mounting a composite mounts each member at the same parent.
#: The key is the *enclosing function name*; a composite referenced but not mapped
#: here is a hard error.
_COMPOSITE_ROUTERS: dict[str, list[str]] = {
    "sockets_router": [
        "sockets.py::conversation_sockets_router",
        "session_socket.py::session_router",
        "sockets.py::bash_sockets_router",
    ],
}

#: The app / api root sentinels (a router mounted directly on the FastAPI app).
_ROOT_NAMES = frozenset({"app", "api"})

#: Internal sentinel id used for "mounted directly on the app root".
_ROOT_SENTINEL = "\x00ROOT"

#: Allowed **top-level** mount prefixes for a decorated router family.
_ALLOWED_TOP_PREFIXES = frozenset({"", "/api", "/sockets"})

#: The official root surface: every emitted http path must live under one of these.
_ALLOWED_ROOTS = ("/api/", "/sockets/", "/v1/")
_ALLOWED_ROOT_EXACT = frozenset({"/", "/alive", "/health", "/ready", "/server_info"})

_DEFAULT_OUT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "inc30"
    / "official_agent_server_contract.json"
)


class ContractError(RuntimeError):
    """Raised when the contract cannot be extracted *unambiguously* (fail loud)."""


def _norm(path: str) -> str:
    """Normalise one route template (converter suffix only; keep absolute prefix)."""
    return _CONVERTER.sub(r"{\1}", path) or "/"


def _module_to_file(module: str) -> str:
    """``openhands.agent_server.bash_router`` -> ``bash_router.py``."""
    prefix = "openhands.agent_server."
    rel = module[len(prefix):] if module.startswith(prefix) else module
    return rel.replace(".", "/") + ".py"


def _is_router_like(call: ast.Call) -> bool:
    """``APIRouter(...)`` or a subclass like ``RuntimeRouter(...)``."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "APIRouter" or func.id.endswith("Router")
    if isinstance(func, ast.Attribute):
        return func.attr == "APIRouter" or func.attr.endswith("Router")
    return False


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
        cur = parents.get(cur)
    return None


def _expr_name(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _expr_name(node.func)
    return None


class _StaticScan:
    """Router-graph resolver over the official server source tree (fail-loud).

    Two phases: first collect every file's definitions/imports/decorators/edges
    (raw), then resolve them once the whole tree is known — so a cross-file import
    in ``api.py`` (scanned first) resolves against a router defined in a later file.
    """

    def __init__(self, server_root: pathlib.Path) -> None:
        self.server_root = server_root
        self.consts: dict[str, str] = {}
        self.imports: dict[str, dict[str, str]] = {}
        self.routers: dict[str, str] = {}
        self.factories: dict[str, str] = {}       # factory name -> module
        self.raw_decorators: list[tuple[str, ast.AST, str, str, int]] = []
        self.raw_edges: list[tuple[str, ast.AST, ast.AST, str | None, int]] = []
        # resolved
        self.decorators: list[tuple[str, str, str]] = []
        self.edges: list[tuple[str, str]] = []
        self.used_factories: set[str] = set()
        self.dynamic_mounts: list[str] = []
        self.conditional_mounts: list[str] = []
        self.unresolved: list[str] = []

    # -- phase 1: extraction ----------------------------------------------- #
    def scan(self) -> None:
        for path in sorted(self.server_root.rglob("*.py")):
            self._scan_file(path)
        self._resolve_all()
        if self.unresolved:
            raise ContractError(
                "无法解析以下 include_router 符号（拒绝回落到幽灵父节点继续）：\n  - "
                + "\n  - ".join(sorted(set(self.unresolved)))
            )

    def _scan_file(self, path: pathlib.Path) -> None:
        module = path.relative_to(self.server_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        self.imports.setdefault(module, {})
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            self._collect_assignment(module, node)
            self._collect_import(module, node)
        self._collect_factories(module, tree)
        for node in ast.walk(tree):
            self._collect_decorator(module, node)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "include_router" and node.args:
                self.raw_edges.append(
                    (module, node.func.value, node.args[0], _enclosing_function(node, parents), node.lineno)
                )

    def _collect_assignment(self, module: str, node: ast.AST) -> None:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
            return
        target = node.targets[0].id
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            self.consts[f"{module}::{target}"] = node.value.value
        # only a module-level ``APIRouter(...)`` literal is a mountable router definition
        if isinstance(node.value, ast.Call) and _expr_name(node.value.func) == "APIRouter":
            self.routers[f"{module}::{target}"] = self._prefix_of(node.value, module)

    def _collect_import(self, module: str, node: ast.AST) -> None:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                self.imports[module][alias.asname or alias.name] = (
                    f"{_module_to_file(node.module)}::{alias.name}"
                )

    def _collect_factories(self, module: str, tree: ast.AST) -> None:
        """A function that ``return``s a locally-built router is a router *factory*."""
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            local_routers: set[str] = set()
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Assign)
                    and len(sub.targets) == 1
                    and isinstance(sub.targets[0], ast.Name)
                    and isinstance(sub.value, ast.Call)
                    and _is_router_like(sub.value)
                ):
                    local_routers.add(sub.targets[0].id)
            if not local_routers:
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Name) and sub.value.id in local_routers:
                    self.factories[node.name] = module
                    break

    def _collect_decorator(self, module: str, node: ast.AST) -> None:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return
        for dec in node.decorator_list:
            if not (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr in _ALL_METHODS
                and dec.args
            ):
                continue
            route = self._const_str(dec.args[0], module)
            if route is None:
                continue
            self.raw_decorators.append((module, dec.func.value, dec.func.attr, route, node.lineno))

    # -- phase 2: resolution (fail-loud) ----------------------------------- #
    def _resolve_all(self) -> None:
        for module, expr, method, route, lineno in self.raw_decorators:
            router_id = self._resolve_decorator_target(module, expr, lineno)
            if router_id is not None:
                self.decorators.append((router_id, method, route))
        for module, parent_expr, child_node, enclosing, lineno in self.raw_edges:
            parent_id, skip = self._resolve_parent(module, parent_expr, enclosing, lineno)
            if skip:
                continue
            for child_id in self._resolve_child(module, child_node, lineno):
                if parent_id is not None and child_id is not None:
                    self.edges.append((parent_id, child_id))
        for name, module in self.factories.items():
            if name not in self.used_factories:
                self.unresolved.append(
                    f"{module}: router 工厂 {name!r} 未被任何 include_router 引用（fail-loud）"
                )

    def _resolve_decorator_target(self, module: str, expr: ast.AST, lineno: int) -> str | None:
        if isinstance(expr, ast.Name):
            if expr.id in _ROOT_NAMES:
                return _ROOT_SENTINEL
            return self._resolve_symbol(module, expr.id, expr, lineno, "装饰器目标")
        self.unresolved.append(f"{module}:{lineno}: 无法解析装饰器目标 {ast.unparse(expr)!r}")
        return None

    def _resolve_parent(
        self, module: str, expr: ast.AST, enclosing: str | None, lineno: int
    ) -> tuple[str | None, bool]:
        """Return ``(parent_id, skip)``. ``skip`` marks factory/composite-internal wiring."""
        if isinstance(expr, ast.Name):
            if expr.id in _ROOT_NAMES:
                return _ROOT_SENTINEL, False
            if expr.id == _RECEIVER_PARAM_NAME:
                if enclosing == _EXECUTION_METHOD:
                    return _EXECUTION_ROUTER_RECEIVER, False
                if enclosing in self.factories:
                    # wiring inside a router factory — not part of the static mount graph
                    return None, True
                self.unresolved.append(
                    f"{module}:{lineno}: 局部接收者 {expr.id!r}（不在 {_EXECUTION_METHOD} 中，"
                    "也不是已知 router 工厂）"
                )
                return None, False
            return self._resolve_symbol(module, expr.id, expr, lineno, "父"), False
        self.unresolved.append(f"{module}:{lineno}: 无法解析父表达式 {ast.unparse(expr)!r}")
        return None, False

    def _resolve_child(self, module: str, expr: ast.AST, lineno: int) -> list[str | None]:
        if isinstance(expr, ast.Call):
            # A call child is a recognised **dynamic factory** (e.g. ``create_runtime_router()``):
            # recorded, contributes no static route, never fabricated.
            name = _expr_name(expr.func)
            if name:
                self.used_factories.add(name)
            self.dynamic_mounts.append(f"{module}:{lineno}: {ast.unparse(expr)}")
            return []
        if isinstance(expr, ast.Attribute):
            attr = expr.attr
            if attr in _COMPOSITE_ROUTERS:
                self.used_factories.add(attr)
                return list(_COMPOSITE_ROUTERS[attr])
            if attr in _PROPERTY_ROUTERS:
                return [self._require_known(_PROPERTY_ROUTERS[attr], module, lineno, expr)]
            self.unresolved.append(
                f"{module}:{lineno}: 未映射的属性路由 {ast.unparse(expr)!r}"
                "（属性名未命中 _PROPERTY_ROUTERS / _COMPOSITE_ROUTERS）"
            )
            return []
        if isinstance(expr, ast.Name):
            return [self._resolve_symbol(module, expr.id, expr, lineno, "子")]
        self.unresolved.append(f"{module}:{lineno}: 无法解析子表达式 {ast.unparse(expr)!r}")
        return []

    def _resolve_symbol(self, module: str, name: str, expr: ast.AST, lineno: int, role: str) -> str | None:
        if name in self.imports.get(module, {}):
            return self._require_known(self.imports[module][name], module, lineno, expr, role)
        local = f"{module}::{name}"
        if local in self.routers:
            return local
        self.unresolved.append(
            f"{module}:{lineno}: 未解析的{role}符号 {name!r}（非 import、非模块级 APIRouter 定义）"
        )
        return None

    def _require_known(self, router_id: str, module: str, lineno: int, expr: ast.AST, role: str = "子") -> str | None:
        if router_id in self.routers:
            return router_id
        self.unresolved.append(
            f"{module}:{lineno}: {role}符号 {router_id!r} 未指向已知 APIRouter 定义（{ast.unparse(expr)}）"
        )
        return None

    # -- small AST helpers ------------------------------------------------- #
    def _prefix_of(self, call: ast.Call, module: str) -> str:
        for kw in call.keywords:
            if kw.arg == "prefix":
                return self._const_str(kw.value, module) or ""
        return ""

    def _const_str(self, node: ast.AST, module: str) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return self.consts.get(f"{module}::{node.id}")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self._const_str(node.left, module)
            right = self._const_str(node.right, module)
            if left is not None and right is not None:
                return left + right
        return None

    # -- resolution -------------------------------------------------------- #
    def check_edges(self) -> None:
        known = set(self.routers) | {_ROOT_SENTINEL}
        bad = sorted({endpoint for edge in self.edges for endpoint in edge if endpoint not in known})
        if bad:
            raise ContractError(f"挂载边指向未知 router：{bad}")

    def resolve(self) -> tuple[set[str], set[tuple[str, str]], set[str]]:
        """Return ``(http_paths, http_operations, socket_paths)`` (all absolute)."""
        self.check_edges()
        parent: dict[str, str] = {}
        for par, child in self.edges:
            parent.setdefault(child, par)

        def compose(cid: str, seen: frozenset[str] = frozenset()) -> str:
            if cid == _ROOT_SENTINEL:
                return ""
            if cid in seen:
                raise ContractError(f"挂载图成环：{cid}")
            par = parent.get(cid)
            if par is None:
                raise ContractError(f"router {cid!r} 未挂载到任何父节点")
            return compose(par, seen | {cid}) + self.routers.get(cid, "")

        def top_prefix(cid: str) -> str:
            seen: set[str] = set()
            cur = cid
            while True:
                if cur in seen:
                    raise ContractError(f"挂载图成环：{cid}")
                seen.add(cur)
                par = parent.get(cur)
                if par is None:
                    raise ContractError(f"router {cur!r} 的挂载链未到达 app 根")
                if par == _ROOT_SENTINEL:
                    return self.routers.get(cur, "")
                cur = par

        mounted_paths: set[str] = set()
        mounted_ops: set[tuple[str, str]] = set()
        sockets: set[str] = set()
        # A decorated router that is **not** the endpoint of any ``include_router``
        # edge is *not* silently dropped: it is reachable only through a subclass
        # property override / conditional app assembly (e.g. the docker-runtime
        # alternates of the workspace & sockets routers). Such an alternate is
        # accepted **only** when every route it emits is already covered by a
        # mounted router; otherwise it is a hard error (a ghost path).
        conditional: list[tuple[str, str, str]] = []
        for router_id, method, route in self.decorators:
            if router_id == _ROOT_SENTINEL:
                full = _norm(route)
            elif parent.get(router_id) is None:
                local = _norm(self.routers.get(router_id, "") + route)
                conditional.append((router_id, method, local))
                continue
            else:
                full = _norm(compose(router_id) + route)
                prefix = top_prefix(router_id)
                if prefix not in _ALLOWED_TOP_PREFIXES:
                    raise ContractError(
                        f"router {router_id!r} 的顶层挂载前缀 {prefix!r} 不在白名单 "
                        f"{sorted(_ALLOWED_TOP_PREFIXES)} 内（疑似缺 /api 的幽灵路径）"
                    )
            if method == "websocket":
                sockets.add(full)
            else:
                mounted_paths.add(full)
                mounted_ops.add((method.upper(), full))

        for router_id, method, full in conditional:
            # Conditional/alternate router the default app does not mount: acceptable
            # **only** when its exact route is already covered by a mounted router.
            covered = (
                full in mounted_paths
                or full in sockets
                or ("/api" + full) in mounted_paths
                or ("/sockets" + full) in sockets
            )
            if not covered:
                raise ContractError(
                    f"未挂载（conditional）router {router_id!r} 产出了未被任何挂载 router "
                    f"覆盖的路径 {method.upper()} {full!r} —— 拒绝写出幽灵路径"
                )
            self.conditional_mounts.append(f"{router_id} -> {method.upper()} {full}")
        return mounted_paths, mounted_ops, sockets


def _extract_search_contract(event_router: pathlib.Path) -> tuple[list[str], int]:
    """Read ``search_conversation_events``'s Annotated query params + ``limit`` cap."""
    tree = ast.parse(event_router.read_text(encoding="utf-8", errors="replace"))
    params: list[str] = []
    limit_cap = 100
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "search_conversation_events":
            for arg in node.args.args:
                if isinstance(arg.annotation, ast.Subscript) and _expr_name(arg.annotation.value) == "Annotated":
                    params.append(arg.arg)
                    if arg.arg == "limit":
                        limit_cap = _query_le(arg.annotation) or limit_cap
    if not params:
        raise ContractError("event_router.py::search_conversation_events 未找到查询参数")
    return params, limit_cap


def _query_le(annotation: ast.Subscript) -> int | None:
    """Pull ``le=<int>`` out of ``Annotated[int, Query(..., le=100)]``."""
    for elt in annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else []:
        if isinstance(elt, ast.Call) and _expr_name(elt.func) == "Query":
            for kw in elt.keywords:
                if kw.arg == "le" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, int):
                    return kw.value.value
    return None


def _load_openapi(path: pathlib.Path) -> tuple[set[str], set[tuple[str, str]]]:
    """Accept a raw OpenAPI spec or a compact ``{path: [methods]}`` dump."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    paths_obj = raw.get("paths") or {}
    http_paths: set[str] = set()
    ops: set[tuple[str, str]] = set()
    for route, value in paths_obj.items():
        if isinstance(value, dict):
            methods = [m for m in value if str(m).lower() in _HTTP_METHODS]
        else:
            methods = [m for m in value if str(m).lower() in _HTTP_METHODS]
        normalized = _norm(route)
        http_paths.add(normalized)
        for method in methods:
            ops.add((str(method).upper(), normalized))
    return http_paths, ops


def _validate_contract(contract: dict[str, Any]) -> None:
    """Output invariants — any failure refuses to write (fail loud)."""
    http_paths = contract["http_paths"]
    http_ops = contract["http_operations"]
    sockets = contract["socket_paths"]

    if len(set(http_paths)) != len(http_paths):
        raise ContractError("http_paths 含重复项")
    path_set = set(http_paths)
    for method, path in http_ops:
        if path not in path_set:
            raise ContractError(f"http_operations 的 {method} {path} 不在 http_paths 内")
    if len({(m, p) for m, p in http_ops}) != len(http_ops):
        raise ContractError("http_operations 含重复项")
    if path_set & set(sockets):
        raise ContractError("http_paths 与 socket_paths 相交")
    for path in list(http_paths) + list(sockets):
        if not path.startswith("/"):
            raise ContractError(f"非法路径（不以 / 开头）：{path!r}")
    for path in http_paths:
        if not (path in _ALLOWED_ROOT_EXACT or path.startswith(_ALLOWED_ROOTS)):
            raise ContractError(f"http 路径未落在已知根面（疑似缺 /api 前缀的幽灵路径）：{path!r}")
    for path in sockets:
        if not path.startswith("/sockets/"):
            raise ContractError(f"socket 路径未落在 /sockets/ 下：{path!r}")
    if len(http_paths) < 130:
        raise ContractError(f"http_paths 可疑地少：{len(http_paths)} < 130")


def build_contract(
    sdk_root: pathlib.Path, openapi_json: pathlib.Path | None
) -> tuple[dict[str, Any], list[str]]:
    """Return ``(contract, dynamic_mount_diagnostics)`` or raise :class:`ContractError`."""
    server_root = sdk_root / "openhands-agent-server" / "openhands" / "agent_server"
    if not server_root.is_dir():
        raise ContractError(f"--sdk-root 缺少 {server_root}（需指向 software-agent-sdk-main）")

    scan = _StaticScan(server_root)
    scan.scan()
    static_paths, static_ops, sockets = scan.resolve()

    runtime_paths: set[str] = set()
    runtime_ops: set[tuple[str, str]] = set()
    if openapi_json is not None:
        runtime_paths, runtime_ops = _load_openapi(openapi_json)

    http_paths = static_paths | runtime_paths
    http_operations = static_ops | runtime_ops
    sockets = {_norm(s) for s in sockets}

    search_params, limit_cap = _extract_search_contract(server_root / "event_router.py")
    version = _read_version(sdk_root)

    static_only = sorted(static_paths - runtime_paths)
    runtime_only = sorted(runtime_paths - static_paths)
    how_to = (
        "在装有 openhands-agent-server 的解释器中 dump 运行时 OpenAPI，再合并静态扫描：\n"
        "  python -c \"import json;from openhands.agent_server.api import api;"
        "s=api.openapi();json.dump({'openapi':s.get('openapi'),"
        "'paths':{p:sorted(s['paths'][p]) for p in sorted(s['paths'])}},"
        "open('openapi.json','w'),indent=1)\"\n"
        "  python scripts/regenerate_official_contract.py "
        f"--sdk-root {sdk_root.as_posix()} --openapi-json openapi.json"
    )

    contract = {
        "_provenance": {
            "fixture": _DEFAULT_OUT.name,
            "package": "openhands-agent-server",
            "sdk_version": version,
            "method": (
                "静态 AST 扫描官方 server 源码（router-graph resolver）∪ 运行时 "
                "app.openapi()（--openapi-json）"
            ),
            "normalization": (
                "路由模板参数仅归一 starlette 转换器后缀（{x:path} -> {x}）；"
                "路径保存为绝对形式（挂载在 api router 下的带 /api 前缀）。"
            ),
            "how_to_regenerate": how_to,
            "counts": {
                "static_http_paths": len(static_paths),
                "runtime_http_paths": len(runtime_paths),
                "http_paths": len(http_paths),
                "http_operations": len(http_operations),
                "socket_paths": len(sockets),
            },
        },
        "http_paths": sorted(http_paths),
        "http_operations": sorted([m, p] for m, p in http_operations),
        "socket_paths": sorted(sockets),
        "search_query_params": sorted(search_params),
        "limit_cap": limit_cap,
        "known_blind_spots": {
            "static_only_unseen_at_runtime_default": static_only,
            "runtime_only_missed_by_static_scan": runtime_only,
            "note": (
                "官方路径权威集合 = 静态扫描 ∪ 运行时 OpenAPI；"
                "include_in_schema=False 与条件挂载的路由只在静态侧，"
                "动态组装的 router 族只在运行时侧。"
            ),
        },
    }
    _validate_contract(contract)
    diagnostics = [f"dynamic-factory: {d}" for d in sorted(set(scan.dynamic_mounts))]
    diagnostics += [
        f"conditional-alternate: {c}" for c in sorted(set(scan.conditional_mounts))
    ]
    return contract, diagnostics


def _read_version(sdk_root: pathlib.Path) -> str:
    pyproject = sdk_root / "openhands-agent-server" / "pyproject.toml"
    if pyproject.is_file():
        for line in pyproject.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("version"):
                return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return "unknown"


def _contract_diff(existing: dict[str, Any], fresh: dict[str, Any]) -> list[str]:
    """Human-readable summary of the meaningful (non-provenance) differences."""
    diffs: list[str] = []
    for key in ("http_paths", "socket_paths", "http_operations", "search_query_params", "limit_cap"):
        old, new = existing.get(key), fresh.get(key)
        if old == new:
            continue
        if isinstance(old, list) and isinstance(new, list):
            added = sorted(set(map(str, new)) - set(map(str, old)))
            removed = sorted(set(map(str, old)) - set(map(str, new)))
            diffs.append(f"{key}: +{len(added)}/-{len(removed)} (added={added[:5]} removed={removed[:5]})")
        else:
            diffs.append(f"{key}: {old!r} -> {new!r}")
    return diffs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-root", type=pathlib.Path, required=True,
                        help="Path to a software-agent-sdk-main checkout.")
    parser.add_argument("--openapi-json", type=pathlib.Path, default=None,
                        help="Runtime OpenAPI dump (raw spec or {path:[methods]}).")
    parser.add_argument("--out", type=pathlib.Path, default=_DEFAULT_OUT,
                        help="Fixture output path (default: tests/fixtures/inc30/...).")
    parser.add_argument("--check", action="store_true",
                        help="Extract + validate and compare against --out; never write.")
    args = parser.parse_args(argv)

    try:
        contract, dynamic_mounts = build_contract(args.sdk_root, args.openapi_json)
    except ContractError as exc:
        print(f"CONTRACT ERROR: {exc}", file=sys.stderr)
        return 2

    if dynamic_mounts:
        print(
            "NOTE: 以下挂载不经由静态 include_router 边（已逐一校验，不产出未覆盖路径）："
        )
        for diag in dynamic_mounts:
            print(f"  - {diag}")

    exists = args.out.is_file()
    existing: dict[str, Any] | None = None
    if exists:
        try:
            existing = json.loads(args.out.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            existing = None
            print(f"WARN: 无法读取既有 fixture（{exc}）", file=sys.stderr)

    if args.check:
        if existing is None:
            print(f"CHECK: 无既有 fixture 可比对；提取通过（{args.out} 不存在或不可读）")
            return 0
        diffs = _contract_diff(existing, contract)
        if diffs:
            print("CHECK MISMATCH:\n  - " + "\n  - ".join(diffs), file=sys.stderr)
            return 1
        print(f"CHECK OK: {args.out} 与提取结果逐键一致")
        return 0

    if existing is not None and existing == contract:
        print(f"UNCHANGED {args.out}（逐键一致，未覆盖）")
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"WROTE {args.out}")

    print(
        f"http_paths={len(contract['http_paths'])} "
        f"http_operations={len(contract['http_operations'])} "
        f"socket_paths={len(contract['socket_paths'])} "
        f"limit_cap={contract['limit_cap']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
