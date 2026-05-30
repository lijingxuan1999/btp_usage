"""
Tool loader for BTP Usage Agent.

This agent does NOT use MCP / Agent Gateway.
It directly calls the SAP UAS Reporting API using OAuth2 client credentials
stored in the local .env file.

To add more tools, import and add them to the list returned by get_mcp_tools().
"""

import logging

from uas_tool import get_btp_usage, get_btp_services_summary

logger = logging.getLogger(__name__)

_UAS_TOOLS = [get_btp_usage, get_btp_services_summary]


async def get_mcp_tools(use_cache: bool = True) -> list:
    """Return the BTP UAS tools for the agent.

    Signature is kept compatible with the standard mcp_tools contract
    so agent_executor.py requires no changes.
    """
    logger.info("Loaded %d UAS tool(s): %s", len(_UAS_TOOLS), [t.name for t in _UAS_TOOLS])
    return _UAS_TOOLS
