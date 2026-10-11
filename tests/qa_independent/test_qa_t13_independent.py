"""QA independent verification — INC46 T13 native backend (frozen sha256 f182ac…).

Owned by QA (严过关). Added only; no existing assertion weakened.

Covers:
* W2-②  native "no network egress" is honestly ``degraded`` — independent
  reproduction (violations contains ``network_egress``, verdict=fail,
  local listener accepted=0) + a QA counterfactual (guard removed ⇒ connects).
* W2-③  native "read-only rootfs" is honestly ``unlanded`` (no ``landed`` claim).
* W2-⑧  no production DB / object-store credentials — parent sets prod creds,
  child reads ``None`` and ``evidence["no_prod_credentials"] is True``.
* W5    QA-designed counterfactual: strip the native quota ⇒ a *memory* bomb is
  no longer killed by the quota (was ``quota_exceeded=="memory"``).
* R2    no ``forgeflow.sandbox.real_isolation`` anchor ⇒ interlock R2 unresolved.

Self-attest: v1 2026-10-03 — initial QA independent probe.
"""

from __future__ import annotations

import socket
import threading

import pytest

from forgeflow.skills import publish_interlock as pi
from forgeflow.skills.sandbox_isolated import (
    IsolationLimits,
    isolation_capability_report,
    run_isolated,
)


# --------------------------------------------------------------------------- #
# a 127.0.0.1 listener — proof of reachability independent of the internet      #
# --------------------------------------------------------------------------- #
class _Listener:
    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self.accepted = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.accepted += 1
            try:
                conn.sendall(b"OK")
            finally:
                conn.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._sock.close()


_CONNECT = (
    "import socket\n"
    "try:\n"
    "    c = socket.create_connection(('127.0.0.1', {port}), timeout=2)\n"
    "    print('CONNECTED', c.recv(8))\n"
    "    c.close()\n"
    "except Exception as e:\n"
    "    print('CONNECT-FAILED', type(e).__name__)\n"
)


# --------------------------------------------------------------------------- #
# W2-② — network egress honestly degraded + counterfactual                       #
# --------------------------------------------------------------------------- #
def test_w2_network_egress_blocked_and_counterfactual_connects():
    listener = _Listener()
    try:
        blocked = run_isolated(_CONNECT.format(port=listener.port))
        assert blocked.verdict == "fail", blocked.to_dict()
        assert "network_egress" in blocked.violations
        assert "CONNECTED" not in blocked.stdout
        assert listener.accepted == 0, "guard must stop the TCP handshake"

        unblocked = run_isolated(
            _CONNECT.format(port=listener.port), network_block=False
        )
        accepted = listener.accepted
    finally:
        listener.close()

    # counterfactual: guard removed ⇒ the SAME assertion flips (real connect).
    assert "network_egress" not in unblocked.violations
    assert "CONNECTED" in unblocked.stdout
    assert accepted >= 1


# --------------------------------------------------------------------------- #
# W2-③ — read-only rootfs honesty                                             #
# --------------------------------------------------------------------------- #
def test_w2_readonly_rootfs_is_unlanded_and_not_overclaimed():
    report = isolation_capability_report()
    ro = [r for r in report if "只读" in r["requirement"]]
    assert ro and ro[0]["status"] == "unlanded", ro
    # no field of the RO row may claim "landed"
    assert all("landed" != str(v) for k, v in ro[0].items() if k != "requirement")
    assert "landed" not in str(ro[0].get("means", ""))

    # positive control: a genuinely-landed item DOES say landed (not vacuous).
    subproc = next(r for r in report if "子进程" in r["requirement"])
    assert subproc["status"] == "landed"


# --------------------------------------------------------------------------- #
# W2-⑧ — no production DB / object-store credentials                           #
# --------------------------------------------------------------------------- #
def test_w2_no_production_credentials(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://prod/db")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "QA-SHOULD-NOT-LEAK")
    monkeypatch.setenv("OPENAI_API_KEY", "QA-SHOULD-NOT-LEAK")
    result = run_isolated(
        "import os\n"
        "print('DB', repr(os.environ.get('DATABASE_URL')))\n"
        "print('AWS', repr(os.environ.get('AWS_SECRET_ACCESS_KEY')))\n"
        "print('OAI', repr(os.environ.get('OPENAI_API_KEY')))\n"
    )
    assert result.verdict == "pass", result.to_dict()
    assert "DB None" in result.stdout
    assert "AWS None" in result.stdout
    assert "OAI None" in result.stdout
    assert "SHOULD-NOT-LEAK" not in result.stdout
    assert result.evidence["no_prod_credentials"] is True

    # positive control: an explicitly-passed var IS visible (proves the probe
    # isn't just an empty environment).
    passed = run_isolated(
        "import os\nprint('PASSED', os.environ.get('QA_PASSED_VAR'))\n",
        extra_env={"QA_PASSED_VAR": "visible"},
    )
    assert "PASSED visible" in passed.stdout


# --------------------------------------------------------------------------- #
# W5 — QA counterfactual: strip the native quota ⇒ memory bomb escapes quota    #
# --------------------------------------------------------------------------- #
def test_w5_memory_bomb_quota_counterfactual():
    bomb = (
        "import time\n"
        "blocks = []\n"
        "for _ in range(40):\n"
        "    blocks.append(bytearray(8 * 1024 * 1024))\n"
        "    time.sleep(0.03)\n"
        "time.sleep(30)\n"
    )
    limits = IsolationLimits(timeout_s=6, max_memory_mb=48, max_processes=6)

    guarded = run_isolated(bomb, limits=limits)
    assert guarded.quota_exceeded == "memory", guarded.to_dict()
    assert guarded.verdict == "error"
    assert guarded.passed is False

    # strip the supervising quota ⇒ the SAME bomb is no longer memory-killed.
    unguarded = run_isolated(bomb, limits=limits, quota=False)
    assert unguarded.quota_exceeded != "memory", unguarded.to_dict()
    assert unguarded.passed is False  # wall-clock backstop still refuses a pass


# --------------------------------------------------------------------------- #
# R2 — no real_isolation anchor ⇒ interlock R2 unresolved                       #
# --------------------------------------------------------------------------- #
def test_r2_remains_unmet_no_real_isolation_anchor():
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("forgeflow.sandbox.real_isolation")

    status = pi.evaluate_interlock("qa-tenant-t13")
    r2 = next(r for r in status.requirements if r.requirement == "R2")
    assert r2.met is False
    assert "无证据" in r2.evidence
    assert status.missing == [f"R{i}" for i in range(2, 9)]
    assert status.released is False
