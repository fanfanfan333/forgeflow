"""Failure classification and aggregation (§40/§90).

Layer L3. Turns a raw :class:`ProcessResult` into one of the 14
:class:`FailureClass` values, and aggregates a whole run's evidence for
``failure_analysis.json``.

The single most important rule: **a failed test is never assumed to be a source
bug.** Environment problems are detected first and win; a generic failure with no
recognisable signature is ``UNKNOWN``. ``SOURCE_BUG`` is reserved for an
unambiguous source-level signature (a ``SyntaxError``), so the classifier can
never manufacture false blame. When a transcript carries **two or more distinct**
failure signatures (common on aggregate targets, whose output is the whole
suite), the result is likewise ``UNKNOWN`` rather than letting an unrelated line
relabel a real failure.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from typing import Any

from forgeflow.qa.environment_guard import EnvFinding
from forgeflow.qa.models import (
    Evidence,
    EvidenceStatus,
    FailureClass,
    FailureFingerprint,
    ProcessResult,
    ProcessStatus,
    RootCauseCandidate,
    TestPlanItem,
)
from forgeflow.qa.text_signals import (
    COLLECTION_NOTICE_PHRASES,
    normalise_signature,
    output_has_notice,
)

logger = logging.getLogger(__name__)

#: ``item.requires`` token → the substring expected in a service finding's detail.
_REQUIRE_MATCH: dict[str, str] = {
    "pg": "postgres",
    "postgres": "postgres",
    "postgresql": "postgres",
    "ollama": "ollama",
    "browser": "browser",
    "node": "node",
}

_HTTP_DENIED_RE = re.compile(r"\b(401|403)\b")

#: Failure class → the source of the failure (§41). Environment/tooling classes
#: are *never* blamed on the source; anything else is honestly ``unknown``.
_ATTRIBUTION_BY_CLASS: dict[FailureClass, str] = {
    FailureClass.ENVIRONMENT_FAILURE: "environment",
    FailureClass.TOOL_FAILURE: "environment",
    FailureClass.SOURCE_BUG: "source",
    FailureClass.TEST_BUG: "test",
}

#: Bounded excerpt length recorded on a root-cause candidate.
_EXCERPT_MAX_CHARS = 400


class FailureClassifier:
    """Maps process results (and env findings) to a :class:`FailureClass`."""

    # ── per-result classification ────────────────────────────────────────
    def _environment_failure(
        self, item: TestPlanItem, env_findings: list[EnvFinding]
    ) -> EnvFinding | None:
        """Return the blocking env finding relevant to ``item``, if any."""
        for finding in env_findings:
            if finding.ok:
                continue
            if finding.check in ("wrong_python", "missing_dependency"):
                return finding
            detail = finding.detail.lower()
            required = {_REQUIRE_MATCH.get(token, token) for token in item.requires}
            if finding.check == "service_unavailable" and any(
                token in detail for token in required
            ):
                return finding
            if finding.check == "browser_unavailable" and "browser" in item.requires:
                return finding
            if finding.check == "port_in_use" and "port" in item.requires:
                return finding
        return None

    def classify(
        self,
        result: ProcessResult,
        item: TestPlanItem,
        *,
        env_findings: list[EnvFinding] | None = None,
    ) -> FailureClass:
        """Classify one failed run. Never returns ``SOURCE_BUG`` without evidence."""
        findings = env_findings or []

        # 1) A blocking environment problem wins outright (§41).
        if self._environment_failure(item, findings) is not None:
            return FailureClass.ENVIRONMENT_FAILURE

        # 2) Timeout / spawn failures are tooling problems, not source bugs.
        if result.status is ProcessStatus.TIMEOUT or result.timed_out:
            if item.requires:
                return FailureClass.ENVIRONMENT_FAILURE
            return FailureClass.TOOL_FAILURE
        if result.status is ProcessStatus.SPAWN_ERROR:
            return FailureClass.TOOL_FAILURE

        text = f"{result.stdout}\n{result.stderr}".lower()

        # 3)–9) Each honest signature *family* contributes at most one candidate
        # class. Aggregate targets (``check`` / ``test`` / ``test-integration``)
        # emit one multi-megabyte transcript, so an unrelated line can carry a
        # second, spurious signature and relabel a real failure. To stop that
        # drift, a transcript that matches **two or more distinct families** is
        # reported honestly as ``UNKNOWN`` rather than guessed at; exactly one
        # family is trusted; zero families is ``UNKNOWN``.
        candidates: dict[str, FailureClass] = {}

        # 3) A source-level SyntaxError is the only unambiguous SOURCE_BUG signal.
        if "syntaxerror" in text or "indentationerror" in text:
            candidates["syntax"] = (
                FailureClass.TEST_BUG
                if ("test_" in text or "tests/" in text)
                else FailureClass.SOURCE_BUG  # a broken *test* file is a test bug
            )

        # 4) Missing imports ⇒ dependency failure.
        if "modulenotfounderror" in text or "importerror" in text or "no module named" in text:
            candidates["import"] = FailureClass.DEPENDENCY_FAILURE

        # 5) Connection failures: database vs generic environment.
        if any(
            key in text
            for key in (
                "connection refused",
                "could not connect",
                "connection reset",
                "operationalerror",
                "server closed the connection",
            )
        ):
            candidates["connection"] = (
                FailureClass.DATABASE_FAILURE
                if any(
                    key in text
                    for key in ("postgres", "asyncpg", "psycopg", "database", "sqlalchemy")
                )
                else FailureClass.ENVIRONMENT_FAILURE
            )

        # 6) Migrations.
        if "alembic" in text or "migration" in text or "target database is not up to date" in text:
            candidates["migration"] = FailureClass.MIGRATION_FAILURE

        # 7) Collection / "nothing ran" is a test-harness problem. The detection
        #    is shared with the executor (:mod:`forgeflow.qa.text_signals`) and
        #    is **line-anchored**, so a passing parametrised id that merely
        #    *echoes* "no tests ran" can no longer decide a failure's
        #    attribution.
        if output_has_notice(text, COLLECTION_NOTICE_PHRASES):
            candidates["collection"] = FailureClass.TEST_BUG

        # 8) Auth / permission.
        if (
            "permission denied" in text
            or "unauthorized" in text
            or "forbidden" in text
            or _HTTP_DENIED_RE.search(text) is not None
        ):
            candidates["permission"] = FailureClass.PERMISSION_FAILURE

        # 9) A few more honest signatures, still conservative.
        if "failed to build" in text or "build failed" in text or "compilation error" in text:
            candidates["build"] = FailureClass.BUILD_FAILURE
        if "llm" in text and ("timed out" in text or "rate limit" in text or "model" in text):
            candidates["llm"] = FailureClass.LLM_FAILURE

        # 10) Exactly one family ⇒ trust it. Zero families, or an ambiguous
        #     ≥2-family transcript, is honestly ``UNKNOWN`` — never SOURCE_BUG
        #     without a signature, and never a guess under ambiguity.
        if len(candidates) == 1:
            return next(iter(candidates.values()))
        return FailureClass.UNKNOWN

    # ── run-level aggregation (§90) ──────────────────────────────────────
    def analyze(self, evidence: list[Evidence]) -> dict[str, Any]:
        """Aggregate evidence into a §90 failure-analysis payload."""
        by_failure_class: dict[str, int] = {cls.value: 0 for cls in FailureClass}
        by_status: dict[str, int] = {status.value: 0 for status in EvidenceStatus}
        failed_tests: list[dict[str, Any]] = []
        environment_failures: list[str] = []

        for record in evidence:
            by_status[record.status.value] = by_status.get(record.status.value, 0) + 1
            if record.failure_class is not None:
                by_failure_class[record.failure_class.value] = (
                    by_failure_class.get(record.failure_class.value, 0) + 1
                )
                if record.failure_class is FailureClass.ENVIRONMENT_FAILURE:
                    environment_failures.append(record.test_id)
            if record.status in (EvidenceStatus.FAIL, EvidenceStatus.BLOCKED):
                failed_tests.append(
                    {
                        "test_id": record.test_id,
                        "status": record.status.value,
                        "failure_class": (
                            record.failure_class.value if record.failure_class else None
                        ),
                    }
                )

        return {
            "total": len(evidence),
            "by_status": by_status,
            "by_failure_class": by_failure_class,
            "failed_tests": failed_tests,
            "environment_failures": sorted(environment_failures),
        }

    # ── Phase D: failure deepening (§68/§41/§90) ─────────────────────────
    #
    # ``analyze()`` above is a frozen contract (§90 + two existing tests depend
    # on its exact keys); everything below is *additive* — the deep chain
    # (fingerprints, environment-vs-source attribution, root-cause candidates)
    # and its superset ``analyze_deep()``. Signatures are recovered from the
    # real transcript when one is supplied (``transcripts``: test_id → text, read
    # by the orchestrator from the run's artifacts) and otherwise fall back to a
    # deterministic, evidence-derived fact string — both stable across runs.
    @staticmethod
    def _signature_for(record: Evidence, transcripts: Mapping[str, str] | None) -> list[str]:
        """Recover a stable signature for ``record`` (transcript first, else facts)."""
        if transcripts is not None:
            text = transcripts.get(record.test_id)
            if text:
                return normalise_signature(text)
        facts = " ".join(
            [
                record.failure_class.value if record.failure_class else "",
                record.status.value,
                record.test_id,
                " ".join(record.command),
            ]
        )
        return normalise_signature(facts)

    @staticmethod
    def _fingerprint_of(signature: list[str]) -> str:
        """sha1 of the sorted, ``|``-joined signature, truncated to 12 hex chars."""
        return hashlib.sha1("|".join(signature).encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _failure_rows(evidence: list[Evidence]) -> list[Evidence]:
        """Only the evidence that is a real failure with a non-empty class."""
        return [
            record
            for record in evidence
            if record.status in (EvidenceStatus.FAIL, EvidenceStatus.BLOCKED)
            and record.failure_class is not None
        ]

    def fingerprints(
        self, evidence: list[Evidence], *, transcripts: Mapping[str, str] | None = None
    ) -> list[FailureFingerprint]:
        """Cluster failures by their stable cross-run fingerprint (§68)."""
        groups: dict[str, FailureFingerprint] = {}
        for record in self._failure_rows(evidence):
            assert record.failure_class is not None  # guaranteed by _failure_rows
            signature = self._signature_for(record, transcripts)
            key = self._fingerprint_of(signature)
            existing = groups.get(key)
            if existing is None:
                groups[key] = FailureFingerprint(
                    fingerprint=key,
                    failure_class=record.failure_class,
                    test_ids=[record.test_id],
                    count=1,
                    signature=signature,
                )
            else:
                if record.test_id not in existing.test_ids:
                    existing.test_ids.append(record.test_id)
                existing.count += 1
        return sorted(groups.values(), key=lambda item: item.fingerprint)

    def environment_attribution(self, evidence: list[Evidence]) -> dict[str, Any]:
        """Split failures into environment / source / test / unknown (§41).

        Environment and tooling failures are *never* counted as source bugs; the
        returned ``note`` states this so a reader cannot mis-read the tally.
        """
        counts: dict[str, int] = {"environment": 0, "source": 0, "test": 0, "unknown": 0}
        environment_tests: list[str] = []
        for record in evidence:
            if record.status not in (EvidenceStatus.FAIL, EvidenceStatus.BLOCKED):
                continue
            attribution = (
                _ATTRIBUTION_BY_CLASS.get(record.failure_class, "unknown")
                if record.failure_class
                else "unknown"
            )
            counts[attribution] += 1
            if attribution == "environment":
                environment_tests.append(record.test_id)
        return {
            **counts,
            "environment_tests": sorted(environment_tests),
            "note": "environment failures are NOT source bugs (§41)",
        }

    def _excerpt(self, record: Evidence, transcripts: Mapping[str, str] | None) -> str:
        """A bounded excerpt of the real transcript, or ``""`` when unavailable."""
        if not transcripts:
            return ""
        text = transcripts.get(record.test_id) or ""
        for raw in text.splitlines():
            line = raw.strip()
            if line:
                return line[:_EXCERPT_MAX_CHARS]
        return ""

    @staticmethod
    def _hypothesis(failure_class: FailureClass, signature: list[str]) -> str:
        if not signature:
            return f"{failure_class.value}: no stable signature recovered"
        return f"{failure_class.value}: {','.join(signature[:5])}"

    @staticmethod
    def _confidence(failure_class: FailureClass, attribution: str, signature: list[str]) -> float:
        """A deliberately conservative confidence (UNKNOWN is always low)."""
        if failure_class is FailureClass.UNKNOWN:
            base = 0.2
        elif attribution == "source":
            base = 0.6
        elif attribution in ("environment", "test"):
            base = 0.5
        else:
            base = 0.4
        return min(base, 0.2) if not signature else base

    def root_cause_candidates(
        self, evidence: list[Evidence], *, transcripts: Mapping[str, str] | None = None
    ) -> list[RootCauseCandidate]:
        """Propose a conservative candidate root cause per failure (§90 深链)."""
        candidates: list[RootCauseCandidate] = []
        for record in self._failure_rows(evidence):
            assert record.failure_class is not None  # guaranteed by _failure_rows
            failure_class = record.failure_class
            signature = self._signature_for(record, transcripts)
            attribution = _ATTRIBUTION_BY_CLASS.get(failure_class, "unknown")
            candidates.append(
                RootCauseCandidate(
                    test_id=record.test_id,
                    failure_class=failure_class,
                    fingerprint=self._fingerprint_of(signature),
                    attribution=attribution,  # type: ignore[arg-type]
                    hypothesis=self._hypothesis(failure_class, signature),
                    evidence_excerpt=self._excerpt(record, transcripts),
                    confidence=self._confidence(failure_class, attribution, signature),
                )
            )
        return candidates

    def analyze_deep(
        self, evidence: list[Evidence], *, transcripts: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        """The Phase D deep chain: a strict **superset** of :meth:`analyze`."""
        deep: dict[str, Any] = dict(self.analyze(evidence))
        deep["phase"] = "D"
        deep["fingerprints"] = [
            fingerprint.model_dump(mode="json")
            for fingerprint in self.fingerprints(evidence, transcripts=transcripts)
        ]
        deep["environment_attribution"] = self.environment_attribution(evidence)
        deep["root_cause_candidates"] = [
            candidate.model_dump(mode="json")
            for candidate in self.root_cause_candidates(evidence, transcripts=transcripts)
        ]
        return deep


__all__ = ["FailureClassifier"]
