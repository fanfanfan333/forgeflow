"""主理人独立端到端探针：不打 lifespan（免 PG 依赖），直接 TestClient(app)。
覆盖：/cost/board、/cost/savings、/metrics/slo、/metrics/、/skills/evolution-advice，
以及「被拒绝的请求是否留审计痕迹」（401 无 token / 403 越权）。
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("STORAGE_BACKEND", "memory")
os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("EMBEDDING_PROVIDER", "mock")

sys.path.insert(0, os.getcwd())

from fastapi.testclient import TestClient  # noqa: E402
from forgeflow.api.main import app  # noqa: E402
from forgeflow.auth.jwt import create_access_token  # noqa: E402
from forgeflow.api.routers.audit import _RING, clear_audit_ring  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_probe_before.json")

client = TestClient(app)  # 故意不进 with：不进 lifespan，避免 init_pool()

admin = {"Authorization": f"Bearer {create_access_token(user_id='admin', role='admin')}"}
viewer = {"Authorization": f"Bearer {create_access_token(user_id='viewer-1', role='viewer')}"}

report: dict = {}


def probe(name: str, path: str, headers: dict | None):
    try:
        r = client.get(path, headers=headers or {})
        body = None
        try:
            body = r.json()
        except Exception:
            body = r.text[:400]
        report[name] = {"status": r.status_code, "body": body}
    except Exception as exc:  # noqa: BLE001
        report[name] = {"status": "EXC", "body": f"{type(exc).__name__}: {exc}"}


probe("cost_board_admin", "/cost/board", admin)
probe("cost_savings_admin", "/cost/savings", admin)
probe("metrics_admin", "/metrics/", admin)
probe("metrics_slo_admin", "/metrics/slo", admin)
probe("evolution_advice_admin", "/skills/evolution-advice", admin)
probe("metrics_cost_admin", "/metrics/cost", admin)

# —— 审计留痕：被拒绝的请求是否入库 ——
clear_audit_ring()
before = len(_RING)
probe("no_token_401", "/cost/board", None)
probe("no_token_401b", "/skills", None)
probe("viewer_403", "/cost/board", viewer)  # viewer 无 read:cost → 应 403（若登记）
viewer_write = None
try:
    r = client.post("/skills", headers=viewer, json={"name": "x"})
    viewer_write = {"status": r.status_code, "body": r.text[:200]}
except Exception as exc:  # noqa: BLE001
    viewer_write = {"status": "EXC", "body": f"{type(exc).__name__}: {exc}"}
report["viewer_write_403"] = viewer_write
probe("admin_ok_200", "/skills", admin)

rows = list(_RING)
report["_audit"] = {
    "ring_size_delta": len(rows) - before,
    "outcome_counts": {},
    "denied_paths": [],
}
counts: dict = {}
for row in rows:
    try:
        outcome = str(row.get("outcome"))
    except Exception:
        outcome = "?"
    counts[outcome] = counts.get(outcome, 0) + 1
    if outcome == "denied":
        report["_audit"]["denied_paths"].append(
            {"path": row.get("path") or row.get("resource"), "status": (row.get("metadata") or {}).get("status_code")}
        )
report["_audit"]["outcome_counts"] = counts

with open(OUT, "w", encoding="utf-8") as fh:
    json.dump(report, fh, ensure_ascii=False, indent=2, default=str)

print(json.dumps(report, ensure_ascii=False, indent=2, default=str)[:6000])
