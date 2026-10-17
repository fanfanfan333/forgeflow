"""Static checks — the minimum credible static-analysis set (§8.1/§9/§15).

Layer L2. Five checks, all *read-only* and offline:

1. **Route registration** — every ``forgeflow/api/routers/*.py`` module should be
   wired into ``api/main.py`` via ``include_router``; un-mapped routers are
   reported as candidates.
2. **Permission registration** — ``rbac/policies.py::ROUTE_PERMISSION_MAP`` must
   be parseable; if it is not, the result is honestly ``INCONCLUSIVE`` rather
   than a fabricated ``0`` unmapped routes.
3. **Unresolved imports** — AST-scan the package for imports that resolve to
   neither the stdlib, a declared dependency, the local packages, nor an
   importable module.
4. **Missing env vars** — ``Settings`` field names absent from ``.env.example``.
5. **Client/backend port mismatch** — the SPA dev-proxy target port vs the
   backend ``DEFAULT_PORT``.

None of these raise: an uncertain result is reported as such (empty / annotated),
never dressed up as a clean pass.
"""

from __future__ import annotations

import ast
import importlib.util
import logging
import re
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ROUTER_VERB_RE = re.compile(
    r"@router\.(get|post|put|patch|delete|options|head)\s*\(", re.IGNORECASE
)
_INCLUDE_ROUTER_RE = re.compile(
    r"include_router\(\s*([A-Za-z_]\w*)\.\w*router\s*,\s*prefix\s*=\s*[\"']([^\"']+)[\"']",
    re.DOTALL,
)
_SETTINGS_FIELD_RE = re.compile(r"^ {4}([a-z_][a-z0-9_]*)\s*:\s*[^=\n]+=\s*Field\(", re.MULTILINE)
_ENV_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)
_VITE_TARGET_RE = re.compile(r"DEFAULT_API_TARGET\s*=\s*[\"']([^\"']+)[\"']")
_BACKEND_PORT_RE = re.compile(r"DEFAULT_PORT\s*=\s*(\d+)")
_URL_PORT_RE = re.compile(r":(\d{2,5})(?:/|$)")

_LOCAL_PACKAGES = ("forgeflow", "dashboard")


