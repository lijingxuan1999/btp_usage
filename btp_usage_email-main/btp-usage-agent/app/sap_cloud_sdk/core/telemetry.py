"""
Stub for sap_cloud_sdk.core.telemetry.

auto_instrument() is a no-op; OpenTelemetry instrumentation for Starlette is
handled explicitly via opentelemetry-instrumentation-starlette in requirements.txt.
"""
import logging

logger = logging.getLogger(__name__)


def auto_instrument() -> None:
    """No-op stub — Starlette OTel instrumentation is configured in main.py."""
    logger.debug("auto_instrument: stub called (no-op)")
