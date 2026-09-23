"""Experience / memory de-duplication + conflict resolution — INC2-16 (B4).

Implements docs/sop/05-ARCHITECTURE-INC2.md §2.11. The algorithm is frozen by
the INC2 hard constraint (R1): **one real LLM call ≈ 18.6s**, therefore the LLM
is NEVER consulted per item. The decision pipeline is:

  on_extract(record, tenant):
    1) embedding similarity against the tenant's recent experiences
       (``ExperienceRepository.find_similar``, floor ``CONFLICT_FLOOR`` = 0.60)
    2) the single best match s:
         s >= settings.dedup_merge_threshold (default 0.95)
              → MERGE: fold the new decisions / reusable_steps / tags into the
                existing row and append the new id to ``merged_from``.
                **No new row is written.**
         CONFLICT_FLOOR <= s < threshold and the two outcomes disagree
              → CONFLICT: append each id to the other's ``conflict_with`` and
                keep both rows for human review.
         otherwise → persist the new row unchanged.
    3) the LLM is used ONLY for the conflict band, and then **once per batch**
       (a single call covering every conflict pair), with the verdict cached by
       a stable pair-hash so a repeated batch never re-invokes the model.

The extra ``experiences`` columns (``merged_from`` / ``conflict_with`` /
``dedup_key`` / ``confidence``) are supplied by migration ``011`` and are now
**real fields** on ``ExperienceRecord``, so both the memory and PostgreSQL
repositories persist and round-trip them (see the respective repositories).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from forgeflow.config import get_settings

logger = logging.getLogger(__name__)

# docs §2.11: below this similarity two experiences are not considered related.
CONFLICT_FLOOR: float = 0.60

# pair-hash → arbitration verdict. Module level so a repeated batch is free.
_ARBITRATION_CACHE: dict[str, dict[str, Any]] = {}


# --------------------------------------------------------------------------- #
# small helpers                                                                #
# --------------------------------------------------------------------------- #

def dedup_key(text: str) -> str:
    """Normalised content fingerprint (sha1 of the first 200 chars).

    Used as the ``dedup_key`` column value and as a cheap exact-duplicate
    short-circuit signal.
    """
    normalized = " ".join((text or "").lower().split())
    return hashlib.sha1(normalized[:200].encode("utf-8")).hexdigest()


def _pair_hash(a: str, b: str) -> str:
    """Order-independent hash of an id pair (stable across callers)."""
    low, high = sorted((str(a), str(b)))
    return hashlib.sha1(f"{low}|{high}".encode("utf-8")).hexdigest()


def _attr_list(record: Any, attr: str) -> list[Any]:
    """Return ``record.attr`` as a list, creating it lazily when absent.

    ``ExperienceRecord`` is a plain dataclass, so instances accept dynamic
    attributes (``merged_from`` / ``conflict_with``) without a schema change.
    """
    value = getattr(record, attr, None)
    if value is None:
        value = []
        setattr(record, attr, value)
    return value


def _extend_unique(record: Any, attr: str, values: list[Any]) -> None:
    """Append ``values`` to ``record.attr``, skipping exact duplicates."""
    target = _attr_list(record, attr)
    for value in values:
        if value not in target:
            target.append(value)


def clear_arbitration_cache() -> None:
    """Drop the pair-hash arbitration cache. Test helper only."""
    _ARBITRATION_CACHE.clear()


# --------------------------------------------------------------------------- #
# result object                                                                #
# --------------------------------------------------------------------------- #

@dataclass
class DedupOutcome:
    """What de-duplication did with one incoming record."""

    action: str                      # "created" | "merged" | "conflict"
    record: Any                      # the surviving / persisted ExperienceRecord
    similarity: float = 0.0
    target_id: str | None = None     # the record it matched/merged into
    llm_calls: int = 0               # batch arbitration calls (0 or 1)
    arbitrated: bool = False


# --------------------------------------------------------------------------- #
# deduplicator                                                                 #
# --------------------------------------------------------------------------- #

class ExperienceDeduplicator:
    """Similarity-first de-duplication with a single batched LLM arbitration."""

    def __init__(
        self,
        repo: Any | None = None,
        *,
        llm: Any | None = None,
        merge_threshold: float | None = None,
        conflict_floor: float = CONFLICT_FLOOR,
    ) -> None:
        self._repo = repo
        self._llm = llm
        self._merge_threshold = merge_threshold
        self._conflict_floor = conflict_floor

    # -- configuration ----------------------------------------------------- #

    def _repo_or_default(self) -> Any:
        if self._repo is not None:
            return self._repo
        from forgeflow.repositories import get_experience_repository

        return get_experience_repository()

    @property
    def merge_threshold(self) -> float:
        if self._merge_threshold is not None:
            return float(self._merge_threshold)
        return float(get_settings().dedup_merge_threshold)

    @property
    def conflict_floor(self) -> float:
        return float(self._conflict_floor)

    # -- public API -------------------------------------------------------- #

    async def resolve(self, record: Any, *, tenant_id: str | None = None) -> DedupOutcome:
        """De-duplicate a single record and return its outcome."""
        outcomes = await self.resolve_batch([record], tenant_id=tenant_id)
        return outcomes[0]

    async def resolve_batch(
        self, records: list[Any], *, tenant_id: str | None = None
    ) -> list[DedupOutcome]:
        """De-duplicate a batch, invoking the LLM **at most once**.

        All conflict pairs discovered across the whole batch are arbitrated in a
        single call (docs §2.11 step 3).
        """
        repo = self._repo_or_default()
        outcomes: list[DedupOutcome] = []
        conflicts: list[tuple[Any, Any]] = []          # (incoming, existing)

        for record in list(records or []):
            tenant = tenant_id if tenant_id is not None else getattr(record, "tenant_id", None)
            existing, similarity = await self._best_match(repo, tenant, record)
            threshold = self.merge_threshold

            if existing is not None and similarity >= threshold:
                self._merge_into(existing, record)
                await repo.save(existing)
                outcomes.append(
                    DedupOutcome("merged", existing, similarity, existing.id)
                )
                continue

            if (
                existing is not None
                and self.conflict_floor <= similarity < threshold
                and self._outcomes_conflict(existing, record)
            ):
                self._mark_conflict(existing, record)
                await repo.save(record)
                await repo.save(existing)
                conflicts.append((record, existing))
                outcomes.append(
                    DedupOutcome("conflict", record, similarity, existing.id)
                )
                continue

            await repo.save(record)
            outcomes.append(DedupOutcome("created", record, similarity, None))

        llm_calls = 0
        if conflicts:
            llm_calls = await self._arbitrate(conflicts)
        for outcome in outcomes:
            outcome.llm_calls = llm_calls
            outcome.arbitrated = outcome.action == "conflict"
        return outcomes

    # -- internals --------------------------------------------------------- #

    async def _best_match(
        self, repo: Any, tenant: str | None, record: Any
    ) -> tuple[Any | None, float]:
        """Return the single most-similar stored record (excluding self)."""
        try:
            similar = await repo.find_similar(
                tenant,
                getattr(record, "embedding", None),
                k=5,
                min_similarity=self.conflict_floor,
                tags=list(getattr(record, "tags", []) or []),
            )
        except Exception as exc:  # noqa: BLE001 — dedup must never break extraction
            logger.warning("dedup similarity lookup failed: %s", exc)
            return None, 0.0

        best: Any | None = None
        best_sim = 0.0
        record_id = getattr(record, "id", None)
        for candidate, similarity in similar:
            # A record already in the store must never be matched against itself.
            if record_id is not None and getattr(candidate, "id", None) == record_id:
                continue
            if float(similarity) > best_sim:
                best, best_sim = candidate, float(similarity)
        return best, best_sim

    @staticmethod
    def _outcomes_conflict(a: Any, b: Any) -> bool:
        """True when the two records reached genuinely different conclusions."""
        return str(getattr(a, "outcome", "")) != str(getattr(b, "outcome", ""))

    @staticmethod
    def _merge_into(existing: Any, record: Any) -> None:
        """Fold ``record`` into ``existing`` and record the merge lineage."""
        _extend_unique(existing, "decisions", list(getattr(record, "decisions", []) or []))
        _extend_unique(
            existing, "reusable_steps", list(getattr(record, "reusable_steps", []) or [])
        )
        _extend_unique(existing, "tags", list(getattr(record, "tags", []) or []))
        merged_from = _attr_list(existing, "merged_from")
        record_id = getattr(record, "id", None)
        if record_id is not None and record_id not in merged_from:
            merged_from.append(record_id)
        existing.dedup_key = dedup_key(getattr(existing, "summary", ""))

    @staticmethod
    def _mark_conflict(a: Any, b: Any) -> None:
        """Cross-link two conflicting records via ``conflict_with``."""
        for left, right in ((a, b), (b, a)):
            ids = _attr_list(left, "conflict_with")
            right_id = getattr(right, "id", None)
            if right_id is not None and right_id not in ids:
                ids.append(right_id)
            left.dedup_key = dedup_key(getattr(left, "summary", ""))

    # -- LLM arbitration (batch) ------------------------------------------- #

    async def _arbitrate(self, conflicts: list[tuple[Any, Any]]) -> int:
        """Arbitrate every conflict pair in ONE call. Returns the call count."""
        uncached = [
            (incoming, existing)
            for incoming, existing in conflicts
            if _pair_hash(incoming.id, existing.id) not in _ARBITRATION_CACHE
        ]
        if not uncached:
            return 0

        llm = self._llm_or_default()
        verdicts: dict[str, dict[str, Any]] = {}
        if llm is not None:
            prompt = self._build_prompt(uncached)
            try:
                response = await llm.ainvoke(prompt)
                verdicts = _parse_verdicts(getattr(response, "content", response), uncached)
            except Exception as exc:  # noqa: BLE001 — degrade to "keep both rows"
                logger.warning("conflict arbitration failed; keeping both rows: %s", exc)

        for incoming, existing in uncached:
            key = _pair_hash(incoming.id, existing.id)
            verdict = verdicts.get(
                key, {"winner": None, "confidence": 0.5, "reason": "unresolved"}
            )
            _ARBITRATION_CACHE[key] = verdict
            _apply_verdict(incoming, existing, verdict)
        return 1

    def _llm_or_default(self) -> Any | None:
        if self._llm is not None:
            return self._llm
        try:
            from forgeflow.models import get_model

            return get_model(strong=True)
        except Exception as exc:  # noqa: BLE001 — no model ⇒ skip arbitration
            logger.warning("no LLM available for conflict arbitration: %s", exc)
            return None

    @staticmethod
    def _build_prompt(conflicts: list[tuple[Any, Any]]) -> str:
        lines = [
            "You arbitrate pairs of conflicting execution experiences.",
            "Return ONE JSON object mapping each pair_hash to "
            '{"winner": "A"|"B"|null, "confidence": 0..1, "reason": string}.',
            "",
        ]
        for incoming, existing in conflicts:
            lines.append(f"pair_hash: {_pair_hash(incoming.id, existing.id)}")
            lines.append(
                f"  A outcome={getattr(incoming, 'outcome', '')} "
                f"summary={str(getattr(incoming, 'summary', ''))[:200]}"
            )
            lines.append(
                f"  B outcome={getattr(existing, 'outcome', '')} "
                f"summary={str(getattr(existing, 'summary', ''))[:200]}"
            )
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# arbitration parsing                                                          #
# --------------------------------------------------------------------------- #

def _parse_verdicts(content: Any, conflicts: list[tuple[Any, Any]]) -> dict[str, dict[str, Any]]:
    """Parse the single arbitration response into ``pair_hash -> verdict``."""
    if isinstance(content, list):  # some providers return content blocks
        content = " ".join(str(part) for part in content)
    text = str(content or "")
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}

    parsed: dict[str, dict[str, Any]] = {}
    for incoming, existing in conflicts:
        key = _pair_hash(incoming.id, existing.id)
        value = data.get(key)
        if isinstance(value, dict):
            parsed[key] = {
                "winner": value.get("winner"),
                "confidence": _as_float(value.get("confidence"), 0.5),
                "reason": str(value.get("reason", "")),
            }
    return parsed


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _apply_verdict(incoming: Any, existing: Any, verdict: dict[str, Any]) -> None:
    """Stamp the arbitration result onto both conflict rows."""
    confidence = _as_float(verdict.get("confidence"), 0.5)
    reason = str(verdict.get("reason", ""))
    winner = verdict.get("winner")
    for record in (incoming, existing):
        record.confidence = confidence
        decisions = _attr_list(record, "decisions")
        marker = {"source": "dedup_arbitration", "winner": winner, "reason": reason}
        if marker not in decisions:
            decisions.append(marker)


# --------------------------------------------------------------------------- #
# functional entry points                                                      #
# --------------------------------------------------------------------------- #

async def dedup_experience(
    record: Any,
    *,
    repo: Any | None = None,
    tenant_id: str | None = None,
    llm: Any | None = None,
) -> DedupOutcome:
    """De-duplicate a single experience record (functional form)."""
    return await ExperienceDeduplicator(repo=repo, llm=llm).resolve(
        record, tenant_id=tenant_id
    )


async def dedup_batch(
    records: list[Any],
    *,
    repo: Any | None = None,
    tenant_id: str | None = None,
    llm: Any | None = None,
) -> list[DedupOutcome]:
    """De-duplicate a batch with at most one LLM call (functional form)."""
    return await ExperienceDeduplicator(repo=repo, llm=llm).resolve_batch(
        records, tenant_id=tenant_id
    )
