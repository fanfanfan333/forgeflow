"""INC45 T04 — mixed-declaration execution-plane priority (D8).

When a task declares inputs for several planes at once, execution-plane priority
is **unique**: only the highest-priority plane runs, and every other declared input
is stated as unhandled in the winning plane's injection note — never silently
dropped (design §1.6 / §7 T04).
"""

from __future__ import annotations

import pytest

from forgeflow.runtime import orchestrator as orch
from forgeflow.runtime.orchestrator import RequestContext, TaskCreate

pytestmark = pytest.mark.asyncio

_CTX = RequestContext(tenant_id="t-inc45", user_id="u", role="manager")


async def test_csv_plus_xlsx_has_a_single_sheet_winner():
    task = TaskCreate(
        intent="混合处理", context={"paths": ["/srv/a/leads.csv", "/srv/a/book.xlsx"]}
    )
    assert orch._is_sheet_task(task, _CTX) is True
    assert orch._is_analysis_task(task, _CTX) is False

    candidates = orch._candidates_for(task, _CTX)
    tools = [c["tool"] for c in candidates]
    # The winning plane is the ONLY one planned — no analysis step.
    assert "analysis.profile" not in tools
    assert "sheet.inspect" in tools and "sheet.edit" in tools


async def test_the_unhandled_input_is_named_in_the_note():
    task = TaskCreate(
        intent="混合处理", context={"paths": ["/srv/a/leads.csv", "/srv/a/book.xlsx"]}
    )
    assert orch._unhandled_inputs(task, _CTX) == ["leads.csv"]

    notes = " ".join(c.get("note", "") for c in orch._candidates_for(task, _CTX))
    assert "未处理" in notes
    assert "leads.csv" in notes


async def test_pure_sheet_declaration_has_no_unhandled_note():
    task = TaskCreate(intent="改表格", context={"paths": ["/srv/a/book.xlsx"]})
    assert orch._unhandled_inputs(task, _CTX) == []
    notes = " ".join(c.get("note", "") for c in orch._candidates_for(task, _CTX))
    assert "未处理" not in notes


async def test_analysis_winner_has_no_lower_plane():
    # A bare CSV path is not an analysis signal, but a declared analysis tool is.
    task = TaskCreate(
        intent="分析", context={"declared_tools": ["analysis.profile"], "paths": ["/srv/a/leads.csv"]}
    )
    assert orch._is_analysis_task(task, _CTX) is True
    assert orch._unhandled_inputs(task, _CTX) == []
