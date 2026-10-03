"""INC45 T01 — XLSX edit-plane layered pins (offline; no model, no PG).

Nails the three hard requirements of the ``sheet.*`` domain (design §1.1 / §8):

  1. **Layering** — ``resolve_intent`` (LLM) never writes; ``apply_edits`` (Tool)
     is the only writer.
  2. **Recomputable diff + honest verification** — ``compute_diff`` is cell-level
     LCS (shared ``textdiff``); ``verify_sheet`` is tri-state (unmeasured ⇒ None).
  3. **Honest degradation** — a missing ``openpyxl`` extra raises
     ``SheetInspectionError`` with a verbatim reason; nothing is fabricated.
"""

from __future__ import annotations

import hashlib
import io
import sys
from types import SimpleNamespace

import pytest

from forgeflow.documents import (
    SheetEditOp,
    SheetInspectionError,
    UnknownEditOpError,
    apply_sheet_edits,
    compute_sheet_diff,
    inspect_sheet,
    resolve_sheet_intent,
    verify_sheet,
)

# NOTE: no module-level ``pytest.mark.asyncio`` here on purpose — this module mixes
# sync domain pins (pure-function checks on the sheet editor) with ``async`` handler
# tests. ``pyproject.toml`` sets ``asyncio_mode = "auto"``, so the async tests are
# collected without an explicit mark, and the sync ones stay unmarked (a blanket
# mark would raise a PytestWarning on each sync test).


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
def _build_workbook(value_b2: object = 3) -> bytes:
    """A tiny two-column workbook: header row + one data row."""
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet["A1"] = "name"
    sheet["B1"] = "qty"
    sheet["A2"] = "apple"
    sheet["B2"] = value_b2
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# Built at import time so the "missing openpyxl" pins still have a valid sample
# to feed the code under test (they hide ``openpyxl`` *after* this is built).
_SAMPLE_XLSX = _build_workbook()


def _workbook_bytes(*, value_b2: object = 3) -> bytes:
    """A tiny two-column workbook: header row + one data row."""
    return _build_workbook(value_b2)


