"""
Local CLI test for BTP Usage Agent.
Run: python test_local.py

Tests:
  1. UAS API tools directly (no LLM)
  2. Full conversational Agent (LLM + tools end-to-end)
"""

import asyncio
import json
import sys

from dotenv import load_dotenv
load_dotenv()

# AI Core must be configured before AI framework imports
from sap_cloud_sdk.aicore import set_aicore_config
set_aicore_config()

sys.path.insert(0, "app")

from langchain_litellm import ChatLiteLLM
from langchain_core.messages import HumanMessage, ToolMessage, SystemMessage
from uas_tool import get_btp_usage, get_btp_services_summary
from mcp_tools import get_mcp_tools


MAY_FROM = "2026-05-01"
MAY_TO   = "2026-05-30"


def sep(title: str):
    print(f"\n{'='*62}\n  {title}\n{'='*62}")


def print_records(data: dict, n: int = 6):
    if "error" in data:
        print(f"  ✗ Error: {data['error']}")
        return
    recs = data.get("records") or data.get("detail") or []
    print(f"  total_records: {data.get('total_records', len(recs))}")
    for r in recs[:n]:
        date = r.get("date", r.get("from_date", ""))
        svc  = r.get("service", "")
        met  = r.get("metric", "")
        tot  = r.get("usage", r.get("total_usage", ""))
        unit = r.get("unit", "")
        print(f"  [{date}] {svc:<28} | {met:<42} = {str(tot):>10} {unit}")


# ──────────────────────────────────────────────────────────────────────────────
# PART 1 — UAS Tools only (no LLM)
# ──────────────────────────────────────────────────────────────────────────────
async def test_tools():
    sep("Tool Test 1 — get_btp_usage (key services)")
    r = await get_btp_usage.ainvoke({"from_date": MAY_FROM, "to_date": MAY_TO, "service_filter": "key"})
    print_records(json.loads(r))

    sep("Tool Test 2 — get_btp_services_summary")
    r = await get_btp_services_summary.ainvoke({"from_date": MAY_FROM, "to_date": MAY_TO})
    d = json.loads(r)
    print(f"  services: {d.get('service_count')} | records: {d.get('total_records')}")
    for row in d.get("detail", [])[:8]:
        print(f"  {row['service']:<28} | {row['metric']:<40} | total={row['total_usage']:>12} {row['unit']}")


# ──────────────────────────────────────────────────────────────────────────────
# PART 2 — Full conversational agent (LLM + tools)
# ──────────────────────────────────────────────────────────────────────────────
async def chat(llm_with_tools, tools_map, system_msg: SystemMessage, user_query: str):
    """Single conversational turn: user query → tool call → final answer."""
    print(f"\n💬 User: {user_query}")
    messages = [system_msg, HumanMessage(content=user_query)]

    # Round 1: LLM decides which tool to call
    r1 = llm_with_tools.invoke(messages)
    if not r1.tool_calls:
        print(f"🤖 Agent: {r1.content}")
        return

    # Execute all tool calls (may be multiple)
    messages.append(r1)
    for tc in r1.tool_calls:
        print(f"  🔧 Calling {tc['name']}({tc['args']})")
        result = await tools_map[tc["name"]].ainvoke(tc["args"])
        data   = json.loads(result)
        print(f"  ✓ {data.get('total_records', data.get('service_count', '?'))} records")
        messages.append(ToolMessage(content=result, tool_call_id=tc["id"]))

    # Round 2: LLM formulates final answer
    r2 = llm_with_tools.invoke(messages)
    print(f"\n🤖 Agent:\n{r2.content}")


async def test_agent():
    from app.agent import get_system_prompt  # noqa
    llm       = ChatLiteLLM(model="sap/anthropic--claude-4.5-sonnet", temperature=0.0)
    tools     = await get_mcp_tools()
    llm_wt    = llm.bind_tools(tools)
    tools_map = {t.name: t for t in tools}
    sys_msg   = SystemMessage(content=get_system_prompt())

    sep("Agent Test 1 — HANA 用量查询（中文）")
    await chat(llm_wt, tools_map, sys_msg, "五月份 SAP HANA Cloud 存储和计算用量各是多少，请用表格显示。")

    sep("Agent Test 2 — 服务总览（英文）")
    await chat(llm_wt, tools_map, sys_msg, "Give me a summary of all BTP services used in May 2026, sorted by total usage.")

    sep("Agent Test 3 — Integration Suite（中文）")
    await chat(llm_wt, tools_map, sys_msg, "Integration Suite 在五月份有多少 tenant 实例在运行？")


# ──────────────────────────────────────────────────────────────────────────────
async def main():
    await test_tools()
    await test_agent()
    print("\n\n✅ All tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
