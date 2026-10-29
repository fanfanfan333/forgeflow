"""ExecutorAgent — takes approved actions: draft proposals, write CRM, send emails.

Two sub-modes:
  1. PROPOSE mode: generate the proposal document + store it
  2. EXECUTE mode: after human approval, send email + update CRM lead status
"""

from __future__ import annotations

import logging
import uuid
from typing import cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from forgeflow.agents.base import BaseAgent
from forgeflow.state.workflow_state import WorkflowState

logger = logging.getLogger(__name__)


async def _tool_data(tool: BaseTool, args: dict) -> dict:
    """Machine payload stays out of the guarded model-text projection."""
    from langchain_core.messages import ToolMessage

    result = await tool.ainvoke(
        {"type": "tool_call", "id": uuid.uuid4().hex, "name": tool.name, "args": args}
    )
    if isinstance(result, ToolMessage):
        result = result.artifact
    if not isinstance(result, dict):
        raise TypeError("MCP operation did not return a structured object")
    if (
        result.get("ok") is False
        or result.get("success") is False
        or result.get("error")
        or result.get("mock") is True
    ):
        raise RuntimeError("MCP operation failed")
    return result


EXECUTOR_SYSTEM_PROPOSE = """You are the Executor Agent for ForgeFlow — Proposal Mode.

Your task: Generate a compelling, personalized sales proposal for the lead.

The proposal must include:
1. Executive Summary (2-3 sentences tailored to their specific situation)
2. Pain Points Addressed (based on research findings)
3. Our Solution (how ForgeFlow Enterprise AI addresses their challenges)
4. Pricing Tiers (3 options: Starter, Growth, Enterprise)
5. Expected ROI and timeline
6. Clear next steps

Be specific, confident, and business-focused. Avoid generic language.
Reference actual data points from the research (funding, team size, growth)."""

EXECUTOR_SYSTEM_EXECUTE = """You are the Executor Agent for ForgeFlow — Execution Mode.

The proposal has been approved by a manager. Your tasks:
1. Confirm the email content to send to the prospect
2. List the CRM fields to update
3. Define the follow-up timeline

Format your response as JSON with keys:
  email_subject, email_body, crm_updates, follow_up_date"""


class ProposalContent(BaseModel):
    # All fields default so a non-deterministic LLM omission (under
    # method="function_calling", which is not strict) degrades to an empty
    # value instead of raising a ValidationError that 500s the whole run.
    executive_summary: str = ""
    pain_points_addressed: list[str] = Field(default_factory=list)
    solution_overview: str = ""
    pricing_tiers: list[dict] = Field(default_factory=list)
    expected_roi: str = ""
    next_steps: list[str] = Field(default_factory=list)
    estimated_deal_value_usd: int = 0


