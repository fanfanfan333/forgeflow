"""Bounded, data-only Agent Skills package import; scripts never execute."""

from __future__ import annotations

import io
import stat
import zipfile

from forgeflow.skills.export_bundle import _validate_bundle_paths
from forgeflow.skills.skill_md import SkillBundle, compute_content_hash
from forgeflow.skills.spec_validator import validate_skill_md

MAX_PACKAGE_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024
MAX_FILES = 64


def package_spec(files: dict[str, str]) -> dict:
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError("Skill package requires 1-64 text files")
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in files.items()):
        raise ValueError("Skill package resources must be UTF-8 text")
    sizes = [len(v.encode("utf-8")) for v in files.values()]
    if max(sizes) > MAX_FILE_BYTES or sum(sizes) > MAX_PACKAGE_BYTES:
        raise ValueError("Skill package exceeds resource size limit")
    report = validate_skill_md(files.get("SKILL.md", ""))
    if not report.ok or not report.body.strip():
        raise ValueError("Invalid SKILL.md: " + "; ".join(report.errors or ["empty instructions"]))
    slug = report.frontmatter["name"]
    _validate_bundle_paths(SkillBundle(slug, "", "", files, "", {}, {}))
    if any(k != "SKILL.md" and not k.startswith(("references/", "scripts/", "assets/")) for k in files):
        raise ValueError("Resources must be under references/, scripts/ or assets/")
    spec = {
        "kind": "skill_package", "instructions": report.body, "prompt": report.body,
        "package_files": dict(files), "package_hash": compute_content_hash(files),
        "manifest": {"name": slug, "description": report.frontmatter["description"]},
        "import_provenance": {"trusted": False, "scripts_executed": False},
    }
    # An omitted restriction inherits the task scope; an explicit empty list denies all.
    if "allowed-tools" in report.frontmatter:
        spec["tools"] = report.frontmatter["allowed-tools"].split()
    return spec


def read_package(data: bytes, *, zip_format: bool) -> dict:
    if len(data) > MAX_PACKAGE_BYTES:
        raise ValueError("Skill package upload exceeds 2 MB")
    if not zip_format:
        return package_spec({"SKILL.md": data.decode("utf-8-sig")})
    files = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES or sum(i.file_size for i in entries) > MAX_PACKAGE_BYTES:
            raise ValueError("Skill package archive exceeds resource limit")
        for entry in entries:
            if entry.orig_filename != entry.filename:
                raise ValueError("Skill package resource path was normalized")
            if entry.is_dir():
                continue
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode) or entry.flag_bits & 1 or entry.file_size > MAX_FILE_BYTES:
                raise ValueError("Skill package contains unsupported or oversized resource")
            if entry.filename in files:
                raise ValueError("Duplicate skill package resource")
            files[entry.filename] = archive.read(entry).decode("utf-8-sig")
    return package_spec(files)
