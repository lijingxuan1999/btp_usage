"""
BTP UAS Reporting API Tool
Calls GET /reports/v1/subaccountUsage directly with OAuth2 client credentials.

API reference:
  https://api.sap.com/api/APIUasReportingService/path/dailySubaccountUsage

Required environment variables (.env):
  BTP_UAS_URL       = https://uas-reporting.cfapps.eu10.hana.ondemand.com
  BTP_AUTH_URL      = https://<subdomain>.authentication.<region>.hana.ondemand.com/oauth/token
  BTP_CLIENT_ID     = ...
  BTP_CLIENT_SECRET = ...
  BTP_SUBACCOUNT_ID = ...
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv
from langchain_core.tools import tool

load_dotenv()
logger = logging.getLogger(__name__)

# ─── OAuth2 token cache ────────────────────────────────────────────────────────
_token_cache: dict = {"token": None, "expires_at": 0.0}


async def _get_token() -> str:
    """Fetch (or return cached) OAuth2 access token from XSUAA."""
    if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 30:
        return _token_cache["token"]

    auth_url      = os.environ["BTP_AUTH_URL"]
    client_id     = os.environ["BTP_CLIENT_ID"]
    client_secret = os.environ["BTP_CLIENT_SECRET"]

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            auth_url,
            data={
                "grant_type":    "client_credentials",
                "client_id":     client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        data = resp.json()

    _token_cache["token"]      = data["access_token"]
    _token_cache["expires_at"] = time.time() + data.get("expires_in", 3600)
    logger.info("OAuth2 token refreshed, expires in %ds", data.get("expires_in", 3600))
    return _token_cache["token"]


# ─── Date helpers ──────────────────────────────────────────────────────────────
def _to_uas_date(iso_date: str) -> str:
    """YYYY-MM-DD → YYYYMMDD (UAS API format)."""
    return iso_date.replace("-", "")


def _validate_date(date_str: str, field_name: str) -> str:
    """Validate and auto-correct date strings.

    Rejects dates from more than 1 year ago (likely LLM training-data bias,
    e.g. 2023) and replaces them with today's UTC date.
    Also clamps future dates to today.
    """
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        current_year = datetime.now(tz=timezone.utc).year
        if dt.year < current_year - 1:
            logger.warning(
                "Date validation: %s=%r is from %d — likely LLM training-data bias. "
                "Overriding with today (%s).",
                field_name, date_str, dt.year, today,
            )
            return today
        if date_str > today:
            logger.warning(
                "Date validation: %s=%r is in the future. Clamping to today (%s).",
                field_name, date_str, today,
            )
            return today
    except ValueError:
        logger.warning(
            "Date validation: %s=%r is not a valid YYYY-MM-DD date. Using today (%s).",
            field_name, date_str, today,
        )
        return today
    return date_str


def _last_n_days(n: int) -> tuple[str, str]:
    today = datetime.now(tz=timezone.utc)
    start = today - timedelta(days=n - 1)
    return start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


def _yesterday() -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


# ─── Service classification ────────────────────────────────────────────────────
_SERVICE_GROUPS = {
    "hana":        ["hana-cloud"],
    "aicore":      ["aicore", "ai-core"],
    "cf":          ["linux-container"],
    "integration": ["integrationsuite", "IntegrationSuite", "integration-suite"],
}


def _classify(service_id: str) -> str:
    sid = service_id.lower()
    for group, ids in _SERVICE_GROUPS.items():
        if any(sid == k.lower() or k.lower() in sid for k in ids):
            return group
    return "other"


# ─── Core API call ─────────────────────────────────────────────────────────────
async def _fetch_usage(from_date: str, to_date: str) -> list[dict]:
    """
    Call UAS API and return flat list of usage records.

    Response item fields:
      serviceId, serviceName, plan, planName,
      measureId, metricName, unitSingular, usage,
      startIsoDate, categoryName, dataCenter,
      spaceName, instanceId
    """
    uas_url      = os.environ.get("BTP_UAS_URL", "https://uas-reporting.cfapps.eu10.hana.ondemand.com")
    subaccount   = os.environ["BTP_SUBACCOUNT_ID"]
    token        = await _get_token()

    url    = f"{uas_url}/reports/v1/subaccountUsage"
    params = {
        "subaccountId":      subaccount,
        "fromDate":          _to_uas_date(from_date),
        "toDate":            _to_uas_date(to_date),
        "periodPerspective": "DAY",
    }
    logger.info("UAS GET %s  params=%s", url, params)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()

    records = data.get("content", data) if isinstance(data, dict) else data
    return records if isinstance(records, list) else []


# ─── LangChain Tools ───────────────────────────────────────────────────────────

@tool
async def get_btp_usage(
    from_date: str,
    to_date: str,
    service_filter: Optional[str] = "all",
) -> str:
    """
    Query BTP subaccount usage from the SAP UAS Reporting API.

    Args:
        from_date: Start date in YYYY-MM-DD format (e.g. "2026-05-01")
        to_date: End date in YYYY-MM-DD format (e.g. "2026-05-31")
        service_filter: Filter results. One of:
            "all"         – all services (default)
            "hana"        – SAP HANA Cloud only
            "aicore"      – SAP AI Core only
            "cf"          – Cloud Foundry Runtime only
            "integration" – SAP Integration Suite only
            "key"         – all 4 key services above

    Returns:
        JSON string with usage records. Each record contains:
          service, plan, metric, usage, unit, date, dataCenter, space
    """
    # Validate / auto-correct dates before calling the API.
    # This guards against LLM training-data bias causing 2023/2024 dates.
    from_date = _validate_date(from_date, "from_date")
    to_date   = _validate_date(to_date,   "to_date")
    if from_date > to_date:
        logger.warning("from_date %s > to_date %s — swapping.", from_date, to_date)
        from_date, to_date = to_date, from_date

    try:
        records = await _fetch_usage(from_date, to_date)
    except Exception as exc:
        return json.dumps({"error": str(exc), "from_date": from_date, "to_date": to_date})

    # Apply service filter
    filt = (service_filter or "all").lower()
    if filt == "key":
        records = [r for r in records if _classify(r.get("serviceId", "")) != "other"]
    elif filt != "all":
        records = [r for r in records if _classify(r.get("serviceId", "")) == filt]

    # Normalise to a clean, LLM-friendly format
    rows = [
        {
            "service":    r.get("serviceName") or r.get("serviceId", ""),
            "serviceId":  r.get("serviceId", ""),
            "plan":       r.get("planName") or r.get("plan", ""),
            "metric":     r.get("metricName") or r.get("measureId", ""),
            "measureId":  r.get("measureId", ""),
            "usage":      r.get("usage", 0),
            "unit":       r.get("unitSingular") or r.get("unitPlural", ""),
            "date":       r.get("startIsoDate", ""),
            "category":   r.get("categoryName", ""),
            "dataCenter": r.get("dataCenter", ""),
            "space":      r.get("spaceName") or "",
        }
        for r in records
    ]

    return json.dumps({
        "from_date":      from_date,
        "to_date":        to_date,
        "service_filter": filt,
        "total_records":  len(rows),
        "records":        rows,
    }, ensure_ascii=False)


@tool
async def get_btp_services_summary(
    from_date: str,
    to_date: str,
) -> str:
    """
    Get a grouped summary of BTP service usage, aggregated by service and metric.
    Useful for high-level overview questions like 'what services were used?'

    Args:
        from_date: Start date in YYYY-MM-DD format
        to_date: End date in YYYY-MM-DD format

    Returns:
        JSON with per-service totals grouped by metric, sorted by total usage.
    """
    # Validate / auto-correct dates before calling the API
    from_date = _validate_date(from_date, "from_date")
    to_date   = _validate_date(to_date,   "to_date")
    if from_date > to_date:
        from_date, to_date = to_date, from_date

    try:
        records = await _fetch_usage(from_date, to_date)
    except Exception as exc:
        return json.dumps({"error": str(exc)})

    # Aggregate: service → metric → { total_usage, unit, dates, count }
    summary: dict = {}
    for r in records:
        svc     = r.get("serviceName") or r.get("serviceId", "unknown")
        metric  = r.get("metricName")  or r.get("measureId", "unknown")
        unit    = r.get("unitSingular") or r.get("unitPlural", "")
        usage   = float(r.get("usage", 0))
        date    = r.get("startIsoDate", "")
        grp     = _classify(r.get("serviceId", ""))

        key = (svc, metric, unit, grp)
        if key not in summary:
            summary[key] = {"total": 0.0, "count": 0, "dates": set()}
        summary[key]["total"]  += usage
        summary[key]["count"]  += 1
        summary[key]["dates"].add(date)

    rows = [
        {
            "service":      k[0],
            "group":        k[3],
            "metric":       k[1],
            "unit":         k[2],
            "total_usage":  round(v["total"], 6),
            "record_count": v["count"],
            "date_count":   len(v["dates"]),
        }
        for k, v in sorted(summary.items(), key=lambda x: -x[1]["total"])
    ]

    # Top-level totals per key service group
    group_totals: dict = {}
    for row in rows:
        g = row["group"]
        group_totals.setdefault(g, {"service_display": row["service"], "metrics": []})
        group_totals[g]["metrics"].append({
            "metric":      row["metric"],
            "total_usage": row["total_usage"],
            "unit":        row["unit"],
        })

    return json.dumps({
        "from_date":     from_date,
        "to_date":       to_date,
        "total_records": len(records),
        "service_count": len({r[0] for r in summary}),
        "group_summary": group_totals,
        "detail":        rows,
    }, ensure_ascii=False)
