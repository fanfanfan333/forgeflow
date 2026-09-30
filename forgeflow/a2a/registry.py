"""AgentRegistry — singleton for agent discovery and health tracking.

Agents register themselves at startup with an AgentCard.
The supervisor uses capability-based discovery to find the right worker.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any

from forgeflow.a2a.protocol import AgentCard

logger = logging.getLogger(__name__)


class AgentRegistry:
    """In-memory registry with capability-based discovery and heartbeat tracking."""

    # How long a heartbeat stays "fresh". Only meaningful for agents that have
    # actually emitted one — see `all_agents`.
    HEARTBEAT_FRESH_S = 60.0

    _instance: AgentRegistry | None = None

    def __init__(self) -> None:
        self._agents: dict[str, AgentCard] = {}
        self._capability_index: dict[str, set[str]] = defaultdict(set)
        self._heartbeats: dict[str, float] = {}
        # Agents that have actually emitted a heartbeat. Registration alone is
        # NOT a heartbeat, so this set is what separates "no liveness signal"
        # from "the signal went stale" in `all_agents`.
        self._heartbeat_seen: set[str] = set()
        self._run_counts: dict[str, int] = defaultdict(int)

    @classmethod
    def get_instance(cls) -> AgentRegistry:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register(self, card: AgentCard) -> None:
        self._agents[card.agent_id] = card
        for cap in card.capabilities:
            self._capability_index[cap].add(card.agent_id)
        # Registration is deliberately NOT recorded as a heartbeat: it says the
        # agent *exists*, not that it is reachable. Conflating the two made every
        # statically-registered card (the four `internal://` cards registered at
        # boot never call `heartbeat()`) report `healthy=False` once 60s had
        # elapsed since startup, which the console rendered as 「● 异常」 — a
        # fabricated outage for agents that are in-process and perfectly fine.
        self._heartbeats.pop(card.agent_id, None)
        self._heartbeat_seen.discard(card.agent_id)
        logger.info(
            "Agent registered: %s (%s) | caps=%s",
            card.name,
            card.agent_id[:8],
            card.capabilities,
        )

    def heartbeat(self, agent_id: str) -> None:
        self._heartbeats[agent_id] = time.monotonic()
        self._heartbeat_seen.add(agent_id)

    def increment_runs(self, agent_id: str) -> None:
        self._run_counts[agent_id] += 1

    def discover(self, capability: str) -> list[AgentCard]:
        """Find all registered agents that advertise the given capability."""
        ids = self._capability_index.get(capability, set())
        return [self._agents[aid] for aid in ids if aid in self._agents]

    def get(self, agent_id: str) -> AgentCard | None:
        return self._agents.get(agent_id)

    def all_agents(self) -> list[dict[str, Any]]:
        """Serialise every registered agent for the console registry table.

        `healthy` and `last_heartbeat_seconds_ago` are **tri-state**:

        * ``None``  — this agent has never emitted a heartbeat, so liveness is
          *unknown*. This is not a claim that it is down. Callers must render
          「—」, never a fault badge; returning ``False`` here fabricates an
          outage for agents that simply have no heartbeat source.
        * ``True``  — heartbeated within :attr:`HEARTBEAT_FRESH_S`.
        * ``False`` — the last heartbeat is stale. Note this means "no recent
          activity", which for an idle-but-healthy agent is not a fault either.

        ``heartbeat()`` is only called when work is dispatched
        (``a2a/dispatcher.py``), so in this codebase the signal means "recently
        executed a task", *not* "the process is up".
        """
        now = time.monotonic()
        result = []
        for agent_id, card in self._agents.items():
            if agent_id in self._heartbeat_seen:
                age: float | None = round(now - self._heartbeats[agent_id], 1)
                healthy: bool | None = age < self.HEARTBEAT_FRESH_S
            else:
                age = None
                healthy = None
            result.append({
                **card.model_dump(),
                "last_heartbeat_seconds_ago": age,
                "runs_completed": self._run_counts[agent_id],
                "healthy": healthy,
            })
        return result

    def deregister(self, agent_id: str) -> None:
        card = self._agents.pop(agent_id, None)
        if card:
            for cap in card.capabilities:
                self._capability_index[cap].discard(agent_id)
            self._heartbeats.pop(agent_id, None)
            self._heartbeat_seen.discard(agent_id)
            logger.info("Agent deregistered: %s", agent_id)


def get_registry() -> AgentRegistry:
    return AgentRegistry.get_instance()


# Pre-registered agent cards for the four ForgeFlow agents
def register_default_agents() -> None:
    registry = get_registry()

    registry.register(AgentCard(
        agent_id="supervisor-001",
        name="supervisor",
        description="将工作流任务路由到各专业 Agent",
        capabilities=["routing", "orchestration", "decision_making"],
        endpoint="internal://supervisor",
    ))
    registry.register(AgentCard(
        agent_id="researcher-001",
        name="researcher",
        description="采集企业与市场情报",
        capabilities=["web_search", "data_gathering", "company_research"],
        endpoint="internal://researcher",
    ))
    registry.register(AgentCard(
        agent_id="analyzer-001",
        name="analyzer",
        description="对销售线索进行评分与资格判定",
        capabilities=["lead_scoring", "icp_analysis", "risk_assessment"],
        endpoint="internal://analyzer",
    ))
    registry.register(AgentCard(
        agent_id="executor-001",
        name="executor",
        description="执行动作：方案起草、CRM 写入、邮件发送",
        capabilities=["proposal_drafting", "crm_update", "email_sending"],
        endpoint="internal://executor",
    ))