class ExecutorAgent(BaseAgent):
    def __init__(
        self,
        model: BaseChatModel,
        tools: list[BaseTool] | None = None,
        system_prompt: str | None = None,
    ) -> None:
        super().__init__(
            name="executor",
            model=model,
            tools=tools or [],
            system_prompt=system_prompt or EXECUTOR_SYSTEM_PROPOSE,
        )
        # method="function_calling" keeps the structured-output binding on the
        # tool-calling path rather than a strict JSON-schema mode, which tolerates
        # the free-form `pricing_tiers: list[dict]` field (a strict schema would
        # require additionalProperties:false on every nested object). The
        # parameter is provider-agnostic and is passed through to the configured
        # chat model unchanged.
        self._proposal_model = model.with_structured_output(
            ProposalContent, method="function_calling"
        )
        self._tool_map = {t.name: t for t in (tools or [])}

    async def run(self, state: WorkflowState) -> dict:
        self._log_start(state)

        stage = state.get("current_stage", "propose")
        approval_status = state.get("approval_status")

        if stage == "execute" or approval_status == "approved":
            return await self._execute_approved(state)
        else:
            return await self._draft_proposal(state)

    async def _draft_proposal(self, state: WorkflowState) -> dict:
        lead_data = state.get("lead_data") or {}
        research_results = state.get("research_results", [])
        analysis_scores = state.get("analysis_scores", [])

        company = lead_data.get("company_name", "Unknown")
        score = analysis_scores[-1].get("score", 0) if analysis_scores else 0
        deal_value = (
            analysis_scores[-1].get("estimated_deal_value_usd", 50000) if analysis_scores else 50000
        )

        research_summary = ""
        for r in research_results:
            if "summary" in r:
                research_summary = r["summary"]
                break

        prompt = [
            SystemMessage(content=self.system_prompt),
            HumanMessage(
                content=f"Draft a proposal for:\n\nCompany: {company}\n"
                f"Qualification Score: {score}/10\n"
                f"Estimated Deal Value: ${deal_value:,}\n"
                f"Research: {research_summary[:1500]}\n\n"
                f"Generate a compelling proposal."
            ),
        ]

        proposal = cast(ProposalContent, await self._proposal_model.ainvoke(prompt))

        proposal_dict = proposal.model_dump()
        proposal_dict["lead_id"] = state.get("lead_id")
        proposal_dict["company"] = company
        proposal_dict["proposal_id"] = str(uuid.uuid4())
        proposal_dict["status"] = "pending_approval"

        logger.info(
            "Executor drafted proposal for %s | deal_value=$%d",
            company,
            proposal.estimated_deal_value_usd,
        )

        return {
            "proposal": proposal_dict,
            "current_stage": "propose",
            "executed_actions": ["proposal_drafted"],
            "messages": [
                AIMessage(
                    content=(
                        f"[Executor] Proposal drafted for {company}.\n"
                        f"Estimated deal: ${proposal.estimated_deal_value_usd:,}\n"
                        f"Key pitch: {proposal.executive_summary[:200]}"
                    ),
                    name="executor",
                )
            ],
        }

    async def _execute_approved(self, state: WorkflowState) -> dict:
        lead_data = state.get("lead_data") or {}
        proposal = state.get("proposal") or {}
        company = lead_data.get("company_name", "Unknown")
        dry_run = bool(state.get("dry_run"))
        workflow_id = state.get("workflow_id", "")

        actions: list[str] = []

        # Cap deal value — LLM-controlled "estimated_deal_value_usd" can be
        # nudged through prompt injection. Workspace-level override lives in
        # workspaces.settings.max_deal_value_usd; we fall back to $1M.
        ws_settings = state.get("workspace_settings") or {}
        max_deal = float(ws_settings.get("max_deal_value_usd", 1_000_000))
        dv = proposal.get("estimated_deal_value_usd")
        if isinstance(dv, (int, float)) and dv > max_deal:
            logger.warning("Capping deal_value | requested=%s cap=%s", dv, max_deal)
            proposal["estimated_deal_value_usd"] = max_deal

        allowed_domains = list(ws_settings.get("email_allowed_domains") or [])

        if dry_run:
            actions = ["crm_updated_dry_run", "email_sent_dry_run"]
            logger.info("Executor DRY-RUN — skipped CRM + email side effects for %s", company)
        else:
            actions.extend(
                await self._sync_to_crm(state, lead_data, proposal, company, workflow_id)
            )
            actions.extend(
                await self._send_proposal_email(
                    lead_data, proposal, company, allowed_domains=allowed_domains
                )
            )
        incomplete = [
            action
            for action in actions
            if action.endswith(("_failed", "_unavailable")) or "_skipped_" in action
        ]
        if not actions and not dry_run:
            incomplete = ["no_actions_available"]

        logger.info("Executor completed actions: %s", actions)

        return {
            "current_stage": "failed" if incomplete else "done",
            "executed_actions": actions,
            "errors": ["Workflow incomplete: " + ", ".join(incomplete)] if incomplete else [],
            "messages": [
                AIMessage(
                    content=(
                        f"[Executor] Workflow incomplete for {company}: {', '.join(incomplete)}."
                        if incomplete
                        else f"[Executor] {'Simulated' if dry_run else 'Executed'}: {', '.join(actions)} for {company}."
                    ),
                    name="executor",
                )
            ],
        }

    async def _sync_to_crm(
        self,
        state: WorkflowState,
        lead_data: dict,
        proposal: dict,
        company: str,
        workflow_id: str,
    ) -> list[str]:
        """Sync the lead + deal to HubSpot when configured.

        Detection order:
          1. HubSpot — if HUBSPOT_ACCESS_TOKEN is set, call hubspot_upsert_contact
             + hubspot_upsert_company + hubspot_create_deal_idempotent. Idempotent
             on email + domain + workflow_id, so retried workflows don't dupe.
          2. No HubSpot credentials — report that real CRM is unavailable.
        """
        from forgeflow.config import get_settings

        actions: list[str] = []
        settings = get_settings()
        # SecretStr is always present; the value can be empty string (no token).
        hubspot_token = settings.hubspot_access_token.get_secret_value()

        if hubspot_token:
            # Real CRM path. Each step is wrapped so a partial failure still
            # records what succeeded — audit log + downstream retry both benefit.
            email = lead_data.get("contact_email")
            domain = lead_data.get("domain")
            deal_value = float(proposal.get("estimated_deal_value_usd") or 0)

            # FastMCP mounts tools as "{prefix}_{function_name}", so the
            # registered names are hubspot_hubspot_upsert_contact etc.
            upsert_contact = self._tool_map.get("hubspot_hubspot_upsert_contact")
            upsert_company = self._tool_map.get("hubspot_hubspot_upsert_company")
            create_deal = self._tool_map.get("hubspot_hubspot_create_deal_idempotent")
            add_note = self._tool_map.get("hubspot_hubspot_add_note")

            contact_id: str | None = None
            company_id: str | None = None

            if upsert_contact and email:
                try:
                    contact = await _tool_data(
                        upsert_contact,
                        {
                            "email": email,
                            "firstname": lead_data.get("contact_firstname"),
                            "lastname": lead_data.get("contact_lastname"),
                            "company": company,
                        },
                    )
                    contact_id = contact.get("id")
                    if not contact_id:
                        raise ValueError("HubSpot contact response has no ID")
                    actions.append("hubspot_contact_upserted")
                except Exception as e:
                    logger.error("HubSpot contact upsert failed for %s: %s", email, e)
                    actions.append("hubspot_contact_failed")
            else:
                actions.append("hubspot_contact_unavailable")

            if upsert_company and domain:
                try:
                    co = await _tool_data(
                        upsert_company,
                        {
                            "name": company,
                            "domain": domain,
                            "industry": lead_data.get("industry"),
                        },
                    )
                    company_id = co.get("id")
                    if not company_id:
                        raise ValueError("HubSpot company response has no ID")
                    actions.append("hubspot_company_upserted")
                except Exception as e:
                    logger.error("HubSpot company upsert failed for %s: %s", domain, e)
                    actions.append("hubspot_company_failed")
            else:
                actions.append("hubspot_company_unavailable")

            if create_deal and deal_value > 0 and contact_id and company_id:
                try:
                    deal = await _tool_data(
                        create_deal,
                        {
                            "run_id": workflow_id,
                            "deal_name": f"{company} — ForgeFlow proposal",
                            "amount": deal_value,
                            "deal_stage": "presentationscheduled",
                            "contact_id": contact_id,
                            "company_id": company_id,
                        },
                    )
                    if not deal.get("id"):
                        raise ValueError("HubSpot deal response has no ID")
                    actions.append("hubspot_deal_created")
                except Exception as e:
                    logger.error("HubSpot deal create failed for run %s: %s", workflow_id, e)
                    actions.append("hubspot_deal_failed")
            elif deal_value > 0:
                actions.append("hubspot_deal_unavailable")

            if add_note and (contact_id or company_id):
                try:
                    await _tool_data(
                        add_note,
                        {
                            "body": (
                                f"ForgeFlow run {workflow_id} — proposal drafted.\n\n"
                                f"{proposal.get('executive_summary', '')[:500]}"
                            ),
                            "contact_id": contact_id,
                            "company_id": company_id,
                        },
                    )
                    actions.append("hubspot_note_added")
                except Exception as e:
                    logger.error("HubSpot note add failed: %s", e)
            return actions

        return ["crm_update_unavailable"]

    async def _send_proposal_email(
        self,
        lead_data: dict,
        proposal: dict,
        company: str,
        allowed_domains: list[str] | None = None,
    ) -> list[str]:
        """Report missing address or unavailable real email delivery."""
        to_address = lead_data.get("contact_email")

        if not to_address:
            logger.warning("Email skipped — no contact_email on lead for %s", company)
            return ["email_skipped_no_address"]

        # email_send_email is an in-memory demonstration, not a mail provider.
        return ["email_unavailable"]
