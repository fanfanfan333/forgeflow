"""Optional OpenViking HTTP bridge. No vendor code or database is embedded.

Contract checked against upstream 55c34934e7b61a8755303e0df1da7a4d4cffcc53.
Only identity-specific user keys, bounded reads, no remote writes or promotion.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from typing import Any
from urllib.parse import urlsplit

import httpx


async def recall(settings: Any, tenant: str, user: str, query: str) -> tuple[list[dict], dict]:
    if not settings.openviking_url:
        return [], {"provider": "local", "status": "disabled"}
    key = settings.openviking_user_keys.get(f"{tenant}:{user}")
    if not tenant or not user or key is None:
        return [], {"provider": "openviking", "status": "unconfigured_identity"}
    url = urlsplit(settings.openviking_url)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        return [], {"provider": "openviking", "status": "invalid_endpoint"}
    if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return [], {"provider": "openviking", "status": "https_required"}
    namespace = hashlib.sha256(tenant.encode()).hexdigest()[:24]
    if any(c in user for c in "/\\%?#") or user in {".", ".."}:
        return [], {"provider": "openviking", "status": "invalid_identity"}
    roots = [f"viking://user/{user}/", f"viking://resources/forgeflow/{namespace}/"]
    from forgeflow.context.viking_history import revoked_prefixes

    revoked = await asyncio.to_thread(revoked_prefixes, settings, tenant, user)

    async def request(client: httpx.AsyncClient, method: str, path: str, **kwargs: Any):
        async with client.stream(method, path, **kwargs) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 131072:
                    raise ValueError("oversized_response")
            payload = json.loads(body)
            if not isinstance(payload, dict) or payload.get("status") != "ok":
                raise ValueError("remote_error")
            return payload["result"]

    async def fetch():
        async with httpx.AsyncClient(
            base_url=settings.openviking_url.rstrip("/"),
            headers={"X-API-Key": key.get_secret_value()},
            timeout=settings.openviking_timeout_seconds,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            result = await request(
                client,
                "POST",
                "/api/v1/search/find",
                json={
                    "query": query[:4000],
                    "target_uri": roots,
                    "limit": 3,
                    "level": [0, 1, 2],
                    "include_provenance": True,
                },
            )
            sections, rejected, detail_reads = [], 0, 0
            for kind in ("memories", "resources", "skills"):
                for hit in result.get(kind, [])[:3]:
                    if len(sections) >= 3:
                        break
                    uri = str(hit.get("uri", ""))
                    if (
                        not any(uri.startswith(root) for root in roots)
                        or any(p in {".", ".."} for p in uri.split("/"))
                        or "%" in uri
                        or any(
                            uri.startswith(prefix) or uri == prefix.rstrip("/")
                            for prefix in revoked
                        )
                    ):
                        rejected += 1
                        continue
                    text = str(hit.get("overview") or hit.get("abstract") or "")[:6000]
                    level = 1 if hit.get("overview") else 0
                    if hit.get("level") == 2 and detail_reads == 0:
                        try:
                            content = await request(
                                client,
                                "GET",
                                "/api/v1/content/read",
                                params={"uri": uri, "offset": 0, "limit": 80},
                            )
                            if isinstance(content, str) and content:
                                text = content[:6000]
                                level = 2
                                detail_reads += 1
                        except (httpx.HTTPError, ValueError, KeyError):
                            pass  # Retain the authentic summary if this read fails.
                    if not text:
                        continue
                    score = hit.get("score")
                    if not isinstance(score, (int, float)) or not math.isfinite(score):
                        score = None
                    sections.append(
                        {
                            "source": "openviking",
                            "ref_id": uri,
                            "text": text,
                            "remote_score": score,
                            "level": level,
                            "context_type": kind,
                            "provider": "openviking",
                        }
                    )
            return sections[:3], {
                "provider": "openviking",
                "status": "ok",
                "rejected": rejected,
                "returned": min(len(sections), 3),
                "detail_reads": detail_reads,
            }

    try:
        return await asyncio.wait_for(fetch(), timeout=settings.openviking_timeout_seconds)
    except (TimeoutError, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        return [], {"provider": "openviking", "status": "unavailable", "fallback": "local"}
