"""INC44 T02 — text / code file editing layered pins (offline; no model, no PG).

Nails the three hard requirements of the ``textfile.*`` plane (design §1.3):

  1. **Layering** — ``resolve_intent`` (LLM) never writes; ``apply_edits`` (Tool)
     is the only writer.
  2. **Byte conventions preserved** — the rewrite keeps the original **EOL style**
     (LF / CRLF / CR), **encoding** (UTF-8(BOM) / GB18030) and **BOM presence**;
     the four ops (``replace_text`` / ``replace_lines`` / ``insert_after`` /
     ``delete_match``) are exact.
  3. **Recomputable diff + honest verification** — ``compute_diff`` is line-level
     LCS (shared ``textdiff``); ``verify_textfile`` is tri-state, and the optional
     "still compilable Python" check runs ``ast.parse`` *only* when requested.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from forgeflow.documents import (
    TextEditOp,
    UnknownEditOpError,
    apply_textfile_edits,
    compute_textfile_diff,
    inspect_textfile,
    resolve_textfile_intent,
    verify_textfile,
)

pytestmark = pytest.mark.asyncio


class _FakeModel:
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


# --------------------------------------------------------------------------- #
# 1. Layering                                                                  #
# --------------------------------------------------------------------------- #
async def test_apply_edits_really_changes_bytes():
    src = "第一行\n第二行\n".encode("utf-8")
    new, changes = apply_textfile_edits(
        src, [{"op": "replace_text", "match": "第二行", "replace": "改动行"}]
    )
    assert changes == 1
    assert hashlib.sha256(new).hexdigest() != hashlib.sha256(src).hexdigest()
    assert new.decode("utf-8").splitlines() == ["第一行", "改动行"]


async def test_resolve_intent_never_writes_bytes(monkeypatch):
    data = b"value = 40\n"
    structure = inspect_textfile(data)
    fingerprint = hashlib.sha256(data).hexdigest()
    fake = _FakeModel('{"edits":[{"op":"replace_text","match":"40","replace":"38"}]}')
    monkeypatch.setattr("forgeflow.models.provider.get_model", lambda *a, **k: fake)

    ops = await resolve_textfile_intent(data, "把 40 改成 38", structure)

    assert ops == [TextEditOp(op="replace_text", match="40", replace="38")]
    assert fake.calls == 1
    assert hashlib.sha256(data).hexdigest() == fingerprint


async def test_resolve_intent_returns_none_when_model_unavailable(no_model):
    data = b"x = 1\n"
    assert await resolve_textfile_intent(data, "改一下", inspect_textfile(data)) is None


async def test_resolve_intent_returns_none_on_unusable_reply(monkeypatch):
    data = b"x = 1\n"
    structure = inspect_textfile(data)
    monkeypatch.setattr(
        "forgeflow.models.provider.get_model", lambda *a, **k: _FakeModel("not json")
    )
    assert await resolve_textfile_intent(data, "改一下", structure) is None
    monkeypatch.setattr(
        "forgeflow.models.provider.get_model",
        lambda *a, **k: _FakeModel('{"edits":[{"op":"nope"}]}'),
    )
    assert await resolve_textfile_intent(data, "改一下", structure) is None


async def test_unknown_edit_op_is_refused():
    for bad in (
        {"op": "run"},
        {"op": "replace_text", "replace": "x"},  # no match
        {"op": "insert_after", "lines": ["x"]},  # no match
        {"op": "replace_lines", "lines": ["x"]},  # no start
    ):
        with pytest.raises(UnknownEditOpError):
            TextEditOp.from_dict(bad)


# --------------------------------------------------------------------------- #
# 2. Byte conventions preserved + all four ops                                 #
# --------------------------------------------------------------------------- #
async def test_crlf_eol_is_preserved():
    src = "第一行\r\n第二行\r\n".encode("utf-8")
    new, _ = apply_textfile_edits(
        src, [{"op": "replace_text", "match": "第二行", "replace": "改动行"}]
    )
    assert b"\r\n" in new
    assert b"\n" not in new.replace(b"\r\n", b"")  # no bare LF smuggled in
    assert new.decode("utf-8").splitlines() == ["第一行", "改动行"]


async def test_cr_and_lf_eol_are_preserved():
    cr = "a\rb\r".encode("utf-8")
    new_cr, _ = apply_textfile_edits(cr, [{"op": "replace_text", "match": "b", "replace": "B"}])
    assert new_cr == "a\rB\r".encode("utf-8")

    lf = "a\nb\n".encode("utf-8")
    new_lf, _ = apply_textfile_edits(lf, [{"op": "replace_text", "match": "b", "replace": "B"}])
    assert new_lf == "a\nB\n".encode("utf-8")


async def test_utf8_bom_is_preserved():
    src = b"\xef\xbb\xbf" + "行一\n行二\n".encode("utf-8")
    new, _ = apply_textfile_edits(
        src, [{"op": "replace_text", "match": "行二", "replace": "行三"}]
    )
    assert new.startswith(b"\xef\xbb\xbf")
    assert inspect_textfile(new).has_bom is True


async def test_gb18030_encoding_is_preserved():
    src = "中文内容\n".encode("gb18030")
    assert inspect_textfile(src).encoding == "gb18030"
    new, _ = apply_textfile_edits(
        src, [{"op": "replace_text", "match": "中文", "replace": "英文"}]
    )
    assert new.decode("gb18030") == "英文内容\n"


async def test_replace_lines_insert_after_and_delete_match():
    src = "a\nb\nc\n".encode("utf-8")

    rep, n_rep = apply_textfile_edits(
        src, [{"op": "replace_lines", "start": 1, "end": 2, "lines": ["B"]}]
    )
    assert n_rep == 1
    assert rep.decode().splitlines() == ["a", "B", "c"]

    ins, n_ins = apply_textfile_edits(
        src, [{"op": "insert_after", "match": "a", "lines": ["x", "y"]}]
    )
    assert n_ins == 2
    assert ins.decode().splitlines() == ["a", "x", "y", "b", "c"]

    dele, n_del = apply_textfile_edits(src, [{"op": "delete_match", "match": "b"}])
    assert n_del == 1
    assert dele.decode().splitlines() == ["a", "c"]


async def test_no_op_edit_changes_nothing():
    src = "a\nb\n".encode("utf-8")
    new, changes = apply_textfile_edits(src, [{"op": "delete_match", "match": "zzz"}])
    assert changes == 0
    assert new == src


# --------------------------------------------------------------------------- #
# 3. Diff is recomputable                                                      #
# --------------------------------------------------------------------------- #
async def test_compute_diff_counts_modified_added_removed():
    old = "a\nb\nc\n".encode("utf-8")
    rep, _ = apply_textfile_edits(
        old, [{"op": "replace_lines", "start": 1, "end": 2, "lines": ["B"]}]
    )
    report = compute_textfile_diff(old, rep)
    assert report.modified == 1 and report.added == 0 and report.removed == 0

    dele, _ = apply_textfile_edits(old, [{"op": "delete_match", "match": "b"}])
    r2 = compute_textfile_diff(old, dele)
    assert r2.removed == 1 and r2.modified == 0 and r2.added == 0

    ins, _ = apply_textfile_edits(old, [{"op": "insert_after", "match": "a", "lines": ["x"]}])
    r3 = compute_textfile_diff(old, ins)
    assert r3.added == 1 and r3.modified == 0 and r3.removed == 0


async def test_compute_diff_is_eol_agnostic_and_empty_for_identical():
    data = "a\r\nb\r\n".encode("utf-8")
    assert compute_textfile_diff(data, data).to_dict() == {
        "modified": 0,
        "added": 0,
        "removed": 0,
        "numeric_changes": 0,
    }
    # Re-encoding the same text with a different EOL is byte-different but
    # semantically identical ⇒ zero changed lines.
    crlf = "a\r\nb\r\n".encode("utf-8")
    lf = "a\nb\n".encode("utf-8")
    assert compute_textfile_diff(crlf, lf).modified == 0


async def test_compute_diff_numeric_only_change():
    old = "value = 40\n".encode("utf-8")
    new, _ = apply_textfile_edits(old, [{"op": "replace_text", "match": "40", "replace": "38"}])
    report = compute_textfile_diff(old, new)
    assert report.modified == 1 and report.numeric_changes == 1


# --------------------------------------------------------------------------- #
# 4. Inspection + honest tri-state verification                                #
# --------------------------------------------------------------------------- #
async def test_inspect_textfile_reports_measured_structure():
    src = b"first\nsecond\nthird\n"
    structure = inspect_textfile(src)
    assert structure.lines == 3
    assert structure.chars == len("first\nsecond\nthird\n")
    # The shared honest decoder tries ``utf-8-sig`` first (a BOM-less ASCII file
    # decodes under it too), then ``utf-8``.
    assert structure.encoding in ("utf-8", "utf-8-sig")
    assert structure.eol == "lf"
    assert structure.has_bom is False
    assert structure.preview[0] == "first"


async def test_inspect_textfile_lossy_flag_matches_the_honest_decoder():
    from forgeflow.resources import summaries

    raw = b"\xff\xfe\xff\xfe"
    _text, encoding = summaries._decode(raw)
    structure = inspect_textfile(raw)
    # The inspect plane reuses the resource seam's single honest decoder.
    assert structure.encoding == encoding
    assert structure.lossy_decode == (encoding == "latin-1(replace)")


async def test_verify_textfile_compilable_python_is_optional():
    good = b"x = 1\n"
    assert verify_textfile(good, {"expect_compilable_python": True}).structure_ok is True
    bad = b"def (:\n"
    assert verify_textfile(bad, {"expect_compilable_python": True}).structure_ok is False
    # No constraint supplied ⇒ the dimension stays unmeasured (None).
    assert verify_textfile(b"hello", {}).structure_ok is None


async def test_verify_textfile_must_contain_and_char_limits():
    assert verify_textfile(b"hello world", {"must_contain": ["hello"]}).data_ok is True
    assert verify_textfile(b"hello world", {"must_contain": ["nope"]}).data_ok is False
    assert verify_textfile(b"hi", {"max_chars": 1}).requirement_ok is False
    assert verify_textfile(b"hi", {"min_chars": 100}).requirement_ok is False


async def test_verify_textfile_unmeasured_dimensions_are_none():
    report = verify_textfile(b"anything", {})
    assert report.openable is True
    assert report.structure_ok is None
    assert report.data_ok is None
    assert report.requirement_ok is None
