"""INC43 T05a — ``data-testid`` zero-regression pin + frontend honesty scan.

Two guards over the declared scan domain ``frontend/src/**/*.{ts,tsx}``:

  1. **``data-testid`` zero-regression** — every id in PRD §4.5 Appendix A (a
     frozen "只增不改不删" contract, 51 ids) must still appear as a *real*
     ``data-testid`` attribute. Red line: ``REMOVED == 0``. Adding ids is
     allowed (T01/T02 added many), so the pin never asserts a fixed total.

  2. **Honesty scan** — no ``?? 0`` / ``|| 0`` fabrication fallback beyond the
     documented, legitimate count / aggregation defaults
     (``_ZERO_FALLBACK_ALLOW``), and no fabricated metric percentage in a
     real-data view (``_METRIC_PERCENT_ALLOW`` pins the two static demo pages
     that legitimately carry illustrative numbers).

Both scans strip comments **first** (string-aware, so ``"http://…"`` survives).
A bare grep would match ``data-testid`` / ``?? 0`` inside ``//`` and
``{/* … */}`` comments and pass vacuously — this file's own subject,
``FollowUpComposer.tsx``, carries ``conv-followup`` in its doc comment *and* as
the real attribute, so comment-stripping is load-bearing, not decorative.

Each scanner carries a **positive control** proving it can go red; the
out-of-band red-proof runs are archived under ``qa_tmp/inc43_redproof_scan.*``.

Citation discipline: ``file::symbol`` anchors, never ``file:line``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
SCAN_SUFFIXES = (".ts", ".tsx")
SCAN_DOMAIN = "frontend/src/**/*.{ts,tsx}"

# --------------------------------------------------------------------------- #
# 1. data-testid contract (PRD §4.5 Appendix A — "只增不改不删", 51 ids)       #
# --------------------------------------------------------------------------- #
PRESERVED_TESTIDS = frozenset(
    {
        "home-second-screen",
        "hero-attach-files",
        "hero-attach-skills",
        "hero-attach-more",
        "hero-attach-panel",
        "conv-inline-history-row",
        "history-delete-btn",
        "conv-inline-panel",
        "conv-inline-header",
        "conv-inline-stop",
        "conv-inline-close",
        "conv-inline-user",
        "conv-inline-aborted",
        "conv-inline-open-full",
        "conv-inline-error",
        "conv-inline-followup",
        "conv-followup",
        "conv-user-turn",
        "conv-run-summary",
        "conv-inline-answer",
        "conv-artifacts",
        "conv-artifact-chip",
        "conv-artifact-open",
        "conv-artifact-download",
        "conv-exec-detail",
        "conv-exec-trace",
        "workspace-live-step",
        "artifact-panel",
        "artifact-empty",
        "artifact-card",
        "artifact-preview",
        "artifact-preview-unsupported",
        "artifact-download",
        "resource-add",
        "resource-dropzone",
        "resource-list",
        "resource-card",
        "resource-delete",
        "resource-preview",
        "resource-limits-note",
        "resource-upload-row",
        "resource-upload-status",
        "resource-kind-filter",
        "resource-empty-note",
        "resource-preview-truncated",
        "skill-create",
        "skill-create-submit",
        "skill-version-editor",
        "skill-version-save",
        "model-status",
        "theme-toggle",
    }
)

# Design §8 red line: this id must stay a literal attribute (not parameterised).
CONV_FOLLOWUP_ATTRIBUTE = 'data-testid="conv-followup"'
CONV_FOLLOWUP_FILE = "views/runs/FollowUpComposer.tsx"

# Real ``data-testid`` value forms: ="...", ='...', ={"..."}, ={'...'}, ={`...`}.
_TESTID_ATTR = re.compile(
    r"""data-testid\s*=\s*(?:\{)?\s*(?:"([^"]*)"|'([^']*)'|`([^`]*)`)""",
    re.S,
)

# --------------------------------------------------------------------------- #
# 2. Honesty-scan allowlists (every entry is a documented legitimate case)     #
# --------------------------------------------------------------------------- #
# ``?? 0`` / ``|| 0`` — legitimate count / numeric-accumulation defaults. None
# fabricates a *positive* value: metric slots are paired with a sibling
# ``hasData`` flag the consumers gate on (``HomeView::kpiCards`` renders 「—」 /
# 「暂无终态任务」, never 0%), and the rest are sums / "absent ⇒ 0" counters.
_ZERO_FALLBACK_ALLOW = {
    "api/client.ts::?? r.total_cost_usd": "numeric SUM accumulation of cost rows",
    "api/client.ts::?? r.run_count": "numeric SUM accumulation of run counts",
    "api/client.ts::?? r.total_tokens": "numeric SUM accumulation of tokens",
    "api/hooks.ts::?? runs.data?.total": "run total = 0 while the query loads",
    "api/hooks.ts::?? m?.success_rate": "metric value slot; gated by successRate.hasData at the consumer",
    "api/hooks.ts::?? m?.terminal_runs": "sample-size default for an unmeasured metric",
    "api/hooks.ts::?? m?.avg_latency_ms": "metric value slot; gated by avgLatencyMs.hasData at the consumer",
    "api/hooks.ts::?? m?.total_runs": "sample-size default for an unmeasured metric",
    "components/Topbar.tsx::?? approvals.data?.total": "approvals badge count = 0 while loading",
    "views/EvaluationsView.tsx::?? e?.hallucination_rate": "colour-only comparison; the text is gated by `e ? … : '—'`",
    "views/KnowledgeView.tsx::?? q.data?.total": "count = 0, gated by `q.isLoading ? '加载中…' : …`",
    "views/OverviewView.tsx::?? r.total_cost_usd": "per-row numeric cell of an existing row",
    "views/OverviewView.tsx::?? r.total_tokens": "per-row numeric cell of an existing row",
    "views/SkillsView.tsx::?? experiencesQ.data?.total": "experience count = 0 while loading",
    "views/runs/SkillCapture.tsx::?? compile.data?.experience_ids.length": "collected-experience count when nothing is compiled yet",
}

