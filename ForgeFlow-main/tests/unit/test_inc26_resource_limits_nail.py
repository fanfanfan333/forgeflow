"""INC26 T01 — 资源上限/白名单的**跨层防漂移钉子** + 反事实注入（design §7.3）。

源（truth）与靶（target）必须来自**两条独立路径**，钉子才有意义：

* **源** = 运行时真实值：`get_settings().multimodal_max_bytes` 与
  `resources/summaries.py::SUPPORTED_FILE_EXTENSIONS`（不是测试里写死的常量）。
* **靶** = 真实 HTTP 响应：`TestClient(app).get("/resources/limits")`。

再加一条**静态**同源钉子：`frontend/src/**` **不得**出现写死的上限字面量（否则前端
预检就会与后端单一事实源漂移）。

反事实（改坏靶 ⇒ 钉子必须变红，证明钉子真的在比较）：
* C1 改 `MULTIMODAL_MAX_BYTES` ⇒ `/resources/limits` 同步变、且 1.5KB 上传被 413 拒；
* C2 改 `summaries.SUPPORTED_FILE_EXTENSIONS` ⇒ 端点同步缺该扩展名。

引文纪律：一律 `file.py::symbol`，不用行号。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.main import app
from forgeflow.config import get_settings
from forgeflow.resources import summaries

#: 仓库根 = 本文件的 parents[2]（tests/unit/<file> → tests → repo）。
REPO_ROOT = Path(__file__).resolve().parents[2]
#: 前端源码根（静态同源钉子的扫描范围）。
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"

#: 前端**禁止**出现的写死上限字面量（跨层同源）。
_FORBIDDEN_LITERALS: tuple[str, ...] = ("5242880", "5*1024*1024", "5 * 1024 * 1024")


@pytest.fixture(autouse=True)
def _fresh_settings():
    """每个用例前后清空 `get_settings` 的 lru_cache。

    C1/C2 会 monkeypatch 环境变量再 `cache_clear()` 重新取值；若不在 teardown
    再清一次，被污染的 Settings 会残留在缓存里影响后续用例。
    """
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def _admin_headers() -> dict[str, str]:
    from forgeflow.auth.jwt import create_access_token

    token = create_access_token(user_id="admin-1", role="admin")
    return {"Authorization": f"Bearer {token}"}


def _get_limits() -> dict:
    client = TestClient(app)
    resp = client.get("/resources/limits", headers=_admin_headers())
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# 钉子 1 —— 跨层同源：端点值 == 运行时真实源                                   #
# --------------------------------------------------------------------------- #
def test_limits_matches_runtime_sources():
    body = _get_limits()

    # 源 A：真实 Settings 值（运行时，非测试常量）。
    assert body["max_bytes"] == get_settings().multimodal_max_bytes, (
        "端点 max_bytes 与运行时 Settings.multimodal_max_bytes 不一致 —— 上层写死了"
    )

    # 源 B：真实 summaries 白名单（运行时，非测试常量）。
    expected_exts = list(summaries.SUPPORTED_FILE_EXTENSIONS)
    assert set(body["supported_extensions"]) == set(expected_exts), (
        "端点 supported_extensions 与运行时 summaries.SUPPORTED_FILE_EXTENSIONS 不一致"
    )

    # 逐扩展名交叉验证：summaries 自己认它（用运行时判定，不信常量）。
    for ext in body["supported_extensions"]:
        assert summaries.is_supported_file("fixture" + ext) is True, ext

    # 反空转：白名单不能是空集合，否则上面的断言会空转成绿。
    assert body["supported_extensions"], "白名单为空 —— 断言会空转"
    assert isinstance(body["max_bytes"], int) and body["max_bytes"] > 0


# --------------------------------------------------------------------------- #
# 钉子 2 —— 前端静态扫描：不得写死上限                                         #
# --------------------------------------------------------------------------- #
def test_frontend_has_no_hardcoded_limit():
    assert FRONTEND_SRC.is_dir(), f"前端源码根不存在：{FRONTEND_SRC}"
    offenders: list[tuple[str, str]] = []
    scanned = 0
    for path in sorted(FRONTEND_SRC.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        scanned += 1
        for literal in _FORBIDDEN_LITERALS:
            if literal in text:
                offenders.append((str(path.relative_to(REPO_ROOT)), literal))
    # 反空转：真的扫到了文件才说明扫描有效。
    assert scanned > 0, "未扫描到任何前端文件 —— 扫描可能失效"
    assert offenders == [], f"前端出现了写死的上传上限：{offenders}"


# --------------------------------------------------------------------------- #
# 反事实 C1 —— 改上限 ⇒ 端点与上传校验**同时**跟随（改坏则钉子变红）           #
# --------------------------------------------------------------------------- #
def test_c1_limit_change_flows_through_endpoint_and_upload(monkeypatch):
    monkeypatch.setenv("MULTIMODAL_MAX_BYTES", "1024")
    get_settings.cache_clear()

    body = _get_limits()
    assert body["max_bytes"] == 1024, "端点未跟随运行时上限 —— 说明它写了常量"

    # 上传一个 1.5KB 的合法 CSV ⇒ 必须 413（携带实际字节 + 上限），而不是被静默接受。
    payload = b"a,b\n" + b"1,2\n" * 400  # ~2.4KB > 1024
    assert len(payload) > 1024
    client = TestClient(app)
    resp = client.post(
        "/resources/files",
        headers=_admin_headers(),
        files={"file": ("over.csv", payload, "text/csv")},
    )
    assert resp.status_code == 413, resp.text
    detail = str(resp.json().get("detail", ""))
    assert str(len(payload)) in detail and "1024" in detail


# --------------------------------------------------------------------------- #
# 反事实 C2 —— 改白名单 ⇒ 端点同步缺该扩展名（改坏则钉子变红）                 #
# --------------------------------------------------------------------------- #
def test_c2_extensions_change_flows_through_endpoint(monkeypatch):
    assert ".csv" in summaries.SUPPORTED_FILE_EXTENSIONS  # 前置：基线确实含 .csv
    trimmed = tuple(e for e in summaries.SUPPORTED_FILE_EXTENSIONS if e != ".csv")
    monkeypatch.setattr(summaries, "SUPPORTED_FILE_EXTENSIONS", trimmed)

    body = _get_limits()
    assert ".csv" not in body["supported_extensions"], (
        "端点未跟随运行时白名单 —— 说明它写了常量"
    )
    assert set(body["supported_extensions"]) == set(trimmed)
