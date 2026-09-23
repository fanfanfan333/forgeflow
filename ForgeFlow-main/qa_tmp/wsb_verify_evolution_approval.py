"""WS-B independent verification — #7「进化建议 → 待审 Approval」真 HTTP 回归。

归属：software-engineer-2 独立复验（三层里的第二层；不改源码/tests）。
被测：#7 / WS-E2 —— POST /skills/evolution-advice/{skill_id}/approval 把建议落进审批队列。

判据（team-lead 口径）：
  1. GET /skills/evolution-advice 找确有建议的 skill_id；
  2. 计前：该 tenant 的审批队列行数（含 pending）；
  3. 真 HTTP POST …/approval（admin token）→ 记状态码 + body（预期 201 + body.created）；
  4. 计后：**body.created 必须与队列实际增量严格相等**；新行 kind∈{skill.optimize,skill.retire}、
     status=pending、requested_action 指向该 skill；
  5. 反向断言：技能本体（版本/内容）前后完全未变；
  6. 负例：无建议的 skill_id → 404，且队列行数不变。

单事件循环内起 uvicorn（8012, memory 档）+ httpx 打真 HTTP，避免 asyncio.Lock 跨 loop 问题。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HOST = "127.0.0.1"
PORT = 8012
BASE = f"http://{HOST}:{PORT}"

os.environ["STORAGE_BACKEND"] = "memory"
os.environ["POSTGRES_URL"] = "postgresql+asyncpg://forgeflow:forgeflow@127.0.0.1:1/forgeflow"
os.environ["POSTGRES_SYNC_URL"] = "postgresql+psycopg://forgeflow:forgeflow@127.0.0.1:1/forgeflow"
os.environ["LLM_PROVIDER"] = "mock"
os.environ["EMBEDDING_PROVIDER"] = "mock"

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from forgeflow.api.main import app  # noqa: E402
from forgeflow.config import get_settings  # noqa: E402
from forgeflow.repositories import get_policy_repository, get_skill_repository  # noqa: E402
from forgeflow.skills.models import SkillRecord  # noqa: E402

_ADVICE_SKILL = "s-verify-lowusage"   # usage_count>=5 & 无评审分 → optimize 建议
_HEALTHY_SKILL = "s-verify-healthy"   # usage 0 & 新鲜 → 无建议

_EVIDENCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_wsb_evo_evidence.txt")


class _Tee:
    """Mirror stdout into a UTF-8 evidence file (avoids PS console re-encoding)."""

    def __init__(self, path: str) -> None:
        self._fh = open(path, "w", encoding="utf-8")

    def write(self, data: str) -> int:
        self._fh.write(data)
        self._fh.flush()
        sys.__stdout__.write(data)
        return len(data)

    def flush(self) -> None:
        self._fh.flush()
        sys.__stdout__.flush()


sys.stdout = _Tee(_EVIDENCE)


def _ok(subject: str, ok: bool, observed: object) -> bool:
    print(f"  [{'OK ' if ok else 'BAD'}] {subject}: {observed}")
    return ok


async def _queue(repo, tenant: str) -> dict:
    rows = await repo.list_approvals(tenant)
    return {r.id: r for r in rows}


def _fmt_approval(r) -> str:
    d = r.to_dict()
    return json.dumps(
        {
            "kind": d.get("kind"),
            "status": d.get("status"),
            "requested_action": d.get("requested_action"),
            "requester": d.get("requester"),
            "risk_level": d.get("risk_level"),
            "note": (d.get("note") or "")[:60],
        },
        ensure_ascii=False,
    )


async def main() -> int:
    results: list[bool] = []
    settings = get_settings()
    tenant = settings.default_tenant_id
    print(f"=== #7 复验 | profile=memory | {BASE} | tenant={tenant!r} ===")

    from forgeflow.auth.jwt import create_access_token

    token = create_access_token(user_id="verify-admin", role="admin")
    headers = {"Authorization": f"Bearer {token}"}

    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning", lifespan="on", log_config=None)
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    for _ in range(250):
        if server.started:
            break
        await asyncio.sleep(0.1)
    if not server.started:
        print("!!! 服务未能启动")
        return 3

    async with httpx.AsyncClient(base_url=BASE, trust_env=False, timeout=10.0) as client:
        # --- 0. 喂数据：创建两条技能，一条会产建议、一条不会 ---
        skill_repo = get_skill_repository()
        await skill_repo.create_skill(
            SkillRecord(id=_ADVICE_SKILL, tenant_id=tenant, name="复验-低使用技能",
                        domain="general", status="published", usage_count=6)
        )
        await skill_repo.create_skill(
            SkillRecord(id=_HEALTHY_SKILL, tenant_id=tenant, name="复验-健康技能",
                        domain="general", status="published", usage_count=0)
        )
        from forgeflow.skills import evolution as evolution_mod

        evolution_mod.reset_advice_cache()
        print(f"喂数：创建 {_ADVICE_SKILL}(usage=6, 应产建议) + {_HEALTHY_SKILL}(usage=0, 无建议)")

        # --- 1. GET 建议，确认确有 advice ---
        r = await client.get("/skills/evolution-advice", headers=headers)
        advice_body = r.json() if r.status_code == 200 else {}
        items = advice_body.get("items", [])
        adv = next((it for it in items if it["skill_id"] == _ADVICE_SKILL), None)
        print(f"\n[GET /skills/evolution-advice] status={r.status_code} total={advice_body.get('total')}")
        results.append(_ok("目标技能确有建议", adv is not None, json.dumps(adv, ensure_ascii=False) if adv else "NONE"))
        if adv is None:
            await _shutdown(server, serve_task)
            return 4

        # --- 2. 计前 ---
        policy_repo = get_policy_repository()
        before = await _queue(policy_repo, tenant)

        # --- 3. POST 落审批 ---
        url = f"/skills/evolution-advice/{_ADVICE_SKILL}/approval"
        resp = await client.post(url, headers=headers)
        body = resp.json() if resp.content else {}
        print(f"\n[POST {url}] status={resp.status_code} body={json.dumps(body, ensure_ascii=False)[:300]}")

        # --- 4. 计后 + 严格对齐 ---
        after = await _queue(policy_repo, tenant)
        new_ids = [rid for rid in after if rid not in before]
        delta = len(new_ids)
        created = body.get("created")

        results.append(_ok("状态码 == 201", resp.status_code == 201, resp.status_code))
        results.append(_ok("body.created 与队列增量严格相等", created == delta, f"created={created} delta={delta}"))
        results.append(_ok("增量 == 1（该技能 1 条建议）", delta == 1, delta))

        if new_ids:
            row = after[new_ids[0]]
            print("\n新审批行原始内容：")
            print("  " + _fmt_approval(row))
            results.append(_ok("新行 kind ∈ {skill.optimize, skill.retire}", row.kind in ("skill.optimize", "skill.retire"), row.kind))
            results.append(_ok("新行 status == pending", row.status == "pending", row.status))
            results.append(_ok("新行 requested_action 指向该 skill", _ADVICE_SKILL in (row.requested_action or ""), row.requested_action))
            results.append(_ok("新行 requester == 调用者", row.requester == "verify-admin", row.requester))
        else:
            results.append(_ok("存在新行", False, "NONE"))

        # --- 5. 反向断言：技能本体未变 ---
        skill_now = await skill_repo.get_skill(tenant, _ADVICE_SKILL)
        versions_now = await skill_repo.list_versions(tenant, _ADVICE_SKILL)
        # 与 POST 前对比：usage/status/version/名称/spec 均不得变
        results.append(_ok("技能 usage_count 未变 (=6)", skill_now.usage_count == 6, skill_now.usage_count))
        results.append(_ok("技能 status 未变 (published)", skill_now.status == "published", skill_now.status))
        results.append(_ok("技能版本列表未变 (空)", len(versions_now) == 0, len(versions_now)))
        print(f"\n[技能本体] {json.dumps(skill_now.to_dict(), ensure_ascii=False)}")

        # --- 6. 负例：无建议 skill → 404 且队列不变 ---
        before_neg = await _queue(policy_repo, tenant)
        url_neg = f"/skills/evolution-advice/{_HEALTHY_SKILL}/approval"
        resp_neg = await client.post(url_neg, headers=headers)
        after_neg = await _queue(policy_repo, tenant)
        print(f"\n[POST {url_neg}] status={resp_neg.status_code} body={resp_neg.text[:160]}")
        results.append(_ok("负例状态码 == 404", resp_neg.status_code == 404, resp_neg.status_code))
        results.append(_ok("负例队列行数不变", len(after_neg) == len(before_neg), f"{len(before_neg)} -> {len(after_neg)}"))

    await _shutdown(server, serve_task)

    verdict = all(results)
    print(f"\n#7 VERDICT: {'PASS' if verdict else 'FAIL'}  ({sum(results)}/{len(results)} 断言通过)")
    return 0 if verdict else 5


async def _shutdown(server: uvicorn.Server, serve_task: asyncio.Task) -> None:
    server.should_exit = True
    try:
        await asyncio.wait_for(serve_task, timeout=15)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
