"""INC46 T11 — SKILL.md 物化与导出：DB（权威源）→ SKILL.md + bundle。

Scope (六问 ③④):

* **阳性** — 合规七段 spec ⇒ 物化成功、``SKILL.md`` 过 ``validate_skill_md``、
  ``content_hash`` 两次相同、``bundle_zip_bytes`` 两次字节相同（可复算）。
* **阴性** — DB 无该 skill ⇒ **诚实 404**（真跑 FastAPI TestClient + 真路由）；
  缺必需段 / name 非法 / 缺 slug ⇒ :class:`SkillMaterializationError` 且消息含
  具体段名（**禁止静默补全**，**禁止**猜测 slug）。
* **反事实（真跑）** — 改 DB 内容不重导 ⇒ 导出 ``content_hash`` 必变；改回原值
  ⇒ hash 复原（阳性对照）。证明 DB 是权威源、不存在缓存/本地回填。
* **诚实口径** — ``skills-ref`` 缺失 ⇒ ``status=="skipped"`` 且 ``passed is None``
  （绝不为 True/False）；skipped 在告警里显式标注「官方校验未执行」。

契约 ↔ 物化内容通道：T07 段模型对每段 ``extra="forbid"``，故正文内容走显式的
**非契约** 通道（顶层 ``_materialization`` 或内联 ``content`` / ``script_contents``），
在校验前被精确剥离；其余多余字段仍被 T07 如实拒绝。见 ``skill_md`` 模块 docstring。
"""

from __future__ import annotations

import asyncio
import copy
import io
import json
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.main import app as forgeflow_app
from forgeflow.api.routers import skills as skills_router
from forgeflow.auth.jwt import create_access_token
from forgeflow.config import get_settings
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.skills.export_bundle import (
    SkillNotFoundError,
    bundle_to_dict,
    bundle_zip_bytes,
    export_skill_bundle,
    write_bundle,
)
from forgeflow.skills.models import SkillRecord, SkillVersionRecord
from forgeflow.skills.skill_md import (
    SkillMaterializationError,
    compute_content_hash,
    materialize_skill,
)
from forgeflow.skills.spec_validator import validate_skill_md

_TENANT = "default"


# --------------------------------------------------------------------------- #
# fixtures / helpers                                                           #
# --------------------------------------------------------------------------- #
def _contract() -> dict:
    """A fresh, spec-compliant seven-segment contract (合规七段契约)."""
    return {
        "manifest": {
            "name": "demo-skill",
            "display_name": "演示技能",
            "version": "1.0.0",
            "description": (
                "A demo skill that materialises a compliant SKILL.md. "
                "Use when exporting a skill bundle."
            ),
        },
        "knowledge": {"references": [{"file": "guide.md", "summary": "快速指南"}]},
        "procedure": {"steps": ["读取输入", "生成输出"]},
        "policies": {"constraints": ["不得泄露密钥"], "allowed_tools": ["log.write"]},
        "tool_bindings": {"tools": ["data.read"], "scripts": ["run.py"]},
        "evaluation": {"ref": "assets/evals/", "note": "权威记录在 ForgeFlow DB"},
        "examples": {"examples": ["输入 A → 输出 B"]},
    }


def _records(spec: dict, *, skill_id: str = "skill-1", semver: str = "1.0.0"):
    skill = SkillRecord(
        id=skill_id, tenant_id=_TENANT, name="演示技能", current_version=semver
    )
    version = SkillVersionRecord(
        skill_id=skill_id, tenant_id=_TENANT, semver=semver, spec=spec
    )
    return skill, version


class FakeSkillRepo:
    """Minimal repo faithful to the real call surface (``get_skill`` / ``get_version``).

    Uses ``*args`` only, so an unexpected signature never masks a wrong call.
    """

    def __init__(self) -> None:
        self._skills: dict[str, SkillRecord] = {}
        self._versions: dict[tuple[str, str], SkillVersionRecord] = {}

    def add(self, skill: SkillRecord, version: SkillVersionRecord) -> None:
        self._skills[skill.id] = skill
        if version is not None:
            self._versions[(skill.id, version.semver)] = version

    async def get_skill(self, *args, **kwargs) -> SkillRecord | None:
        skill_id = args[-1] if args else kwargs.get("skill_id")
        return self._skills.get(skill_id)

    async def get_version(self, *args, **kwargs) -> SkillVersionRecord | None:
        if len(args) >= 2:
            skill_id, semver = args[-2], args[-1]
        else:
            skill_id, semver = kwargs.get("skill_id"), kwargs.get("semver")
        return self._versions.get((skill_id, semver))


