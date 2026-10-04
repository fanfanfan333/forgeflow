"""INC46 T19 — 种子 Skill 与冷启动（seed skills + cold start）.

A *seed* is a **human-authored** skill shipped with ForgeFlow so a brand-new
tenant (empty experience library, zero learned skills) can still hit a usable
skill on its very first retrieval. Seeds are **not** a second, simplified skill
format: every seed is a full seven-segment contract (T07) that materialises
through the same T11 path as any authored skill.

See :mod:`forgeflow.skills.seeds.loader` for the loader API.
"""

from __future__ import annotations

from forgeflow.skills.seeds.loader import (
    ORIGIN_AUTHORED,
    ORIGIN_SEED,
    PROCEDURE_FILENAME,
    SEEDS_DIR,
    SEED_ORIGIN_TAG,
    SEED_OWNER,
    SEED_STATUS,
    SEED_VERSION,
    SKILL_MD_FILENAME,
    SPEC_FILENAME,
    InstalledSeed,
    Seed,
    SeedError,
    apply_evolution,
    discover_seed_dirs,
    install_seeds,
    is_seed,
    load_seed_dir,
    load_seeds,
    origin_of,
    spec_sha256,
)

__all__ = [
    "ORIGIN_AUTHORED",
    "ORIGIN_SEED",
    "PROCEDURE_FILENAME",
    "SEEDS_DIR",
    "SEED_ORIGIN_TAG",
    "SEED_OWNER",
    "SEED_STATUS",
    "SEED_VERSION",
    "SKILL_MD_FILENAME",
    "SPEC_FILENAME",
    "InstalledSeed",
    "Seed",
    "SeedError",
    "apply_evolution",
    "discover_seed_dirs",
    "install_seeds",
    "is_seed",
    "load_seed_dir",
    "load_seeds",
    "origin_of",
    "spec_sha256",
]
