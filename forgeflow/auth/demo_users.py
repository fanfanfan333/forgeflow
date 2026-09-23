"""Canonical demo-user roster — one source of truth for credential seeding.

Both credential stores (PostgreSQL via ``auth.users``, and the offline
in-memory store via ``auth.memory_store``) seed from this dict, so the offline
profile can never drift into a *different* set of accounts than the database
profile. Seeding from two lists is how "works offline, behaves differently
online" bugs get in.

Keys are usernames, values are RBAC roles.
"""

from __future__ import annotations

DEMO_USERS: dict[str, str] = {
    "admin": "admin",
    "manager-1": "manager",
    "rep-1": "sales_rep",
    "viewer-1": "viewer",
}
