"""Scoped evidence, source reading and selected-skill loading tools."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forgeflow.config import get_settings
from forgeflow.documents.store import DocArtifactStore


def evidence_store(ctx: Any) -> DocArtifactStore:
    if not ctx.tenant_id or not ctx.user_id or ctx.user_id == "anonymous":
        raise PermissionError("Evidence requires an authenticated tenant and actor")
    scope = hashlib.sha256(f"{ctx.tenant_id}\0{ctx.user_id}".encode()).hexdigest()
    return DocArtifactStore(Path(get_settings().resource_store_path()) / "evidence" / scope)


def store_evidence(payload: Any, ctx: Any) -> dict[str, Any]:
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    if len(content) > 2_000_000:
        raise ValueError("Evidence exceeds the 2 MB storage limit")
    ref = evidence_store(ctx).put(content, suffix=".json")
    return {
        "evidence_ref": ref,
        "content_hash": hashlib.sha256(content).hexdigest(),
        "original_bytes": len(content),
    }


async def evidence_read(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    offset, limit = args.get("offset", 0), args.get("limit", 2000)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 2500:
        raise ValueError("offset must be nonnegative; limit must be between 1 and 2500")
    ref = str(args.get("ref") or "")
    data = evidence_store(ctx).get(ref)
    digest = hashlib.sha256(data).hexdigest()
    if args.get("content_hash") and args["content_hash"] != digest:
        raise ValueError("Evidence version does not match")
    text = data.decode("utf-8")
    if offset > len(text):
        raise ValueError("offset exceeds evidence length")
    end = min(offset + limit, len(text))
    return {
        "ok": True,
        "evidence_ref": ref,
        "content_hash": digest,
        "content": text[offset:end],
        "offset": offset,
        "next_offset": end if end < len(text) else None,
        "total_chars": len(text),
    }


async def research_read(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    from bs4 import BeautifulSoup

    from forgeflow.security.ssrf_guard import safe_get

    url = str(args.get("url") or "")
    query = args.get("query", "")
    offset = args.get("offset", 0)
    if not isinstance(query, str) or len(query) > 200 or type(offset) is not int or offset < 0:
        raise ValueError("Source query must be at most 200 characters; offset must be nonnegative")
    retained = None
    records = getattr(getattr(ctx, "task_context", None), "context", {}).get("tool_invocations", [])
    for record in reversed(records):
        payload = record.get("payload") if isinstance(record, dict) else None
        if (
            isinstance(payload, dict)
            and record.get("tool") == "research.read"
            and record.get("status") == "ok"
            and record.get("tenant_id") == ctx.tenant_id
            and record.get("actor_user_id") == ctx.user_id
            and url in {payload.get("source"), payload.get("requested_url")}
            and payload.get("source_ref")
        ):
            blob = evidence_store(ctx).get(payload["source_ref"])
            if hashlib.sha256(blob).hexdigest() != payload.get("source_hash"):
                raise ValueError("Retained source version does not match")
            retained = (json.loads(blob), payload["source_ref"], payload["source_hash"])
            break
    if retained:
        result, source_ref, source_hash = retained
        content = result["content"]
        result.update(source_ref=source_ref, source_hash=source_hash)
    else:
        response = await safe_get(url, timeout=20, max_bytes=1_000_000)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        title = soup.title.get_text(strip=True) if soup.title else ""
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        main = soup.select_one("main, [role=main], article, .document .body") or soup
        content = main.get_text(separator=" ", strip=True)
        if not content:
            raise ValueError("Source returned no readable content")
        result = {
            "ok": True,
            "source": str(response.url),
            "requested_url": url,
            "title": title,
            "content": content,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "summary": f"已读取原始来源：{title or url}",
        }
        saved = store_evidence(result, ctx)
        result.update(source_ref=saved["evidence_ref"], source_hash=saved["content_hash"])
    if query:
        position = content.casefold().find(query.casefold(), offset)
        if position < 0:
            result["query_found"] = False
        else:
            offset = max(offset, position - 150)
            result["query_found"] = True
    if offset > len(content):
        raise ValueError("Source offset exceeds retained content length")
    end = min(offset + 1400, len(content))
    passage_id = sum(
        1
        for record in records
        if isinstance(record, dict)
        and record.get("tool") == "research.read"
        and record.get("status") == "ok"
        and isinstance(record.get("payload"), dict)
        and record["payload"].get("source_ref") == result["source_ref"]
    )
    result.update(passage_id=passage_id, excerpt_start=offset, excerpt_end=end)
    result.update(
        content=content[offset:end],
        offset=offset,
        next_offset=end if end < len(content) else None,
        total_chars=len(content),
        content_truncated=end < len(content) or offset > 0,
    )
    return result


async def skill_read(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    from forgeflow.skills.registry import SkillRegistry

    runtime = ctx.runtime_context
    skill_id = str(args.get("skill_id") or "")
    if runtime is None or skill_id not in runtime.available_skills:
        raise PermissionError("Only skills selected for this task may be loaded")
    trial = getattr(runtime, "_functional_skill_version", None)
    if skill_id == "instruction-draft" and trial is not None:
        from types import SimpleNamespace

        if trial.tenant_id != ctx.tenant_id or trial.user_id != ctx.user_id:
            raise PermissionError("Skill trial belongs to another identity")
        skill = SimpleNamespace(id=skill_id, name="功能评测固定版本")
        version = trial
    else:
        registry = SkillRegistry()
        skill = await registry.get(ctx.tenant_id, skill_id)
        if skill is None or skill.status != "published":
            raise PermissionError("Skill is unavailable")
        version = next(
            (
                v
                for v in await registry.versions(ctx.tenant_id, skill_id)
                if v.semver == skill.current_version
            ),
            None,
        )
    if version is None:
        raise ValueError("Selected skill has no published version")
    selected = next(
        (s for s in getattr(runtime, "injected_skills", []) if s.get("id") == skill_id), None
    )
    requested = args.get("version")
    selected_version = (selected or {}).get("version")
    if (requested and requested != version.semver) or (
        selected_version and selected_version != version.semver
    ):
        raise ValueError("Skill version changed; select the current version again")
    digest = hashlib.sha256(
        json.dumps(version.spec, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()
    selected_hash = (selected or {}).get("content_hash")
    if selected_hash and selected_hash != digest:
        raise ValueError("Selected skill content changed; select it again")
    pinned = getattr(runtime, "_skill_read_versions", {})
    identity = (version.semver, digest)
    if skill_id in pinned and pinned[skill_id] != identity:
        raise ValueError("Skill content changed during this task; select it again")
    if args.get("content_hash") and args["content_hash"] != digest:
        raise ValueError("Skill content hash does not match")
    if "ref" in args:
        from forgeflow.skills.progressive_loader import ProgressiveLoader

        if not isinstance(args["ref"], str) or not args["ref"].strip():
            raise ValueError("An explicit skill resource reference is required")
        loader = ProgressiveLoader(skills=[skill], versions={skill_id: version})
        content = loader.load_resource(skill_id, args["ref"])
        if content is None:
            raise FileNotFoundError("Selected skill resource is unavailable")
        result = {
            "ok": True,
            "skill_id": skill_id,
            "version": version.semver,
            "content_hash": digest,
            "skill_content_hash": digest,
            "ref": args["ref"],
            "content": content,
            "resource_hash": hashlib.sha256(content.encode()).hexdigest(),
            "script_executed": False,
        }
    else:
        result = {
            "ok": True,
            "skill_id": skill_id,
            "name": skill.name,
            "version": version.semver,
            "content_hash": digest,
            "skill_content_hash": digest,
            "instructions": str(
                version.spec.get("instructions") or version.spec.get("prompt") or ""
            ),
            "procedure": version.spec.get("procedure", []),
            "resources": sorted(
                ref for ref in version.spec.get("package_files", {}) if ref != "SKILL.md"
            ),
        }
    pinned[skill_id] = identity
    runtime._skill_read_versions = pinned
    return result
