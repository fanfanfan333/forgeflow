"""Run configuration for the QA Code Agent (Phase A–C).

Layer L0. It captures everything a single QA run needs to be *deterministic and
reproducible*: which project, where artifacts go, which profile (fast/full), the
per-subprocess timeout, and — critically — the environment the child processes
receive.

Two design decisions live here:

* **The environment is explicit, never inherited by accident** (§0.6/§76).
  ``subprocess_env`` always pins ``STORAGE_BACKEND`` and ``LLM_PROVIDER`` so a QA
  run can never silently depend on a developer's shell exports. The
  ``ProcessRunner`` records only the *diff* against the parent environment.
* **The deterministic profile forbids network/LLM.** ``allow_network=False`` and
  ``llm_provider="mock"`` are the defaults; the closed loop's A–C path must run
  offline and repeatably (hard constraint 7).

The Make targets for a profile come from :func:`profile_targets`. An
``QA_MAKE_TARGETS`` override exists so the end-to-end test can drive the full
pipeline against a cheap, real target (``lint``) instead of ``check`` — which
would otherwise re-enter the very test that started it (``check`` runs the whole
suite, including the QA end-to-end test).
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

#: §80 — every subprocess gets a real timeout; nothing may block forever.
DEFAULT_TIMEOUT_S: int = 600

#: §57 — Fast profile = the static + unit + format + typecheck gate (``check``).
FAST_PROFILE_TARGETS: tuple[str, ...] = ("check",)

#: §9 item 8 — A–C Full = ``check`` plus the integration suite.
FULL_PROFILE_TARGETS: tuple[str, ...] = ("check", "test-integration")


class Profile(StrEnum):
    """The two A–C run profiles (§57), reduced to what this increment supports."""

    FAST = "fast"
    FULL = "full"


class QaRunConfig(BaseModel):
    """Immutable-ish configuration for one QA run."""

    project_root: Path
    artifacts_dir: Path
    run_id: str
    profile: Profile = Profile.FAST
    timeout_s: int = Field(DEFAULT_TIMEOUT_S, ge=1)
    storage_backend: str = "memory"
    llm_provider: str = "mock"
    allow_network: bool = False
    extra_env: dict[str, str] = Field(default_factory=dict)

    def subprocess_env(self) -> dict[str, str]:
        """The full environment handed to child processes.

        ``os.environ`` ∪ the pinned ``STORAGE_BACKEND``/``LLM_PROVIDER`` ∪ any
        ``extra_env``. The caller derives ``env_diff`` by subtracting
        ``os.environ`` so the evidence records *exactly* what this run changed.
        """
        env = dict(os.environ)
        env["STORAGE_BACKEND"] = self.storage_backend
        env["LLM_PROVIDER"] = self.llm_provider
        env.update(self.extra_env)
        return env

    def env_overrides(self) -> dict[str, str]:
        """Only the keys this config forces on child processes (the diff source)."""
        overrides = {
            "STORAGE_BACKEND": self.storage_backend,
            "LLM_PROVIDER": self.llm_provider,
        }
        overrides.update(self.extra_env)
        return overrides


def profile_targets(profile: Profile) -> tuple[str, ...]:
    """Make targets for ``profile``, honouring the ``QA_MAKE_TARGETS`` override.

    The override is a comma-separated list of *real* Make targets. It exists for
    the end-to-end test, which must exercise the pipeline without the recursion
    that the production ``check`` target would cause (``check`` re-runs the QA
    end-to-end test). When unset the profile defaults above are used verbatim.
    """
    override = os.environ.get("QA_MAKE_TARGETS", "").strip()
    if override:
        return tuple(target.strip() for target in override.split(",") if target.strip())
    if profile is Profile.FULL:
        return FULL_PROFILE_TARGETS
    return FAST_PROFILE_TARGETS


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "FAST_PROFILE_TARGETS",
    "FULL_PROFILE_TARGETS",
    "Profile",
    "QaRunConfig",
    "profile_targets",
]