def _route_client() -> TestClient:
    """A minimal app carrying the REAL skills router under RBAC (真路由)."""
    minimal = FastAPI()
    minimal.add_middleware(RBACMiddleware)
    minimal.include_router(skills_router.router, prefix="/skills")
    return TestClient(minimal)


def _auth() -> dict[str, str]:
    token = create_access_token(user_id="admin-1", role="admin", workspace_id=_TENANT)
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# 1. 阳性：物化成功 + 可复算                                                    #
# --------------------------------------------------------------------------- #
def test_positive_materialises_a_valid_skill_md():
    skill, version = _records(_contract())
    bundle = materialize_skill(skill, version)
    assert bundle.slug == "demo-skill"
    assert "SKILL.md" in bundle.files
    report = validate_skill_md(bundle.files["SKILL.md"], parent_dir="demo-skill")
    assert report.ok, report.errors
    assert bundle.validation["ok"] is True
    assert bundle.skill_id == "skill-1" and bundle.version == "1.0.0"


def test_content_hash_is_recomputable_and_stable():
    skill, version = _records(_contract())
    first = materialize_skill(skill, version)
    second = materialize_skill(skill, version)
    assert first.content_hash == second.content_hash, "同版本两次导出内容必须一致"
    assert first.content_hash == compute_content_hash(first.files), "hash 必须可复算"


def test_zip_bytes_are_deterministic_and_readable():
    skill, version = _records(_contract())
    bundle = materialize_skill(skill, version)
    first = bundle_zip_bytes(bundle)
    second = bundle_zip_bytes(bundle)
    assert first == second, "确定性 zip：两次调用必须逐字节相同"
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert "SKILL.md" in archive.namelist()
        assert archive.read("SKILL.md").decode("utf-8") == bundle.files["SKILL.md"]


def test_write_bundle_writes_the_tree_to_disk(tmp_path):
    skill, version = _records(_contract())
    bundle = materialize_skill(skill, version)
    root = write_bundle(bundle, tmp_path)
    assert root == tmp_path / "demo-skill"
    assert (root / "SKILL.md").is_file()
    assert (root / "SKILL.md").read_text(encoding="utf-8") == bundle.files["SKILL.md"]


# --------------------------------------------------------------------------- #
# 2. 内容通道：有内容才落盘，无内容不伪造                                       #
# --------------------------------------------------------------------------- #
def test_reference_content_is_materialised_via_sidecar():
    spec = _contract()
    spec["_materialization"] = {"references": {"guide.md": "# 指南正文\n这是知识正文。"}}
    skill, version = _records(spec)
    bundle = materialize_skill(skill, version)
    assert bundle.files.get("references/guide.md", "").startswith("# 指南正文")


def test_reference_content_inline_alias_is_materialised():
    spec = _contract()
    spec["knowledge"]["references"][0]["content"] = "inline body"
    skill, version = _records(spec)
    bundle = materialize_skill(skill, version)
    assert bundle.files.get("references/guide.md") == "inline body"


def test_script_content_is_materialised_when_provided():
    spec = _contract()
    spec["tool_bindings"]["script_contents"] = {"run.py": "print('hi')\n"}
    skill, version = _records(spec)
    bundle = materialize_skill(skill, version)
    assert bundle.files.get("scripts/run.py") == "print('hi')\n"


def test_absent_content_is_not_fabricated_but_declared():
    skill, version = _records(_contract())
    bundle = materialize_skill(skill, version)
    assert "references/guide.md" not in bundle.files
    assert "scripts/run.py" not in bundle.files
    joined = "\n".join(bundle.not_materialised)
    assert "references/guide.md" in joined
    assert "scripts/run.py" in joined
    assert "assets/evals/" in joined


