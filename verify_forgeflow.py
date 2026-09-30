"""ForgeFlow — local verification (status + end-to-end smoke).

    python verify_forgeflow.py            # wait briefly, smoke-test, summarise
    python verify_forgeflow.py --status   # just report what is listening

This does NOT start anything: resident processes on this host must be launched
through WMI (the sandbox job object rejects CREATE_BREAKAWAY_FROM_JOB with
WinError 5, so a normal child is reaped immediately). Launch with
`run_forgeflow.ps1`, then verify here.

Checked path:
  1. GET  :8010/health          -> database + graph wiring
  2. POST :8010/auth/login      -> JWT issuance (demo user manager-1)
  3. GET  :8010/skills (bearer) -> a real authenticated business route
  4. GET  :5173/                -> SPA index.html
  5. GET  :5173/api/health      -> the Vite /api proxy reaching the backend

Proxy environment variables are dropped first: with HTTP_PROXY set, every one
of these 127.0.0.1 calls would return the proxy's 502 instead of the answer.
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
LOG_DIR = ROOT / "qa_tmp"

BE_HOST, BE_PORT = "127.0.0.1", 8010
FE_HOST, FE_PORT = "127.0.0.1", 5173

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1"


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket()
    s.settimeout(1.0)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def call(url: str, method: str = "GET", body: dict | None = None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def wait(port: int, label: str, seconds: int) -> bool:
    t0 = time.time()
    while time.time() - t0 < seconds:
        if port_open(port):
            print(f"  [up]   {label} :{port}  (+{time.time() - t0:.1f}s)")
            return True
        time.sleep(1.5)
    print(f"  [down] {label} :{port}  nothing listening after {seconds}s")
    return False


def smoke() -> int:
    print("\n== smoke ==")
    bad = 0

    st, body = call(f"http://{BE_HOST}:{BE_PORT}/health")
    print(f"  [1] GET  :8010/health           -> {st} {body[:200]}")
    bad += st != 200

    st, body = call(f"http://{BE_HOST}:{BE_PORT}/auth/login", "POST",
                    {"user_id": "manager-1", "password": "forgeflow-dev"})
    print(f"  [2] POST :8010/auth/login       -> {st} {body[:150]}")
    token = None
    if st == 200:
        try:
            token = json.loads(body).get("access_token")
        except Exception:  # noqa: BLE001
            pass
    bad += st != 200

    if token:
        st, body = call(f"http://{BE_HOST}:{BE_PORT}/skills", token=token)
        print(f"  [3] GET  :8010/skills (bearer)  -> {st} {body[:150]}")
        bad += st != 200

    st, body = call(f"http://{FE_HOST}:{FE_PORT}/")
    print(f"  [4] GET  :5173/                 -> {st} len={len(body)}")
    bad += st != 200

    st, body = call(f"http://{FE_HOST}:{FE_PORT}/api/health")
    print(f"  [5] GET  :5173/api/health       -> {st} {body[:120]}")
    bad += st != 200

    return bad


def main() -> int:
    print("=== ForgeFlow — local status ===")
    if "--status" in sys.argv:
        print(f"  backend  :{BE_PORT}  {'UP' if port_open(BE_PORT) else 'down'}")
        print(f"  frontend :{FE_PORT}  {'UP' if port_open(FE_PORT) else 'down'}")
        return 0

    be = wait(BE_PORT, "backend", 20)
    fe = wait(FE_PORT, "frontend", 10)

    bad = smoke() if (be and fe) else 1

    print("\n== summary ==")
    print(f"  backend  :{BE_PORT}  {'UP' if port_open(BE_PORT) else 'down'}")
    print(f"  frontend :{FE_PORT}  {'UP' if port_open(FE_PORT) else 'down'}")
    print(f"  API docs : http://{BE_HOST}:{BE_PORT}/docs")
    print(f"  console  : http://{FE_HOST}:{FE_PORT}")
    print(f"  login    : manager-1 / forgeflow-dev")
    if not port_open(BE_PORT):
        print(f"  backend log  -> {LOG_DIR / '_run_backend_real.log'}")
        print("  start it with: powershell -ExecutionPolicy Bypass -File run_forgeflow.ps1")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
