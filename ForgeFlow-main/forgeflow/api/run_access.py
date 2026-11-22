"""Object-level access to a single run — one rule, shared by every read surface.

A run's detail is the most disclosing object in the product (prompts, tool
output, artifacts, diffs), so the default is strict: **the owner, nobody else**.
``tests/unit/test_enterprise_input_admission.py`` and
``tests/unit/test_inc56_task_owner_isolation.py`` pin that as a 404 — including
for ``admin`` — so the existence of someone else's run is never confirmed.

The one exception is the code-review flow: a reviewer is asked to decide a diff,
and cannot decide what they cannot open. That entitlement is therefore scoped to
*a run that actually carries a code-commit approval*, not to the role alone.
A review seat with no approval trail on the run still gets 404, which is exactly
what the security pins above assert (their fixture runs never enter review).

Denials are 404, never 403: "not yours" and "does not exist" must be
indistinguishable from outside.
"""

from __future__ import annotations

from typing import Any

#: Seats that may look at a run they do not own **when it is under code review**.
REVIEW_ROLES: frozenset[str] = frozenset({"admin", "manager"})


def is_owner(record: Any, user: Any) -> bool:
    """Whether ``user`` is the actor that created ``record``."""
    return str(getattr(record, "actor_user_id", "") or "") == str(
        getattr(user, "user_id", "") or ""
    )


async def has_code_review_trail(tenant: str | None, run_id: str) -> bool:
    """Whether ``run_id`` carries a code-commit approval (pending or decided).

    Both states count on purpose: a reviewer must still be able to reopen a run
    after they decided it (that is the audit trail of their own decision).
    A lookup failure is a denial — :func:`find_code_approval` already swallows
    storage errors and returns ``None``.
    """
    if not run_id:
        return False
    from forgeflow.codeplane.approval import find_code_approval
    from forgeflow.repositories import get_policy_repository

    return await find_code_approval(get_policy_repository(), tenant, run_id) is not None


async def can_read_run(record: Any, tenant: str | None, user: Any) -> bool:
    """Owner, or a review seat on a run that is/was under code review."""
    if is_owner(record, user):
        return True
    if str(getattr(user, "role", "") or "") not in REVIEW_ROLES:
        return False
    return await has_code_review_trail(tenant, str(getattr(record, "run_id", "") or ""))
