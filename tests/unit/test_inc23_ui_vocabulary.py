"""INC23 N1 — cross-layer vocabulary pin: ``extractor._OUTCOME_LABELS`` ⇄ ``realRun.outcomeMeta``.

Why this file exists
--------------------
``forgeflow/experience/extractor.py`` promises (in its own docstring) that its
display vocabulary 「必须与前端 ``frontend/src/views/runs/realRun.ts::outcomeMeta``
严格一致（同源，勿另造第二套）」. Nothing enforced that promise: change one side and
the platform would confidently render two different Chinese labels for the same
run ``outcome``, silently. That is this repo's recurring failure mode — a promise
written in a comment with no alarm wired to it.

This test parses **both real source files** (never a hand-copied duplicate) and
pins the two vocabularies equal:

* **Python side** — ``ast`` reads the ``_OUTCOME_LABELS`` dict literal straight
  out of the ``.py`` source, and extracts the real ``_outcome_label`` function to
  exercise it. The module is deliberately **not imported**: importing
  ``extractor`` would drag the repository / embedding stack in for what is a pure
  text check, and a hand-transcribed copy would itself become a second source of
  truth (exactly the drift we are pinning). The function is instead compiled from
  its own source segment, so its *behavioural* fallback is pinned — not merely the
  word ``default``.
* **TypeScript side** — the ``outcomeMeta`` function text is read out of
  ``realRun.ts`` and its ``case '<outcome>': return { label: '…' }`` arms are
  parsed. The ``default`` arm is asserted to return ``outcome ?? ''`` (and to
  hard-code none of the vocabulary labels), so an unknown ``outcome`` renders
  verbatim and a ``null`` renders ``''``.

Beyond the label map, the two sides are pinned on the **None ⇄ null boundary**:
``_outcome_label(None) == ""`` (behavioural, and the result must be a ``str``) pairs
with the TS ``default`` arm's ``outcome ?? ''`` — the same boundary on both layers
(this is the divergence INC23 E2 removes; before it the Python fallback used the raw
parameter and leaked ``None`` while the TS side rendered ``''``).

The pin is discriminating: editing **either** side alone turns the equality red
(the INC23 delivery report records the live counterfactual injection).

Citation discipline: this file uses ``file::symbol`` anchors (never ``file:line``)
— see ``tests/unit/test_inc22_citation_drift.py`` for why.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

# ForgeFlow-main/  ← tests/unit/test_inc23_ui_vocabulary.py
REPO_ROOT = Path(__file__).resolve().parents[2]
_PY_EXTRACTOR = REPO_ROOT / "forgeflow/experience/extractor.py"
_TS_REALRUN = REPO_ROOT / "frontend/src/views/runs/realRun.ts"

#: The Python constant / function this pin is about.
_LABELS_NAME = "_OUTCOME_LABELS"
_FN_NAME = "_outcome_label"

#: The ONLY TypeScript arm shape the vocabulary is read from —
#: ``case '<outcome>': return { label: '<中文>', … }``. A bare ``label:`` elsewhere
#: in the file (a comment, another function) is deliberately not matched.
_TS_CASE_RE = re.compile(r"case\s+'([^']*)'\s*:\s*return\s*\{\s*label:\s*'([^']*)'")

#: The ``default`` arm must return its own parameter unchanged (identity). The
#: ``?? ''`` tail matches the real source; it is the identity for every non-null
#: outcome, which is exactly the "unknown value renders verbatim" contract.
_TS_DEFAULT_RE = re.compile(r"default\s*:\s*return\s*\{\s*label:\s*outcome\s*\?\?\s*''")

#: The outcome keys the runtime really emits (INC18/INC20/INC14) — used as a
#: non-vacuity control so a parser that silently reads an empty map is caught.
_EXPECTED_KEYS = {"success", "partial", "failure", "aborted"}


def _read(path: Path) -> str:
    """The file's UTF-8 source text (the sole input to every check below)."""
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Python side — parsed from the .py SOURCE (no module import)                    #
# --------------------------------------------------------------------------- #
def _py_labels_dict(src: str) -> dict[str, str]:
    """The real ``_OUTCOME_LABELS`` dict literal, evaluated from the source AST.

    Reads a module-level ``_OUTCOME_LABELS = {…}`` (plain or annotated assignment)
    whose value is a dict literal, and returns ``ast.literal_eval`` of it. Raises
    loudly (rather than returning ``{}``) when the constant cannot be found, so a
    moved / renamed literal can never pass as an "equal empty map".
    """
    tree = ast.parse(src)
    for node in tree.body:
        target: str | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        if target == _LABELS_NAME and isinstance(value, ast.Dict):
            parsed = ast.literal_eval(value)
            assert isinstance(parsed, dict), f"{_LABELS_NAME} literal is not a dict: {parsed!r}"
            return {str(k): str(v) for k, v in parsed.items()}
    raise AssertionError(
        f"{_LABELS_NAME} dict literal not found in {_PY_EXTRACTOR.name}; "
        "the vocabulary moved — update this pin"
    )


