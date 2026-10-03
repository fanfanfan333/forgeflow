"""INC46 T06 — front-end ``data-testid`` zero-regression + insights-surface pins.

T06 adds read-only surfaces to the existing three-column Skill Center
(``SkillRulesPage`` / ``SkillExperienceView`` / ``SkillForge`` / the inspector's
「就绪 · 发布联锁」block) and appends ``data-testid`` hooks. 红线 2 is
「只增不改不删」 — the pre-existing ids must survive, and the new ids must be real
attributes (not parameterised into oblivion).

This file reuses the comment-aware scanner of
``tests/integration/test_inc43_no_testid_regression.py`` (same
``frontend/src/**/*.{ts,tsx}`` scan domain — one scanner, no second
implementation) and adds:

  1. a **REMOVED == 0** report over (Appendix A ∪ T06 ids);
  2. a **positive control** proving the scanner can go red on a removed T06 id;
  3. the 红线 4 front-end honesty pin — the readiness surface renders 「—」 for an
     unmeasured (``null``) rate and never fabricates a ``0%``;
  4. light structural pins that the three-column increment is actually wired.

Citation discipline: ``file::symbol`` anchors, never ``file:line``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

# Reuse the INC43 comment-aware scanner verbatim rather than fork a second copy.
# The sibling test module is loaded *by file path* (the test tree has no package
# ``__init__``, so a plain ``import`` is not on ``sys.path`` under rootdir).
_INC43_PATH = Path(__file__).resolve().parent / "test_inc43_no_testid_regression.py"
_spec = importlib.util.spec_from_file_location("_inc43_testid_scanner", _INC43_PATH)
assert _spec is not None and _spec.loader is not None
inc43 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inc43)

FRONTEND_SRC = inc43.FRONTEND_SRC
CONV_STRIP = inc43._strip_comments  # noqa: SLF001 — intentional reuse of the scanner

# --------------------------------------------------------------------------- #
# The T06-introduced testids (frozen contract — additive, never removed)       #
# --------------------------------------------------------------------------- #
T06_TESTIDS = frozenset(
    {
        # SkillsView — 技能洞察 区
        "skill-insights",
        "skill-insights-empty",
        # SkillRulesPage — 规则 页
        "skill-rules",
        "skill-rules-empty",
        "skill-rules-item",
        "skill-rules-enforcement",
        # SkillExperienceView — 来源经验 视图
        "skill-experience",
        "skill-experience-empty",
        "skill-experience-item",
        # SkillForge — 锻造 入口
        "skill-forge",
        "skill-forge-mode",
        "skill-forge-exp",
        "skill-forge-run",
        "skill-forge-result",
        "skill-forge-read",
        # SkillInspector — 右栏 就绪 / 发布联锁 块
        "skill-inspector-readiness",
        "skill-readiness-facts",
        "skill-inspector-interlock",
    }
)

#: The full frozen contract this pin protects: the INC43 Appendix-A ids **and**
#: the T06 additions. Removing any id from the shipped tree is red.
FROZEN_CONTRACT = frozenset(inc43.PRESERVED_TESTIDS) | T06_TESTIDS


# --------------------------------------------------------------------------- #
# 1. REMOVED == 0                                                              #
# --------------------------------------------------------------------------- #
def test_t06_and_appendix_a_testids_zero_removed():
    """Every frozen id is still a real ``data-testid`` attribute (REMOVED == 0)."""
    found = inc43._scan_testids(FRONTEND_SRC)  # noqa: SLF001
    removed = FROZEN_CONTRACT - found
    added = found - FROZEN_CONTRACT
    assert removed == set(), (
        f"data-testid contract broken (REMOVED={len(removed)}): {sorted(removed)}; "
        f"scan={inc43.SCAN_DOMAIN} total={len(found)} added={len(added)}"
    )
    # The T06 surface ids specifically must be present.
    assert T06_TESTIDS <= found, sorted(T06_TESTIDS - found)


# --------------------------------------------------------------------------- #
# 2. Positive control — the pin *can* go red                                   #
# --------------------------------------------------------------------------- #
def test_positive_control_scanner_flags_a_removed_t06_testid(tmp_path):
    """Renaming a delivered T06 id ⟹ the scanner reports it missing (control)."""
    assert inc43._scan_testids(FRONTEND_SRC) >= {"skill-inspector-readiness"}  # noqa: SLF001
    raw = (FRONTEND_SRC / "views" / "skills" / "SkillInspector.tsx").read_text(
        encoding="utf-8"
    )
    tampered = raw.replace(
        'data-testid="skill-inspector-readiness"',
        'data-testid="skill-inspector-readiness-renamed"',
    )
    assert tampered != raw, "fixture drift: the readiness testid form moved"
    copy = tmp_path / "probe"
    copy.mkdir()
    (copy / "SkillInspector.tsx").write_text(tampered, encoding="utf-8")
    assert "skill-inspector-readiness" not in inc43._scan_testids(copy), (  # noqa: SLF001
        "scanner failed to report a removed T06 testid (control would not go red)"
    )


# --------------------------------------------------------------------------- #
# 3. 红线 4 — the readiness surface renders 「—」, never a fabricated 0%         #
# --------------------------------------------------------------------------- #
def test_readiness_renders_dash_for_null_rate_not_zero_percent():
    """``rate == null`` ⇒ 「—」; the rate is never coerced with ``?? 0``/``|| 0``."""
    raw = (FRONTEND_SRC / "views" / "skills" / "SkillInspector.tsx").read_text(
        encoding="utf-8"
    )
    code = CONV_STRIP(raw)
    # The honest guard is present: a null (unmeasured) rate renders an em-dash.
    assert "rate == null ? '—'" in code, "就绪比率必须在 rate==null 时渲染「—」"
    # …and the rate is never silently turned into a numeric 0.
    assert "rate ?? 0" not in code and "rate || 0" not in code, (
        "未测量比率不得用 ?? 0 / || 0 兜底成 0%（红线 4）"
    )
    # The whole per-rate format is a template, never a hardcoded literal percent.
    assert "${(rate * 100).toFixed(1)}%" in code


def test_readiness_dash_pin_has_a_positive_control(tmp_path):
    """Control: the same containment check goes red on a ``rate ?? 0`` tamper."""
    probe = tmp_path / "probe.tsx"
    probe.write_text("比率 {rate ?? 0}\n", encoding="utf-8")
    code = CONV_STRIP(probe.read_text(encoding="utf-8"))
    assert "rate ?? 0" in code, "the injected fabrication must be detectable"


# --------------------------------------------------------------------------- #
# 4. Structural wiring — the three-column increment is really mounted          #
# --------------------------------------------------------------------------- #
def _read(rel: str) -> str:
    return CONV_STRIP((FRONTEND_SRC / rel).read_text(encoding="utf-8"))


def test_insights_surfaces_are_wired_into_the_columns():
    skills_view = _read("views/SkillsView.tsx")
    assert '<SkillRulesPage' in skills_view and '<SkillExperienceView' in skills_view
    assert 'data-testid="skill-insights"' in skills_view

    engineering = _read("views/skills/SkillEngineering.tsx")
    assert "<SkillForge" in engineering

    inspector = _read("views/skills/SkillInspector.tsx")
    assert "useSkillReadiness" in inspector and "usePublishInterlock" in inspector
