"""GET /audit/export — CSV/JSON export with tenant + time scoping.

In the offline (memory) profile the export reads the same in-memory ring buffer
that ``/audit/search`` reads, so the two views are consistent and neither can
500 just because PostgreSQL is absent.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from forgeflow.api.routers import audit as audit_router
from forgeflow.api.routers.audit import (
    clear_audit_ring,
    export_audit_log,
    record_audit_entry,
    search_audit_log,
)


def _seed() -> None:
    clear_audit_ring()
    record_audit_entry(
        {
            "id": "1",
            "timestamp": "2026-05-19T14:00:00+00:00",
            "user_id": "u-1",
            "role": "manager",
            "action": "POST",
            "resource": "/workflows/run",
            "resource_id": None,
            "outcome": "allowed",
            "request_id": "r-1",
            "metadata": {"status_code": 200},
        }
    )
    record_audit_entry(
        {
            "id": "2",
            "timestamp": "2026-05-20T09:30:00+00:00",
            "user_id": "u-2",
            "role": "viewer",
            "action": "GET",
            "resource": "/metrics",
            "outcome": "denied",
            "request_id": "r-2",
            "metadata": {"status_code": 403},
        }
    )
    record_audit_entry(
        {
            "id": "3",
            "timestamp": "2026-05-21T11:00:00+00:00",
            "user_id": "u-3",
            "role": "admin",
            "action": "GET",
            "resource": "/cost/board",
            "outcome": "allowed",
            "request_id": "r-3",
            "metadata": {},
            "workspace_id": "ws-a",
        }
    )
    # Outside a 2026-05 window — used by the time-filter test.
    record_audit_entry(
        {
            "id": "4-old",
            "timestamp": "2020-01-01T00:00:00+00:00",
            "user_id": "u-old",
            "role": "admin",
            "action": "GET",
            "resource": "/legacy",
            "outcome": "allowed",
            "metadata": {},
        }
    )


# --------------------------------------------------------------------------- #
# Formatting                                                                   #
# --------------------------------------------------------------------------- #

async def test_json_export_lists_items():
    _seed()
    response = await export_audit_log(pool=None, fmt="json")

    assert response.status_code == 200
    assert response.media_type == "application/json"
    body = json.loads(response.body)
    # No workspace claim ⇒ only global (NULL-tenant) rows, matching /audit/search.
    assert body["total"] == 3
    assert {item["id"] for item in body["items"]} == {"1", "2", "4-old"}


async def test_csv_export_has_header_and_attachment():
    _seed()
    response = await export_audit_log(pool=None, fmt="csv")

    assert response.status_code == 200
    assert "text/csv" in response.media_type
    assert "attachment" in response.headers["Content-Disposition"]
    assert ".csv" in response.headers["Content-Disposition"]
    text = response.body.decode("utf-8")
    assert text.splitlines()[0] == (
        "id,timestamp,user_id,role,action,resource,resource_id,outcome,request_id,metadata"
    )
    assert "u-1" in text
    assert "u-2" in text


async def test_unsupported_format_is_rejected():
    _seed()
    with pytest.raises(HTTPException) as excinfo:
        await export_audit_log(pool=None, fmt="xml")
    assert excinfo.value.status_code == 400


# --------------------------------------------------------------------------- #
# Scoping + consistency                                                        #
# --------------------------------------------------------------------------- #

async def test_time_window_filter():
    _seed()
    response = await export_audit_log(
        pool=None, fmt="json", since=None, until=None
    )
    all_ids = {item["id"] for item in json.loads(response.body)["items"]}
    assert "4-old" in all_ids

    from datetime import datetime, timezone

    windowed = await export_audit_log(
        pool=None,
        fmt="json",
        since=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    kept = {item["id"] for item in json.loads(windowed.body)["items"]}
    assert "4-old" not in kept
    assert kept == {"1", "2"}


async def test_tenant_scoping_excludes_other_tenants():
    _seed()
    response = await export_audit_log(pool=None, fmt="json", workspace_id="ws-a")
    ids = {item["id"] for item in json.loads(response.body)["items"]}
    assert ids == {"3"}


async def test_export_is_consistent_with_search():
    _seed()
    exported = json.loads((await export_audit_log(pool=None, fmt="json")).body)
    searched = await search_audit_log(pool=None, limit=1000)

    assert [i["id"] for i in exported["items"]] == [i["id"] for i in searched["items"]]
    assert exported["total"] == searched["total"]


# --------------------------------------------------------------------------- #
# HTTP layer — 200 in the offline profile, 400 on a bad format                 #
# --------------------------------------------------------------------------- #

def _client() -> TestClient:
    app = FastAPI()
    app.include_router(audit_router.router, prefix="/audit")
    return TestClient(app)


def test_export_route_defaults_to_json_200():
    _seed()
    response = _client().get("/audit/export")

    assert response.status_code == 200
    assert response.json()["total"] == 3


def test_export_route_csv_200():
    _seed()
    response = _client().get("/audit/export", params={"format": "csv"})

    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]


def test_export_route_bad_format_400():
    _seed()
    response = _client().get("/audit/export", params={"format": "pdf"})
    assert response.status_code == 400