class _FakeModel:
    """A deterministic chat-model stand-in (returns a fixed JSON intent)."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls = 0

    async def ainvoke(self, _messages):
        self.calls += 1
        return SimpleNamespace(content=self._content)


@pytest.fixture
def no_model(monkeypatch):
    import forgeflow.models.provider as provider

    monkeypatch.setattr(provider, "get_model", lambda *a, **k: None)


@pytest.fixture
def hide_openpyxl(monkeypatch):
    """Make a lazy ``import openpyxl`` fail (simulates a missing ``xlsx`` extra)."""
    monkeypatch.setitem(sys.modules, "openpyxl", None)


# --------------------------------------------------------------------------- #
# 1. Layering                                                                  #
# --------------------------------------------------------------------------- #
async def test_resolve_intent_never_writes_bytes(monkeypatch):
    data = _workbook_bytes()
    structure = inspect_sheet(data)
    before = hashlib.sha256(data).hexdigest()

    model = _FakeModel('{"edits":[{"op":"set_cell","cell":"B2","value":5}]}')
    import forgeflow.models.provider as provider

    monkeypatch.setattr(provider, "get_model", lambda *a, **k: model)

    ops = await resolve_sheet_intent(data, "把 B2 改成 5", structure)

    assert ops is not None and len(ops) == 1
    assert ops[0].op == "set_cell" and ops[0].cell == "B2" and ops[0].value == 5
    # The LLM layer is provably side-effect free.
    assert hashlib.sha256(data).hexdigest() == before
    assert model.calls == 1


async def test_resolve_intent_returns_none_without_model(no_model):
    data = _workbook_bytes()
    assert await resolve_sheet_intent(data, "随便改改", inspect_sheet(data)) is None


async def test_resolve_intent_rejects_unknown_op(monkeypatch):
    data = _workbook_bytes()
    model = _FakeModel('{"edits":[{"op":"delete_sheet","cell":"A1"}]}')
    import forgeflow.models.provider as provider

    monkeypatch.setattr(provider, "get_model", lambda *a, **k: model)
    assert await resolve_sheet_intent(data, "删掉表", inspect_sheet(data)) is None


def test_apply_edits_really_changes_bytes():
    src = _workbook_bytes()
    original_sha = hashlib.sha256(src).hexdigest()
    new, changes = apply_sheet_edits(
        src, [{"op": "set_cell", "cell": "B2", "value": 5}]
    )
    assert changes == 1
    assert new != src
    # The input byte string is never mutated in place (byte-identical after the
    # call) — compared against its OWN pre-call snapshot, not a second
    # ``_workbook_bytes()``: an ``openpyxl`` save embeds a DOS timestamp, so two
    # independent generations are NOT byte-equal (a flaky assertion, not a
    # product fact).
    assert hashlib.sha256(src).hexdigest() == original_sha
    # The edit landed in the NEW bytes only.
    from forgeflow.documents.sheet_inspect import open_workbook

    assert open_workbook(src).active["B2"].value == 3
    assert open_workbook(new).active["B2"].value == 5


def test_apply_edits_set_cell_is_exact_and_idempotent():
    src = _workbook_bytes()
    once, n1 = apply_sheet_edits(src, [{"op": "set_cell", "cell": "B2", "value": 9}])
    assert n1 == 1
    twice, n2 = apply_sheet_edits(once, [{"op": "set_cell", "cell": "B2", "value": 9}])
    assert n2 == 0  # already that value ⇒ no change confessed
    assert inspect_sheet(once).cells == inspect_sheet(src).cells


def test_apply_edits_replace_text_across_sheet():
    src = _workbook_bytes()
    new, changes = apply_sheet_edits(
        src, [{"op": "replace_text", "match": "apple", "replace": "pear"}]
    )
    assert changes == 1
    from forgeflow.documents.sheet_inspect import open_workbook

    values = [c.value for row in open_workbook(new).active.iter_rows() for c in row]
    assert "pear" in values and "apple" not in values


def test_apply_edits_targets_named_sheet_and_refuses_missing():
    src = _workbook_bytes()
    with pytest.raises(SheetInspectionError):
        apply_sheet_edits(src, [{"op": "set_cell", "sheet": "Nope", "cell": "A1", "value": 1}])


def test_unknown_op_and_missing_params_are_refused():
    with pytest.raises(UnknownEditOpError):
        SheetEditOp(op="frobnicate")
    with pytest.raises(UnknownEditOpError):
        SheetEditOp(op="set_cell", cell="")  # missing cell
    with pytest.raises(UnknownEditOpError):
        SheetEditOp(op="replace_text", match="")  # missing match


def test_op_from_dict_accepts_value_and_replace_alias():
    a = SheetEditOp.from_dict({"op": "set_cell", "cell": "A1", "value": "x"})
    b = SheetEditOp.from_dict({"op": "set_cell", "cell": "A1", "replace": "x"})
    assert a.value == "x" and b.value == "x"


# --------------------------------------------------------------------------- #
# 2. Recomputable diff + honest verification                                   #
# --------------------------------------------------------------------------- #
def test_compute_diff_is_recomputable():
    src = _workbook_bytes(value_b2=3)
    new, _ = apply_sheet_edits(src, [{"op": "set_cell", "cell": "B2", "value": 5}])
    report = compute_sheet_diff(src, new)
    assert report.modified == 1
    assert report.added == 0 and report.removed == 0
    assert report.numeric_changes == 1  # 3 → 5
    # Recompute ⇒ identical figures (pure function).
    assert compute_sheet_diff(src, new).to_dict() == report.to_dict()


def test_verify_sheet_is_tri_state():
    src = _workbook_bytes()
    # No constraints ⇒ every dimension unmeasured (None), never a fake pass.
    bare = verify_sheet(src)
    assert bare.openable is True
    assert bare.structure_ok is None and bare.data_ok is None and bare.requirement_ok is None

    good = verify_sheet(src, {"expected_sheets": 1, "max_chars": 1000})
    assert good.structure_ok is True and good.requirement_ok is True
    assert good.data_ok is None  # not measured ⇒ None

    bad = verify_sheet(src, {"expected_sheets": 7, "min_chars": 100000})
    assert bad.structure_ok is False and bad.requirement_ok is False


def test_verify_sheet_marks_unopenable():
    report = verify_sheet(b"not a workbook")
    assert report.openable is False
    assert report.structure_ok is None and report.data_ok is None


# --------------------------------------------------------------------------- #
# 3. Honest degradation                                                        #
# --------------------------------------------------------------------------- #
def test_missing_openpyxl_degrades_with_verbatim_reason(hide_openpyxl):
    with pytest.raises(SheetInspectionError) as exc:
        inspect_sheet(_SAMPLE_XLSX)
    assert "xlsx 支持不可用" in str(exc.value)


def test_missing_openpyxl_makes_edit_refuse_not_fake(hide_openpyxl):
    with pytest.raises(SheetInspectionError):
        apply_sheet_edits(_SAMPLE_XLSX, [{"op": "set_cell", "cell": "A1", "value": 1}])


def test_empty_bytes_are_refused_honestly():
    with pytest.raises(SheetInspectionError):
        inspect_sheet(b"")
