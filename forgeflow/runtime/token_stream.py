"""RunTokenStream — 按 run 的 token 增量旁路通道（**不进** RunEventBus 环形历史）。

设计（C1）：
  * 与 RunEventBus 完全隔离：token 帧只进本模块的**有界合并队列**，永不进
    ``RunEventBus._history``（否则 452 帧会把 run.started/step/observation 整段逐出）；
  * 生产者（executor）用 ``publish`` 非阻塞写入；队列满时**合帧**（把增量并入同轮
    已排队帧）或**丢弃最旧 token 帧**，绝不阻塞模型流（不 backpressure 到 llama）；
  * 消费者（SSE 出口）用 ``frames()`` 异步迭代，``close()`` 后产出哨兵结束；
  * 进程级单例 + ``_MAX_RUNS`` 上限，内存不随 run 数无限增长。
纯 stdlib（asyncio + json）。

实现注记（对应 DESIGN §3.1 的「实现备注」）
------------------------------------------
``asyncio.Queue`` 不暴露队尾，无法同时满足「合帧」「丢最旧 token 帧」「控制帧永不丢」
三条语义。因此内部缓冲改为**有界 ``collections.deque`` + ``asyncio.Event``**：
  * ``publish`` 只在纯同步代码里改 deque（同一事件循环，无需锁），随后 ``set()`` 唤醒消费者；
  * ``frames()`` 用 ``Event.wait()`` 阻塞，队列空时睡眠、有帧时唤醒；
  * ``close()`` 追加 ``None`` 哨兵——deque 无 ``maxsize`` 硬上限，**哨兵必达**，无需
    DESIGN 里那套「QueueFull 时反复丢帧腾位」的兜底（其契约等价实现，见下）。
对外 5 条契约：
  ① token 帧可合并/可丢弃并**绝不阻塞**；
  ② ``EVENT_TURN``（控制帧）**永不丢**；
  ③ ``close()`` 后 ``frames()`` 必结束（哨兵必达）；
  ④ 内存上界 = ``_MAX_QUEUE``(64) 帧 × 帧文本 + ``_MAX_RUNS``(32) 个 run；
  ⑤ **禁止挖洞**：token 帧的丢弃必须保持**每个 turn 内 ``fragment`` 的拼接结果不变**；
     队列满时**优先同 turn 合并**（无损），只有在「每个 turn 仅剩一帧」的极端情形才
     退回丢最旧 token 帧（有损，会 ``logger.warning``）。理由：队列满的前提是消费者
     尚未取走旧帧，直接丢「最旧」会在已排队序列**中间**制造空洞 ⇒ 前端累积文本缺字，
     违反 AC-3（``conv-inline-answer`` 单调前缀增长）与 E2（收即渲染 = 屏幕文本恒等于
     已到达文本的拼接；到达的内容本身不得被静默篡改）。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections import deque
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "EVENT_TOKEN",
    "EVENT_TURN",
    "CHANNEL_PENDING",
    "CHANNEL_NARRATION",
    "CHANNEL_ANSWER",
    "CHANNEL_SUPPRESSED",
    "TokenFrame",
    "RunTokenStream",
    "TokenStreamRegistry",
    "get_token_registry",
    "reset_token_registry",
]

#: 每个 run 的 token 帧队列上限（满则合帧 / 丢最旧 token 帧）。
_MAX_QUEUE: int = 64
#: 同时跟踪的 run 上限（内存天花板；超出时回收最旧的**已关闭** run）。
_MAX_RUNS: int = 32

#: 两个**新增** SSE 事件名（纯增量；旧前端遇未知 type 必须能安全忽略）。
EVENT_TOKEN: str = "run.token"
EVENT_TURN: str = "run.turn"

#: 帧 / 轮归类用的 channel 取值。
CHANNEL_PENDING: str = "pending"      # 流式中，归类未定（轮内不可预知）
CHANNEL_NARRATION: str = "narration"  # 轮末判定：本轮是工具调用轮的过渡文本（旁白）
CHANNEL_ANSWER: str = "answer"        # 轮末判定：本轮是收敛轮（最终答案）
CHANNEL_SUPPRESSED: str = "suppressed"  # 命中疑似工具语法，该轮停发


@dataclass
class TokenFrame:
    """一帧 token 通道事件（run.token / run.turn）。"""

    run_id: str
    event: str                  # EVENT_TOKEN | EVENT_TURN
    data: dict[str, Any]        # 见 §3.2 payload schema
    seq: int = 0                # per-run 单调（token 序列，独立于 bus 的 seq）
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "type": self.event,
                "data": self.data, "seq": self.seq, "ts": self.ts}

    def to_sse(self) -> str:
        return f"data: {json.dumps(self.to_dict(), ensure_ascii=False)}\n\n"


class RunTokenStream:
    """单个 run 的有界、可合并 token 增量缓冲。

    生产者（executor）与消费者（SSE 出口）只经内部缓冲通信，互不感知。
    全部方法都在**同一事件循环**里运行（进程单例），因此无需加锁。
    """

    def __init__(self, run_id: str, *, maxsize: int = _MAX_QUEUE) -> None:
        self.run_id = run_id
        self._maxsize = max(1, int(maxsize))
        #: 内部缓冲：有界 ``deque``（长度由 ``publish`` 主动裁剪到 ``_maxsize``）。
        #: 元素为 ``TokenFrame``，末尾的 ``None`` 是 ``close()`` 的哨兵。
        self._queue: deque[TokenFrame | None] = deque()
        self._seq = itertools.count(1)
        self._closed = False
        self._closed_sentinel_queued = False
        #: 消费者唤醒信号：有帧可读 / 已关闭时置位。
        self._ready = asyncio.Event()

    # -- 生产者（executor 侧） ------------------------------------------------ #
    def publish(self, event: str, data: dict[str, Any]) -> None:
        """写入一帧。**绝不阻塞**：满时合帧（token）或丢最旧 token 帧。

        * ``EVENT_TURN``（控制帧）**永不丢弃**：先把同轮已排队的 token 帧移除（控制帧
          自带权威全文，这些碎片帧冗余），腾出空间；仍满则丢最旧的 ``EVENT_TOKEN`` 帧。
        * ``EVENT_TOKEN``：若队尾是同轮同 channel 的 token 帧，直接并入其
          ``fragment``（合帧，避免碎片帧爆炸）；否则入队；满则**优先同 turn 合并**
          （无损，见 ``_merge_same_turn_pair``），仅在无同 turn 对时才丢最旧 token 帧
          （有损，告警）；若连可丢的 token 帧都没有，则丢弃**本帧**以严格守住内存上界
          （**契约⑤：绝不制造空洞**）。
        """
        if self._closed:
            return
        frame = TokenFrame(run_id=self.run_id, event=event,
                           data=dict(data), seq=next(self._seq))
        if len(self._queue) < self._maxsize:
            self._queue.append(frame)
        elif event == EVENT_TOKEN:
            self._coalesce_into_tail(frame)
        else:
            self._make_room_for_control(frame)
        self._ready.set()

    def _coalesce_into_tail(self, frame: TokenFrame) -> None:
        """尝试把 token 帧并入队尾（同轮同 channel）；否则**优先同轮合并**腾位。

        契约（禁止挖洞）：丢帧必须保持**每个 turn 内 fragment 的拼接结果不变**，
        禁止在已排队序列中间制造空洞。因为队列满的前提是消费者尚未取走旧帧，
        直接丢「最旧 token 帧」会让前端累积文本缺字（违反 AC-3 单调前缀增长 / E2
        收即渲染）。因此优先把**最旧的一对同 turn token 帧**合并（前一个的 fragment
        追加到后一个，顺序不可反）来减少帧数。
        """
        tail = self._queue[-1] if self._queue else None
        if (
            tail is not None
            and tail.event == EVENT_TOKEN
            and tail.data.get("turn") == frame.data.get("turn")
            and tail.data.get("channel") == frame.data.get("channel")
        ):
            # 合帧：把增量并入队尾同轮 token 帧的 ``fragment``。
            tail.data["fragment"] = str(tail.data.get("fragment", "")) + str(
                frame.data.get("fragment", "")
            )
            return
        # 队尾非同轮 token 帧 ⇒ 先合并一对同 turn token 帧（无损）腾位。
        if self._merge_same_turn_pair():
            self._queue.append(frame)
            return
        # 极端：不存在同 turn 的两个 token 帧（每个 turn 仅剩一帧）⇒ 有损路径。
        if self._drop_oldest_token():
            logger.warning(
                "RunTokenStream[%s]: token queue full with no same-turn pair to "
                "coalesce; dropping the oldest token frame (LOSSY)",
                self.run_id,
            )
            self._queue.append(frame)
        # 连可丢的 token 帧都没有（全是控制帧）⇒ 丢弃本帧，严格守住内存上界。

    def _make_room_for_control(self, frame: TokenFrame) -> None:
        """为控制帧腾位：先移除同轮 token 帧，再**优先同轮合并**，控制帧必入队。

        同轮 token 帧整体移除是**安全**的：``run.turn.text`` 是本轮权威全文，
        前端会用它整段校正该轮文本（前缀扩展，非挖洞）。
        """
        turn = frame.data.get("turn")
        # 同轮 token 帧冗余（控制帧 text 已是本轮权威全文）。
        self._drop_token_frames(lambda f: f.data.get("turn") == turn)
        # 仍满：先无损同轮合并，无同轮对时才丢最旧 token 帧（有损，告警）。
        while len(self._queue) >= self._maxsize:
            if self._merge_same_turn_pair():
                continue
            if self._drop_oldest_token():
                logger.warning(
                    "RunTokenStream[%s]: control frame forced a lossy drop of the "
                    "oldest token frame",
                    self.run_id,
                )
                continue
            break
        self._queue.append(frame)

    def _merge_same_turn_pair(self) -> bool:
        """合并队列中**最旧的一对同 turn ``EVENT_TOKEN`` 帧**（无损），成功返回 True。

        「前一个的 fragment 追加到后一个的 fragment 上」（顺序不可反），再删除
        前一个 —— 每个 turn 内 fragment 的拼接结果保持不变，仅减少帧数（无空洞）。
        """
        first_by_turn: dict[Any, int] = {}
        for index in range(len(self._queue)):
            item = self._queue[index]
            if item is None or item.event != EVENT_TOKEN:
                continue
            turn = item.data.get("turn")
            prev = first_by_turn.get(turn)
            if prev is None:
                first_by_turn[turn] = index
                continue
            earlier = self._queue[prev]
            later = self._queue[index]
            later.data["fragment"] = str(earlier.data.get("fragment", "")) + str(
                later.data.get("fragment", "")
            )
            del self._queue[prev]
            return True
        return False

    def _drop_token_frames(self, predicate: Callable[[TokenFrame], bool]) -> int:
        """移除所有满足 ``predicate`` 的 ``EVENT_TOKEN`` 帧，返回移除数量。"""
        kept: deque[TokenFrame | None] = deque()
        removed = 0
        for item in self._queue:
            if (
                item is not None
                and item.event == EVENT_TOKEN
                and predicate(item)
            ):
                removed += 1
                continue
            kept.append(item)
        if removed:
            self._queue = kept
        return removed

    def _drop_oldest_token(self) -> bool:
        """移除队列中**最旧**的 ``EVENT_TOKEN`` 帧；无则返回 ``False``。"""
        for index, item in enumerate(self._queue):
            if item is not None and item.event == EVENT_TOKEN:
                del self._queue[index]
                return True
        return False

    # -- 消费者（SSE 出口侧） ------------------------------------------------- #
    async def frames(self) -> AsyncGenerator[str, None]:
        """产出 SSE 帧字符串，直到 ``close()`` 后哨兵到达。"""
        while True:
            if self._queue:
                item = self._queue.popleft()
                if item is None:
                    return
                yield item.to_sse()
                continue
            if self._closed:
                return
            # 队列已空且未关闭：清信号、二次确认（防丢唤醒）后等待。
            self._ready.clear()
            if self._queue or self._closed:
                continue
            await self._ready.wait()

    def close(self) -> None:
        """幂等关闭（产出哨兵，保证消费者必结束）。"""
        if self._closed:
            return
        self._closed = True
        # deque 无 maxsize，哨兵必达；极端情况下若曾丢弃帧也不影响哨兵入队。
        if not self._closed_sentinel_queued:
            self._queue.append(None)
            self._closed_sentinel_queued = True
        self._ready.set()

    def closed(self) -> bool:
        return self._closed


class TokenStreamRegistry:
    """进程级 run → RunTokenStream 映射（单事件循环，无需锁）。"""

    def __init__(self, *, max_runs: int = _MAX_RUNS) -> None:
        self._streams: dict[str, RunTokenStream] = {}
        self._max_runs = max(1, int(max_runs))

    def open(self, run_id: str) -> RunTokenStream:
        """取该 run 的通道；不存在（或已关闭）则新建并登记。"""
        stream = self._streams.get(run_id)
        if stream is not None and not stream.closed():
            return stream
        self._evict_if_needed()
        stream = RunTokenStream(run_id)
        self._streams[run_id] = stream
        return stream

    def get(self, run_id: str) -> RunTokenStream | None:
        return self._streams.get(run_id)

    def release(self, run_id: str) -> None:
        """SSE 出口的 ``finally`` 调用：关闭并移除。"""
        stream = self._streams.pop(run_id, None)
        if stream is not None:
            stream.close()

    def _evict_if_needed(self) -> None:
        if len(self._streams) < self._max_runs:
            return
        # 优先回收**已关闭**的（dict 保持插入序 ⇒ 遍历即从旧到新）。
        for run_id, stream in list(self._streams.items()):
            if stream.closed():
                del self._streams[run_id]
                return
        # 全未关闭：回收最旧（插入序第一个）并告警——这是异常路径（内存守在
        # ``_MAX_RUNS`` 上限），正常 run 会在 ``release``/``close`` 后自然回收。
        oldest = next(iter(self._streams), None)
        if oldest is not None:
            logger.warning(
                "TokenStreamRegistry: evicting still-open stream for run %s "
                "(reached _MAX_RUNS=%d)",
                oldest,
                self._max_runs,
            )
            self._streams[oldest].close()
            del self._streams[oldest]


_REGISTRY: TokenStreamRegistry | None = None


def get_token_registry() -> TokenStreamRegistry:
    """进程单例（orchestrator/dispatcher 与 SSE 路由器共享同一地址空间）。"""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = TokenStreamRegistry()
    return _REGISTRY


def reset_token_registry() -> None:
    """替换单例。仅测试用。"""
    global _REGISTRY
    _REGISTRY = TokenStreamRegistry()
