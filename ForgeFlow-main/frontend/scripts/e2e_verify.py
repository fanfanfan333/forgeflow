#!/usr/bin/env python
"""ForgeFlow — repeatable browser E2E verification (Playwright, system Chrome).

This is the *frozen* replacement for the ad-hoc Playwright snippets that used to
be rewritten for every verification round. It is a black-box check of the running
SPA and asserts, with NUMBERS (never eyeballing):

  1. Preflight  — backend (:8010 /health) and frontend (:5173) are reachable.
  2. Auth       — POST {backend}/auth/login with the seeded demo user; the
                  returned JWT is injected into sessionStorage so the SPA's live
                  panels load (otherwise every panel 401s and the console fills
                  with errors that have nothing to do with the code under test).
  3. KPI text   — the four home KPI cards show 总任务数 / 成功率 / 节省成本 /
                  平均响应时间.
  4. Hero rects — getBoundingClientRect() of the hero title block and the four
                  decorative cubes; the script proves the rectangles do NOT
                  intersect (numeric, no eyeballing). At viewports where the CSS
                  hides the art (<=1180px) the check is recorded as VACUOUS, not
                  as a silent pass.
  5. Console    — zero console `error` messages and zero page errors.
  6. Four widths: 1440 / 1280 / 1024 / 768.

Output: human summary on stdout + a machine-readable JSON file.
Exit code: 0 when every assertion passes, non-zero otherwise.

Usage:
    python frontend/scripts/e2e_verify.py \
        --backend http://127.0.0.1:8010 \
        --frontend http://127.0.0.1:5173 \
        --user admin --password forgeflow-dev \
        --out frontend/scripts/e2e_verify_result.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VIEWPORTS = [1440, 1280, 1024, 768]
KPI_LABELS = ["总任务数", "成功率", "节省成本", "平均响应时间"]
# The decorative cubes are hidden by CSS at <=1180px (home.css). Anything above
# this width MUST show them beside the copy.
ART_VISIBLE_MIN_WIDTH = 1180


# --------------------------------------------------------------------------- #
# HTTP helpers                                                                 #
# --------------------------------------------------------------------------- #
def _http_json(url: str, payload: dict | None = None, timeout: int = 10) -> tuple[int, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"content-type": "application/json"}, method="POST" if data else "GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body)
            except json.JSONDecodeError:
                return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def wait_reachable(url: str, timeout_s: int = 60) -> tuple[bool, str]:
    deadline = time.time() + timeout_s
    last = "no attempt"
    while time.time() < deadline:
        status, body = _http_json(url, timeout=5)
        if status == 200:
            return True, f"{status}"
        last = f"status={status} body={str(body)[:80]}"
        time.sleep(2)
    return False, last


# --------------------------------------------------------------------------- #
# Rect math                                                                    #
# --------------------------------------------------------------------------- #
def _rects_intersect(a: dict, b: dict, eps: float = 0.5) -> bool:
    """True when two {x,y,width,height} rectangles overlap by more than eps px."""
    if not a or not b:
        return False
    ax2, ay2 = a["x"] + a["width"], a["y"] + a["height"]
    bx2, by2 = b["x"] + b["width"], b["y"] + b["height"]
    return not (ax2 - eps <= b["x"] or bx2 - eps <= a["x"] or ay2 - eps <= b["y"] or by2 - eps <= a["y"])


# --------------------------------------------------------------------------- #
# In-page measurement                                                          #
# --------------------------------------------------------------------------- #
_MEASURE_JS = """
() => {
  const r = (el) => {
    if (!el) return null;
    const b = el.getBoundingClientRect();
    return { x: b.x, y: b.y, width: b.width, height: b.height };
  };
  const title = document.querySelector('.hero-title');
  const cubes = Array.from(document.querySelectorAll('.hero .hero-art .cube'));
  const labels = Array.from(document.querySelectorAll('.kpi-card .k-label'))
    .map((e) => (e.textContent || '').trim());
  const values = Array.from(document.querySelectorAll('.kpi-card .k-val'))
    .map((e) => (e.textContent || '').trim());
  return {
    heroTitleRect: r(title),
    cubeRects: cubes.map((c) => ({ cls: c.className, rect: r(c) })),
    cubeCount: cubes.length,
    kpiLabels: labels,
    kpiValues: values,
    hasHeroInner: !!document.querySelector('.hero-inner'),
    pageTitle: document.title,
  };
}
"""


def _seed_script(token: str, user: str, role: str) -> str:
    """JS that seeds the SPA session (sessionStorage) before app scripts run."""
    return (
        "(() => { try {"
        "window.sessionStorage.setItem('forgeflow.jwt', %s);"
        "window.sessionStorage.setItem('forgeflow.user', %s);"
        "window.sessionStorage.setItem('forgeflow.role', %s);"
        "} catch (e) {} })();"
    ) % (json.dumps(token), json.dumps(user), json.dumps(role))


def run_viewport(
    page: Any,
    frontend: str,
    width: int,
    token: str,
    user: str,
    role: str,
    shot_dir: Path | None = None,
    shot_paths: list[str] | None = None,
) -> dict:
    console_errors: list[str] = []
    page_errors: list[str] = []

    def on_console(msg: Any) -> None:
        if msg.type == "error":
            console_errors.append(msg.text[:300])

    def on_pageerror(err: Any) -> None:
        page_errors.append(str(err)[:300])

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)

    rec: dict[str, Any] = {"width": width}
    try:
        # Seed the session BEFORE any app script runs, then load the home route.
        page.set_viewport_size({"width": width, "height": 900})
        page.add_init_script(_seed_script(token, user, role or "admin"))
        resp = page.goto(frontend + "/", wait_until="domcontentloaded", timeout=30000)
        rec["status"] = resp.status if resp else None
        try:
            page.wait_for_selector(".hero-title", timeout=15000)
        except Exception as e:  # noqa: BLE001
            rec["wait_hero"] = f"timeout: {e}"
        # KPI values render once the queries settle; wait for at least one value.
        try:
            page.wait_for_selector(".kpi-card .k-val", timeout=20000)
        except Exception as e:  # noqa: BLE001
            rec["wait_kpi"] = f"timeout: {e}"
        page.wait_for_timeout(1200)  # let the KPI queries flush

        rec.update(page.evaluate(_MEASURE_JS))

        # --- optional screenshot (Phase 3). Home @1440 keeps the fixed name. ---
        if shot_dir is not None:
            shot_dir.mkdir(parents=True, exist_ok=True)
            name = "home_hero_fixed.png" if width == 1440 else f"home_{width}.png"
            page.screenshot(path=str(shot_dir / name), full_page=False)
            rec["screenshot"] = str(shot_dir / name)
    except Exception as e:  # noqa: BLE001
        rec["fatal"] = f"{type(e).__name__}: {e}"
    finally:
        page.remove_listener("console", on_console)
        page.remove_listener("pageerror", on_pageerror)

    rec["consoleErrors"] = console_errors
    rec["pageErrors"] = page_errors
    return rec


# --------------------------------------------------------------------------- #
# Extra route capture (/ops, /cost, /analytics) — console + API + data state   #
# --------------------------------------------------------------------------- #
def capture_route(page: Any, frontend: str, path: str, width: int, shot_dir: Path | None) -> dict:
    """Visit a route, record console/page errors, the /api responses it made, a
    crude real-data-vs-empty('—') signal, and a full-page screenshot."""
    console_errors: list[str] = []
    page_errors: list[str] = []
    apis: list[dict] = []

    def on_console(msg: Any) -> None:
        if msg.type == "error":
            console_errors.append(msg.text[:300])

    def on_pageerror(err: Any) -> None:
        page_errors.append(str(err)[:300])

    def on_response(resp: Any) -> None:
        u = resp.url
        if "/api/" not in u:
            return
        rec = {"api": "/" + u.split("/api/", 1)[-1].split("?", 1)[0], "status": resp.status}
        try:
            if "application/json" in (resp.headers.get("content-type") or ""):
                j = resp.json()
                if isinstance(j, dict):
                    for k in ("has_data", "has_cost", "amount", "total", "currency", "level", "source"):
                        if k in j:
                            rec[k] = j[k]
                elif isinstance(j, list) and j and isinstance(j[0], dict):
                    rec["rows"] = len(j)
        except Exception:  # noqa: BLE001
            pass
        apis.append(rec)

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    page.on("response", on_response)

    out: dict[str, Any] = {"path": path, "width": width}
    try:
        resp = page.goto(frontend + path, wait_until="domcontentloaded", timeout=30000)
        out["status"] = resp.status if resp else None
        page.wait_for_timeout(3500)
        sig = page.evaluate(
            "() => ({ emDash: (document.body.innerText.match(/—/g)||[]).length,"
            "  h1: (document.querySelector('h1,h2,.sec-title,.card-title')||{}).textContent || '',"
            "  title: document.title })"
        )
        out.update(sig)
        if shot_dir is not None:
            shot_dir.mkdir(parents=True, exist_ok=True)
            slug = path.strip("/").replace("/", "_") or "root"
            shot = shot_dir / f"{slug}_{width}.png"
            page.screenshot(path=str(shot), full_page=True)
            out["screenshot"] = str(shot)
    except Exception as e:  # noqa: BLE001
        out["fatal"] = f"{type(e).__name__}: {e}"
    finally:
        page.remove_listener("console", on_console)
        page.remove_listener("pageerror", on_pageerror)
        page.remove_listener("response", on_response)

    out["consoleErrors"] = console_errors
    out["pageErrors"] = page_errors
    out["apis"] = apis
    # Crude data-state signal (raw evidence; the report states the judgement):
    has_real = any(a.get("has_data") is True or a.get("has_cost") is True or (a.get("rows") or 0) > 0 for a in apis)
    out["dataState"] = "real" if has_real else ("empty(——)" if out.get("emDash", 0) > 0 else "unknown")
    return out


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="ForgeFlow browser E2E verification")
    ap.add_argument("--backend", default="http://127.0.0.1:8010")
    ap.add_argument("--frontend", default="http://127.0.0.1:5173")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="forgeflow-dev")
    ap.add_argument("--out", default=str(Path(__file__).with_name("e2e_verify_result.json")))
    ap.add_argument("--headed", action="store_true")
    # Phase-3 helpers: when --screenshot-dir is given, home@1440 is saved as
    # home_hero_fixed.png and --shots-paths routes are captured (full page).
    ap.add_argument("--screenshot-dir", default=None, help="dir to write PNG evidence into")
    ap.add_argument("--shots-paths", default="/ops,/cost,/analytics", help="extra routes to shoot")
    ap.add_argument(
        "--viewports", default="1440,1280,1024,768",
        help="comma-separated widths to test (e.g. 1440,1280,1200,1181,1180,1024,768)",
    )
    args = ap.parse_args()
    shot_dir = Path(args.screenshot_dir) if args.screenshot_dir else None
    shot_paths = [p.strip() for p in args.shots_paths.split(",") if p.strip()]
    viewports = [int(x) for x in args.viewports.split(",") if x.strip()]

    result: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "backend": args.backend,
        "frontend": args.frontend,
        "viewports": viewports,
    }
    failures: list[str] = []

    # --- 1. preflight ------------------------------------------------------
    be_ok, be_detail = wait_reachable(args.backend + "/health", timeout_s=60)
    fe_ok, fe_detail = wait_reachable(args.frontend + "/", timeout_s=60)
    result["preflight"] = {
        "backend_reachable": be_ok,
        "backend_detail": be_detail,
        "frontend_reachable": fe_ok,
        "frontend_detail": fe_detail,
    }
    if not be_ok:
        failures.append(f"preflight: backend {args.backend} unreachable ({be_detail})")
    if not fe_ok:
        failures.append(f"preflight: frontend {args.frontend} unreachable ({fe_detail})")

    # --- 2. auth -----------------------------------------------------------
    status, body = _http_json(
        args.backend + "/auth/login", {"user_id": args.user, "password": args.password}
    )
    token = body.get("access_token") if isinstance(body, dict) else None
    role = body.get("role") if isinstance(body, dict) else None
    result["auth"] = {"status": status, "role": role, "token_present": bool(token)}
    if not token:
        failures.append(f"auth: login failed status={status} body={str(body)[:120]}")

    # --- 3/4/5. per-viewport ----------------------------------------------
    per_viewport: dict[str, Any] = {}
    if token and be_ok and fe_ok:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:  # noqa: BLE001
            failures.append(f"playwright import failed: {e}")
            sync_playwright = None  # type: ignore[assignment]

        if sync_playwright is not None:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel="chrome", headless=not args.headed, args=["--no-sandbox"])
                try:
                    for w in viewports:
                        ctx = browser.new_context(viewport={"width": w, "height": 900})
                        page = ctx.new_page()
                        rec = run_viewport(
                            page, args.frontend, w, token, args.user, role or "admin",
                            shot_dir=shot_dir, shot_paths=shot_paths,
                        )
                        ctx.close()
                        per_viewport[str(w)] = rec

                    # --- extra routes at the widest viewport ------------------
                    if shot_dir is not None and shot_paths:
                        mw = max(viewports)
                        ectx = browser.new_context(viewport={"width": mw, "height": 1100})
                        epage = ectx.new_page()
                        epage.add_init_script(_seed_script(token, args.user, role or "admin"))
                        extra_routes = {}
                        for p in shot_paths:
                            extra_routes[p] = capture_route(epage, args.frontend, p, mw, shot_dir)
                        ectx.close()
                        result["extra_routes"] = extra_routes
                finally:
                    browser.close()

    # --- assertions --------------------------------------------------------
    for w in viewports:
        key = str(w)
        rec = per_viewport.get(key)
        if rec is None:
            failures.append(f"[{w}] no measurement captured")
            continue

        # page loaded
        if rec.get("status") != 200:
            failures.append(f"[{w}] page not 200 (status={rec.get('status')}, fatal={rec.get('fatal')})")
        if rec.get("fatal"):
            failures.append(f"[{w}] fatal: {rec['fatal']}")

        # KPI labels
        labels = rec.get("kpiLabels") or []
        missing = [lb for lb in KPI_LABELS if lb not in labels]
        rec["kpiLabelsAllPresent"] = not missing
        if missing:
            failures.append(f"[{w}] KPI labels missing {missing}; saw {labels}")

        # hero rects
        title_rect = rec.get("heroTitleRect")
        if not title_rect or title_rect.get("width", 0) <= 0 or title_rect.get("height", 0) <= 0:
            failures.append(f"[{w}] hero title rect empty: {title_rect}")
        cube_rects = [c for c in (rec.get("cubeRects") or []) if c.get("rect")]
        visible_cubes = [c for c in cube_rects if c["rect"]["width"] > 0 and c["rect"]["height"] > 0]
        rec["cubeCount"] = rec.get("cubeCount", len(cube_rects))
        rec["cubesVisible"] = len(visible_cubes)
        if rec["cubeCount"] != 4:
            failures.append(f"[{w}] expected 4 decorative cubes, found {rec['cubeCount']} in DOM")

        overlaps = []
        for c in visible_cubes:
            if title_rect and _rects_intersect(title_rect, c["rect"]):
                overlaps.append({"cube": c["cls"], "rect": c["rect"]})
        rec["overlaps"] = overlaps
        # Numeric horizontal gap between the title's right edge and the nearest
        # visible cube's left edge (positive ⇒ separated).
        if visible_cubes and title_rect:
            rec["horizontalGapPx"] = round(
                min(c["rect"]["x"] for c in visible_cubes) - (title_rect["x"] + title_rect["width"]), 1
            )

        if w > ART_VISIBLE_MIN_WIDTH:
            # Cubes MUST be visible here; overlap must be zero.
            if not visible_cubes:
                failures.append(f"[{w}] cubes expected visible (>1180px) but none measured")
            elif overlaps:
                failures.append(f"[{w}] hero title OVERLAPS {len(overlaps)} cube(s): {overlaps}")
        else:
            # Cubes are CSS-hidden by design at <=1180px: non-intersection is
            # vacuous. Record it as such rather than a silent pass.
            rec["nonintersectionVacuous"] = True

        # console hygiene
        if rec.get("consoleErrors"):
            failures.append(f"[{w}] {len(rec['consoleErrors'])} console error(s): {rec['consoleErrors'][:3]}")
        if rec.get("pageErrors"):
            failures.append(f"[{w}] {len(rec['pageErrors'])} page error(s): {rec['pageErrors'][:3]}")

    # --- extra route hygiene (/ops, /cost, /analytics) ---------------------
    for path, r in (result.get("extra_routes") or {}).items():
        if r.get("status") != 200:
            failures.append(f"[extra {path}] not 200 (status={r.get('status')}, fatal={r.get('fatal')})")
        if r.get("fatal"):
            failures.append(f"[extra {path}] fatal: {r['fatal']}")
        if r.get("consoleErrors"):
            failures.append(f"[extra {path}] {len(r['consoleErrors'])} console error(s): {r['consoleErrors'][:3]}")
        if r.get("pageErrors"):
            failures.append(f"[extra {path}] {len(r['pageErrors'])} page error(s): {r['pageErrors'][:3]}")

    console_total = sum(len(r.get("consoleErrors", [])) for r in per_viewport.values())
    page_total = sum(len(r.get("pageErrors", [])) for r in per_viewport.values())

    result["per_viewport"] = per_viewport
    result["summary"] = {
        "passed": not failures,
        "failure_count": len(failures),
        "failures": failures,
        "console_errors_total": console_total,
        "page_errors_total": page_total,
        "auth_role": role,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    # --- human summary -----------------------------------------------------
    print("=" * 72)
    print("ForgeFlow E2E verification")
    print(f"  backend  : {args.backend}  reachable={be_ok} ({be_detail})")
    print(f"  frontend : {args.frontend}  reachable={fe_ok} ({fe_detail})")
    print(f"  auth     : role={role} token={'yes' if token else 'NO'}")
    for w in viewports:
        rec = per_viewport.get(str(w), {})
        print(
            f"  [{w}] status={rec.get('status')} kpi={rec.get('kpiLabels')} "
            f"cubes_visible={rec.get('cubesVisible')}/{rec.get('cubeCount')} "
            f"overlaps={len(rec.get('overlaps', []) or [])} "
            f"console_err={len(rec.get('consoleErrors', []) or [])} "
            f"page_err={len(rec.get('pageErrors', []) or [])}"
        )
        if rec.get("nonintersectionVacuous"):
            print(f"        note: cubes hidden by CSS (<=1180px) — non-intersection VACUOUS")
        if rec.get("horizontalGapPx") is not None:
            print(f"        horizontal gap title↔nearest cube = {rec['horizontalGapPx']}px")
    for path, r in (result.get("extra_routes") or {}).items():
        print(
            f"  [extra {path}] status={r.get('status')} dataState={r.get('dataState')} "
            f"emDash={r.get('emDash')} console_err={len(r.get('consoleErrors', []) or [])} "
            f"page_err={len(r.get('pageErrors', []) or [])} shot={r.get('screenshot')}"
        )
    print(f"  result json: {out_path}")
    if failures:
        print("-" * 72)
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        print("VERDICT: FAIL")
    else:
        print("VERDICT: PASS")
    print("=" * 72)

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