def _py_label_fn(src: str):
    """Compile ``_outcome_label`` from its own source segment (still no import).

    The ``_OUTCOME_LABELS`` assignment segment and the function segment are the
    only two pieces exec'd, into a throwaway namespace. This gives real
    behavioural coverage of the fallback path without importing ``extractor`` (no
    repository / embedding import side effects).
    """
    tree = ast.parse(src)
    segments: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if _LABELS_NAME in names:
                segments.append(ast.get_source_segment(src, node) or "")
        elif isinstance(node, ast.FunctionDef) and node.name == _FN_NAME:
            segments.append(ast.get_source_segment(src, node) or "")
    assert len(segments) == 2, (
        f"expected exactly the {_LABELS_NAME} literal and the {_FN_NAME} definition "
        f"as module-level nodes, found {len(segments)}"
    )
    namespace: dict = {}
    # The exec'd text is our own repo source, read solely for this pin.
    exec("\n\n".join(segments), namespace)  # noqa: S102
    fn = namespace.get(_FN_NAME)
    assert callable(fn), f"{_FN_NAME} was not defined by its source segment"
    return fn


# --------------------------------------------------------------------------- #
# TypeScript side — parsed from the .ts SOURCE text                              #
# --------------------------------------------------------------------------- #
def _ts_outcome_meta_region(src: str) -> str:
    """The verbatim ``outcomeMeta`` function text (from ``export function`` to its
    column-0 closing brace).

    Boundary chosen so the vocabulary is read from that function **only** — not
    from ``statusLabel`` / ``runStatusMeta``, which carry their own unrelated
    Chinese words.
    """
    lines = src.split("\n")
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith("export function outcomeMeta"))
    except StopIteration:  # pragma: no cover — only if the function is renamed
        raise AssertionError("outcomeMeta not found in realRun.ts")
    for i in range(start + 1, len(lines)):
        if lines[i] == "}":  # a top-level (unindented) closing brace
            return "\n".join(lines[start : i + 1])
    raise AssertionError("outcomeMeta function is not terminated at column 0")


def _ts_labels_dict(region: str) -> dict[str, str]:
    """Parse the ``case '<outcome>': return { label: '…' }`` arms of ``region``."""
    return {m.group(1): m.group(2) for m in _TS_CASE_RE.finditer(region)}


# --------------------------------------------------------------------------- #
# 1. Non-vacuity: each parser really reads a non-empty real vocabulary           #
# --------------------------------------------------------------------------- #
def test_python_label_map_is_parsed_from_the_source_literal():
    """Positive control — the AST parser returns the real, populated map."""
    labels = _py_labels_dict(_read(_PY_EXTRACTOR))
    assert labels, "parsed an EMPTY _OUTCOME_LABELS (parser vacuous / literal moved)"
    assert _EXPECTED_KEYS <= set(labels), f"missing outcome keys: {_EXPECTED_KEYS - set(labels)}"


def test_typescript_label_map_is_parsed_from_the_case_arms():
    """Positive control — the ``case`` parser returns the real, populated map."""
    labels = _ts_labels_dict(_ts_outcome_meta_region(_read(_TS_REALRUN)))
    assert labels, "parsed an EMPTY map from outcomeMeta (case parser vacuous)"
    assert _EXPECTED_KEYS <= set(labels), f"missing outcome keys: {_EXPECTED_KEYS - set(labels)}"


# --------------------------------------------------------------------------- #
# 2. The pin — the two vocabularies are identical                                #
# --------------------------------------------------------------------------- #
def test_outcome_vocabulary_is_identical_across_the_two_layers():
    """(outcome → label) must be identical on the Python and TS sides.

    Keys **and** values must agree: a change to one side alone makes this red.
    """
    py_labels = _py_labels_dict(_read(_PY_EXTRACTOR))
    ts_labels = _ts_labels_dict(_ts_outcome_meta_region(_read(_TS_REALRUN)))

    assert set(py_labels) == set(ts_labels), (
        "outcome key sets diverge across layers\n"
        f"  extractor._OUTCOME_LABELS keys : {sorted(py_labels)}\n"
        f"  realRun.outcomeMeta     keys   : {sorted(ts_labels)}"
    )
    assert py_labels == ts_labels, (
        "outcome vocabulary diverges across layers (same key, different label):\n"
        f"  extractor._OUTCOME_LABELS : {py_labels}\n"
        f"  realRun.outcomeMeta       : {ts_labels}"
    )


