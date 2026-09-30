"""INC25 P0-A self-proof: a code-plane payload's evidence survives bounding.

``tool_executor._bound_payload`` used to replace the WHOLE payload (>4000 chars)
with ``{"truncated": True, ...}``, erasing the ``codeplane`` sub-dict that
``RunRecord.codeplane`` is assembled from — so the more the engine produced, the
more certainly the evidence vanished and a real code change looked like nothing
happened. The fix trims *inside* the codeplane sub-dict; every required key stays
present and each truncation is flagged verbatim.
"""

from __future__ import annotations

from forgeflow.runtime import orchestrator as orch
from forgeflow.runtime import tool_executor as te

_INJECTED_DIFF = "diff --git a/mathlib.py b/mathlib.py\n" + "".join(
    f"+ added line {i}\n" for i in range(60)
)


def _big_payload() -> dict:
    timeline = [
        {
            "seq": i,
            "phase": "action",
            "kind": "action",
            "status": "running",
            "label": "正在执行",
            "detail": f"tool_call_{i} " + "x" * 180,
        }
        for i in range(60)
    ]
    tests = {
        "measured": True,
        "verdict": "passed",
        "passed": 2,
        "failed": 0,
        "command": "python -m pytest -q",
        "raw_stdout": ("." * 90 + "\n") * 40,
    }
    return {
        "ok": True,
        "provider": "openhands-subprocess",
        "workspace_id": "ws_demo_1234",
        "summary": "代码任务已执行：产出变更",
        "codeplane": {
            "engine": {"available": True, "interpreter": "/x/openhands/python"},
            "degraded": None,
            "workspace": {"workspace_id": "ws_demo_1234", "state": "active"},
            "timeline": timeline,
            "tests": tests,
            "diff": _INJECTED_DIFF,
        },
    }


def test_payload_is_actually_over_the_ceiling():
    """The premise: this payload really exceeds MAX_PAYLOAD_CHARS."""
    assert len(te._canonical(_big_payload())) > te.MAX_PAYLOAD_CHARS


def test_bounded_payload_keeps_codeplane_evidence():
    """P0-A — timeline / diff / tests survive; truncation is flagged verbatim."""
    bounded, truncated = te._bound_payload(_big_payload())
    cp = bounded["codeplane"]
    assert truncated is True
    assert cp["timeline"]  # non-empty
    assert cp["diff"] == _INJECTED_DIFF  # kept verbatim (within the diff cap)
    assert cp["tests"]["measured"] is True
    assert cp["timeline_omitted"] == 60 - te.BOUND_CODE_TIMELINE_MAX_ITEMS
    assert cp["tests"]["raw_stdout_truncated"] is True
    assert cp["truncated"] is True


def test_assembler_reads_the_bounded_payload():
    """The bounded payload still assembles into a non-empty ``codeplane``."""
    bounded, _ = te._bound_payload(_big_payload())
    assembled = orch._assemble_codeplane(
        [{"tool": "code.execute", "payload": bounded}], []
    )
    assert assembled["timeline"]
    assert assembled["diff"] == _INJECTED_DIFF
    assert assembled["tests"]["measured"] is True


def test_non_code_payload_keeps_honest_envelope():
    """A non-code payload is unchanged: it still becomes the honest envelope."""
    other = {"ok": True, "rows": ["y" * 500] * 30}
    bounded, truncated = te._bound_payload(other)
    assert truncated is True
    assert bounded["truncated"] is True
    assert "preview" in bounded
    assert "codeplane" not in bounded


def test_counterfactual_old_wholesale_replace_erases_codeplane():
    """P0-A counterfactual — the old implementation erases the codeplane evidence."""

    def old_bound_payload(value):
        text = te._canonical(value)
        if len(text) <= te.MAX_PAYLOAD_CHARS:
            return value, False
        return {
            "truncated": True,
            "original_length": len(text),
            "preview": text[: te.MAX_PAYLOAD_CHARS],
        }, True

    old_bounded, _ = old_bound_payload(_big_payload())
    assert old_bounded.get("codeplane") is None  # erased
    assembled = orch._assemble_codeplane(
        [{"tool": "code.execute", "payload": old_bounded}], []
    )
    assert not assembled.get("timeline")
    assert not assembled.get("diff")
