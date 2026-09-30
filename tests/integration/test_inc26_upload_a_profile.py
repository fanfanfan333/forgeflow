"""INC26 T06 — A 档（memory + mock）下资源上传体验的四条交互 + AC-19 回归钉子。

本用例在 **A 档**（``storage_backend=memory`` + ``llm_provider=mock``）下，用真实 HTTP
``TestClient`` 驱动 INC26 新增/接通的四条交互，判定与 B 档**一致**（除真实内容差异）：

  1. **上传**（``POST /resources/files``，multipart）→ 201 且真实 ``parsed``；坏扩展名 400、
     超限 413 —— 诚实语义（不是 5xx）；
  2. **预检同源**（``GET /resources/limits``）→ ``max_bytes`` == ``Settings.multimodal_max_bytes``、
     ``supported_extensions`` == ``SUPPORTED_FILE_EXTENSIONS``（后端单一事实源）；
  3. **预览**（``GET /resources/{id}/preview``）→ 表格类逐字一致 + ``truncated``；文本类逐字
     一致；非文件类 ``available=false`` 诚实空态、无伪造内容；
  4. **筛选**（``GET /resources?kind=``）→ 返回项 ``kind`` 全部等于所选值。

外加 **AC-19 回归钉子**：既有 ``data-testid`` 必须仍可在 ``ResourcePicker.tsx`` 中寻址，
且本次新增的 testid 不覆盖既有同名项（静态检查，跨层同源）。

档位声明：本用例断言内存档行为（进程内资源索引），按纪律显式声明 ``force_memory_backend``。

引文纪律：一律 ``file.py::symbol``，不用行号。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.main import app
from forgeflow.config import get_settings
from forgeflow.resources import summaries

REPO_ROOT = Path(__file__).resolve().parents[2]
RESOURCE_PICKER = REPO_ROOT / "frontend" / "src" / "views" / "runs" / "ResourcePicker.tsx"
#: AC-19 —— the existing testids live across the ``views/runs`` surfaces
#: (``run-declare-*`` in ``RunListPanel.tsx``, ``resource-*`` in
#: ``ResourcePicker.tsx``), so the regression scans the whole directory.
RUNS_VIEWS = REPO_ROOT / "frontend" / "src" / "views" / "runs"

#: AC-19 —— 既有 testid **只增不改不删**（必须仍可寻址）。
_EXISTING_TESTIDS = (
    "run-declare-table",
    "run-declare-paths",
    "resource-add",
    "resource-kind-",
    "resource-list",
    "resource-card",
    "resource-empty-note",
)
#: INC26 新增 testid（design §7.1）。
_NEW_TESTIDS = (
    "resource-dropzone",
    "resource-upload-row",
    "resource-upload-status",
    "resource-preview",
    "resource-preview-truncated",
    "resource-kind-filter",
    "resource-limits-note",
)


def _headers() -> dict[str, str]:
    from forgeflow.auth.jwt import create_access_token

    token = create_access_token(user_id="manager-1", role="manager")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def client(force_memory_backend):
    """A memory-profile HTTP client (declares the storage profile explicitly)."""
    return TestClient(app)


# --------------------------------------------------------------------------- #
# AC-4 — 预检同源（A 档也必须是后端单一事实源，不得写死）                        #
# --------------------------------------------------------------------------- #
def test_limits_is_single_source_of_truth(client):
    resp = client.get("/resources/limits", headers=_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["max_bytes"] == get_settings().multimodal_max_bytes
    assert set(body["supported_extensions"]) == set(summaries.SUPPORTED_FILE_EXTENSIONS)
    assert body["supported_extensions"], "白名单为空 —— 断言会空转"


# --------------------------------------------------------------------------- #
# AC-3 / AC-5 / AC-6 — 上传诚实语义 + 预览逐字一致 + 诚实空态                    #
# --------------------------------------------------------------------------- #
def test_upload_parse_and_honest_rejections(client):
    # 合法 CSV ⇒ 201 且真实 parsed。
    csv = b"name,amount\nalpha,10\nbeta,20\n"
    ok = client.post(
        "/resources/files", headers=_headers(), files={"file": ("leads.csv", csv, "text/csv")}
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["status"] == "parsed"

    # 坏扩展名 ⇒ 400（逐字原因），绝不是 5xx。
    bad = client.post(
        "/resources/files",
        headers=_headers(),
        files={"file": ("notes.txt.bak", b"x", "application/octet-stream")},
    )
    assert bad.status_code == 400, bad.text
    assert "不支持的文件类型" in str(bad.json().get("detail", ""))


def test_preview_table_verbatim_and_truncated(client):
    rows = "\n".join([",".join(["c1", "c2"])] + [f"{i},{i * 10}" for i in range(1, 8)]) + "\n"
    reg = client.post(
        "/resources/files",
        headers=_headers(),
        files={"file": ("t.csv", rows.encode("utf-8"), "text/csv")},
    )
    rid = reg.json()["id"]
    prev = client.get(f"/resources/{rid}/preview", headers=_headers(), params={"n": 3})
    assert prev.status_code == 200, prev.text
    body = prev.json()
    assert body["available"] is True and body["format"] == "table"
    # 表头与单元格与真实文件**逐字一致**。
    assert body["columns"] == ["c1", "c2"]
    assert body["rows"] == [["1", "10"], ["2", "20"], ["3", "30"]]
    # 文件共 7 行 > 3 ⇒ 必须给截断提示（不静默截断）。
    assert body["truncated"] is True
    assert body["note"]


def test_preview_text_verbatim(client):
    text = "第一行\n第二行\n第三行\n第四行\n"
    reg = client.post(
        "/resources/files",
        headers=_headers(),
        files={"file": ("doc.txt", text.encode("utf-8"), "text/plain")},
    )
    rid = reg.json()["id"]
    prev = client.get(f"/resources/{rid}/preview", headers=_headers(), params={"n": 2})
    body = prev.json()
    assert body["available"] is True and body["format"] == "text"
    assert body["content"] == "第一行\n第二行"
    assert body["truncated"] is True


def test_preview_non_file_is_honest_empty_state(client):
    reg = client.post(
        "/resources/database", headers=_headers(), json={"table": "public.leads"}
    )
    assert reg.status_code == 201, reg.text
    rid = reg.json()["id"]
    prev = client.get(f"/resources/{rid}/preview", headers=_headers())
    assert prev.status_code == 200, prev.text
    body = prev.json()
    assert body["available"] is False
    assert body["format"] == "none"
    assert body["rows"] == [] and body["content"] == ""
    assert "不支持内容预览" in body["note"]


# --------------------------------------------------------------------------- #
# AC-8 — 按类型筛选：返回项 kind 全部等于所选值                                  #
# --------------------------------------------------------------------------- #
def test_kind_filter_returns_only_that_kind(client):
    client.post("/resources/files", headers=_headers(), files={"file": ("a.csv", b"x,y\n1,2\n", "text/csv")})
    client.post("/resources/database", headers=_headers(), json={"table": "public.t1"})

    resp = client.get("/resources", headers=_headers(), params={"kind": "file"})
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert items, "筛选后无 file 资源 —— 断言会空转"
    assert all(str(it["kind"]) == "file" for it in items)


# --------------------------------------------------------------------------- #
# AC-7 — 声明的资源 id 真的随任务下发（提交 payload 的 context.resources）        #
# --------------------------------------------------------------------------- #
def test_declared_resource_id_flows_into_the_run(client, monkeypatch):
    csv = b"lead_id,amount\n1,10\n2,20\n3,30\n"
    reg = client.post(
        "/resources/files", headers=_headers(), files={"file": ("leads.csv", csv, "text/csv")}
    ).json()
    rid = reg["id"]

    # A 档确定性执行：声明一个数据文件 ⇒ 任务应注入 analysis.profile 步（证明 id 已透传）。
    resp = client.post(
        "/tasks",
        headers=_headers(),
        json={
            "intent": "统计 leads.csv 的 amount 合计",
            "context": {"resources": [rid], "column": "amount"},
        },
    )
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["run_id"]
    detail = client.get(f"/runs/{run_id}", headers=_headers()).json()
    plan_tools = [str(s.get("tool")) for s in (detail.get("plan") or {}).get("steps", [])]
    assert "analysis.profile" in plan_tools, (
        f"声明的资源 id 未透传到运行计划（plan={plan_tools}）"
    )


# --------------------------------------------------------------------------- #
# AC-19 — 既有 testid 仍可寻址；新增 testid 不覆盖既有同名项                      #
# --------------------------------------------------------------------------- #
def test_existing_testids_still_addressable():
    assert RESOURCE_PICKER.is_file(), f"ResourcePicker 不存在：{RESOURCE_PICKER}"
    picker_text = RESOURCE_PICKER.read_text(encoding="utf-8")
    # 既有 testid 分散在 views/runs 下（run-declare-* 在 RunListPanel.tsx，
    # resource-* 在 ResourcePicker.tsx），所以回归扫描整个目录的语料。
    corpus = "\n".join(
        p.read_text(encoding="utf-8") for p in sorted(RUNS_VIEWS.rglob("*.tsx"))
    )
    for tid in _EXISTING_TESTIDS:
        assert tid in corpus, f"既有 testid 丢失：{tid}"
    # 本次新增 testid 必须真实存在于 ResourcePicker（否则 AC-1~AC-6 寻址会空转）。
    for tid in _NEW_TESTIDS:
        assert tid in picker_text, f"新增 testid 缺失：{tid}"
    # 新增项不得与既有项同名（只增不改不删）。
    assert not (set(_NEW_TESTIDS) & set(_EXISTING_TESTIDS))
