# Load .env BEFORE importing AI frameworks so credentials are in os.environ.
# load_dotenv() is a no-op when no .env file exists — environment variables
# injected by the container runtime (BTP bindings, K8s Secrets, docker run -e)
# are already present in os.environ and take priority over any .env file.
import atexit
import os
from dotenv import load_dotenv
load_dotenv(override=False)  # override=False: runtime env vars always win over .env file

import logging

import click
import uvicorn
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill

from agent_executor import AgentExecutor
from opentelemetry.instrumentation.starlette import StarletteInstrumentor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "5000"))


def _patch_sap_deployment_url() -> None:
    """Pre-fetch and cache the SAP orchestration deployment URL at startup.

    Root-cause fix for litellm.APIConnectionError / JSONDecodeError:

    LiteLLM's SAP provider (GenAIHubOrchestrationConfig) uses a @cached_property
    `deployment_url` that calls AI Core's /lm/deployments on first access.
    However, LiteLLM creates a BRAND-NEW GenAIHubOrchestrationConfig() instance
    for every completion call (via ProviderConfigManager lambda).  Because
    @cached_property stores its value in the *instance* __dict__, the cache is
    thrown away on every request — meaning 2 extra HTTP round-trips to AI Core
    happen on EVERY LLM call.  Any transient network hiccup on those probes
    crashes the agent with JSONDecodeError.

    Fix: resolve the URL once at startup, then replace the descriptor on the
    class itself with the resolved string.  From that point on, every new
    instance's attribute lookup finds the class-level string immediately,
    making zero extra HTTP calls.
    """
    try:
        from litellm.llms.sap.chat.transformation import GenAIHubOrchestrationConfig

        config = GenAIHubOrchestrationConfig()
        url = config.deployment_url          # triggers the 2 HTTP calls — just this once
        GenAIHubOrchestrationConfig.deployment_url = url   # replaces @cached_property for all future instances
        logger.info("SAP deployment URL cached: %s", url)
    except Exception as exc:
        logger.warning(
            "Could not pre-fetch SAP deployment URL (will retry on first request): %s",
            exc,
        )


@click.command()
@click.option("--host", default=HOST)
@click.option("--port", default=PORT)
def main(host: str, port: int):
    # Initialize AI Core config and telemetry inside main() so that any
    # failure here does not crash the process before uvicorn can start
    # (which would cause the startup health-probe to fail).
    try:
        from sap_cloud_sdk.aicore import set_aicore_config
        set_aicore_config()
    except Exception as exc:
        logger.warning("set_aicore_config() failed (non-fatal): %s", exc)

    try:
        from sap_cloud_sdk.core.telemetry import auto_instrument
        auto_instrument()
    except Exception as exc:
        logger.warning("auto_instrument() failed (non-fatal): %s", exc)

    # Pre-fetch SAP orchestration deployment URL and cache it at class level
    # (fixes JSONDecodeError: new instance created per call, @cached_property never re-used)
    _patch_sap_deployment_url()

    # Start APScheduler in background thread for daily reports and anomaly alerts
    try:
        from scheduler import build_scheduler
        scheduler = build_scheduler()
        scheduler.start()
        atexit.register(lambda: scheduler.shutdown(wait=False))
        logger.info("APScheduler started — daily report and anomaly check jobs active")
    except Exception as exc:
        logger.warning("APScheduler failed to start (non-fatal): %s", exc)

    skill = AgentSkill(
        id="btp-usage-agent",
        name="btp-usage-agent",
        description="A Joule AI agent that helps BTP administrators query and analyze daily subaccount usage for SAP HANA Cloud, AI Core, Cloud Foundry Runtime, and Integration Suite via the SAP UAS Reporting API",
        tags=["btp", "usage", "monitoring", "hana", "aicore"],
        examples=[
            "Show me HANA Cloud usage for last week",
            "今天的BTP用量是多少？",
            "Compare AI Core token usage for May 2026",
            "What services consumed the most resources this month?",
        ],
    )
    agent_card = AgentCard(
        name="btp-usage-agent",
        description="A Joule AI agent that helps BTP administrators query and analyze daily subaccount usage for SAP HANA Cloud, AI Core, Cloud Foundry Runtime, and Integration Suite via the SAP UAS Reporting API",
        url=os.environ.get("AGENT_PUBLIC_URL", f"http://{host}:{port}/"),
        version="1.0.0",
        default_input_modes=["text", "text/plain"],
        default_output_modes=["text", "text/plain"],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        skills=[skill],
    )
    server = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=DefaultRequestHandler(
            agent_executor=AgentExecutor(),
            task_store=InMemoryTaskStore(),
        ),
    )
    app = server.build()

    try:
        StarletteInstrumentor().instrument_app(app)
    except Exception as exc:
        logger.warning("StarletteInstrumentor.instrument_app() failed (non-fatal): %s", exc)

    logger.info(f"Starting A2A server at http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
