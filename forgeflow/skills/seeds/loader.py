"""INC46 T19 — seed skills: load human-authored seeds + cold-start provisioning.

What this module is
-------------------
ForgeFlow's skill loop (compile → evaluate → publish → evolve) needs *history*
to produce anything: experiences, patterns, candidates. A **brand-new tenant**
has none of that, so retrieval against an empty experience library returns
nothing and the platform looks broken on first contact. **Seeds** close that
gap: a small, curated set of skills that ship with the build and are installed
for a cold tenant so its very first :meth:`SkillRegistry.retrieve` already hits
something usable.

A seed is deliberately **not** a second, simplified format:

* its source is a full seven-segment contract (T07) — the *same* shape every
  authored skill is reviewed in — stored as ``spec.json`` in the seed's
  directory;
* it is validated with :func:`forgeflow.skills.schemas.validate_contract_document`
  (a missing segment / illegal field is an **explicit, named error**, never a
  silent skip);
* it materialises through the **exact same** T11 path as any authored skill
  (:func:`forgeflow.skills.skill_md.materialize_skill` → ``SKILL.md`` rendered by
  :func:`forgeflow.skills.spec_mapping.render_skill_md`, judged by
  :func:`forgeflow.skills.spec_validator.validate_skill_md`);
* it is stored as an ordinary :class:`SkillRecord` / :class:`SkillVersionRecord`,
  retrieved by the ordinary T09 chain, and executed by the ordinary T03 runtime.

Origin (``origin=seed``) — recorded with existing columns, **no migration**
--------------------------------------------------------------------------
This task book gives no migration slot, and migrations are owned by other tasks,
so a new ``origin`` column is **out of scope**. A seed is instead marked with the
**existing** ``SkillRecord.owner`` field (``"seed"``) plus the **existing**
``SkillRecord.tags`` list (``"origin:seed"``). :func:`is_seed` / :func:`origin_of`
read those two conveniences; :func:`origin_of` reports ``"seed"`` or
``"authored"`` so a caller can distinguish provenance without a schema change.

Runtime procedure (``procedure.json``) — the orchestratability fix
-----------------------------------------------------------------
T07's seven-segment contract expresses Procedure as ``procedure.steps`` — a list
of human step *labels*, and the segment model forbids extra keys. T03's runtime
drives a **structured** procedure (``[{purpose, tool, input, output,
validation}]``) whose ``tool`` names a platform tool. The two shapes are
deliberately distinct: the contract is a *review* artefact, the structured
procedure is an *executable* declaration.

A seed therefore carries BOTH:

* ``spec.json``      — the seven-segment contract (T07 / T11);
* ``procedure.json`` — ``{"domain": str, "procedure": [ {purpose, tool, ...} ]}``
  — the T03 structured procedure, validated against the platform whitelist
  (:data:`forgeflow.runtime.gate.PLATFORM_PLAN_TOOLS`) at load time;
* ``SKILL.md``       — the materialised product (optional; when present it must
  be the byte-exact render of ``spec.json``).

Crucially, the **persisted** version ``spec`` (:meth:`Seed.runtime_spec`) is the
*runtime* shape: its ``procedure`` key is the structured **list**, and it also
carries ``steps`` (the contract's labels) and ``tools``. That is what the real
orchestrator path reads —
:func:`forgeflow.runtime.orchestrator._resolve_injected_skills` reconstructs the
skill dict from a stored version's ``spec`` and
:func:`forgeflow.skills.runtime.load_procedure` :func:`~forgeflow.skills.runtime.to_plan_candidates`
drive it — so a **discovered seed is really orchestratable**, not advisory text.
:meth:`InstalledSeed.runtime_skill` reproduces exactly that dict for tests /
callers, and execution is never re-implemented here (it is the *same* T03 entry
points the orchestrator uses).

Honest failure (红线：禁止静默跳过)
----------------------------------
:class:`SeedError` is raised — never a silent skip — when a seed is malformed:
an illegal directory slug, a ``manifest.name`` that disagrees with its
directory, a contract that fails T07 (naming the exact segment/field), a
procedure step whose tool is outside the platform whitelist, or a contract that
fails T11 materialisation. :func:`load_seeds` propagates the first failure
rather than quietly shipping a partial catalogue.

红线 6 (seed body is immutable)
-------------------------------
:func:`apply_evolution` records an evolved seed through the existing
:func:`forgeflow.skills.versioning.create_version`, which mints a **new** semver
and lifts ``current_version`` — it never rewrites the incumbent version's body.
A test pins that the seed's original ``spec`` sha256 is unchanged by an
evolution, and a counterfactual proves the pin is load-bearing.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from forgeflow.runtime.gate import PLATFORM_PLAN_TOOLS
from forgeflow.skills.models import SkillRecord, SkillVersionRecord
from forgeflow.skills.schemas import validate_contract_document
from forgeflow.skills.segments import (
    SEGMENT_EVALUATION,
    SEGMENT_EXAMPLES,
    SEGMENT_KNOWLEDGE,
    SEGMENT_MANIFEST,
    SEGMENT_POLICIES,
    SEGMENT_PROCEDURE,
    SEGMENT_TOOL_BINDINGS,
)
from forgeflow.skills.skill_md import SkillBundle, materialize_skill
from forgeflow.skills.spec_validator import validate_skill_name

logger = logging.getLogger(__name__)

__all__ = [
    "SEEDS_DIR",
    "SPEC_FILENAME",
    "PROCEDURE_FILENAME",
    "SKILL_MD_FILENAME",
    "SEED_OWNER",
    "SEED_ORIGIN_TAG",
    "SEED_VERSION",
    "SEED_STATUS",
    "ORIGIN_SEED",
    "ORIGIN_AUTHORED",
    "SeedError",
    "Seed",
    "InstalledSeed",
    "spec_sha256",
    "is_seed",
    "origin_of",
    "discover_seed_dirs",
    "load_seed_dir",
    "load_seeds",
    "install_seeds",
    "apply_evolution",
]

#: The directory that holds the seed sub-directories (this package's home).
SEEDS_DIR: Path = Path(__file__).resolve().parent

#: The seven-segment contract file inside a seed directory.
SPEC_FILENAME = "spec.json"
#: The T03 structured-procedure file inside a seed directory (optional).
PROCEDURE_FILENAME = "procedure.json"
#: The materialised product inside a seed directory (optional; when present the
#: loader verifies it is the exact render of ``spec.json``).
SKILL_MD_FILENAME = "SKILL.md"

#: ``SkillRecord.owner`` written for every seed (existing column — no migration).
SEED_OWNER = "seed"
#: ``SkillRecord.tags`` entry that marks a seed (existing column — no migration).
SEED_ORIGIN_TAG = "origin:seed"
#: The semver every seed ships as.
SEED_VERSION = "1.0.0"
#: A seed is published, so it enters the T09 candidate pool verbatim.
SEED_STATUS = "published"

#: :func:`origin_of` return values.
ORIGIN_SEED = "seed"
ORIGIN_AUTHORED = "authored"

class SeedError(Exception):
    """Raised when a seed is malformed — never silently skipped.

    Carries a human-readable reason naming the concrete problem (the offending
    slug / segment / field / tool), so a broken seed fails the load loudly
    instead of being quietly dropped from the catalogue.
    """


# --------------------------------------------------------------------------- #
# provenance helpers (origin=seed via existing columns)                        #
# --------------------------------------------------------------------------- #
def _value(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a ``SkillRecord`` **or** a plain mapping."""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def is_seed(skill: Any) -> bool:
    """Whether ``skill`` is a seed (owner ``"seed"`` or the ``origin:seed`` tag).

    Reads only the **existing** ``owner`` / ``tags`` fields, so it works for a
    stored ``SkillRecord`` and for a plain dict alike.
    """
    if str(_value(skill, "owner", "") or "") == SEED_OWNER:
        return True
    tags = _value(skill, "tags", []) or []
    if isinstance(tags, (list, tuple, set)):
        return SEED_ORIGIN_TAG in {str(t) for t in tags}
    return False