# Metric-shaped hardcoded percentages. Only the two *static demo* pages carry
# illustrative numbers (no API — e.g. ``ClustersView.tsx`` has no data fetch);
# NO real-data view fabricates a metric. A new hit here forces a review.
_METRIC_PERCENT_ALLOW = {
    "views/ArchitecturePage.tsx::cpu": "illustrative infra diagram (static sample nodes, no API)",
    "views/ArchitecturePage.tsx::mem": "illustrative infra diagram (static sample nodes, no API)",
    "views/ClustersView.tsx::cpu": "static demo cluster table (ClustersView.tsx has no data fetch)",
    "views/ClustersView.tsx::mem": "static demo cluster table (no data fetch)",
    "views/ClustersView.tsx::rps": "static demo cluster table (no data fetch)",
    "views/ClustersView.tsx::健康": "static demo cluster table (no data fetch)",
}

_ZERO_RE = re.compile(r"(?P<left>[\w$.?\[\]]+)\s*(?P<op>\?\?|\|\|)\s*0\b")
_PERCENT_RE = re.compile(r"""['"`]\d{1,3}(?:\.\d+)?%['"`]""")
_METRIC_RE = re.compile(
    r"(成功率|健康|可用|cpu|mem|内存|rps|负载|util|使用率|命中率|准确率)"
)
_LAYOUT_RE = re.compile(
    r"(viewBox|offset=|cx=|cy=|stopOpacity|borderRadius|skel|stroke=|fill=)"
)


# --------------------------------------------------------------------------- #
# Scanners                                                                    #
# --------------------------------------------------------------------------- #
def _strip_comments(src: str) -> str:
    """Remove JS/TS ``//`` and ``/* */`` comments, preserving string literals.

    A plain regex strip would corrupt ``"https://…"`` (the ``//`` inside it) and
    could unbalance quotes; a bare grep would count commented-out ids. This
    walks the source so quoted/template runs are copied verbatim and only real
    comments are dropped.
    """
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        two = src[i : i + 2]
        if two == "//":
            j = src.find("\n", i)
            i = n if j == -1 else j
            continue
        if two == "/*":
            j = src.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        ch = src[i]
        if ch in ("'", '"', "`"):
            out.append(ch)
            i += 1
            while i < n:
                if src[i] == "\\":
                    out.append(src[i])
                    i += 1
                    if i < n:
                        out.append(src[i])
                        i += 1
                    continue
                out.append(src[i])
                if src[i] == ch:
                    i += 1
                    break
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _iter_sources(root: Path):
    for path in sorted(root.rglob("*")):
        if path.suffix in SCAN_SUFFIXES:
            yield path.relative_to(root).as_posix(), _strip_comments(
                path.read_text(encoding="utf-8")
            )


def _scan_testids(root: Path) -> set[str]:
    """Static ``data-testid`` ids in ``root`` (comment-stripped)."""
    found: set[str] = set()
    for _rel, code in _iter_sources(root):
        for m in _TESTID_ATTR.finditer(code):
            value = next((g for g in m.groups() if g is not None), "")
            if "${" not in value:  # dynamic templates carry no pinnable literal
                found.add(value)
    return found


def _scan_zero_fallbacks(root: Path) -> set[str]:
    """``?? 0`` / ``|| 0`` hits as ``rel::<op> <left-expr>`` (comment-stripped)."""
    hits: set[str] = set()
    for rel, code in _iter_sources(root):
        for line in code.splitlines():
            for m in _ZERO_RE.finditer(line):
                hits.add(f"{rel}::{m.group('op')} {m.group('left')}")
    return hits


def _scan_metric_percents(root: Path) -> set[str]:
    """Metric-shaped hardcoded percent literals as ``rel::<metric>``.

    Only lines that both look like *data* (carry a metric word) and are not
    pure SVG/layout (``viewBox`` / gradient offsets / skeleton widths) are
    reported, so decorative ``width="100%"`` noise is excluded by construction.
    """
    hits: set[str] = set()
    for rel, code in _iter_sources(root):
        for line in code.splitlines():
            if not _PERCENT_RE.search(line):
                continue
            if _LAYOUT_RE.search(line):
                continue
            for m in _METRIC_RE.finditer(line):
                hits.add(f"{rel}::{m.group(0)}")
    return hits


