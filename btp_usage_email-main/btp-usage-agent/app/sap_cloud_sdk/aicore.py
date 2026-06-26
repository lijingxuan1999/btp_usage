"""
Stub for sap_cloud_sdk.aicore.

Provides set_aicore_config() which parses VCAP_SERVICES to extract AI Core
service binding credentials and sets the AICORE_* environment variables that
LiteLLM's SAP provider requires.

If the AICORE_* env vars are already present (injected by the platform), this
is a safe no-op.  If they are absent, it parses the SAP AI Core binding from
VCAP_SERVICES automatically.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Standard VCAP_SERVICES service label for SAP AI Core
_AICORE_LABELS = {"aicore", "ml-foundation-services"}


def _find_aicore_binding(vcap: dict) -> Optional[dict]:
    """Return the first AI Core service-instance credentials dict, or None."""
    for label, instances in vcap.items():
        if label.lower() in _AICORE_LABELS or "aicore" in label.lower():
            if instances:
                return instances[0].get("credentials", {})
    return None


def set_aicore_config() -> None:
    """Read VCAP_SERVICES and populate AICORE_* environment variables.

    Environment variables set (only when not already present):
      AICORE_AUTH_URL            → token endpoint (with /oauth/token appended)
      AICORE_CLIENT_ID           → OAuth2 client ID
      AICORE_CLIENT_SECRET       → OAuth2 client secret
      AICORE_BASE_URL            → AI Core API base URL
      AICORE_RESOURCE_GROUP      → resource group (default: "default")

    This mirrors the behaviour of the real sap-cloud-sdk function so that
    LiteLLM's SAP provider can discover credentials automatically.
    """
    # If the platform already injected AICORE_* env vars, nothing to do.
    if os.environ.get("AICORE_AUTH_URL") and os.environ.get("AICORE_CLIENT_ID"):
        logger.debug("set_aicore_config: AICORE_* env vars already present — skipping VCAP parse")
        return

    vcap_raw = os.environ.get("VCAP_SERVICES", "")
    if not vcap_raw:
        logger.warning(
            "set_aicore_config: VCAP_SERVICES not set and AICORE_* env vars missing. "
            "AI Core authentication may fail."
        )
        return

    try:
        vcap = json.loads(vcap_raw)
    except json.JSONDecodeError as exc:
        logger.warning("set_aicore_config: Failed to parse VCAP_SERVICES JSON: %s", exc)
        return

    creds = _find_aicore_binding(vcap)
    if not creds:
        logger.warning("set_aicore_config: No AI Core service binding found in VCAP_SERVICES")
        return

    def _set(key: str, value: Optional[str]) -> None:
        if value and not os.environ.get(key):
            os.environ[key] = value
            logger.debug("set_aicore_config: Set %s", key)

    # Common field names used by SAP AI Core service binding
    url = creds.get("url") or creds.get("serviceurls", {}).get("AI_API_URL", "")
    auth_server = creds.get("uaa", {}) if "uaa" in creds else creds

    _set("AICORE_BASE_URL", url)
    _set(
        "AICORE_AUTH_URL",
        (auth_server.get("url") or auth_server.get("authurl", "")).rstrip("/") + "/oauth/token",
    )
    _set("AICORE_CLIENT_ID", auth_server.get("clientid") or auth_server.get("client_id", ""))
    _set(
        "AICORE_CLIENT_SECRET",
        auth_server.get("clientsecret") or auth_server.get("client_secret", ""),
    )
    _set(
        "AICORE_RESOURCE_GROUP",
        creds.get("resource_group") or creds.get("resourceGroup", "default"),
    )

    logger.info("set_aicore_config: AI Core credentials loaded from VCAP_SERVICES")
