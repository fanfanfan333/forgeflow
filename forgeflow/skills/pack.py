"""Multi-skill packs: the packaging layer above single-skill bundles.

Layers (Phase 9 B8), each validated, none executing anything:

* **package** (``package_import``): one skill's zip/text bytes.
* **bundle** (``export_bundle`` / ``skill_md.SkillBundle``): one skill's
  materialised file tree + validation record.
* **pack** (this module): a distributable unit of 1-16 skills plus a
  ``pack.json`` manifest (name, version, skill directories). Loading a
  pack validates every member skill with the same ``package_spec``
  gate as a single-skill package, so a pack cannot smuggle in a skill
  that would fail on its own.

Loading returns validated specs; *installing* them into a tenant
registry stays the existing publish flow (``SkillRepository``) — this
module deliberately does not write to any store.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from forgeflow.skills.package_import import MAX_FILES, MAX_PACKAGE_BYTES, package_spec

MAX_PACK_SKILLS = 16


def _pack_hash(files: dict[str, str]) -> str:
    blob = "\n".join(f"{path}\0{content}" for path, content in sorted(files.items()))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_skill_pack(files: dict[str, str]) -> dict[str, Any]:
    """Validate a flat ``{path: text}`` pack tree; return the pack spec.

    Layout: ``pack.json`` at the root plus ``skills/<dir>/SKILL.md``
    (and optional ``references/`` / ``scripts/`` / ``assets/``) per
    member. Raises ``ValueError`` on any structural problem — a pack
    is all-or-nothing, never partially loaded.
    """
    if not isinstance(files, dict) or not files:
        raise ValueError("Skill pack requires files")
    if len(files) > MAX_FILES:
        raise ValueError("Skill pack exceeds file limit")
    if sum(len(v.encode("utf-8")) for v in files.values()) > MAX_PACKAGE_BYTES:
        raise ValueError("Skill pack exceeds size limit")
    raw_manifest = files.get("pack.json")
    if raw_manifest is None:
        raise ValueError("Skill pack requires pack.json")
    try:
        manifest = json.loads(raw_manifest)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Skill pack manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("Skill pack manifest must be an object")
    name = manifest.get("name")
    version = manifest.get("version")
    skill_dirs = manifest.get("skills")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Skill pack manifest requires a non-empty name")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("Skill pack manifest requires a non-empty version")
    if not isinstance(skill_dirs, list) or not 1 <= len(skill_dirs) <= MAX_PACK_SKILLS:
        raise ValueError(f"Skill pack requires 1-{MAX_PACK_SKILLS} skills")
    if any(not isinstance(d, str) or not d.strip() for d in skill_dirs):
        raise ValueError("Skill pack skill entries must be non-empty directory names")
    if len(set(skill_dirs)) != len(skill_dirs):
        raise ValueError("Skill pack skill entries must be unique")
    allowed_prefixes = {"pack.json", *(f"skills/{d}/" for d in skill_dirs)}
    for path in files:
        if path != "pack.json" and not any(path.startswith(p) for p in allowed_prefixes):
            raise ValueError(f"Skill pack contains unexpected file: {path}")
    specs: list[dict[str, Any]] = []
    slugs: set[str] = set()
    for skill_dir in skill_dirs:
        prefix = f"skills/{skill_dir}/"
        subset = {
            path[len(prefix) :]: content
            for path, content in files.items()
            if path.startswith(prefix)
        }
        if "SKILL.md" not in subset:
            raise ValueError(f"Skill pack member {skill_dir} has no SKILL.md")
        spec = package_spec(subset)
        slug = str(spec["manifest"]["name"])
        if slug in slugs:
            raise ValueError(f"Skill pack contains duplicate skill slug: {slug}")
        slugs.add(slug)
        specs.append(spec)
    return {
        "kind": "skill_pack",
        "name": name,
        "version": version,
        "description": manifest.get("description", ""),
        "skills": specs,
        "pack_hash": _pack_hash(files),
    }
