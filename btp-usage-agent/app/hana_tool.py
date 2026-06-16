"""
SAP HANA Cloud Monitoring Tools
Calls the SAP HANA Cloud Metering and Metrics Service APIs using OAuth2 client credentials.

Endpoints covered (verified from OpenAPI 3.0.3 spec):
  Metering API  — GET /metering/v1/definitions
                  GET /metering/v1/serviceInstances/{serviceInstanceID}/definitions
                  GET /metering/v1/serviceInstances/{serviceInstanceID}/values
  Metrics API   — GET /metrics/v1/definitions
                  GET /metrics/v1/serviceInstances/{serviceInstanceID}/definitions
                  GET /metrics/v1/serviceInstances/{serviceInstanceID}/values

Metering vs Metrics:
  Metering  — CU-based billing data (capacity units).  Unit: CU.
              Metric type is always "delta".  Aggregates: count, last, sum.
              Interval options (seconds): 3600, 7200, 21600, 43200, 86400, 172800, 604800.
  Metrics   — Technical performance data (memory, CPU, storage, network).
              Units: byte, MiB, %, s, ms, us.  Types: counter, delta, gauge.
              Aggregates: avg, count, delta, last, max, min.
              Interval options (seconds): 60, 300, 900, 1800, 3600, 7200, 21600, 43200,
                                         86400, 172800, 604800.
              Response includes a "dimensions" object (host, port, service_name, etc.)
              that is absent in the Metering API.

API base URL:
  https://api.gateway.orchestration.{region}.hanacloud.ondemand.com

Required environment variables (.env):
  HANA_REGION          = prod-eu10   (landscape region, e.g. prod-eu10, prod-us10)
  HANA_AUTH_URL        = https://<landscape>.authentication.sap.hana.ondemand.com/oauth/token
  HANA_CLIENT_ID       = ...
  HANA_CLIENT_SECRET   = ...
  HANA_SERVICE_INSTANCE_ID = ...   (optional default; can be passed per tool call)
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

load_dotenv(override=False)
logger = logging.getLogger(__name__)

# ── OAuth2 token cache ────────────────────────────────────────────────────────
# Separate cache from uas_tool — different auth endpoint and credentials.
_hana_token_cache: dict = {"token": None, "expires_at": 0.0}


async def _get_hana_token() -> str:
    """Fetch (or return cached) OAuth2 access token for HANA Cloud APIs."""
    if _hana_token_cache["token"] and time.time() < _hana_token_cache["expires_at"] - 30:
        return _hana_token_cache["token"]

    auth_url      = os.environ["HANA_AUTH_URL"]
    client_id     = os.environ["HANA_CLIENT_ID"]
    client_secret = os.environ["HANA_CLIENT_SECRET"]

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

    _hana_token_cache["token"]      = data["access_token"]
    _hana_token_cache["expires_at"] = time.time() + data.get("expires_in", 3600)
    logger.info("HANA OAuth2 token refreshed, expires in %ds", data.get("expires_in", 3600))
    return _hana_token_cache["token"]


# ── Base URL helper ───────────────────────────────────────────────────────────
def _hana_base_url() -> str:
    region = os.environ.get("HANA_REGION", "prod-eu10")
    return f"https://api.gateway.orchestration.{region}.hanacloud.ondemand.com"


# ── Timestamp helpers ─────────────────────────────────────────────────────────
_HTTP_TIMEOUT_SECONDS = 60

# ISO 8601 format required by both APIs
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _validate_timestamp(ts: str, field_name: str) -> str:
    """Validate an ISO 8601 timestamp (YYYY-MM-DDTHH:MM:SSZ).

    Rejects timestamps from more than 2 years ago (guards against LLM
    training-data bias) and clamps future timestamps to now.
    Returns a valid ISO 8601 string.
    """
    now = datetime.now(tz=timezone.utc)
    try:
        dt = datetime.strptime(ts, _TS_FMT).replace(tzinfo=timezone.utc)
        if dt.year < now.year - 2:
            logger.warning(
                "Timestamp validation: %s=%r is from %d — likely LLM bias. "
                "Overriding with 1 hour ago.",
                field_name, ts, dt.year,
            )
            return (now - timedelta(hours=1)).strftime(_TS_FMT)
        if dt > now:
            logger.warning(
                "Timestamp validation: %s=%r is in the future. Clamping to now.",
                field_name, ts,
            )
            return now.strftime(_TS_FMT)
        return ts
    except ValueError:
        logger.warning(
            "Timestamp validation: %s=%r is not valid ISO 8601. Using 1 hour ago.",
            field_name, ts,
        )
        return (now - timedelta(hours=1)).strftime(_TS_FMT)


def _default_time_range_1h() -> tuple[str, str]:
    """Return (startTimestamp, endTimestamp) for the last hour."""
    now   = datetime.now(tz=timezone.utc)
    start = now - timedelta(hours=1)
    return start.strftime(_TS_FMT), now.strftime(_TS_FMT)


def _default_time_range_24h() -> tuple[str, str]:
    """Return (startTimestamp, endTimestamp) for the last 24 hours."""
    now   = datetime.now(tz=timezone.utc)
    start = now - timedelta(hours=24)
    return start.strftime(_TS_FMT), now.strftime(_TS_FMT)


# ── Validated interval sets (from OpenAPI spec) ───────────────────────────────
_METERING_INTERVALS = {3600, 7200, 21600, 43200, 86400, 172800, 604800}
_METRICS_INTERVALS  = {60, 300, 900, 1800, 3600, 7200, 21600, 43200, 86400, 172800, 604800}

_METERING_AGGREGATES = {"count", "last", "sum"}
_METRICS_AGGREGATES  = {"avg", "count", "delta", "last", "max", "min"}


def _resolve_service_instance_id(provided: Optional[str]) -> str:
    """Return provided ID, or fall back to HANA_SERVICE_INSTANCE_ID env var."""
    sid = (provided or "").strip()
    if not sid:
        sid = os.environ.get("HANA_SERVICE_INSTANCE_ID", "").strip()
    if not sid:
        raise ValueError(
            "service_instance_id is required. Pass it explicitly or set "
            "HANA_SERVICE_INSTANCE_ID in your environment."
        )
    return sid


# ── Core HTTP helpers ─────────────────────────────────────────────────────────

async def _hana_get(path: str, params: dict) -> dict:
    """Authenticated GET request to the HANA Cloud API gateway.

    Returns the parsed JSON response dict.
    Raises httpx.HTTPStatusError on non-2xx responses.
    """
    base  = _hana_base_url()
    token = await _get_hana_token()
    url   = f"{base}{path}"

    # Remove None/empty params so the API doesn't receive blank query strings
    clean_params = {k: v for k, v in params.items() if v is not None and v != ""}
    logger.info("HANA GET %s  params=%s", url, clean_params)

    timeout = httpx.Timeout(connect=15.0, read=_HTTP_TIMEOUT_SECONDS, write=15.0, pool=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            url,
            params=clean_params,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()


# ── LangChain Tools ───────────────────────────────────────────────────────────

@tool
async def list_hana_instances(
    service_instance_ids: Optional[str] = None,
) -> str:
    """
    Discover SAP HANA Cloud service instances and their metering definitions.

    Calls GET /metering/v1/definitions to return the metering metric definitions
    for all (or specified) service instances.  Use this tool FIRST to discover
    what service instances exist and what metering metrics they support before
    querying actual metering or performance values.

    Resource types returned:
      - hana-cloud-hdb     SAP HANA database
      - hana-cloud-hdl     SAP HANA Cloud data lake (relational engine)
      - hana-cloud-hdlfs   SAP HANA Cloud data lake Files

    Args:
        service_instance_ids: Optional comma-separated list of service instance GUIDs
            to filter results (e.g. "886b6bef-...,daefd1e7-...").
            Leave empty to retrieve definitions for ALL instances.

    Returns:
        JSON with:
          instance_count     — number of distinct service instances found
          instances          — list of { serviceInstanceID, resourceType,
                               metrics: [{ name, type, metricCategory,
                               description, interval, aggregates, retention }] }
          raw_definitions    — full flat list of definition objects as returned
                               by the API (for audit / debugging)
    """
    params: dict = {"$count": "true"}
    if service_instance_ids:
        params["serviceInstanceIDs"] = service_instance_ids

    try:
        data = await _hana_get("/metering/v1/definitions", params)
    except Exception as exc:
        return json.dumps({"error": str(exc)})

    definitions = data.get("data", [])
    total_count = data.get("count", len(definitions))

    # Group definitions by serviceInstanceID
    # Note: metering definitions are per-resourceType, not per-instance-ID, so
    # we group by resourceType when no instance filter is applied.
    by_resource: dict[str, list[dict]] = {}
    for d in definitions:
        rt = d.get("resourceType", "unknown")
        by_resource.setdefault(rt, [])
        by_resource[rt].append({
            "name":           d.get("name", ""),
            "type":           d.get("type", ""),
            "metricCategory": d.get("metricCategory", ""),
            "description":    d.get("description", ""),
            "interval":       d.get("interval"),
            "aggregates":     d.get("aggregates", []),
            "retention":      d.get("retention"),
        })

    instances = [
        {
            "resourceType": rt,
            "metric_count": len(metrics),
            "metrics":      sorted(metrics, key=lambda m: m["name"]),
        }
        for rt, metrics in sorted(by_resource.items())
    ]

    return json.dumps({
        "total_definitions": total_count,
        "resource_type_count": len(instances),
        "instances":           instances,
        "raw_definitions":     definitions,
    }, ensure_ascii=False)


@tool
async def get_hana_metering_values(
    service_instance_id: Optional[str] = None,
    start_timestamp: Optional[str] = None,
    end_timestamp: Optional[str] = None,
    metric_names: Optional[str] = None,
    aggregates: Optional[str] = "sum",
    interval: Optional[int] = 86400,
) -> str:
    """
    Query SAP HANA Cloud metering (capacity unit) consumption for a service instance.

    Calls GET /metering/v1/serviceInstances/{serviceInstanceID}/values.
    Metering data represents the billing-relevant CU (capacity unit) consumption
    of a HANA Cloud instance, covering categories: backup, memory, network,
    storage, vcpu.

    The API always returns a "delta" metric type — each value represents usage
    within the given interval, not a running total.

    Supported aggregates (comma-separated): count, last, sum
    Supported intervals (seconds):
      3600 (1h), 7200 (2h), 21600 (6h), 43200 (12h),
      86400 (1d, default), 172800 (2d), 604800 (7d)

    Args:
        service_instance_id: GUID of the HANA Cloud service instance.
            Falls back to HANA_SERVICE_INSTANCE_ID env var if not provided.
        start_timestamp: Start time in ISO 8601 format (e.g. "2026-06-01T00:00:00Z").
            Defaults to 24 hours ago.
        end_timestamp: End time in ISO 8601 format (e.g. "2026-06-15T00:00:00Z").
            Defaults to now.
        metric_names: Optional comma-separated metric names to filter
            (e.g. "DefaultNodeMemory,DefaultNodeVCPU").
            Leave empty to retrieve all metrics.
        aggregates: Comma-separated list of aggregation functions to apply.
            Default "sum". Options: count, last, sum.
            Pass empty string to get raw (non-aggregated) per-interval values.
        interval: Aggregation interval in seconds. Default 86400 (daily).
            Only used when aggregates is non-empty.

    Returns:
        JSON with:
          service_instance_id
          start_timestamp / end_timestamp
          aggregates / interval
          metric_count       — number of distinct metrics returned
          total_values       — total number of value points across all metrics
          metrics            — list of { name, resourceType, category, values }
            where each value has startTimestamp, endTimestamp, interval,
            and the requested aggregate fields (sum / count / last), OR
            timestamp + value for raw (non-aggregated) mode
          summary_by_metric  — { metric_name: { total_sum, value_count } }
                               for quick totals across the time range
    """
    try:
        sid = _resolve_service_instance_id(service_instance_id)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    # Resolve / validate timestamps
    default_start, default_end = _default_time_range_24h()
    s_ts = _validate_timestamp(start_timestamp or default_start, "start_timestamp")
    e_ts = _validate_timestamp(end_timestamp   or default_end,   "end_timestamp")
    if s_ts >= e_ts:
        logger.warning("start_timestamp %s >= end_timestamp %s — using last 24h.", s_ts, e_ts)
        s_ts, e_ts = default_start, default_end

    # Validate aggregates
    agg_str = (aggregates or "").strip()
    if agg_str:
        requested = {a.strip().lower() for a in agg_str.split(",")}
        invalid   = requested - _METERING_AGGREGATES
        if invalid:
            logger.warning(
                "Invalid metering aggregate(s) %s — removing. Valid: %s",
                invalid, _METERING_AGGREGATES,
            )
            requested -= invalid
        agg_str = ",".join(sorted(requested)) if requested else ""

    # Validate interval
    resolved_interval: Optional[int] = None
    if agg_str and interval is not None:
        if interval not in _METERING_INTERVALS:
            closest = min(_METERING_INTERVALS, key=lambda x: abs(x - interval))
            logger.warning(
                "Invalid metering interval %d — snapping to nearest valid value %d.",
                interval, closest,
            )
            resolved_interval = closest
        else:
            resolved_interval = interval

    params: dict = {
        "startTimestamp": s_ts,
        "endTimestamp":   e_ts,
    }
    if metric_names:
        params["names"] = metric_names
    if agg_str:
        params["aggregates"] = agg_str
    if resolved_interval is not None:
        params["interval"] = resolved_interval

    try:
        data = await _hana_get(
            f"/metering/v1/serviceInstances/{sid}/values", params
        )
    except Exception as exc:
        return json.dumps({"error": str(exc), "service_instance_id": sid})

    raw_metrics = data.get("data", [])

    # Normalise and enrich
    metrics_out = []
    summary: dict[str, dict] = {}

    for m in raw_metrics:
        name    = m.get("name", "")
        rt      = m.get("resourceType", "")
        values  = m.get("values", [])

        total_sum   = 0.0
        value_count = len(values)

        for v in values:
            s = v.get("sum")
            if s is not None:
                total_sum += float(s)

        metrics_out.append({
            "name":         name,
            "resourceType": rt,
            "values":       values,
        })

        summary[name] = {
            "total_sum":   round(total_sum, 6),
            "value_count": value_count,
        }

    total_values = sum(len(m["values"]) for m in metrics_out)

    return json.dumps({
        "service_instance_id": sid,
        "start_timestamp":     s_ts,
        "end_timestamp":       e_ts,
        "aggregates":          agg_str or "(raw)",
        "interval":            resolved_interval,
        "metric_count":        len(metrics_out),
        "total_values":        total_values,
        "metrics":             metrics_out,
        "summary_by_metric":   summary,
    }, ensure_ascii=False)


@tool
async def get_hana_metric_definitions(
    service_instance_id: Optional[str] = None,
    service_instance_ids: Optional[str] = None,
) -> str:
    """
    Discover available technical performance metrics for SAP HANA Cloud instances.

    Calls GET /metrics/v1/definitions (all instances) or
          GET /metrics/v1/serviceInstances/{id}/definitions (single instance).

    Metrics Service provides sub-minute operational data (memory, CPU, storage,
    network) — different from the Metering Service which tracks billing CUs.
    Use this tool to explore what metrics are available and their properties
    (unit, interval, supported aggregates, dimensions) before querying values.

    Resource types:
      - hana-cloud-hdb          SAP HANA database
      - hana-cloud-hdb-tenant   SAP HANA tenant database
      - hana-cloud-hdl          SAP HANA Cloud data lake

    Metric types: counter, delta, gauge
    Units: null, %, MiB, byte, s, ms, us

    Args:
        service_instance_id: Single instance GUID to fetch definitions for.
            Mutually exclusive with service_instance_ids.
            Falls back to HANA_SERVICE_INSTANCE_ID env var if both are empty.
        service_instance_ids: Comma-separated list of GUIDs for multi-instance
            query.  Leave both empty to get definitions for all instances.

    Returns:
        JSON with:
          total_definitions
          by_resource_type  — grouped view: resourceType → list of metric defs
          by_category       — cross-instance grouped by category name (memory,
                              vcpu, storage, network, backup)
          raw_definitions   — full flat list as returned by the API
    """
    try:
        if service_instance_id:
            sid = _resolve_service_instance_id(service_instance_id)
            path = f"/metrics/v1/serviceInstances/{sid}/definitions"
            params: dict = {"$count": "true"}
        else:
            path = "/metrics/v1/definitions"
            params = {"$count": "true"}
            if service_instance_ids:
                params["serviceInstanceIDs"] = service_instance_ids

        data = await _hana_get(path, params)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        return json.dumps({"error": str(exc)})

    definitions = data.get("data", [])
    total_count = data.get("count", len(definitions))

    # Group by resourceType
    by_rt: dict[str, list[dict]] = {}
    # Group by category (cross-type)
    by_cat: dict[str, list[dict]] = {}

    for d in definitions:
        rt   = d.get("resourceType", "unknown")
        name = d.get("name", "")
        defn = {
            "name":        name,
            "type":        d.get("type", ""),
            "unit":        d.get("unit", ""),
            "dimensions":  d.get("dimensions", []),
            "description": d.get("description", ""),
            "interval":    d.get("interval"),
            "aggregates":  d.get("aggregates", []),
            "retention":   d.get("retention"),
        }
        by_rt.setdefault(rt, []).append(defn)

        # Infer category from name prefix for grouping convenience
        cat = _infer_metric_category(name)
        by_cat.setdefault(cat, []).append({**defn, "resourceType": rt})

    return json.dumps({
        "total_definitions":  total_count,
        "by_resource_type":   {
            rt: sorted(defs, key=lambda d: d["name"])
            for rt, defs in sorted(by_rt.items())
        },
        "by_category":        {
            cat: sorted(defs, key=lambda d: d["name"])
            for cat, defs in sorted(by_cat.items())
        },
        "raw_definitions":    definitions,
    }, ensure_ascii=False)


def _infer_metric_category(name: str) -> str:
    """Infer a human-friendly category from a metric name prefix."""
    n = name.upper()
    if any(k in n for k in ("CPU", "VCPU")):
        return "vcpu"
    if any(k in n for k in ("MEM", "MEMORY")):
        return "memory"
    if any(k in n for k in ("DISK", "STOR", "FS", "FILE", "LOG")):
        return "storage"
    if any(k in n for k in ("NET", "CONN", "TRAFFIC")):
        return "network"
    if "BACKUP" in n:
        return "backup"
    return "other"


@tool
async def get_hana_metrics(
    service_instance_id: Optional[str] = None,
    start_timestamp: Optional[str] = None,
    end_timestamp: Optional[str] = None,
    metric_names: Optional[str] = None,
    aggregates: Optional[str] = "max",
    interval: Optional[int] = 3600,
    filter_expr: Optional[str] = None,
) -> str:
    """
    Query SAP HANA Cloud technical performance metrics for a service instance.

    Calls GET /metrics/v1/serviceInstances/{serviceInstanceID}/values.
    Returns operational metrics (memory usage, CPU, storage, network) with
    sub-minute granularity.  Unlike metering values (billing CUs), these are
    real-time performance indicators useful for capacity analysis and alerting.

    Each returned record includes a "dimensions" object (host, port, service_name)
    that identifies which HANA internal service the metric belongs to (e.g.
    indexserver, nameserver, compileserver).

    Supported aggregates (comma-separated): avg, count, delta, last, max, min
    Supported intervals (seconds):
      60 (1min), 300 (5min), 900 (15min), 1800 (30min),
      3600 (1h, default), 7200 (2h), 21600 (6h), 43200 (12h),
      86400 (1d), 172800 (2d), 604800 (7d)

    Example useful metric names (use get_hana_metric_definitions to discover all):
      HDBMemoryUsed         — memory in bytes used by each HANA service
      HDBCPU                — CPU usage percentage
      HDBDiskUsed           — disk space used in bytes
      HDBConnectionCount    — number of active connections

    Args:
        service_instance_id: GUID of the HANA Cloud service instance.
            Falls back to HANA_SERVICE_INSTANCE_ID env var if not provided.
        start_timestamp: Start time ISO 8601 (e.g. "2026-06-14T00:00:00Z").
            Defaults to 1 hour ago.
        end_timestamp: End time ISO 8601 (e.g. "2026-06-15T00:00:00Z").
            Defaults to now.
        metric_names: Optional comma-separated metric names to filter.
            Leave empty to retrieve all available metrics (can be large).
        aggregates: Comma-separated aggregation functions. Default "max".
            Options: avg, count, delta, last, max, min.
            Pass empty string for raw (per-sample) values.
        interval: Aggregation interval in seconds. Default 3600 (hourly).
            Only used when aggregates is non-empty.
        filter_expr: Optional OData-style filter string, e.g.
            "resourceType eq hana-cloud-hdb" or
            "(dimensions/service_name eq indexserver and values/max gt 100)".

    Returns:
        JSON with:
          service_instance_id
          start_timestamp / end_timestamp
          aggregates / interval
          metric_count       — number of distinct metric+dimension combinations
          total_values       — total number of value points
          metrics            — list of {
              name, resourceType, dimensions,
              values: [{ startTimestamp, endTimestamp, interval,
                         max/avg/min/count/delta/last (whichever requested) }]
            }
          summary_by_metric  — { metric_name: { value_count, max_value,
                                 avg_value (when 'avg' requested) } }
    """
    try:
        sid = _resolve_service_instance_id(service_instance_id)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    # Resolve / validate timestamps
    default_start, default_end = _default_time_range_1h()
    s_ts = _validate_timestamp(start_timestamp or default_start, "start_timestamp")
    e_ts = _validate_timestamp(end_timestamp   or default_end,   "end_timestamp")
    if s_ts >= e_ts:
        logger.warning("start_timestamp %s >= end_timestamp %s — using last 1h.", s_ts, e_ts)
        s_ts, e_ts = default_start, default_end

    # Validate aggregates
    agg_str = (aggregates or "").strip()
    if agg_str:
        requested = {a.strip().lower() for a in agg_str.split(",")}
        invalid   = requested - _METRICS_AGGREGATES
        if invalid:
            logger.warning(
                "Invalid metrics aggregate(s) %s — removing. Valid: %s",
                invalid, _METRICS_AGGREGATES,
            )
            requested -= invalid
        agg_str = ",".join(sorted(requested)) if requested else ""

    # Validate interval
    resolved_interval: Optional[int] = None
    if agg_str and interval is not None:
        if interval not in _METRICS_INTERVALS:
            closest = min(_METRICS_INTERVALS, key=lambda x: abs(x - interval))
            logger.warning(
                "Invalid metrics interval %d — snapping to nearest valid value %d.",
                interval, closest,
            )
            resolved_interval = closest
        else:
            resolved_interval = interval

    params: dict = {
        "startTimestamp": s_ts,
        "endTimestamp":   e_ts,
    }
    if metric_names:
        params["names"] = metric_names
    if agg_str:
        params["aggregates"] = agg_str
    if resolved_interval is not None:
        params["interval"] = resolved_interval
    if filter_expr:
        params["$filter"] = filter_expr

    try:
        data = await _hana_get(
            f"/metrics/v1/serviceInstances/{sid}/values", params
        )
    except Exception as exc:
        return json.dumps({"error": str(exc), "service_instance_id": sid})

    raw_metrics = data.get("data", [])

    # Normalise: collect per-metric summaries
    metrics_out = []
    summary: dict[str, dict] = {}

    requested_agg_set = {a.strip().lower() for a in agg_str.split(",")} if agg_str else set()

    for m in raw_metrics:
        name       = m.get("name", "")
        rt         = m.get("resourceType", "")
        dimensions = m.get("dimensions", {})
        values     = m.get("values", [])

        # Per-metric summary stats
        max_val = None
        avg_sum = 0.0
        avg_cnt = 0

        for v in values:
            mx = v.get("max")
            if mx is not None:
                max_val = max(max_val, float(mx)) if max_val is not None else float(mx)
            av = v.get("avg")
            if av is not None:
                avg_sum += float(av)
                avg_cnt += 1

        metrics_out.append({
            "name":         name,
            "resourceType": rt,
            "dimensions":   dimensions,
            "values":       values,
        })

        entry: dict = {"value_count": len(values)}
        if max_val is not None:
            entry["max_value"] = round(max_val, 6)
        if avg_cnt > 0:
            entry["avg_value"] = round(avg_sum / avg_cnt, 6)
        summary[name] = entry

    total_values = sum(len(m["values"]) for m in metrics_out)

    return json.dumps({
        "service_instance_id": sid,
        "start_timestamp":     s_ts,
        "end_timestamp":       e_ts,
        "aggregates":          agg_str or "(raw)",
        "interval":            resolved_interval,
        "metric_count":        len(metrics_out),
        "total_values":        total_values,
        "metrics":             metrics_out,
        "summary_by_metric":   summary,
    }, ensure_ascii=False)
