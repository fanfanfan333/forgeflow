"""Shared limits backend. Redis is mandatory for production; no fail-open."""

from __future__ import annotations

import asyncio
from functools import lru_cache

from limits import RateLimitItemPerMinute
from limits.storage import storage_from_string
from limits.strategies import FixedWindowRateLimiter

from forgeflow.config import get_settings


@lru_cache(maxsize=8)
def limiter(uri: str) -> FixedWindowRateLimiter:
    settings = get_settings()
    if (settings.is_production() or settings.environment() != "dev") and uri.startswith("memory:"):
        raise RuntimeError("Production rate limiting requires shared Redis storage")
    return FixedWindowRateLimiter(storage_from_string(uri))


async def consume(key: str, amount: int) -> bool:
    engine = limiter(get_settings().rate_limit_storage_uri)
    return await asyncio.to_thread(engine.hit, RateLimitItemPerMinute(amount), key)
