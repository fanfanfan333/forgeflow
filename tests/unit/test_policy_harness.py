"""Phase 9 B6 — approval-policy eval harness (second frozen dataset)."""

from __future__ import annotations

import json

import pytest

from forgeflow.evaluation.policy_harness import (
    DEFAULT_DATASET,
    DEFAULT_MANIFEST,
    PolicyManifest,
    default_predict,
    freeze_manifest,
    load_policy_cases,
    score_policy_cases,
    verify_manifest,
)


def test_policy_dataset_scores_perfect_on_default_policy() -> None:
    cases = load_policy_cases(DEFAULT_DATASET)
    assert len(cases) == 10
    result = score_policy_cases(cases, {c.id: default_predict(c.tool) for c in cases})
    assert result["gate"] == "pass"
    assert result["accuracy"] == 1.0


def test_policy_manifest_matches_frozen_file() -> None:
    cases = load_policy_cases(DEFAULT_DATASET)
    manifest = PolicyManifest.from_dict(json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8")))
    verify_manifest(manifest, DEFAULT_DATASET, cases)
    assert manifest.version == "approval-v1"


def test_policy_manifest_accepts_crlf_checkout(tmp_path) -> None:
    """A Windows autocrlf checkout must still match the LF manifest."""
    cases = load_policy_cases(DEFAULT_DATASET)
    manifest = freeze_manifest(DEFAULT_DATASET)
    crlf = tmp_path / "crlf.json"
    normalized = DEFAULT_DATASET.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    crlf.write_bytes(normalized.replace(b"\n", b"\r\n"))
    verify_manifest(manifest, crlf, load_policy_cases(crlf))
    assert load_policy_cases(crlf)[0].id == cases[0].id


def test_policy_manifest_catches_drift() -> None:
    cases = load_policy_cases(DEFAULT_DATASET)
    manifest = freeze_manifest(DEFAULT_DATASET)
    tampered = [c for c in cases]
    tampered[0] = tampered[0].__class__(
        id=tampered[0].id, tool=tampered[0].tool,
        expected_gated=not tampered[0].expected_gated, rationale=tampered[0].rationale,
    )
    with pytest.raises(ValueError, match="gated distribution mismatch"):
        verify_manifest(manifest, DEFAULT_DATASET, tampered)


def test_policy_scoring_names_false_positives_and_negatives() -> None:
    cases = load_policy_cases(DEFAULT_DATASET)
    predictions = {c.id: default_predict(c.tool) for c in cases}
    predictions["pol-01"] = False  # gated tool predicted open: false negative
    predictions["pol-06"] = True  # open tool predicted gated: false positive
    result = score_policy_cases(cases, predictions)
    assert result["false_negatives"] == ["pol-01"]
    assert result["false_positives"] == ["pol-06"]
    assert result["gate"] == "fail"
