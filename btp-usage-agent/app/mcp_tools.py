"""
Tool loader for BTP Usage Agent.

This agent does NOT use MCP / Agent Gateway.
It directly calls the SAP UAS Reporting API and the SAP HANA Cloud
Metering/Metrics APIs using OAuth2 client credentials stored in the local
.env file.

UAS tools (uas_tool.py) — BTP global-account and subaccount usage reporting.
HANA tools (hana_tool.py) — HANA Cloud metering (CU billing) and technical
    performance metrics (memory, CPU, storage, network).

To add more tools, import and add them to the list returned by get_mcp_tools().
"""

import logging

from uas_tool import (
    # ── Global account monthly usage ─────────────────────────────────────────
    list_subaccounts,
    get_global_account_monthly_usage,
    # ── Subaccount daily usage ────────────────────────────────────────────────
    get_btp_usage,
    get_btp_services_summary,
    get_aicore_model_cu_usage,
    simulate_aicore_cu_eom_forecast,
    detect_aicore_cu_anomaly,
    # ── Contract quota tracking ───────────────────────────────────────────────
    check_quota_status,
)
from hana_tool import (
    # ── HANA Cloud instance discovery ────────────────────────────────────────
    list_hana_instances,
    # ── HANA Cloud metering (billing CUs) ────────────────────────────────────
    get_hana_metering_values,
    # ── HANA Cloud technical metrics ─────────────────────────────────────────
    get_hana_metric_definitions,
    get_hana_metrics,
)

logger = logging.getLogger(__name__)

_UAS_TOOLS = [
    # ── Global account discovery & monthly reporting ──────────────────────────
    list_subaccounts,
    get_global_account_monthly_usage,
    # ── Subaccount daily usage (UAS /reports/v1/subaccountUsage) ─────────────
    get_btp_usage,
    get_btp_services_summary,
    get_aicore_model_cu_usage,
    simulate_aicore_cu_eom_forecast,
    detect_aicore_cu_anomaly,
    # ── Contract quota tracking ───────────────────────────────────────────────
    check_quota_status,
]

_HANA_TOOLS = [
    # ── Discovery: call first to find instance IDs and available metrics ──────
    list_hana_instances,
    get_hana_metric_definitions,
    # ── Metering (billing CUs) ────────────────────────────────────────────────
    get_hana_metering_values,
    # ── Technical performance metrics ─────────────────────────────────────────
    get_hana_metrics,
]


async def get_mcp_tools(use_cache: bool = True) -> list:
    """Return all tools (UAS + HANA) for the agent.

    Signature is kept compatible with the standard mcp_tools contract
    so agent_executor.py requires no changes.
    """
    all_tools = _UAS_TOOLS + _HANA_TOOLS
    logger.info("Loaded %d tool(s): %s", len(all_tools), [t.name for t in all_tools])
    return all_tools
