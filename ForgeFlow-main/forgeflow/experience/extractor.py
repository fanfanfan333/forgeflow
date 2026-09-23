"""Experience extraction — jump ② of the closed loop.

Turns a finished run (+ its verdict + source memories) into a first-class
``Experience`` record, embeds it and links it to its source memories via the
N:M ``experience_memory`` relation.

The input is intentionally duck-typed (dict *or* dataclass) so the orchestrator,
the API and tests can all call it without sharing a concrete Run model.
"""

from __future__ import annotations

from typing import Any

from forgeflow.experience.dedup import ExperienceDeduplicator
from forgeflow.experience.embedding import embed_text
from forgeflow.experience.models import ExperienceRecord
from forgeflow.repositories import get_experience_repository

# Statuses that mean "the run reached a terminal state".
_TERMINAL = {"completed", "success", "done", "failed", "failure", "aborted", "error"}


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a dict or an attribute-bearing object."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _resolve_outcome(run: Any, verdict: Any) -> str:
    outcome = _get(verdict, "outcome")
    if outcome:
        return str(outcome)
    status = str(_get(run, "status", "") or "").lower()
    if status in {"completed", "success", "done"}:
        return "success"
    if status in {"failed", "failure", "error"}:
        return "failure"
    if status == "aborted":
        return "aborted"
    errors = _get(run, "errors") or []
    return "failure" if errors else "success"


def _build_summary(run: Any, outcome: str) -> str:
    explicit = _get(run, "summary")
    if explicit:
        return str(explicit)
    intent = _get(run, "intent") or _get(run, "title") or _get(run, "workflow_type") or "任务"
    steps = _get(run, "steps") or []
    return f"{intent} — 共执行 {len(steps)} 个步骤，结果：{outcome}"


def _build_decisions(run: Any, verdict: Any) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for raw in _get(run, "decisions") or []:
        decisions.append(raw if isinstance(raw, dict) else {"decision": str(raw)})
    for reason in _get(verdict, "reasons") or []:
        decisions.append({"source": "validation", "reason": str(reason)})
    errors = _get(run, "errors") or []
    for err in errors:
        decisions.append({"source": "error", "detail": str(err)})
    return decisions


def _build_reusable_steps(run: Any, outcome: str) -> list[dict[str, Any]]:
    """Distil tool-level steps into a reusable trace (only kept for successes)."""
    steps = _get(run, "steps") or []
    reusable: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        tool = _get(step, "tool") or _get(step, "name") or _get(step, "step_type") or f"step_{index}"
        reusable.append(
            {
                "index": index,
                "tool": str(tool),
                "step_type": str(_get(step, "step_type", "tool")),
                "note": str(_get(step, "note", "")),
                "outcome": outcome,
            }
        )
    return reusable


def _build_tags(run: Any) -> list[str]:
    tags = [str(t) for t in (_get(run, "tags") or [])]
    for candidate in (
        _get(run, "domain"),
        _get(run, "workflow_type"),
        _get(run, "agent_id"),
    ):
        if candidate and str(candidate) not in tags:
            tags.append(str(candidate))
    return tags


class ExperienceExtractor:
    """Extracts and persists ``Experience`` records for terminal runs.

    Persistence goes through ``ExperienceDeduplicator`` (INC2-16, docs §2.11):
    a near-duplicate is merged into the existing row (no new row, ``merged_from``
    accumulates) and a semantic conflict is cross-linked via ``conflict_with``,
    instead of blindly appending a new experience every time.
    """

    def __init__(
        self, repo: Any | None = None, deduplicator: ExperienceDeduplicator | None = None
    ) -> None:
        self._repo = repo
        self._deduplicator = deduplicator

    def _repo_or_default(self) -> Any:
        return self._repo or get_experience_repository()

    def _dedup_or_default(self, repo: Any) -> ExperienceDeduplicator:
        if self._deduplicator is not None:
            return self._deduplicator
        return ExperienceDeduplicator(repo=repo)

    async def extract(
        self,
        run: Any,
        verdict: Any = None,
        memories: list[Any] | None = None,
        *,
        tenant_id: str | None = None,
        team_id: str | None = None,
    ) -> ExperienceRecord:
        """Build, embed, de-duplicate, persist and link one Experience.

        Always returns the *surviving* record: on a merge that is the pre-existing
        row the new one was folded into; otherwise it is the freshly created row.
        """
        repo = self._repo_or_default()

        tenant = tenant_id if tenant_id is not None else _get(run, "tenant_id")
        team = team_id if team_id is not None else _get(run, "team_id")
        run_id = str(_get(run, "run_id") or _get(run, "id") or "")

        outcome = _resolve_outcome(run, verdict)
        summary = _build_summary(run, outcome)
        tags = _build_tags(run)

        record = ExperienceRecord(
            tenant_id=tenant,
            team_id=team,
            run_id=run_id,
            summary=summary,
            decisions=_build_decisions(run, verdict),
            outcome=outcome,
            reusable_steps=_build_reusable_steps(run, outcome),
            tags=tags,
            embedding=embed_text(f"{summary} {' '.join(tags)}"),
        )

        # De-duplication owns persistence (merge ⇒ reuse the existing row).
        dedup_outcome = await self._dedup_or_default(repo).resolve(
            record, tenant_id=tenant
        )
        survivor = dedup_outcome.record

        for memory in memories or []:
            memory_id = _get(memory, "id") or _get(memory, "memory_id")
            if memory_id:
                relation = str(_get(memory, "relation", "source"))
                await repo.link_memory(tenant, survivor.id, str(memory_id), relation)

        # The repository uses value semantics (writes are snapshots), so re-read
        # the persisted row to return an instance that reflects the linked
        # memories instead of the pre-save in-memory object.
        getter = getattr(repo, "get", None)
        if getter is not None:
            refreshed = await getter(tenant, survivor.id)
            if refreshed is not None:
                survivor = refreshed

        return survivor

    async def extract_terminal(
        self,
        run: Any,
        verdict: Any = None,
        memories: list[Any] | None = None,
        *,
        tenant_id: str | None = None,
        team_id: str | None = None,
    ) -> ExperienceRecord | None:
        """Extract only when the run is terminal; otherwise return ``None``."""
        status = str(_get(run, "status", "") or "").lower()
        if status and status not in _TERMINAL:
            return None
        return await self.extract(
            run, verdict, memories, tenant_id=tenant_id, team_id=team_id
        )


async def extract_experience(
    run: Any,
    verdict: Any = None,
    memories: list[Any] | None = None,
    *,
    repo: Any | None = None,
    tenant_id: str | None = None,
    team_id: str | None = None,
) -> ExperienceRecord:
    """Functional entry point (matches docs §2 ② signature)."""
    return await ExperienceExtractor(repo=repo).extract(
        run, verdict, memories, tenant_id=tenant_id, team_id=team_id
    )
