"""MCP tools: mock data enrichment and internal DB queries."""

from __future__ import annotations

import logging
import random

from fastmcp import FastMCP

from forgeflow.config import get_settings

logger = logging.getLogger(__name__)

router = FastMCP("data-tools")


def _allows_development_tools(settings) -> bool:
    """Whether the current deployment may return development-stub results.

    Reads ``Settings.allows_development_tools()`` (INC12 A3); a ``Settings`` that
    predates the field degrades to ``True`` (historical offline behaviour).
    """
    fn = getattr(settings, "allows_development_tools", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:  # noqa: BLE001 — a label must never break the call
            return True
    return True

# Mock enrichment data (replace with Clearbit/Apollo/ZoomInfo API)
_MOCK_ENRICHMENT = {
    "default": {
        "employee_count": random.randint(100, 5000),
        "annual_revenue_usd": random.randint(5_000_000, 200_000_000),
        "funding_total_usd": random.randint(1_000_000, 100_000_000),
        "last_funding_round": "Series B",
        "founded_year": random.randint(2010, 2020),
        "headquarters": "San Francisco, CA",
        "tech_stack": ["AWS", "Python", "React", "PostgreSQL", "Kubernetes"],
        "linkedin_url": "https://linkedin.com/company/example",
        "glassdoor_rating": round(random.uniform(3.5, 4.8), 1),
    }
}


@router.tool()
async def fetch_enrichment(company_name: str) -> dict:
    """Fetch enriched company data from data providers (Clearbit / Apollo mock).

    Args:
        company_name: Name of the company to enrich

    Returns:
        Enriched company profile with employee count, revenue, funding, tech stack
    """
    logger.info("Enriching company: %s", company_name)

    # Return mock data (swap with real API call)
    data = dict(_MOCK_ENRICHMENT["default"])
    data["company_name"] = company_name
    data["domain"] = f"{company_name.lower().replace(' ', '')}.com"
    employee_count = random.randint(50, 5000)
    data["employee_count"] = employee_count
    data["annual_revenue_usd"] = employee_count * random.randint(100_000, 300_000)

    return data


@router.tool()
async def query_db(
    table: str,
    filters: dict | None = None,
    limit: int = 10,
) -> list[dict]:
    """Query the internal ForgeFlow database for historical lead data.

    Args:
        table: Table name (leads, proposals, workflow_runs)
        filters: Key-value pairs to filter by (e.g. {"status": "qualified"})
        limit: Maximum rows to return

    Returns:
        List of row dicts

    Failure mode (INC12 A3): this function is **permanently a development stub**
    — it returns hard-coded synthetic rows and no real warehouse is wired
    anywhere on the platform. In ``dev`` that behaviour is kept; **outside dev it
    raises** ``RuntimeError`` (fail loud) rather than returning rows a caller
    could mistake for real data. Raising — instead of returning an
    ``{"error": ...}`` dict — is chosen deliberately: the declared return type is
    ``list[dict]``, so an error dict would silently violate the contract and
    could be iterated as if it were data.
    """
    if not _allows_development_tools(get_settings()):
        logger.error(
            "query_db is a development stub and the environment is not 'dev' — "
            "refusing to return synthetic rows"
        )
        raise RuntimeError(
            "query_db is a development stub; no real warehouse is wired "
            "(refused outside the dev environment)"
        )

    # In production this would use the asyncpg pool
    # For the mock, return synthetic examples
    logger.info("Mock DB query: table=%s filters=%s limit=%d", table, filters, limit)

    if table == "leads":
        return [
            {
                "id": "mock-lead-1",
                "company_name": "TechCorp Inc",
                "status": "qualified",
                "qualification_score": 7.5,
            }
        ]
    return []