# --------------------------------------------------------------------------- #
# 3. 阴性：显式报错，不静默补全 / 不猜测 slug                                    #
# --------------------------------------------------------------------------- #
def test_missing_required_segment_raises_explicit_error():
    spec = _contract()
    del spec["examples"]
    skill, version = _records(spec)
    with pytest.raises(SkillMaterializationError) as exc:
        materialize_skill(skill, version)
    assert "examples" in str(exc.value), "错误必须点名缺失的段"


def test_illegal_manifest_name_raises_explicit_error():
    spec = _contract()
    spec["manifest"]["name"] = "Demo Skill"  # 大写 + 空格 ⇒ 非法 slug
    skill, version = _records(spec)
    with pytest.raises(SkillMaterializationError) as exc:
        materialize_skill(skill, version)
    assert "name" in str(exc.value)


def test_missing_slug_is_refused_not_guessed():
    spec = _contract()
    del spec["manifest"]["name"]
    skill, version = _records(spec)
    with pytest.raises(SkillMaterializationError) as exc:
        materialize_skill(skill, version)
    assert "slug" in str(exc.value), "缺 slug 必须显式拒绝，绝不猜测"


def test_chinese_display_name_is_never_used_as_slug():
    spec = _contract()
    del spec["manifest"]["name"]
    spec["manifest"]["display_name"] = "文档处理"
    skill, version = _records(spec)
    with pytest.raises(SkillMaterializationError):
        materialize_skill(skill, version)


# --------------------------------------------------------------------------- #
# 4. export_skill_bundle：诚实 404（无缓存）                                    #
# --------------------------------------------------------------------------- #
async def test_unknown_skill_raises_notfound():
    repo = FakeSkillRepo()
    with pytest.raises(SkillNotFoundError):
        await export_skill_bundle("missing", _TENANT, repo=repo)


async def test_skill_without_current_version_raises_notfound():
    repo = FakeSkillRepo()
    repo.add(SkillRecord(id="s1", tenant_id=_TENANT, current_version=None), None)  # type: ignore[arg-type]
    with pytest.raises(SkillNotFoundError):
        await export_skill_bundle("s1", _TENANT, repo=repo)


# --------------------------------------------------------------------------- #
# 5. 反事实：DB 是权威源（改 DB 不重导 ⇒ 导出必变；改回 ⇒ 复原）                 #
# --------------------------------------------------------------------------- #
async def test_db_is_authoritative_and_uncached():
    spec = _contract()
    skill, version = _records(spec)
    repo = FakeSkillRepo()
    repo.add(skill, version)

    before = await export_skill_bundle("skill-1", _TENANT, repo=repo)

    # 改 DB 内容（同一 version 对象），不重导/不动任何缓存：
    version.spec["procedure"]["steps"] = ["读取输入", "生成输出", "新增步骤"]
    after = await export_skill_bundle("skill-1", _TENANT, repo=repo)
    assert after.content_hash != before.content_hash, "改 DB 内容后导出必须变化"

    # 阳性对照：改回原值 ⇒ hash 复原。
    version.spec["procedure"]["steps"] = ["读取输入", "生成输出"]
    restored = await export_skill_bundle("skill-1", _TENANT, repo=repo)
    assert restored.content_hash == before.content_hash, "改回原值后导出必须复原"


# --------------------------------------------------------------------------- #
# 6. skills-ref：缺失 ⇒ 诚实 skipped（绝不冒充 PASS，红线 10）                   #
# --------------------------------------------------------------------------- #
def test_skills_ref_absent_is_skipped_not_pass(monkeypatch):
    import forgeflow.skills.spec_validator as sv

    monkeypatch.setattr(sv.shutil, "which", lambda *a, **k: None)
    skill, version = _records(_contract())
    bundle = materialize_skill(skill, version)
    assert bundle.skills_ref["status"] == "skipped"
    assert bundle.skills_ref["passed"] is None, "未测量 ⇒ None，绝不为 True/False"
    assert any("未执行" in w for w in bundle.warnings), "skipped 必须在告警里显式可见"


# --------------------------------------------------------------------------- #
# 7. JSON 友好                                                                  #
# --------------------------------------------------------------------------- #
def test_bundle_to_dict_is_json_serialisable():
    skill, version = _records(_contract())
    payload = bundle_to_dict(materialize_skill(skill, version))
    text = json.dumps(payload, ensure_ascii=False)
    decoded = json.loads(text)
    assert decoded["slug"] == "demo-skill"
    for key in ("files", "content_hash", "validation", "skills_ref", "not_materialised"):
        assert key in decoded, f"{key} 必须在 JSON 输出中"