# --------------------------------------------------------------------------- #
# Deliverable 1 — data-testid zero-regression                                 #
# --------------------------------------------------------------------------- #
def test_appendix_a_testids_survive_data_testid_zero_regression():
    """REMOVED == 0: every Appendix A id is still a real attribute.

    Scan domain: ``frontend/src/**/*.{ts,tsx}``. Adding ids is allowed, so this
    asserts nothing about the total; only the frozen contract must survive.
    """
    found = _scan_testids(FRONTEND_SRC)
    removed = PRESERVED_TESTIDS - found
    assert removed == set(), (
        f"data-testid contract broken (REMOVED={len(removed)}): {sorted(removed)}; "
        f"scan={SCAN_DOMAIN} total={len(found)} added={len(found - PRESERVED_TESTIDS)}"
    )


def test_conv_followup_is_still_an_attribute_literal():
    """Design §8 red line — ``conv-followup`` stays a literal attribute.

    It also appears in the file's doc comment, so this reads the *comment-
    stripped* source: the pin is the shipped attribute, not prose.
    """
    raw = (FRONTEND_SRC / CONV_FOLLOWUP_FILE).read_text(encoding="utf-8")
    code = _strip_comments(raw)
    assert CONV_FOLLOWUP_ATTRIBUTE in code, (
        f"{CONV_FOLLOWUP_FILE}::FollowUpComposer must keep "
        f"{CONV_FOLLOWUP_ATTRIBUTE!r} as a literal attribute"
    )


def test_positive_control_scanner_flags_a_removed_testid(tmp_path):
    """Control: the pin *can* go red — rename ``conv-followup`` ⟹ REMOVED.

    Done on a throwaway copy (never the real tree): (a) a comment-only source
    yields nothing, proving a bare grep would have been fooled; (b) after the
    attribute is renamed, the scanner reports ``conv-followup`` missing.
    """
    comment_only = tmp_path / "comments"
    comment_only.mkdir()
    (comment_only / "x.tsx").write_text(
        '// data-testid="conv-followup"\n/* data-testid="conv-followup" */\n',
        encoding="utf-8",
    )
    assert _scan_testids(comment_only) == set(), "comments must never count"

    raw = (FRONTEND_SRC / CONV_FOLLOWUP_FILE).read_text(encoding="utf-8")
    assert _scan_testids(FRONTEND_SRC) >= {"conv-followup", "conv-inline-followup"}
    tampered = raw.replace(
        CONV_FOLLOWUP_ATTRIBUTE, 'data-testid="conv-followup-renamed"'
    )
    assert tampered != raw, "fixture drift: the attribute form moved"
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / "FollowUpComposer.tsx").write_text(tampered, encoding="utf-8")
    assert "conv-followup" not in _scan_testids(copy), (
        "scanner failed to report a removed testid (control would not go red)"
    )


# --------------------------------------------------------------------------- #
# Deliverable 2 — honesty scan (?? 0 / || 0, fabricated metric percents)       #
# --------------------------------------------------------------------------- #
def test_honesty_no_undocumented_zero_fallback():
    """Every ``?? 0`` / ``|| 0`` is a documented, legitimate default."""
    hits = _scan_zero_fallbacks(FRONTEND_SRC)
    undocumented = hits - set(_ZERO_FALLBACK_ALLOW)
    assert undocumented == set(), (
        f"new/fabrication-risk ?? 0 / || 0 fallback(s): {sorted(undocumented)} — "
        "classify each (legitimate default vs dishonesty) before allowlisting"
    )


def test_honesty_positive_control_detects_a_new_zero_fallback(tmp_path):
    """Control: the fallback scanner *can* go red on an unvetted ``?? 0``."""
    (tmp_path / "probe.ts").write_text(
        "export const fake = metrics.successRate ?? 0\n", encoding="utf-8"
    )
    hits = _scan_zero_fallbacks(tmp_path)
    assert hits == {"probe.ts::?? metrics.successRate"}, hits
    assert hits - set(_ZERO_FALLBACK_ALLOW), "the injected fallback must be flagged"


def test_honesty_no_fabricated_metric_percent_outside_demo_pages():
    """No real-data view fabricates a metric percentage."""
    hits = _scan_metric_percents(FRONTEND_SRC)
    undocumented = hits - set(_METRIC_PERCENT_ALLOW)
    assert undocumented == set(), (
        f"hardcoded metric percent outside the demo-page allowlist: "
        f"{sorted(undocumented)} — a real-data view must never fabricate a metric"
    )


def test_honesty_positive_control_detects_a_fabricated_metric_percent(tmp_path):
    """Control: the percent scanner *can* go red on a fabricated 成功率."""
    (tmp_path / "probe.tsx").write_text(
        "export const card = { 成功率: '99%' }\n", encoding="utf-8"
    )
    hits = _scan_metric_percents(tmp_path)
    assert hits == {"probe.tsx::成功率"}, hits
    assert hits - set(_METRIC_PERCENT_ALLOW), "the injected fake metric must be flagged"
