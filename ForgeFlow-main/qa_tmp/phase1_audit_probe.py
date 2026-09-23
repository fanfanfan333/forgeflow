"""QA Phase 1 — audit export + middleware-order (denied 留痕) independent probe.

Uses a REAL HTTP client (Starlette TestClient) against the REAL ASGI app, so the
middleware stack (SecurityHeaders → Audit → RBAC → RateLimit → Security) is
exercised exactly as in production. TestClient is deliberately NOT used as a
context manager: that avoids the app lifespan (which unconditionally init_pool()).
Without a pool the audit sink falls back to the in-process ring buffer, so the
whole /audit read path is verifiable offline.

No engineer assertion is imported; every check re-derives its own expectation
from the response objects.
"""
from __future__ import annotations

import json
import os
import uuid

# --- environment MUST be set before importing the app ----------------------
os.environ["STORAGE_BACKEND"] = "memory"
os.environ["LLM_PROVIDER"] = "mock"
os.environ["EMBEDDING_PROVIDER"] = "mock"
os.environ.setdefault(
    "API_SECRET_KEY", "b54c87a6212ee4f39fbfa49aa1458e4bbd3e112f5ead66b736f6d885a7f863ae"
)

from starlette.testclient import TestClient  # noqa: E402

from forgeflow.api.main import app  # noqa: E402
from forgeflow.api.routers.audit import _RING, clear_audit_ring  # noqa: E402
from forgeflow.auth.jwt import create_access_token  # noqa: E402

