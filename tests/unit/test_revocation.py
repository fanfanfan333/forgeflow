"""INC-multinode — shared JWT revocation store (protocol + backends + factory).

Covers:
  * ``InProcessRevocationStore`` semantics (the preserved ``_entries``/``_expiry``
    contract the offline profile depends on);
  * the factory default (``memory``) and the explicit ``redis`` opt-in;
  * ``RedisRevocationStore`` raising ``RevocationUnavailable`` when unreachable;
  * fail-closed vs. availability-first inside ``decode_access_token``;
  * the cross-instance guarantee proven with a shared-backing stub (no real
    Redis needed — the real-redis case lives in test_multinode_shared_state.py).
"""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest

from forgeflow.auth import revocation as rev
from forgeflow.auth.jwt import (
    AUDIENCE,
    ISSUER,
    JWTError,
    create_access_token,
    decode_access_token,
    revoke_token,
)
from forgeflow.config import get_settings

_CLOSED_REDIS = "redis://127.0.0.1:6399/0"  # a port nothing listens on


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("API_SECRET_KEY", "test-secret-revocation-suite")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_store():
    rev.reset_revocation_store()
    yield
    rev.reset_revocation_store()


# --------------------------------------------------------------------------- #
# In-process backend (offline, unchanged semantics)                            #
# --------------------------------------------------------------------------- #
def test_inprocess_store_revoke_and_expiry(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(rev.time, "time", lambda: now[0])
    store = rev.InProcessRevocationStore()

    store.revoke("j", 110)
    assert store.is_revoked("j")
    assert store._entries == {"j": 110.0}
    assert len(store._expiry) == 1

    now[0] = 111  # past the natural exp ⇒ swept, no scan of valid entries
    assert not store.is_revoked("j")
    assert not store._entries and not store._expiry


def test_inprocess_store_ignores_expired_and_duplicate_revoke(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(rev.time, "time", lambda: now[0])
    store = rev.InProcessRevocationStore()

    store.revoke("old", 90)  # already expired ⇒ ignored
    assert not store.is_revoked("old")

    store.revoke("j", 110)
    store.revoke("j", 110)  # duplicate ⇒ no double count
    assert len(store._expiry) == 1


# --------------------------------------------------------------------------- #
# Factory                                                                      #
# --------------------------------------------------------------------------- #
def test_factory_default_is_memory():
    rev.reset_revocation_store()
    assert isinstance(rev.get_revocation_store(), rev.InProcessRevocationStore)


def test_factory_explicit_redis_backend(monkeypatch):
    monkeypatch.setenv("AUTH_REVOCATION_BACKEND", "redis")
    get_settings.cache_clear()
    try:
        rev.reset_revocation_store()
        assert isinstance(rev.get_revocation_store(), rev.RedisRevocationStore)
    finally:
        get_settings.cache_clear()


def test_factory_auto_falls_back_to_memory_when_redis_unreachable(monkeypatch):
    monkeypatch.setenv("AUTH_REVOCATION_BACKEND", "auto")
    monkeypatch.setenv("AUTH_REVOCATION_REDIS_URL", _CLOSED_REDIS)
    get_settings.cache_clear()
    try:
        rev.reset_revocation_store()
        assert isinstance(rev.get_revocation_store(), rev.InProcessRevocationStore)
    finally:
        get_settings.cache_clear()


def test_set_and_reset_store():
    custom = rev.InProcessRevocationStore()
    rev.set_revocation_store(custom)
    assert rev.get_revocation_store() is custom
    rev.reset_revocation_store()
    assert rev.get_revocation_store() is not custom


# --------------------------------------------------------------------------- #
# Redis backend — unavailable ⇒ RevocationUnavailable (never silent)           #
# --------------------------------------------------------------------------- #
def test_redis_store_is_revoked_raises_when_unreachable():
    store = rev.RedisRevocationStore(_CLOSED_REDIS)
    with pytest.raises(rev.RevocationUnavailable):
        store.is_revoked("j")


def test_redis_store_revoke_raises_when_unreachable():
    store = rev.RedisRevocationStore(_CLOSED_REDIS)
    with pytest.raises(rev.RevocationUnavailable):
        store.revoke("j", int(time.time()) + 60)


def test_redis_ping_is_false_when_unreachable():
    assert rev.RedisRevocationStore(_CLOSED_REDIS).ping(0.5) is False


# --------------------------------------------------------------------------- #
# fail-closed / availability-first in decode_access_token                      #
# --------------------------------------------------------------------------- #
class _BoomStore:
    """A shared store whose every call fails (simulated Redis/DB outage)."""

    def revoke(self, jti: str, exp_epoch: int) -> None:
        raise rev.RevocationUnavailable(jti, "simulated outage")

    def is_revoked(self, jti: str) -> bool:
        raise rev.RevocationUnavailable(jti, "simulated outage")


def test_fail_closed_when_store_unavailable():
    token = create_access_token(user_id="u-fc", role="admin")
    rev.set_revocation_store(_BoomStore())
    with pytest.raises(JWTError, match="revocation"):
        decode_access_token(token)


def test_availability_first_when_required_false(monkeypatch):
    monkeypatch.setenv("AUTH_REVOCATION_REQUIRED", "false")
    get_settings.cache_clear()
    try:
        token = create_access_token(user_id="u-av", role="admin")
        rev.set_revocation_store(_BoomStore())
        claims = decode_access_token(token)  # must NOT raise
        assert claims["sub"] == "u-av"
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Regression contract: the alias + cross-instance sharing                      #
# --------------------------------------------------------------------------- #
def test_RevocationStore_alias_preserves_internals():
    from forgeflow.auth import jwt as auth_jwt

    assert auth_jwt._RevocationStore is rev.InProcessRevocationStore
    store = auth_jwt._RevocationStore()
    assert hasattr(store, "_entries") and hasattr(store, "_expiry")


class _SharedBacking:
    """A process-wide ``{jti: exp}`` map standing in for shared Redis."""

    def __init__(self) -> None:
        self.revoked: dict[str, int] = {}

    def revoke(self, jti: str, exp_epoch: int) -> None:
        self.revoked[jti] = exp_epoch

    def is_revoked(self, jti: str) -> bool:
        return jti in self.revoked


class _BackedStore:
    """A ``RevocationStore`` over a shared backing (two instances, one backing)."""

    def __init__(self, backing: _SharedBacking) -> None:
        self._b = backing

    def revoke(self, jti: str, exp_epoch: int) -> None:
        self._b.revoke(jti, exp_epoch)

    def is_revoked(self, jti: str) -> bool:
        return self._b.is_revoked(jti)


def _claims_without_revocation(token: str) -> dict:
    return pyjwt.decode(
        token,
        get_settings().api_secret_key.get_secret_value(),
        algorithms=["HS256"],
        audience=AUDIENCE,
        issuer=ISSUER,
    )


def test_shared_backend_makes_revocation_cross_instance():
    """A revoke on instance A ⇒ instance B rejects the token (AC-05).

    Red proof: swap ``_BackedStore`` for two independent
    ``InProcessRevocationStore`` instances and this test fails — B would accept
    the token, which is exactly the process-local bug the increment removes.
    """
    backing = _SharedBacking()
    store_a = _BackedStore(backing)
    store_b = _BackedStore(backing)

    token = create_access_token(user_id="u-shared", role="admin")
    claims = _claims_without_revocation(token)

    rev.set_revocation_store(store_a)  # "instance A" logs the user out
    revoke_token(claims["jti"], claims["exp"])

    rev.set_revocation_store(store_b)  # "instance B" now verifies the token
    with pytest.raises(JWTError, match="revoked"):
        decode_access_token(token)


def test_independent_inprocess_stores_do_not_share():
    """Documents *why* a shared backend is required: process-local stores diverge."""
    a = rev.InProcessRevocationStore()
    b = rev.InProcessRevocationStore()
    a.revoke("j", int(time.time()) + 60)
    assert a.is_revoked("j")
    assert not b.is_revoked("j")