# --------------------------------------------------------------------------- #
# 3. Unknown / None outcomes fall back on BOTH sides (the None ⇄ null boundary)  #
# --------------------------------------------------------------------------- #
def test_python_unknown_outcome_falls_back_to_the_original_value():
    """Behavioural: the real ``_outcome_label`` returns an unknown value verbatim."""
    fn = _py_label_fn(_read(_PY_EXTRACTOR))
    labels = _py_labels_dict(_read(_PY_EXTRACTOR))

    # Known keys map through (behavioural, from the compiled real function).
    for key, label in labels.items():
        assert fn(key) == label, f"{key!r} -> {fn(key)!r}, expected {label!r}"
    # Unknown / empty values are returned unchanged — no invented business name.
    assert fn("weird") == "weird"
    assert fn("") == ""


def test_python_none_outcome_falls_back_to_the_empty_string():
    """None is the boundary where the two layers must agree: ``None`` → ``""``.

    The real ``_outcome_label`` (compiled from source) must return a ``str`` for a
    ``None`` input — it must never leak ``None`` — because the TS ``default`` arm
    (``{ label: outcome ?? '' }``) renders ``null`` as ``''`` (see
    ``test_typescript_default_branch_returns_the_original_value``). This pairs the
    Python ``None`` with the TypeScript ``null`` on the **same** boundary, which is
    exactly the divergence INC23 E2 removes.
    """
    fn = _py_label_fn(_read(_PY_EXTRACTOR))
    result = fn(None)
    assert isinstance(result, str), f"_outcome_label(None) leaked a non-str: {result!r}"
    assert result == ""


def test_typescript_default_branch_returns_the_original_value():
    """Structural: ``outcomeMeta``'s ``default`` arm returns ``outcome ?? ''``.

    This is the TS half of the **None / null boundary** evidence: the ``?? ''``
    tail means a ``null`` outcome renders ``''`` — pairing with the Python
    ``_outcome_label(None) == ""`` assertion above
    (``test_python_none_outcome_falls_back_to_the_empty_string``). It is a
    structural check on the arm's *return expression* (not word-presence): the
    expression is pinned to the ``outcome`` parameter and asserted to hard-code
    none of the vocabulary labels, so an unknown outcome can never silently render
    a fabricated label.
    """
    region = _ts_outcome_meta_region(_read(_TS_REALRUN))
    labels = _ts_labels_dict(region)

    default_idx = region.find("default:")
    assert default_idx >= 0, "outcomeMeta has no default arm (unknown values would render undefined)"
    default_arm = region[default_idx:]

    assert _TS_DEFAULT_RE.search(default_arm), (
        "outcomeMeta default arm must return `outcome ?? ''` (so null renders '' and an "
        "unknown outcome renders verbatim), "
        f"found: {default_arm.strip()!r}"
    )
    leaked = [lab for lab in labels.values() if lab in default_arm]
    assert leaked == [], f"default arm hard-codes a vocabulary label instead of the input: {leaked}"


# --------------------------------------------------------------------------- #
# 4. The two parsers REACT to a mutated vocabulary (so the pin cannot pass by    #
#    reading nothing / the wrong text)                                          #
# --------------------------------------------------------------------------- #
def test_positive_control_python_parser_detects_a_drifted_label():
    real = _read(_PY_EXTRACTOR)
    drifted = real.replace('"success": "已完成"', '"success": "大功告成"', 1)
    assert drifted != real, "fixture drift: the extractor '成功' arm moved (update this control)"
    assert _py_labels_dict(drifted)["success"] == "大功告成"


def test_positive_control_ts_parser_detects_a_drifted_case():
    real = _read(_TS_REALRUN)
    region = _ts_outcome_meta_region(real)
    drifted = region.replace("label: '已完成'", "label: '大功告成'", 1)
    assert drifted != region, "fixture drift: the realRun '成功' arm moved (update this control)"
    assert _ts_labels_dict(drifted)["success"] == "大功告成"