RESULT: dict = {}
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULT.setdefault("checks", {})[name] = {"pass": bool(cond), "detail": detail}
    if not cond:
        FAILS.append(f"{name}: {detail}")


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def main() -> int:
    client = TestClient(app)
    clear_audit_ring()

    admin = create_access_token("admin-qa", "admin")
    viewer = create_access_token("viewer-qa", "viewer")
    ws = str(uuid.uuid4())
    ws_admin = create_access_token("admin-ws", "admin", workspace_id=ws)

    # ---------- Part A: denied requests must be audited --------------------
    r_401 = client.get("/audit/search")                      # no token
    r_403 = client.post("/experiences", json={}, headers=auth(viewer))  # viewer write
    RESULT["partA_status"] = {"no_token": r_401.status_code, "viewer_write": r_403.status_code}

    # 429 — anon tier is 10/min keyed on client IP for /auth/*
    codes_429 = []
    for _ in range(12):
        rr = client.post("/auth/login", json={"username": "nobody", "password": "x"})
        codes_429.append(rr.status_code)
    RESULT["partA_login_codes"] = codes_429

    # read the audit stream back over HTTP (admin) --------------------------
    search_all = client.get("/audit/search", params={"limit": 500}, headers=auth(admin))
    RESULT["partA_search_status"] = search_all.status_code
    items = search_all.json().get("items", [])

    def rows_for(path_sub: str, status: int) -> list[dict]:
        out = []
        for it in items:
            md = it.get("metadata") or {}
            if path_sub in str(it.get("resource") or "") and md.get("status_code") == status:
                out.append(it)
        return out

    row401 = rows_for("/audit/search", 401)
    row403 = rows_for("/experiences", 403)
    row429 = rows_for("/auth/login", 429)

    check("401_row_exists", bool(row401), f"found {len(row401)} rows")
    check("401_outcome_denied", bool(row401) and all(r["outcome"] == "denied" for r in row401),
          f"outcomes={[r['outcome'] for r in row401]}")
    check("403_row_exists", bool(row403), f"found {len(row403)} rows")
    check("403_outcome_denied", bool(row403) and all(r["outcome"] == "denied" for r in row403),
          f"outcomes={[r['outcome'] for r in row403]}")
    check("429_row_exists", bool(row429), f"found {len(row429)} rows")
    # NOTE (honest reporting): the middleware maps only 401/403 -> "denied";
    # 429 falls into the "error" bucket (see middleware/audit.py:87-89, whose
    # own comment says "429 rate-limit" is a downstream error). The row IS
    # written (留痕成立); the outcome LABEL differs from the task's expectation.
    check("429_outcome_label_is_denied",
          bool(row429) and all(r["outcome"] == "denied" for r in row429),
          f"actual outcomes={[r['outcome'] for r in row429]} (task expected 'denied')")

    # ---------- Part B: /audit/export --------------------------------------
    # Observer effect: EVERY request (including the audit read calls below) is
    # itself audited, so a naive "export.total == search.total" across two
    # separate calls can never hold (the first call adds a row before the
    # second runs). We therefore compare each endpoint against the shared
    # store's length snapshotted immediately BEFORE the call — this proves both
    # read the SAME source (the ring) and report it faithfully.
    n_before_export_json = len(_RING)
    exp_json = client.get("/audit/export", params={"format": "json"}, headers=auth(admin))
    RESULT["partB_export_json_status"] = exp_json.status_code
    RESULT["partB_export_json_ct"] = exp_json.headers.get("content-type")
    ej = exp_json.json()
    check("export_json_200", exp_json.status_code == 200, str(exp_json.status_code))
    check("export_json_total_equals_store_at_call", ej.get("total") == n_before_export_json,
          f"export.total={ej.get('total')} store_before={n_before_export_json}")
    check("export_json_items_len_matches_total", len(ej.get("items", [])) == ej.get("total"),
          f"len={len(ej.get('items', []))} total={ej.get('total')}")

    n_before_search = len(_RING)
    search_total = client.get("/audit/search", params={"limit": 1}, headers=auth(admin)).json()["total"]
    RESULT["partB_search_total"] = search_total
    RESULT["partB_store_before_search"] = n_before_search
    check("search_total_equals_store_at_call", search_total == n_before_search,
          f"search.total={search_total} store_before={n_before_search}")
    RESULT["partB_observer_effect"] = (
        f"export.json.total({ej.get('total')}) == search.total({search_total}) + 1 "
        "because the export request itself is audited between the two reads"
    )

    n_before_csv = len(_RING)
    exp_csv = client.get("/audit/export", params={"format": "csv"}, headers=auth(admin))
    RESULT["partB_export_csv_status"] = exp_csv.status_code
    RESULT["partB_export_csv_ct"] = exp_csv.headers.get("content-type")
    RESULT["partB_export_csv_cd"] = exp_csv.headers.get("content-disposition")
    lines = [ln for ln in exp_csv.text.splitlines() if ln.strip()]
    data_rows = max(len(lines) - 1, 0)
    # CSV header + one line per item (metadata JSON-encoded may not contain newlines)
    check("export_csv_200", exp_csv.status_code == 200, str(exp_csv.status_code))
    check("export_csv_content_type", "text/csv" in (exp_csv.headers.get("content-type") or ""),
          exp_csv.headers.get("content-type") or "")
    check("export_csv_rows_equal_store_at_call", data_rows == n_before_csv,
          f"data_rows={data_rows} store_before={n_before_csv}")

    exp_bad = client.get("/audit/export", params={"format": "xml"}, headers=auth(admin))
    RESULT["partB_export_bad_status"] = exp_bad.status_code
    check("export_bad_format_400", exp_bad.status_code == 400, str(exp_bad.status_code))

    # time window: only rows strictly before an instant
    exp_win = client.get(
        "/audit/export", params={"format": "json", "to": "2000-01-01T00:00:00Z"}, headers=auth(admin)
    )
    RESULT["partB_export_window_total"] = exp_win.json().get("total")
    check("export_window_filters", exp_win.json().get("total") == 0,
          f"total={exp_win.json().get('total')} (window to 2000 must be empty)")

    exp_win2 = client.get(
        "/audit/export", params={"format": "json", "from": "2999-01-01T00:00:00Z"}, headers=auth(admin)
    )
    RESULT["partB_export_from_total"] = exp_win2.json().get("total")
    check("export_from_filters", exp_win2.json().get("total") == 0,
          f"total={exp_win2.json().get('total')} (window from 2999 must be empty)")

    # ---------- Part C: tenant isolation -----------------------------------
    # a request under workspace W creates a row with workspace_id = W
    client.get("/audit/search", headers=auth(ws_admin))
    null_export = client.get("/audit/export", params={"format": "json"}, headers=auth(admin)).json()
    ws_export = client.get("/audit/export", params={"format": "json"}, headers=auth(ws_admin)).json()
    null_ids = {it["id"] for it in null_export.get("items", [])}
    ws_ids = {it["id"] for it in ws_export.get("items", [])}
    RESULT["partC_null_total"] = null_export.get("total")
    RESULT["partC_ws_total"] = ws_export.get("total")
    RESULT["partC_intersection"] = len(null_ids & ws_ids)
    check("tenant_isolation_disjoint", len(null_ids & ws_ids) == 0,
          f"overlap={len(null_ids & ws_ids)}")
    check("tenant_ws_nonempty", ws_export.get("total", 0) > 0,
          f"ws_total={ws_export.get('total')}")

    RESULT["verdict"] = "PASSED" if not FAILS else "FAILED"
    RESULT["failures"] = FAILS
    print("=== PHASE1_AUDIT_PROBE ===")
    print(json.dumps(RESULT, indent=2, ensure_ascii=False))
    print("=== VERDICT:", RESULT["verdict"], "with", len(FAILS), "failure(s) ===")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
