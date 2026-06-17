import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncGenerator, Literal, Sequence

from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from langchain_litellm import ChatLiteLLM
from langgraph.checkpoint.memory import InMemorySaver
from sap_cloud_sdk.agent_decorators import agent_config, agent_model, prompt_section

logger = logging.getLogger(__name__)


@agent_model(
    key="config.model",
    label="LLM Model",
    description="The language model powering this agent",
)
def get_model_name() -> str:
    return "sap/gpt-4o"


@agent_config(
    key="config.temperature",
    label="LLM Temperature",
    description="Controls randomness of responses (0.0 = deterministic, 1.0 = creative)",
)
def get_temperature() -> float:
    return 0.0


@prompt_section(
    key="prompts.system",
    label="System Prompt",
    description="The full system prompt defining the agent's role and behavior",
    validation={"format": "markdown", "max_length": 5000},
)
def get_system_prompt() -> str:
    # Always compute current date at call time so the LLM always knows "today".
    # This prevents the model from falling back to its training-data cutoff (2023).
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    current_month_start = datetime.now(tz=timezone.utc).strftime("%Y-%m-01")
    return f"""You are a BTP (SAP Business Technology Platform) usage monitoring assistant for administrators.

## Current Date
Today's date is **{today}** (UTC). Always use this as the reference for ALL relative date expressions.
NEVER use dates from 2023 or 2024. The current year is {today[:4]}.

You help BTP admins query and analyze daily subaccount usage by calling the SAP UAS Reporting API.

## Available Tools

### Global-account monthly usage (calls /reports/v1/monthlyUsage)
- **list_subaccounts**: Discover all subaccounts that reported usage under the global account for a given month range. Returns subaccount IDs, names, and directory info. Use as the first step when the user asks "which subaccounts exist?" or before querying a specific subaccount. Optional: from_month, to_month (YYYY-MM, default last 3 months).
- **get_global_account_monthly_usage**: Query monthly usage aggregated across ALL subaccounts. Use for global-level cost trending, cross-subaccount comparisons, or capacity planning. Required: from_month, to_month (YYYY-MM). Optional: service_filter (all/hana/aicore/cf/integration/key), group_by (service/month/subaccount/directory).

### Subaccount daily usage (calls /reports/v1/subaccountUsage)
- **get_btp_usage**: Query subaccount daily usage for a date range. Required: from_date, to_date (YYYY-MM-DD). Optional: service_filter (hana/aicore/cf/integration/all).
- **get_btp_services_summary**: Get a grouped summary of services and their total usage for a date range.
- **get_aicore_model_cu_usage**: AI Core CU consumption broken down by model. Optional time_granularity: none/day/month.
- **simulate_aicore_cu_eom_forecast**: Forecast AI Core CU consumption by end of the current month using three methods (linear, 7-day trend, historical ratio) plus an ensemble estimate. Optional: reference_date (YYYY-MM-DD, defaults to today). Use whenever the user asks about projected, estimated, or forecasted CU usage for the rest of the month.
- **detect_aicore_cu_anomaly**: Detect anomalies in AI Core CU daily consumption. Automatically selects the best algorithm (IQR / Z-score / MAD) based on data shape. Optional: lookback_days (default 30, range 7–90), reference_date, sensitivity (low/medium/high). Use whenever the user asks about unusual usage, spikes, anomalies, or outliers in AI Core CU consumption.
- **check_quota_status**: Check whether AI Core CU consumption is on track against an annual contract quota. Required: contract_cu (total CU in contract), contract_start (YYYY-MM-DD), contract_end (YYYY-MM-DD). Optional: reference_date. Returns three verdicts: (1) this month on track? (2) cumulative on track? (3) projected year-end SAFE / AT_RISK / WILL_EXCEED. Use whenever the user asks about contract limits, quota, whether they will exceed their purchased CU, or burn rate against contract.

## Key Services You Monitor
| Service | serviceId | What to watch |
|---------|-----------|---------------|
| SAP HANA Cloud | hana-cloud | Storage (GB-hours), Compute (vCPU-hours, Memory GB-hours), Backup |
| SAP AI Core | aicore | Token consumption (input/output) |
| Cloud Foundry Runtime | linux-container | Memory (GB-hours) |
| Integration Suite | IntegrationSuite | Tenant instances, message count |

## How to Answer Usage Questions
1. Parse the user's intent to identify: date range, service(s) of interest
2. Call get_btp_usage or get_btp_services_summary with appropriate parameters
3. Present results in a clear, structured format with totals and highlights
4. Proactively flag any unusual patterns (e.g., high storage, zero usage)

## Date Handling (today = {today})
- "Today" → from_date={today}, to_date={today}
- "Yesterday" → subtract 1 day from today
- "Last week" → 7 days ago to yesterday
- "This month" → from_date={current_month_start}, to_date={today}
- "Last month" → first to last day of prior calendar month
- "May 2026" → 2026-05-01 to 2026-05-31
- Always default from_date/to_date to the last 7 days if user doesn't specify
- NEVER pass dates from 2023 or 2024 to any tool — the current year is {today[:4]}

## Response Format
- Use markdown tables for usage data
- Show: Service | Plan | Metric | Usage | Unit | Date
- Summarize key services at the top
- Respond in the same language the user uses (Chinese/English)
"""


