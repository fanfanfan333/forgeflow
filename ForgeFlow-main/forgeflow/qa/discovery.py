"""Repository intelligence and the capability inventory (Phase A, §3/§4/§5).

Layer L2. This module answers "what is actually in this repository?" without
hard-coding a technology stack:

* :meth:`RepositoryIntelligence.detect_technology` reads the *facts on disk*
  (``pyproject.toml``, ``Makefile``, CI workflow, compose files, the SPA
  ``package.json``, the router/test file counts) and returns them as data.
* :meth:`RepositoryIntelligence.discover` turns those facts into a
  :class:`CapabilityInventory` whose every entry points at a **real path**; all
  19 layers are always present as keys (empty when nothing was detected).
* :meth:`RepositoryIntelligence.discovery_report` adds the static-check summary
  and the §5 *dead-capability candidates* — candidates only, never a verdict,
  because a true dead-code call needs runtime evidence (a later phase).

Detection is additive and conservative: a capability is ``verified=False`` until
something actually runs it.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forgeflow.qa.models import (
    ALL_LAYERS,
    Capability,
    CapabilityInventory,
    DiscoveryReport,
    Layer,
)
from forgeflow.qa.static_checks import StaticChecks
from forgeflow.qa.task_runner import parse_makefile_targets

logger = logging.getLogger(__name__)

#: Layer → the concrete, repo-relative paths that evidence it. Paths that do not
#: exist are skipped, so this is a *map of possibilities*, not a requirement.
_CAPABILITY_SPECS: dict[Layer, tuple[tuple[str, str], ...]] = {
    Layer.CORE: (
        ("forgeflow/config.py", "central settings"),
        ("forgeflow/__init__.py", "package bootstrap"),
    ),
    Layer.API: (
        ("forgeflow/api/main.py", "FastAPI application"),
        ("forgeflow/api/routers", "HTTP routers"),
    ),
    Layer.DATABASE: (
        ("forgeflow/database.py", "connection pool"),
        ("alembic", "alembic migrations"),
        ("alembic.ini", "alembic configuration"),
    ),
    Layer.RUNTIME: (("forgeflow/runtime", "agent runtime"),),
    Layer.AGENT: (
        ("forgeflow/agent", "agent loop"),
        ("forgeflow/agents", "agent implementations"),
    ),
    Layer.TOOL: (
        ("forgeflow/runtime/tool_registry.py", "tool registry"),
        ("forgeflow/runtime/tool_handlers.py", "tool handlers"),
    ),
    Layer.MCP: (("forgeflow/mcp", "mcp server/client"),),
    Layer.SKILL: (("forgeflow/skills", "skill hub"),),
    Layer.MEMORY: (("forgeflow/memory", "memory hub"),),
    Layer.EXPERIENCE: (("forgeflow/experience", "experience hub"),),
    Layer.EVALUATION: (("forgeflow/evaluation", "evaluation hub"),),
    Layer.GOVERNANCE: (("forgeflow/governance", "governance hub"),),
    Layer.AUTH: (
        ("forgeflow/auth", "authentication"),
        ("forgeflow/rbac", "rbac policies"),
    ),
    Layer.DATA: (
        ("forgeflow/documents", "document plane"),
        ("forgeflow/resources", "resource center"),
    ),
    Layer.UI: (
        ("frontend", "vite spa"),
        ("dashboard", "streamlit dashboard"),
    ),
    Layer.OBSERVABILITY: (
        ("forgeflow/observability", "observability"),
        ("forgeflow/metrics", "metrics"),
    ),
    Layer.DURABILITY: (
        ("forgeflow/jobs", "background jobs"),
        ("forgeflow/state", "workflow state"),
    ),
    Layer.BUILD: (
        ("pyproject.toml", "python build config"),
        ("Makefile", "make orchestration"),
    ),
    Layer.DEPLOYMENT: (
        ("docker-compose.yml", "docker compose"),
        ("terraform", "terraform"),
        ("helm", "helm chart"),
        ("k8s", "kubernetes manifests"),
        ("Dockerfile", "container image"),
    ),
}


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib

        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001 - a malformed file yields no facts, not a crash
        logger.warning("could not parse %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


class RepositoryIntelligence:
    """Detects technology and builds the capability inventory from a root."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        """The project root this intelligence is scoped to."""
        return self._root

    # ── technology detection ─────────────────────────────────────────────
    def detect_technology(self) -> dict[str, Any]:
        """Return detected technology facts (data only; nothing is assumed)."""
        root = self._root
        tech: dict[str, Any] = {}

        pyproject = root / "pyproject.toml"
        if pyproject.is_file():
            data = _read_toml(pyproject)
            project = data.get("project", {})
            if isinstance(project, dict):
                tech["python_package"] = project.get("name")
                tech["requires_python"] = project.get("requires-python")
                raw_deps = project.get("dependencies", [])
                if isinstance(raw_deps, list):
                    tech["dependencies"] = sorted(
                        str(dep).split(";")[0].split(">")[0].split("<")[0].split("=")[0]
                        for dep in raw_deps
                        if isinstance(dep, str)
                    )

        makefile = root / "Makefile"
        tech["has_makefile"] = makefile.is_file()
        if makefile.is_file():
            tech["make_targets"] = sorted(
                parse_makefile_targets(makefile.read_text(encoding="utf-8", errors="replace"))
            )

        tech["has_ci_workflow"] = (root / ".github" / "workflows" / "ci.yml").is_file()
        tech["docker_compose_files"] = sorted(p.name for p in root.glob("docker-compose*.yml"))
        tech["has_alembic"] = (root / "alembic.ini").is_file()
        alembic_versions = root / "alembic" / "versions"
        tech["alembic_versions"] = (
            len(list(alembic_versions.glob("*.py"))) if alembic_versions.is_dir() else 0
        )

        frontend_pkg = root / "frontend" / "package.json"
        if frontend_pkg.is_file():
            try:
                pkg = json.loads(frontend_pkg.read_text(encoding="utf-8", errors="replace"))
            except json.JSONDecodeError:
                pkg = {}
            if isinstance(pkg, dict):
                tech["frontend_name"] = pkg.get("name")
                deps = pkg.get("dependencies")
                if isinstance(deps, dict):
                    tech["frontend_dependencies"] = sorted(deps)

        routers = root / "forgeflow" / "api" / "routers"
        tech["router_files"] = len(list(routers.glob("*.py"))) if routers.is_dir() else 0
        tests = root / "tests"
        tech["test_files"] = len(list(tests.rglob("test_*.py"))) if tests.is_dir() else 0

        forgeflow_dir = root / "forgeflow"
        if forgeflow_dir.is_dir():
            tech["forgeflow_modules"] = sorted(
                child.name for child in forgeflow_dir.iterdir() if child.is_dir()
            )
        return tech

    # ── inventory ────────────────────────────────────────────────────────
    def _entrypoints_for(self, rel: str, layer: Layer) -> list[str]:
        """Real symbols/targets that make a capability concrete, when known."""
        root = self._root
        if rel == "Makefile":
            makefile = root / "Makefile"
            if makefile.is_file():
                return sorted(
                    parse_makefile_targets(makefile.read_text(encoding="utf-8", errors="replace"))
                )
        if rel == "forgeflow/api/routers":
            routers = root / rel
            return sorted(py.stem for py in routers.glob("*.py") if py.name != "__init__.py")
        return []

    def discover(self, *, run_id: str) -> CapabilityInventory:
        """Build the :class:`CapabilityInventory` for ``run_id``."""
        root = self._root
        technology = self.detect_technology()
        layers: dict[Layer, list[Capability]] = {layer: [] for layer in ALL_LAYERS}

        for layer, specs in _CAPABILITY_SPECS.items():
            for rel, name in specs:
                abspath = root / rel
                if not abspath.exists():
                    continue
                is_dir = abspath.is_dir()
                layers[layer].append(
                    Capability(
                        capability_id=f"{layer.value}:{rel}",
                        layer=layer,
                        name=name,
                        path=rel,
                        entrypoints=self._entrypoints_for(rel, layer),
                        detected_by=["dir_exists" if is_dir else "file_exists"],
                        confidence=0.9 if is_dir else 1.0,
                        verified=False,
                    )
                )

        totals = {layer.value: len(caps) for layer, caps in layers.items()}
        totals["_all"] = sum(totals[layer.value] for layer in ALL_LAYERS)
        return CapabilityInventory(
            run_id=run_id,
            generated_at=_utc_now_iso(),
            root=str(root),
            technology=technology,
            layers=layers,
            totals=totals,
        )

    # ── report ───────────────────────────────────────────────────────────
    def _dead_capability_candidates(self, static_result: dict[str, Any]) -> list[str]:
        """§5 subset — router modules not wired into ``api/main.py`` (candidates)."""
        routes = static_result.get("route_registration", {})
        unmapped = routes.get("unmapped_routers", []) if isinstance(routes, dict) else []
        return [f"API:forgeflow/api/routers/{module}.py" for module in unmapped]

    def discovery_report(self, *, run_id: str) -> DiscoveryReport:
        """Inventory + static-check summary + dead-capability candidates."""
        inventory = self.discover(run_id=run_id)
        static_result = StaticChecks(self._root).run_all()
        return DiscoveryReport(
            run_id=run_id,
            generated_at=_utc_now_iso(),
            inventory=inventory,
            dead_capability_candidates=self._dead_capability_candidates(static_result),
            static_checks=static_result,
        )


__all__ = ["RepositoryIntelligence"]
