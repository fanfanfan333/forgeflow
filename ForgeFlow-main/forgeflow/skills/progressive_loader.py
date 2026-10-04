"""INC46 T30 — 渐进披露加载（Progressive Disclosure）。

What this module is
-------------------
Skill **不再一次性全量注入**。把一个 skill 的注入拆成三层，按需加载：

* **L1 元数据** — ``slug`` / ``display_name`` / ``description`` / **一行摘要**。
  **常驻**内存（体积可控），构成可检索的「清单」。构建清单**绝不**触碰
  ``SKILL.md`` 正文。
* **L2 正文** — ``SKILL.md`` 主体（步骤 / 约束 / 工具绑定 / 示例 …）。
  **按需**：只有被选中 / 被显式请求的 skill 才加载。
* **L3 资源** — ``references/*`` / ``scripts/*`` 等附属文件。
  仅在**显式引用**（显式给出 ``ref``）时加载。

每次加载都**记账**（:class:`LoadRecord`）记录加载了哪些层、字节数与条目数。

红线 19（本任务的承重指标）
---------------------------
**未选中 skill 的正文加载计数必须 = 0。** 该计数由 :meth:`LoadLedger.body_load_count`
返回，是**实测**出来的（数着加载次数），因此 ``0`` 是「真的加载了 0 次」的**已测量
零**，而非「未测量」。

诚实纪律（INC46 §8 红线 4）
---------------------------
* **未测量 ⇒ ``None``，禁止写 ``0``。** :class:`LoadRecord` 的 ``bytes`` /
  ``entries`` 在「没测到」时为 ``None``（例如请求一个不存在的 skill —— 没有任何
  内容可量）；**绝不**用 ``0`` 冒充未测量。
* 「已经量出来的 0 次加载」与「未测量的字节数」是两件事，不得相互冒充：
  前者是计数（``body_load_count``），后者是体积（``LoadRecord.bytes``）。
* 超预算时 :func:`assert_within_budget` **显式抛错**（:class:`ProgressiveBudgetError`），
  **绝不静默截断 / 静默丢弃**。

复用既有接缝（不重复造轮子）
----------------------------
* 物化：默认注入点 = T11 的 :func:`forgeflow.skills.skill_md.materialize_skill`
  —— **默认路径真跑 T11 物化**，产出 ``SkillBundle``（``files`` 必含 ``SKILL.md``）。
* 引用归一化：T07/T18 的 :func:`forgeflow.skills.segments.normalize_reference`。
* 记录类型：T07/T11 的 :class:`SkillRecord` / :class:`SkillVersionRecord`
  （``spec`` 是七段契约）；本条**不新增任何模型**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from forgeflow.skills.segments import normalise_segment_key, normalize_reference
from forgeflow.skills.skill_md import SkillBundle, materialize_skill

__all__ = [
    "LAYER_METADATA",
    "LAYER_BODY",
    "LAYER_RESOURCE",
    "LoadRecord",
    "LoadLedger",
    "ProgressiveBudgetError",
    "ProgressiveLoader",
    "budget_violations",
    "assert_within_budget",
]

#: 三个披露层的稳定标识（用于记账的 ``layers`` 字段）。
LAYER_METADATA = "L1"
LAYER_BODY = "L2"
LAYER_RESOURCE = "L3"

#: 一行摘要的最大字符数（超出即截断，只在**清单**层截断，正文整份保留）。
_SUMMARY_MAX = 120


def _one_line(text: str) -> str:
    """Collapse ``text`` to a single, length-bounded line (清单摘要).

    取第一行 / 第一句，压掉多余空白，超长则截断到 :data:`_SUMMARY_MAX`。这不是
    正文 —— 只是给常驻清单用的一行摘要。
    """
    raw = " ".join(str(text or "").split())
    if not raw:
        return ""
    for sep in ("。", "！", "？", ". ", "!", "?"):
        idx = raw.find(sep)
        if idx != -1:
            raw = raw[: idx]
            break
    return raw[:_SUMMARY_MAX]


def _as_version_map(versions: Any) -> dict[str, Any]:
    """Normalise ``versions`` into ``{skill_id: version_record}``.

    Accepts a mapping (``skill_id -> record``) or any iterable of records carrying
    a ``skill_id`` attribute. Unknown shapes are dropped silently **only** here —
    a missing version is later reported honestly (``None``), never fabricated.
    """
    if versions is None:
        return {}
    if isinstance(versions, Mapping):
        return {str(k): v for k, v in versions.items()}
    out: dict[str, Any] = {}
    for record in versions:
        sid = getattr(record, "skill_id", None)
        if sid is not None:
            out[str(sid)] = record
    return out


def _measure_index_bytes(entries: Sequence[Mapping[str, Any]]) -> int:
    """The measured UTF-8 byte size of a resident L1 index (deterministic JSON)."""
    return len(
        json.dumps(list(entries), ensure_ascii=False, sort_keys=True).encode("utf-8")
    )


# --------------------------------------------------------------------------- #
# 记账                                                                         #
# --------------------------------------------------------------------------- #
@dataclass
class LoadRecord:
    """One accounted load: which layer(s), how many bytes, how many entries.

    ``bytes`` / ``entries`` are ``None`` when the quantity was **not measured**
    (红线 4：未测量 ⇒ None，禁止写 0)。``layers`` 里每一项形如 ``"L1"`` /
    ``"L2"`` / ``"L3:references/guide.md"``。
    """

    skill_id: str
    layers: list[str]
    bytes: int | None = None
    entries: int | None = None


@dataclass
class LoadLedger:
    """The append-only ledger of everything the loader ever read.

    ``l1_loaded`` / ``l2_loaded`` 记录 **skill_id**；``l3_loaded`` 记录
    ``"<skill_id>:<ref>"``（资源是「某个 skill 的某个文件」，需保留 ref 才有意义）。
    """

    records: list[LoadRecord] = field(default_factory=list)
    l1_loaded: list[str] = field(default_factory=list)
    l2_loaded: list[str] = field(default_factory=list)
    l3_loaded: list[str] = field(default_factory=list)

    def body_load_count(self, skill_id: str) -> int:
        """How many times ``skill_id``'s **L2 body** was loaded.

        这是本任务的红线指标：**未选中 skill 必须返回 ``0``**。返回值是**实测**
        计数（数着加载次数），故 ``0`` 是已测量的零，不是「未测量」。
        """
        return self.l2_loaded.count(str(skill_id))

    def resource_load_count(self, skill_id: str, ref: str | None = None) -> int:
        """How many L3 resources of ``skill_id`` were loaded (optionally per ``ref``)."""
        sid = str(skill_id)
        if ref is None:
            return sum(1 for tagged in self.l3_loaded if tagged.startswith(f"{sid}:"))
        target = f"{sid}:{normalize_reference(ref)}"
        return self.l3_loaded.count(target)

    def measured_bytes(self) -> int:
        """Sum of the **measured** byte counts (unmeasured records contribute 0 here).

        未测量记录（``bytes is None``）**不计入**该总量 —— 它们不是 0 字节，而是
        「不知道多少字节」；由 :func:`budget_violations` 单独点名，不被静默吞掉。
        """
        return sum(rec.bytes for rec in self.records if rec.bytes is not None)

    def unmeasured_records(self) -> list[LoadRecord]:
        """Records whose byte/entry counts were not measured (``None``)."""
        return [
            rec
            for rec in self.records
            if rec.bytes is None or rec.entries is None
        ]


class ProgressiveBudgetError(Exception):
    """Raised when a load ledger exceeds its byte budget.

    This is the *explicit* failure mode demanded by T30 — an over-budget load is
    reported, never silently truncated or dropped. The message names the measured
    total and the limit so the breach is reproducible.
    """


# --------------------------------------------------------------------------- #
# 预算                                                                         #
# --------------------------------------------------------------------------- #
def budget_violations(ledger: LoadLedger, *, max_bytes: int) -> list[str]:
    """Structured budget violations — empty list means "within budget".

    * 超过预算 ⇒ 一条含**实测**总量与超出的违规；
    * 存在未测量记录 ⇒ 一条点名记录数的提示（未测量不计入总量，但不被吞掉）。
    """
    violations: list[str] = []
    total = ledger.measured_bytes()
    if total > max_bytes:
        violations.append(
            f"加载字节总量 {total} 超过预算 {max_bytes}（超出 {total - max_bytes} 字节）"
        )
    unmeasured = ledger.unmeasured_records()
    if unmeasured:
        named = "；".join(
            f"{rec.skill_id}[{'/'.join(rec.layers)}]" for rec in unmeasured[:5]
        )
        violations.append(
            f"{len(unmeasured)} 条加载记录字节未测量（未测量 ⇒ None，不计入总量）：{named}"
        )
    return violations


def assert_within_budget(ledger: LoadLedger, *, max_bytes: int) -> None:
    """Assert the ledger's **measured** bytes are within ``max_bytes``.

    Raises:
        ProgressiveBudgetError: when the measured total exceeds the budget.
            The exception is raised — the load is never silently truncated.
    """
    over = [v for v in budget_violations(ledger, max_bytes=max_bytes) if "超过预算" in v]
    if over:
        raise ProgressiveBudgetError("；".join(over))


# --------------------------------------------------------------------------- #
# 加载器                                                                       #
# --------------------------------------------------------------------------- #
class ProgressiveLoader:
    """Three-layer, lazily-loaded skill injector (T30).

    Args:
        skills: 可选的 ``SkillRecord`` 列表（也可随后通过 :meth:`l1_index` 提供）。
        versions: 可选的 ``{skill_id: SkillVersionRecord}`` 或记录序列。
        repo: 可选仓储；仅当 skills/versions 未直接给出时，:meth:`resolve_versions`
            会用它补齐当前版本（默认路径不依赖它）。
        materializer: 物化注入点，**默认 = T11 的** :func:`materialize_skill`。
        ledger: 复用既有账本（默认新建）。

    默认路径**真跑 T11 物化**：:meth:`load_body` / :meth:`load_resource` 调用的就是
    注入的（默认 T11）物化器，产出 ``SKILL.md`` 正文与附属文件。
    """

    def __init__(
        self,
        *,
        skills: Iterable[Any] | None = None,
        versions: Any | None = None,
        repo: Any | None = None,
        materializer: Any = materialize_skill,
        ledger: LoadLedger | None = None,
    ) -> None:
        self._skills: dict[str, Any] = {}
        for skill in skills or ():
            self._skills[str(getattr(skill, "id", "") or "")] = skill
        self._versions: dict[str, Any] = _as_version_map(versions)
        self._repo = repo
        self._materialize = materializer
        self.ledger: LoadLedger = ledger if ledger is not None else LoadLedger()
        self._bundle_cache: dict[str, SkillBundle] = {}

    # -- L1 ----------------------------------------------------------------- #
    def l1_index(self, skills: Sequence[Any], versions: Any | None = None) -> list[dict]:
        """Build the **resident metadata index** — never reads a ``SKILL.md`` body.

        每个条目只含元数据 + 一行摘要（``slug`` / ``display_name`` /
        ``description`` / ``summary``），**不触发任何物化**，因此未选中 skill 的
        正文加载计数恒为 0。

        Returns:
            ``list[dict]``，每个 dict 是一个 skill 的 L1 条目。
        """
        if versions is not None:
            self._versions.update(_as_version_map(versions))

        entries: list[dict] = []
        for skill in skills:
            sid = str(getattr(skill, "id", "") or "")
            self._skills[sid] = skill
            entries.append(self._metadata_entry(skill))

        self.ledger.records.append(
            LoadRecord(
                skill_id="*",
                layers=[LAYER_METADATA],
                bytes=_measure_index_bytes(entries),
                entries=len(entries),
            )
        )
        for entry in entries:
            self.ledger.l1_loaded.append(entry["skill_id"])
        return entries

    def known_skill_ids(self) -> list[str]:
        """The skill ids the loader currently knows about (deterministic order)."""
        return sorted(self._skills)

    def _manifest(self, skill_id: str) -> Mapping[str, Any]:
        """The manifest segment of ``skill_id``'s version spec, or ``{}`` (metadata only)."""
        version = self._versions.get(str(skill_id))
        spec = getattr(version, "spec", None)
        if not isinstance(spec, Mapping):
            return {}
        for key, value in spec.items():
            if normalise_segment_key(key) == "manifest" and isinstance(value, Mapping):
                return value
        return {}

    def _metadata_entry(self, skill: Any) -> dict:
        sid = str(getattr(skill, "id", "") or "")
        manifest = self._manifest(sid)
        # slug 只从 manifest.name 取，绝不猜测（A3：slug 系统生成并锁定）。
        slug = str(manifest.get("name") or "").strip()
        display_name = (
            str(manifest.get("display_name") or "").strip()
            or str(getattr(skill, "name", "") or "")
        )
        description = (
            str(manifest.get("description") or "").strip()
            or str(getattr(skill, "description", "") or "")
        )
        return {
            "skill_id": sid,
            "slug": slug,
            "display_name": display_name,
            "description": description,
            "summary": _one_line(description),
            "layer": LAYER_METADATA,
        }

    # -- L2 ----------------------------------------------------------------- #
    def load_body(self, skill_id: str) -> str | None:
        """Load the ``SKILL.md`` **body** of ``skill_id`` — only on explicit request.

        只有被显式请求的 skill 才会走到物化；不存在的 skill 返回 ``None`` 并记一条
        **未测量**记录（``bytes=None``，绝不写 0）。成功加载则记实测字节 / 行数，
        并把 ``skill_id`` 计入 ``ledger.l2_loaded``。
        """
        sid = str(skill_id)
        skill = self._skills.get(sid)
        bundle = self._bundle(sid) if skill is not None else None
        body = bundle.files.get("SKILL.md") if bundle is not None else None

        if body is None:
            self.ledger.records.append(
                LoadRecord(skill_id=sid, layers=[LAYER_BODY])  # 未测量 ⇒ None
            )
            return None

        self.ledger.l2_loaded.append(sid)
        self.ledger.records.append(
            LoadRecord(
                skill_id=sid,
                layers=[LAYER_BODY],
                bytes=len(body.encode("utf-8")),
                entries=len(body.splitlines()),
            )
        )
        return body

    def load_selected(self, selected_ids: Sequence[str]) -> dict[str, str]:
        """Load bodies for the **selected** skills only (the positive path).

        This is the load-bearing entry point: it is called with the *selection set*
        so that未选中的 skill 永远不会被请求正文，其 ``body_load_count`` 恒为 0。
        """
        bodies: dict[str, str] = {}
        for sid in selected_ids:
            body = self.load_body(sid)
            if body is not None:
                bodies[str(sid)] = body
        return bodies

    # -- L3 ----------------------------------------------------------------- #
    def load_resource(self, skill_id: str, ref: str) -> str | None:
        """Load one resource of ``skill_id`` — **requires an explicit** ``ref``.

        Args:
            skill_id: 资源所属 skill。
            ref: 显式引用（如 ``"references/guide.md"`` / ``"guide.md"``）。空引用
                被视为「隐式批量加载」而**显式拒绝**（T30：L3 只在明确引用时加载）。

        Raises:
            ValueError: ``ref`` 为空。
        """
        if not str(ref or "").strip():
            raise ValueError(
                "L3 资源加载必须显式给出 ref（禁止隐式 / 批量加载 —— 渐进披露："
                "资源只在明确引用时才加载）"
            )
        sid = str(skill_id)
        path = normalize_reference(ref)
        skill = self._skills.get(sid)
        bundle = self._bundle(sid) if skill is not None else None
        content = bundle.files.get(path) if bundle is not None else None

        if content is None:
            self.ledger.records.append(
                LoadRecord(skill_id=sid, layers=[f"{LAYER_RESOURCE}:{path}"])  # 未测量
            )
            return None

        self.ledger.l3_loaded.append(f"{sid}:{path}")
        self.ledger.records.append(
            LoadRecord(
                skill_id=sid,
                layers=[f"{LAYER_RESOURCE}:{path}"],
                bytes=len(content.encode("utf-8")),
                entries=len(content.splitlines()),
            )
        )
        return content

    # -- 物化（默认真跑 T11） ------------------------------------------------- #
    def _bundle(self, skill_id: str) -> SkillBundle | None:
        """Materialise (and memoise) ``skill_id``'s bundle; ``None`` on inability.

        物化失败（缺版本 / 契约非法 …）**不抛穿调用方**，而是返回 ``None`` —— 调用方
        会如实记一条**未测量**记录，绝不伪造空正文。
        """
        sid = str(skill_id)
        if sid in self._bundle_cache:
            return self._bundle_cache[sid]
        skill = self._skills.get(sid)
        version = self._versions.get(sid)
        if skill is None or version is None:
            return None
        try:
            bundle = self._materialize(skill, version)
        except Exception:  # noqa: BLE001 — 单个 skill 物化失败不得拖垮加载器
            return None
        if bundle is not None:
            self._bundle_cache[sid] = bundle
        return bundle

    # -- repo 补齐（可选路径） ------------------------------------------------- #
    async def resolve_versions(self, tenant_id: str) -> None:
        """Fill missing current-version records from ``repo`` (optional path).

        只有当 :class:`ProgressiveLoader` 传入了 ``repo`` 才有效；默认（无 repo）为
        空操作。补齐后 :meth:`load_body` 走的就是仓储里的真实版本 + T11 物化。
        """
        if self._repo is None:
            return
        for sid, skill in self._skills.items():
            if sid in self._versions:
                continue
            try:
                versions = await self._repo.list_versions(tenant_id, sid)
            except Exception:  # noqa: BLE001
                continue
            if not versions:
                continue
            current = getattr(skill, "current_version", None)
            chosen = next(
                (v for v in versions if getattr(v, "semver", None) == current),
                versions[-1],
            )
            self._versions[sid] = chosen