@dataclass
class AgentResponse:
    status: Literal["input_required", "completed", "error"]
    message: str


THREAD_TTL_SECONDS = 3600  # evict threads inactive for 1 hour


class SampleAgent:
    SUPPORTED_CONTENT_TYPES = ["text", "text/plain"]

    def __init__(self):
        self.llm = ChatLiteLLM(model=get_model_name(), temperature=get_temperature())
        self._checkpointer = InMemorySaver()
        self._last_active: dict[str, float] = {}
        self._summarization_middleware = SummarizationMiddleware(
            model=self.llm,
            trigger=("tokens", 100_000),
        )

    def _touch(self, thread_id: str) -> None:
        """Refresh TTL and evict any threads that have been inactive for over an hour."""
        now = time.monotonic()
        expired = [tid for tid, ts in list(self._last_active.items()) if now - ts > THREAD_TTL_SECONDS]
        for tid in expired:
            self._checkpointer.delete_thread(tid)
            del self._last_active[tid]
            logger.info("Evicted inactive thread: %s", tid)
        self._last_active[thread_id] = now

    async def stream(
        self,
        query: str,
        context_id: str,
        tools: Sequence[BaseTool] | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Stream agent responses.

        Args:
            query: User query to process
            context_id: Context identifier for the conversation
            tools: Optional sequence of LangChain tools. If None, agent runs without tools.

        Yields:
            Status updates and final response with structure:
            - is_task_complete: Whether the task is complete
            - require_user_input: Whether user input is needed
            - content: The response content or status message
        """
        self._touch(context_id)
        yield {
            "is_task_complete": False,
            "require_user_input": False,
            "content": "Processing...",
        }

        try:
            if tools:
                logger.info("Running agent with %d tool(s): %s", len(tools), [t.name for t in tools])
            else:
                logger.info("Running agent without tools")

            # get_system_prompt() is called here so the date is always fresh
            # (evaluated at request time, not cached at startup).
            graph = create_agent(
                self.llm,
                tools=list(tools) if tools else [],
                system_prompt=get_system_prompt(),
                checkpointer=self._checkpointer,
                middleware=[self._summarization_middleware],
            )
            config = {"configurable": {"thread_id": context_id}}

            # Run LLM in background task; send heartbeat every 8s to prevent
            # Istio idle-timeout (~15s) from dropping the SSE stream.
            task = asyncio.create_task(
                graph.ainvoke({"messages": [HumanMessage(content=query)]}, config)
            )
            while not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=8.0)
                except asyncio.TimeoutError:
                    # LLM still running — send a heartbeat to keep SSE alive
                    yield {
                        "is_task_complete": False,
                        "require_user_input": False,
                        "content": "Thinking...",
                    }
                except asyncio.CancelledError:
                    task.cancel()
                    raise

            result = task.result()
            self._touch(context_id)
            response = result["messages"][-1].content

            yield {
                "is_task_complete": True,
                "require_user_input": False,
                "content": response,
            }

        except asyncio.CancelledError:
            logger.info("Agent stream() cancelled by client")
            raise
        except Exception as e:
            logger.exception("Agent stream() failed")
            yield {
                "is_task_complete": True,
                "require_user_input": False,
                "content": f"I encountered an error while processing your request: {str(e)}. Please try again.",
            }

    async def invoke(
        self,
        query: str,
        context_id: str,
        tools: Sequence[BaseTool] | None = None,
    ) -> AgentResponse:
        """Invoke agent and return final response.

        Args:
            query: User query to process
            context_id: Context identifier for the conversation
            tools: Optional sequence of LangChain tools. If None, agent runs without tools.

        Returns:
            AgentResponse with status and message
        """
        last: dict = {}
        async for chunk in self.stream(query, context_id, tools=tools):
            last = chunk
        if last.get("is_task_complete"):
            return AgentResponse(status="completed", message=last["content"])
        if last.get("require_user_input"):
            return AgentResponse(status="input_required", message=last["content"])
        return AgentResponse(status="error", message=last.get("content", "Unknown error"))
