"""JWT issuance + verification.

Hardened post-audit:
  - aud / iss / nbf / jti claims added; all verified on decode.
  - Default TTL dropped to 1 hour (was 24).
  - HS256 retained for OSS dev — flagged for RS256 + KMS migration in
    SECURITY_AUDIT.md. The signing secret is now used *only* for JWT signing;
    the old "API_SECRET as wildcard admin bearer" code path has been removed
    from middleware/auth.py.
  - Revocation hook is in-process today (set-based denylist with TTL via
    asyncio.TimerHandle). Production should swap _RevocationStore for a Redis
    SETEX-based store — interface intentionally narrow.
"""

from __future__ import annotations

import logging
import time  # noqa: F401 — re-exported: tests monkeypatch forgeflow.auth.jwt.time.time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from forgeflow.auth.revocation import (
    InProcessRevocationStore,
    RevocationUnavailable,
    get_revocation_store,
)
from forgeflow.config import get_settings

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"
DEFAULT_TTL_HOURS = 1
ISSUER = "forgeflow-api"
AUDIENCE = "forgeflow-api"


class JWTError(Exception):
    """Raised when a JWT cannot be decoded or has expired."""


#: Backward-compatible alias: the in-process store now lives in
#: ``forgeflow.auth.revocation`` but its name (and ``_entries`` / ``_expiry``
#: internals) are a regression contract relied on by ``tests/unit/test_jwt.py``.
_RevocationStore = InProcessRevocationStore


def revoke_token(jti: str, exp_epoch: int) -> None:
    """Add a jti to the denylist until its natural exp (delegates to the store).

    The store is resolved through :func:`get_revocation_store`: in-process by
    default, Redis when configured — so a logout on one instance is visible to
    every instance sharing that backend.
    """
    get_revocation_store().revoke(jti, exp_epoch)


def create_access_token(
    user_id: str,
    role: str,
    workspace_id: str | None = None,
    ttl_hours: int = DEFAULT_TTL_HOURS,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Issue a signed JWT for the given identity.

    Adds aud, iss, nbf, jti so the token can be replay-detected and revoked.
    """
    settings = get_settings()
    secret = settings.api_secret_key.get_secret_value()
    if not secret or secret == "change-me-in-production":
        # Refuse to mint tokens with a placeholder secret — silent failure
        # here would let a misconfigured deploy issue tokens any attacker
        # could forge.
        raise JWTError("API_SECRET_KEY is unset or placeholder — refusing to mint tokens")

    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": user_id,
        "role": role,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(hours=ttl_hours)).timestamp()),
        "iss": ISSUER,
        "aud": AUDIENCE,
        "jti": str(uuid.uuid4()),
    }
    if workspace_id:
        payload["workspace"] = workspace_id
    if extra_claims:
        payload.update(extra_claims)

    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """Verify signature + expiry + aud/iss/nbf + revocation and return claims.

    Raises JWTError on any failure.
    """
    settings = get_settings()
    secret = settings.api_secret_key.get_secret_value()

    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "nbf", "iss", "aud", "sub", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise JWTError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise JWTError(f"invalid token: {exc}") from exc

    if "sub" not in claims or "role" not in claims:
        raise JWTError("token missing required claims (sub, role)")

    jti = claims.get("jti", "")
    if jti:
        try:
            revoked = get_revocation_store().is_revoked(jti)
        except RevocationUnavailable as exc:
            # fail-closed: a shared store we cannot reach must never become a
            # silent "allow". Only an explicit auth_revocation_required=false
            # (availability-first, self-accepted) permits the request through,
            # and it is logged.
            if get_settings().auth_revocation_required:
                raise JWTError("revocation store unavailable") from exc
            logger.warning(
                "revocation store unavailable for jti=%s; allowing request "
                "(AUTH_REVOCATION_REQUIRED=false): %s",
                jti,
                exc,
            )
            revoked = False
        if revoked:
            raise JWTError("token revoked")

    return claims