# --------------------------------------------------------------------------- #
# 8. 路由：真跑 FastAPI TestClient（扩展既有端点，默认语义不变）                #
# --------------------------------------------------------------------------- #
def test_route_bundle_unknown_skill_returns_honest_404():
    from forgeflow.repositories.memory.skill_repo import clear_skill_store

    clear_skill_store()
    resp = _route_client().get(
        "/skills/definitely-missing/export?format=bundle", headers=_auth()
    )
    assert resp.status_code == 404, "DB 无该 skill ⇒ 诚实 404，禁止空壳 SKILL.md"


def test_route_default_json_unknown_skill_is_still_404():
    from forgeflow.repositories.memory.skill_repo import clear_skill_store

    clear_skill_store()
    resp = _route_client().get("/skills/definitely-missing/export", headers=_auth())
    assert resp.status_code == 404, "默认 json 分支语义未变（既有 404 行为保留）"


def test_route_bundle_and_zip_positive_on_real_router(force_memory_backend):
    """真路由 + 真 memory 仓储（同一事件循环内注入，避免跨循环锁问题）。"""
    from forgeflow.repositories import get_skill_repository
    from forgeflow.repositories.memory.skill_repo import clear_skill_store

    async def _drive() -> None:
        clear_skill_store()
        repo = get_skill_repository()
        tenant = get_settings().default_tenant_id
        skill = SkillRecord(
            id="skill-route-1", tenant_id=tenant, name="路由技能", current_version="1.0.0"
        )
        version = SkillVersionRecord(
            skill_id=skill.id, tenant_id=tenant, semver="1.0.0", spec=_contract()
        )
        await repo.create_skill(skill)
        await repo.add_version(tenant, version)

        # 旧默认分支：既有 JSON 文档形状逐键保留。
        default_payload = await skills_router.export_skill(
            "skill-route-1", tenant=tenant
        )
        assert default_payload["format"] == "forgeflow.skill"
        assert default_payload["format_version"] == 1
        assert "skill" in default_payload and "version" in default_payload

        # 扩展分支：bundle 的 JSON。
        bundle_payload = await skills_router.export_skill(
            "skill-route-1", format="bundle", tenant=tenant
        )
        assert bundle_payload["slug"] == "demo-skill"
        assert "SKILL.md" in bundle_payload["files"]

        # 扩展分支：zip 字节。
        zip_response = await skills_router.export_skill(
            "skill-route-1", format="zip", tenant=tenant
        )
        assert zip_response.media_type == "application/zip"
        with zipfile.ZipFile(io.BytesIO(zip_response.body)) as archive:
            assert "SKILL.md" in archive.namelist()
        clear_skill_store()

    asyncio.run(_drive())


def test_route_default_json_positive_matches_legacy_shape(force_memory_backend):
    """默认 json 分支在**有 skill** 时也保持既有逐键形状（回归证据）。"""
    from forgeflow.repositories import get_skill_repository
    from forgeflow.repositories.memory.skill_repo import clear_skill_store

    async def _drive() -> None:
        clear_skill_store()
        repo = get_skill_repository()
        tenant = get_settings().default_tenant_id
        skill = SkillRecord(
            id="skill-legacy-1", tenant_id=tenant, name="旧技能", current_version="1.0.0"
        )
        version = SkillVersionRecord(
            skill_id=skill.id, tenant_id=tenant, semver="1.0.0", spec={"prompt": "p"}
        )
        await repo.create_skill(skill)
        await repo.add_version(tenant, version)
        payload = await skills_router.export_skill("skill-legacy-1", tenant=tenant)
        assert set(payload) == {
            "format",
            "format_version",
            "exported_at",
            "skill",
            "version",
        }
        clear_skill_store()

    asyncio.run(_drive())


def test_openapi_registers_export_route():
    """既有端点的路由注册未变（扩展而非新增：路径仍是 /skills/{skill_id}/export）。"""
    assert "/skills/{skill_id}/export" in forgeflow_app.openapi()["paths"]