def _rel(path: Path, root: Path) -> str:
    """A repo-relative POSIX path (stable across platforms)."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _read(path: Path) -> str:
    """Read text, tolerating decode surprises (checks must never crash)."""
    return path.read_text(encoding="utf-8", errors="replace")


def _token_present(text: str, name: str) -> bool:
    """True when ``name`` appears in ``text`` as a whole word."""
    return re.search(rf"\b{re.escape(name)}\b", text) is not None


def _normalise_dependency(spec: str) -> str:
    """Reduce a PEP 508 requirement to its bare, importable module name."""
    name = re.split(r"[<>=!~;\[\s]", spec.strip(), maxsplit=1)[0]
    return name.strip().replace("-", "_")


class StaticChecks:
    """The five static checks, scoped to one project root."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    # ── 1. route registration ────────────────────────────────────────────
    def route_registration(self) -> dict[str, Any]:
        """Cross-check router modules against ``api/main.py`` ``include_router``."""
        routers_dir = self._root / "forgeflow" / "api" / "routers"
        result: dict[str, Any] = {
            "status": "OK",
            "router_files": [],
            "registered_prefixes": [],
            "unmapped_routers": [],
            "unmapped": [],
            "route_decorators": 0,
        }
        if not routers_dir.is_dir():
            result["status"] = "NOT_FOUND"
            return result

        decorators = 0
        modules: list[str] = []
        for py in sorted(routers_dir.glob("*.py")):
            if py.name == "__init__.py":
                continue
            text = _read(py)
            count = len(_ROUTER_VERB_RE.findall(text))
            decorators += count
            modules.append(py.stem)
            result["router_files"].append(
                {"module": py.stem, "path": _rel(py, self._root), "routes": count}
            )

        main_py = self._root / "forgeflow" / "api" / "main.py"
        if main_py.is_file():
            main_text = _read(main_py)
            result["registered_prefixes"] = sorted(
                {prefix for _mod, prefix in _INCLUDE_ROUTER_RE.findall(main_text)}
            )
            unmapped = [m for m in modules if not _token_present(main_text, m)]
        else:
            unmapped = list(modules)
        result["unmapped_routers"] = sorted(unmapped)
        result["unmapped"] = result["unmapped_routers"]
        result["route_decorators"] = decorators
        return result

    # ── 2. permission registration ───────────────────────────────────────
    def permission_registration(self) -> dict[str, Any]:
        """Parse ``ROUTE_PERMISSION_MAP``; ``INCONCLUSIVE`` if it cannot be read."""
        policies = self._root / "forgeflow" / "rbac" / "policies.py"
        if not policies.is_file():
            return {
                "status": "INCONCLUSIVE",
                "reason": f"{_rel(policies, self._root)} not found",
                "mapped_routes": 0,
            }
        try:
            tree = ast.parse(_read(policies))
        except SyntaxError as exc:
            return {
                "status": "INCONCLUSIVE",
                "reason": f"cannot parse policies.py: {exc}",
                "mapped_routes": 0,
            }
        mapped: int | None = None
        for node in ast.walk(tree):
            # ``ROUTE_PERMISSION_MAP`` is declared with a type annotation, so it
            # is an ``AnnAssign`` (not a plain ``Assign``) — handle both.
            target: ast.expr | None = None
            value: ast.expr | None = None
            if isinstance(node, ast.Assign):
                value = node.value
                for candidate in node.targets:
                    if isinstance(candidate, ast.Name) and candidate.id == "ROUTE_PERMISSION_MAP":
                        target = candidate
            elif isinstance(node, ast.AnnAssign):
                value = node.value
                if isinstance(node.target, ast.Name) and node.target.id == "ROUTE_PERMISSION_MAP":
                    target = node.target
            if target is not None:
                if isinstance(value, ast.Dict):
                    mapped = len(value.keys)
                break
        if mapped is None:
            return {
                "status": "INCONCLUSIVE",
                "reason": "ROUTE_PERMISSION_MAP not found in policies.py",
                "mapped_routes": 0,
            }
        return {
            "status": "OK",
            "mapped_routes": mapped,
            "source": _rel(policies, self._root),
        }

    # ── 3. unresolved imports ────────────────────────────────────────────
    def _declared_dependencies(self) -> set[str]:
        """Bare module names declared in ``pyproject.toml`` ``[project].dependencies``."""
        pyproject = self._root / "pyproject.toml"
        if not pyproject.is_file():
            return set()
        try:
            import tomllib

            data = tomllib.loads(_read(pyproject))
        except Exception as exc:  # noqa: BLE001 - a malformed pyproject is not fatal here
            logger.warning("could not parse pyproject.toml: %s", exc)
            return set()
        project = data.get("project", {}) if isinstance(data, dict) else {}
        deps = project.get("dependencies", []) if isinstance(project, dict) else []
        return {_normalise_dependency(dep) for dep in deps if isinstance(dep, str)}

    def _iter_py_files(self) -> list[Path]:
        files: list[Path] = []
        for package in _LOCAL_PACKAGES:
            base = self._root / package
            if base.is_dir():
                files.extend(sorted(base.rglob("*.py")))
        return files

    @staticmethod
    def _is_importable(name: str) -> bool:
        try:
            return importlib.util.find_spec(name) is not None
        except (ImportError, ValueError, ModuleNotFoundError):
            return False

    def unresolved_imports(self) -> list[str]:
        """Imports that map to no stdlib / declared / local / importable module."""
        declared = self._declared_dependencies()
        unresolved: set[str] = set()
        for path in self._iter_py_files():
            try:
                tree = ast.parse(_read(path))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level and node.level > 0:
                        continue  # relative import — resolved inside the package
                    if node.module:
                        names = [node.module]
                for name in names:
                    top = name.split(".")[0]
                    if not top or top in sys.stdlib_module_names:
                        continue
                    if top in _LOCAL_PACKAGES or top in declared:
                        continue
                    if self._is_importable(top):
                        continue
                    unresolved.add(name)
        return sorted(unresolved)

    # ── 4. missing env vars ──────────────────────────────────────────────
    def missing_env_vars(self) -> list[str]:
        """``Settings`` field names (UPPER) that do not appear in ``.env.example``."""
        config_py = self._root / "forgeflow" / "config.py"
        env_example = self._root / ".env.example"
        if not config_py.is_file() or not env_example.is_file():
            return []
        fields = {m.group(1).upper() for m in _SETTINGS_FIELD_RE.finditer(_read(config_py))}
        keys = {m.group(1) for m in _ENV_KEY_RE.finditer(_read(env_example))}
        return sorted(fields - keys)

    # ── 5. client / backend port mismatch ────────────────────────────────
    def client_backend_mismatch(self) -> list[str]:
        """Compare the SPA proxy target port with the backend ``DEFAULT_PORT``."""
        vite = self._root / "frontend" / "vite.config.ts"
        runtime = self._root / "forgeflow" / "bootstrap" / "runtime.py"
        if not vite.is_file() or not runtime.is_file():
            return []
        vite_match = _VITE_TARGET_RE.search(_read(vite))
        backend_match = _BACKEND_PORT_RE.search(_read(runtime))
        if vite_match is None or backend_match is None:
            return [
                "could not resolve both ports "
                f"(vite_target={vite_match.group(1) if vite_match else None}, "
                f"backend_port={backend_match.group(1) if backend_match else None})"
            ]
        target = vite_match.group(1)
        port_match = _URL_PORT_RE.search(target)
        vite_port = port_match.group(1) if port_match else None
        backend_port = backend_match.group(1)
        if vite_port != backend_port:
            return [
                f"frontend DEFAULT_API_TARGET port {vite_port} != backend "
                f"DEFAULT_PORT {backend_port} (target={target})"
            ]
        return []

    # ── aggregate ────────────────────────────────────────────────────────
    def run_all(self) -> dict[str, Any]:
        """Run all five checks and return the raw results plus a summary."""
        routes = self.route_registration()
        permissions = self.permission_registration()
        imports = self.unresolved_imports()
        env_vars = self.missing_env_vars()
        mismatch = self.client_backend_mismatch()
        return {
            "route_registration": routes,
            "permission_registration": permissions,
            "unresolved_imports": imports,
            "missing_env_vars": env_vars,
            "client_backend_mismatch": mismatch,
            "summary": {
                "router_files": len(routes.get("router_files", [])),
                "unmapped_routers": len(routes.get("unmapped_routers", [])),
                "permission_status": permissions.get("status"),
                "mapped_routes": permissions.get("mapped_routes", 0),
                "unresolved_imports": len(imports),
                "missing_env_vars": len(env_vars),
                "client_backend_mismatch": len(mismatch),
            },
        }


__all__ = ["StaticChecks"]