def origin_of(skill: Any) -> str:
    """The provenance of a skill: ``"seed"`` when :func:`is_seed`, else ``"authored"``."""
    return ORIGIN_SEED if is_seed(skill) else ORIGIN_AUTHORED


def spec_sha256(spec: Mapping[str, Any] | None) -> str:
    """sha256 of a contract's canonical JSON — the re-computable body currency.

    Deterministic and order-independent key ordering, so the same contract
    hashes identically while any content change flips the digest (used to prove
    红线 6: evolution never rewrites a seed's body).
    """
    blob = json.dumps(
        spec or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# loaded seed value objects                                                     #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Seed:
    """One loaded seed: the seven-segment contract + its runtime procedure.

    ``spec`` is the raw seven-segment contract (the reviewed source artefact);
    :meth:`runtime_spec` is the runtime-shaped body persisted in the version
    record; ``procedure`` is the validated T03 structured procedure; ``steps``
    are the contract's human step labels; ``bundle`` is the T11 materialisation
    (its ``files`` always contains ``SKILL.md``).
    """

    slug: str
    path: Path
    spec: Mapping[str, Any]
    procedure: tuple[Mapping[str, Any], ...]
    steps: tuple[str, ...]
    domain: str
    display_name: str
    description: str
    bundle: SkillBundle

    def spec_hash(self) -> str:
        """The seven-segment contract's body hash (see :func:`spec_sha256`)."""
        return spec_sha256(self.spec)

    def procedure_tools(self) -> list[str]:
        """The platform tool ids the runtime procedure drives (in order)."""
        return [str(step.get("tool") or "") for step in self.procedure]

    def declared_tools(self) -> list[str]:
        """The tools the contract declares in its Tool-bindings segment (in order)."""
        bindings = self.spec.get(SEGMENT_TOOL_BINDINGS)
        if not isinstance(bindings, Mapping):
            return []
        return [
            str(tool).strip()
            for tool in (bindings.get("tools") or [])
            if str(tool or "").strip()
        ]

    def runtime_spec(self) -> dict[str, Any]:
        """The **runtime spec** persisted as the version's ``spec`` (T03 shape).

        This is the single fact that makes a seed *orchestratable*: the
        orchestrator (:func:`forgeflow.runtime.orchestrator._resolve_injected_skills`)
        and :class:`forgeflow.skills.runtime.SkillRuntime` read a version's
        ``spec`` and drive it only when it carries a **structured**
        ``procedure`` list — not the contract's ``procedure`` mapping
        (``{"steps": [...]}``). A seed therefore persists its procedure in the
        runtime shape while the reviewed seven-segment contract stays the source
        artefact in the seed directory (and its body hash is carried along as
        ``contract_spec_sha256`` for provenance / 红线 6).

        Keys:
            ``procedure`` — the validated structured steps (the runtime drives
            these verbatim); ``steps`` — the contract's human step labels (the
            legacy text the code plane still injects); ``tools`` — the declared
            tool ids (capability filter :func:`forgeflow.skills.retrieval.capability_filter`
            and :func:`forgeflow.skills.tool_permissions.class_of_skill` read this);
            the six non-procedure contract segments are mirrored verbatim so the
            DB record stays traceable to the reviewed contract.
        """
        runtime: dict[str, Any] = {}
        for segment in (
            SEGMENT_MANIFEST,
            SEGMENT_KNOWLEDGE,
            SEGMENT_POLICIES,
            SEGMENT_TOOL_BINDINGS,
            SEGMENT_EVALUATION,
            SEGMENT_EXAMPLES,
        ):
            value = self.spec.get(segment)
            if value is not None:
                runtime[segment] = value
        # The **runtime** procedure (list of structured steps) replaces the
        # contract's ``{"steps": [...]}`` mapping — this is the whole point.
        runtime[SEGMENT_PROCEDURE] = [dict(step) for step in self.procedure]
        runtime["steps"] = list(self.steps)
        runtime["tools"] = self.declared_tools()
        runtime["contract_spec_sha256"] = self.spec_hash()
        return runtime

    def label(self) -> str:
        """``slug@SEED_VERSION`` — a convenient label for logs / evidence."""
        return f"{self.slug}@{SEED_VERSION}"


@dataclass(frozen=True)
class InstalledSeed:
    """A seed provisioned for a concrete tenant (records carry the tenant id).

    ``skill`` / ``version`` are the real, tenant-scoped records persisted through
    the ordinary repository, so retrieval / materialisation / execution all run
    on the same objects any authored skill would.
    """

    seed: Seed
    skill: SkillRecord
    version: SkillVersionRecord

    @property
    def slug(self) -> str:
        return self.seed.slug

    def contract_hash(self) -> str:
        """The **loaded seven-segment contract's** body hash (review artefact)."""
        return self.seed.spec_hash()

    def spec_hash(self) -> str:
        """The **installed version's** persisted spec body hash (红线 6 currency).

        With :meth:`Seed.runtime_spec` the persisted body is the runtime spec;
        its digest is the byte-identical currency an evolution must never
        rewrite in place.
        """
        return spec_sha256(self.version.spec)

    def runtime_skill(self) -> dict[str, Any]:
        """The T03 runtime skill dict (``{id, name, version, steps[, procedure]}``).

        Built from the **persisted** version spec — i.e. exactly what
        :func:`forgeflow.runtime.orchestrator._resolve_injected_skills` reconstructs
        from a stored record — so :func:`forgeflow.skills.runtime.load_procedure` /
        :func:`~forgeflow.skills.runtime.to_plan_candidates` consume it with no
        adapter. ``procedure`` is present only when the seed declares one — a
        seed without a procedure keeps the pre-INC46 shape and degrades honestly.
        """
        spec = self.version.spec if isinstance(self.version.spec, Mapping) else {}
        raw_steps = spec.get("steps")
        steps = (
            [str(s) for s in raw_steps if str(s or "").strip()]
            if isinstance(raw_steps, (list, tuple))
            else list(self.seed.steps)
        )
        entry: dict[str, Any] = {
            "id": str(self.skill.id),
            "name": str(self.skill.name),
            "version": str(self.version.semver),
            "description": str(self.skill.description),
            "steps": steps,
        }
        raw_procedure = spec.get(SEGMENT_PROCEDURE)
        if isinstance(raw_procedure, (list, tuple)) and raw_procedure:
            entry["procedure"] = [
                dict(item) for item in raw_procedure if isinstance(item, Mapping)
            ]
        return entry


# --------------------------------------------------------------------------- #
# loading                                                                       #
# --------------------------------------------------------------------------- #
def _read_json(path: Path) -> Any:
    """Read a JSON file, raising :class:`SeedError` (never ``json``'s exc)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SeedError(f"缺少文件 {path.name}（{path}）") from exc
    try:
        return json.loads(text)
    except (ValueError, TypeError) as exc:
        raise SeedError(f"{path.name} 不是合法 JSON：{exc}") from exc


def _validate_slug(path: Path) -> str:
    """Return the seed's slug, or raise when the directory name is illegal."""
    slug = path.name
    errors = validate_skill_name(slug)
    if errors:
        raise SeedError(
            f"种子目录名 '{slug}' 不是合法 slug（A3）：" + "；".join(errors)
        )
    return slug


def _parse_procedure(raw: Any, *, spec: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Validate the ``procedure.json`` body into structured steps (fail-closed).

    Every step must be a mapping with a non-empty ``purpose`` and a ``tool`` that
    is a member of :data:`forgeflow.runtime.gate.PLATFORM_PLAN_TOOLS` — a tool
    outside the platform catalogue is an **explicit** :class:`SeedError`, never a
    silent drop (an off-catalogue tool could never execute anyway; T03 would
    refuse it).
    """
    if not isinstance(raw, Mapping):
        raise SeedError("procedure.json 必须是对象 {'domain': str, 'procedure': [...]}")
    steps_raw = raw.get("procedure", [])
    if not isinstance(steps_raw, (list, tuple)):
        raise SeedError("procedure.json 的 'procedure' 必须是数组")
    steps: list[dict[str, Any]] = []
    for index, item in enumerate(steps_raw):
        if not isinstance(item, Mapping):
            raise SeedError(f"procedure[{index}] 必须是对象")
        tool = str(item.get("tool") or "").strip()
        purpose = str(item.get("purpose") or "").strip()
        if not purpose:
            raise SeedError(f"procedure[{index}] 缺少 purpose")
        if not tool:
            raise SeedError(f"procedure[{index}] 缺少 tool")
        if tool not in PLATFORM_PLAN_TOOLS:
            raise SeedError(
                f"procedure[{index}] 的工具 '{tool}' 不在平台白名单内"
                "（PLATFORM_PLAN_TOOLS）——不得静默跳过"
            )
        steps.append(dict(item))
    return tuple(steps)


def load_seed_dir(path: Path | str) -> Seed:
    """Load and validate one seed directory into a :class:`Seed`.

    The load is strict and loud (禁止静默跳过): an illegal directory slug, a
    ``manifest.name`` that disagrees with the directory, a contract that fails
    T07 validation, an off-catalogue procedure tool, or a T11 materialisation
    failure each raise :class:`SeedError` naming the concrete problem.

    Raises:
        SeedError: the seed is malformed for any of the reasons above.
    """
    path = Path(path)
    if not path.is_dir():
        raise SeedError(f"种子路径不是目录：{path}")

    slug = _validate_slug(path)

    spec = _read_json(path / SPEC_FILENAME)
    if not isinstance(spec, Mapping):
        raise SeedError(f"{slug}/{SPEC_FILENAME} 必须是对象（七段契约）")

    manifest = spec.get("manifest")
    if not isinstance(manifest, Mapping):
        raise SeedError(f"{slug} 的契约缺少 manifest 段")
    name = str(manifest.get("name") or "").strip()
    if name != slug:
        raise SeedError(
            f"{slug} 的 manifest.name '{name}' 必须与目录名 '{slug}' 一致（A3）"
        )

    report = validate_contract_document(spec, parent_dir=slug)
    if not report.ok or report.document is None:
        raise SeedError(
            f"种子 {slug} 的七段契约未通过 T07 校验：" + "；".join(report.errors)
        )
    document = report.document

    steps: tuple[str, ...] = ()
    if document.procedure is not None:
        steps = tuple(str(s) for s in document.procedure.steps)

    domain = str(manifest.get("metadata", {}).get("domain") or "").strip() if isinstance(
        manifest.get("metadata"), Mapping
    ) else ""

    procedure_path = path / PROCEDURE_FILENAME
    if procedure_path.exists():
        raw_proc = _read_json(procedure_path)
        procedure = _parse_procedure(raw_proc, spec=spec)
        if not domain and isinstance(raw_proc, Mapping):
            domain = str(raw_proc.get("domain") or "").strip()
    else:
        # No runtime procedure ⇒ an honest, non-executable seed (degradation is
        # T03's job: UNDECLARED_PROCEDURE_REASON). Not an error.
        procedure = ()

    display_name = str(manifest.get("display_name") or "").strip() or slug
    description = str(manifest.get("description") or "").strip()

    # T11 — materialise through the SAME path as any authored skill. A contract
    # that passes T07 but cannot render a spec-compliant SKILL.md is a defect.
    scratch_skill = SkillRecord(
        id=f"seed:{slug}",
        tenant_id=None,
        name=display_name,
        domain=domain,
        description=description,
        current_version=SEED_VERSION,
        status=SEED_STATUS,
        owner=SEED_OWNER,
        tags=[SEED_ORIGIN_TAG, *([domain] if domain else [])],
    )
    scratch_version = SkillVersionRecord(
        skill_id=scratch_skill.id,
        tenant_id=None,
        semver=SEED_VERSION,
        spec=dict(spec),
    )
    try:
        bundle = materialize_skill(scratch_skill, scratch_version)
    except Exception as exc:  # noqa: BLE001 — surface it as an explicit SeedError
        raise SeedError(f"种子 {slug} 无法物化（T11）：{exc}") from exc

    # A shipped materialised product (the seed's ``SKILL.md``) must be the exact
    # render of the reviewed contract — a silent drift means the directory's
    # "物化产物" no longer matches its "七段数据" (fail loud, 禁止静默跳过).
    shipped_md = path / SKILL_MD_FILENAME
    if shipped_md.exists():
        shipped_text = shipped_md.read_text(encoding="utf-8")
        if shipped_text != bundle.files["SKILL.md"]:
            raise SeedError(
                f"种子 {slug} 随附的 {SKILL_MD_FILENAME} 与契约物化结果不一致"
                "（T18 漂移）：请由契约重新物化，禁止手工漂移"
            )

    return Seed(
        slug=slug,
        path=path,
        spec=dict(spec),
        procedure=procedure,
        steps=steps,
        domain=domain,
        display_name=display_name,
        description=description,
        bundle=bundle,
    )


def discover_seed_dirs(seeds_dir: Path | str | None = None) -> list[Path]:
    """Return the seed sub-directories (sorted, deterministic; no hidden dirs)."""
    root = Path(seeds_dir) if seeds_dir is not None else SEEDS_DIR
    if not root.is_dir():
        return []
    return sorted(
        (child for child in root.iterdir() if child.is_dir() and not child.name.startswith((".", "_"))),
        key=lambda p: p.name,
    )


def load_seeds(seeds_dir: Path | str | None = None) -> list[Seed]:
    """Load **every** seed in ``seeds_dir`` (or the shipped catalogue).

    Strict: the first malformed seed raises :class:`SeedError` — the catalogue is
    never partially and silently shipped. Returns seeds sorted by slug.
    """
    return [load_seed_dir(path) for path in discover_seed_dirs(seeds_dir)]


# --------------------------------------------------------------------------- #
# cold-start provisioning                                                       #
# --------------------------------------------------------------------------- #
async def install_seeds(
    tenant_id: str | None,
    *,
    repo: Any | None = None,
    seeds_dir: Path | str | None = None,
    seeds: Sequence[Seed] | None = None,
) -> list[InstalledSeed]:
    """Provision the seed catalogue for ``tenant_id`` (the cold-start step).

    Idempotent: a seed already present in the tenant (matched by ``name``) is
    reused, never duplicated or overwritten. Tenant **fail-closed**: an
    unresolved tenant is refused (403) before any read/write, so seeds can never
    leak across tenants (红线 5).

    Args:
        tenant_id: the resolved tenant the seeds are provisioned for.
        repo: an optional ``SkillRepository`` (defaults to the configured one).
        seeds_dir / seeds: override the catalogue (tests / callers).

    Returns:
        The tenant-scoped :class:`InstalledSeed` list (sorted by slug).

    Raises:
        GovernanceError: ``tenant_id`` is empty / ``None`` (status_code 403).
        SeedError: a seed in the catalogue is malformed.
    """
    from forgeflow.skills.tenant_scope import require_tenant

    tenant = require_tenant(tenant_id)  # BE-5 — fail closed before any read

    if repo is None:
        from forgeflow.repositories import get_skill_repository

        repo = get_skill_repository()

    catalogue = list(seeds) if seeds is not None else load_seeds(seeds_dir)

    installed: list[InstalledSeed] = []
    for seed in catalogue:
        existing = await repo.get_skill_by_name(tenant, seed.display_name)
        if existing is not None:
            versions = await repo.list_versions(tenant, existing.id)
            current = next(
                (v for v in versions if str(v.semver) == str(existing.current_version)),
                versions[0] if versions else None,
            )
            if current is None:
                raise SeedError(
                    f"种子 '{seed.slug}' 在租户 {tenant} 已存在但缺少版本记录"
                )
            installed.append(InstalledSeed(seed=seed, skill=existing, version=current))
            continue

        skill = SkillRecord(
            tenant_id=tenant,
            name=seed.display_name,
            domain=seed.domain,
            description=seed.description,
            current_version=SEED_VERSION,
            status=SEED_STATUS,
            owner=SEED_OWNER,
            tags=[SEED_ORIGIN_TAG, *([seed.domain] if seed.domain else [])],
        )
        skill = await repo.create_skill(skill)
        version = SkillVersionRecord(
            tenant_id=tenant,
            skill_id=skill.id,
            semver=SEED_VERSION,
            # The persisted body is the RUNTIME spec (T03 shape) — this is what
            # makes a discovered seed actually orchestratable (not advisory
            # text). The reviewed seven-segment contract stays the source
            # artefact in the seed directory; its body hash rides along.
            spec=seed.runtime_spec(),
            changelog=(
                f"[{SEED_VERSION}] 种子技能（origin=seed，人写的七段契约；"
                f"contract_sha256={seed.spec_hash()[:12]}）"
            ),
            approved_by=SEED_OWNER,
        )
        await repo.add_version(tenant, version)
        skill.current_version = SEED_VERSION
        await repo.update_skill(skill)
        installed.append(InstalledSeed(seed=seed, skill=skill, version=version))

    installed.sort(key=lambda item: item.slug)
    return installed


# --------------------------------------------------------------------------- #
# 红线 6 — evolution must not overwrite a seed body in place                     #
# --------------------------------------------------------------------------- #
async def apply_evolution(
    installed: InstalledSeed,
    evolved_spec: Mapping[str, Any],
    *,
    repo: Any,
    bump: str = "patch",
    actor: str = "evolution",
    summary: str = "种子演化：产出新版本（种子本体不变）",
) -> SkillVersionRecord:
    """Record an evolved spec as a **new** version of ``installed`` (红线 6).

    Delegates to the existing :func:`forgeflow.skills.versioning.create_version`,
    which mints the next semver and lifts ``current_version`` — it never rewrites
    the incumbent version's ``spec``. The seed's original body therefore stays
    byte-identical (its :func:`spec_sha256` is unchanged), which is exactly what
    a test pins; a counterfactual flips this to an in-place rewrite to prove the
    pin is load-bearing.
    """
    from forgeflow.skills.versioning import create_version

    tenant = installed.skill.tenant_id
    version = await create_version(
        repo,
        tenant,
        installed.skill,
        dict(evolved_spec),
        bump=bump,
        actor=actor,
        summary=summary,
    )
    return version
