"""
BTP UAS Reporting API Tools
Calls the SAP Usage Accounting Service (UAS) APIs using OAuth2 client credentials.

Endpoints covered:
  GET /reports/v1/subaccountUsage  — daily per-subaccount usage detail
  GET /reports/v1/monthlyUsage     — monthly global-account usage report

API reference:
  https://api.sap.com/api/APIUasReportingService

Required environment variables (.env):
  BTP_UAS_URL           = https://uas-reporting.cfapps.eu10.hana.ondemand.com
  BTP_AUTH_URL          = https://<subdomain>.authentication.<region>.hana.ondemand.com/oauth/token
  BTP_CLIENT_ID         = ...
  BTP_CLIENT_SECRET     = ...
  BTP_SUBACCOUNT_ID     = ...   (for subaccountUsage tools)
  BTP_GLOBAL_ACCOUNT_ID = ...   (reserved; not sent to monthlyUsage — scope is
                                  implicit from the OAuth2 credentials)
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

load_dotenv(override=False)  # no-op in container; runtime env vars take priority
logger = logging.getLogger(__name__)

# ── OAuth2 token cache ───────────────────────────────────────────────────────
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


# ── Date helpers ─────────────────────────────────────────────────────────────
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


# ── Monthly date helpers (for /reports/v1/monthlyUsage) ──────────────────────

def _current_ym() -> str:
    """Return the current year-month as YYYY-MM (UTC)."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m")


def _ym_to_api(ym: str) -> str:
    """Convert YYYY-MM → YYYYMM (the format the monthly usage API expects)."""
    return ym.replace("-", "")


def _validate_month(ym: str, field_name: str) -> str:
    """Validate a YYYY-MM month string.

    - Rejects anything that cannot be parsed as YYYY-MM → uses current month.
    - Rejects months more than 2 years in the past (guards against LLM training-data
      bias producing stale years) → uses current month.
    - Clamps future months to the current month.
    """
    current = _current_ym()
    try:
        dt = datetime.strptime(ym, "%Y-%m")
        current_year = datetime.now(tz=timezone.utc).year
        if dt.year < current_year - 2:
            logger.warning(
                "Month validation: %s=%r is from %d — likely LLM bias. "
                "Overriding with current month (%s).",
                field_name, ym, dt.year, current,
            )
            return current
        if ym > current:
            logger.warning(
                "Month validation: %s=%r is in the future. Clamping to %s.",
                field_name, ym, current,
            )
            return current
    except ValueError:
        logger.warning(
            "Month validation: %s=%r is not a valid YYYY-MM month. Using %s.",
            field_name, ym, current,
        )
        return current
    return ym


def _last_n_months(n: int) -> tuple[str, str]:
    """Return (from_month, to_month) covering the last *n* complete months."""
    from calendar import monthrange

    today = datetime.now(tz=timezone.utc)
    # End of range: one month before today (last complete month)
    if today.month == 1:
        end_year, end_month = today.year - 1, 12
    else:
        end_year, end_month = today.year, today.month - 1

    # Start of range: n-1 months before end
    total_months = end_year * 12 + end_month - (n - 1)
    start_year, start_month = divmod(total_months - 1, 12)
    start_year += 1
    start_month += 1

    return (
        f"{start_year:04d}-{start_month:02d}",
        f"{end_year:04d}-{end_month:02d}",
    )


# ── Service classification ───────────────────────────────────────────────────
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


# ── Core API call ─────────────────────────────────────────────────────────────
def _week_chunks(from_date: str, to_date: str) -> list[tuple[str, str]]:
    """Split a date range into 7-day weekly chunks to avoid large single API requests.

    Each chunk covers exactly 7 days, except the last which may be shorter.
    E.g. 2026-01-01 → 2026-01-20 yields:
        (2026-01-01, 2026-01-07)
        (2026-01-08, 2026-01-14)
        (2026-01-15, 2026-01-20)
    """
    chunks: list[tuple[str, str]] = []
    start = datetime.strptime(from_date, "%Y-%m-%d").date()
    end   = datetime.strptime(to_date,   "%Y-%m-%d").date()

    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=6), end)
        chunks.append((current.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
        current = chunk_end + timedelta(days=1)

    return chunks


# How many days before we split the request into weekly chunks.
_CHUNK_THRESHOLD_DAYS = 60

# HTTP timeout for a single UAS API request (seconds).
# Large date ranges can return thousands of records — give the API enough time.
_HTTP_TIMEOUT_SECONDS = 120


async def _fetch_usage(from_date: str, to_date: str) -> list[dict]:
    """
    Call UAS API and return flat list of usage records.

    For date ranges wider than _CHUNK_THRESHOLD_DAYS the range is
    automatically split into monthly chunks so that each individual
    HTTP request stays small and well within the server's response window.

    Response item fields (verified against live API 2026-05):
      globalAccountId, globalAccountName,
      subaccountId, subaccountName,
      directoryId, directoryName,
      serviceId, serviceName,
      plan, planName,
      periodStartDate, periodEndDate,
      environmentInstanceId, environmentInstanceName,
      spaceId, spaceName,
      instanceId,
      measureId, metricName, unitSingular, unitPlural,
      identityZone, dataCenter, dataCenterName,
      usage, categoryId, categoryName,
      startIsoDate, endIsoDate,
      application          ← model name for ai-core CU records
                             (e.g. "gpt-4o-2024-08-06",
                                   "anthropic--claude-4.6-opus-1")
                             NOTE: spaceName is None for ai-core; plan is
                             always "extended" — neither encodes the model.
    """
    # Decide whether to chunk
    d_from = datetime.strptime(from_date, "%Y-%m-%d")
    d_to   = datetime.strptime(to_date,   "%Y-%m-%d")
    span_days = (d_to - d_from).days

    if span_days > _CHUNK_THRESHOLD_DAYS:
        logger.info(
            "Date range spans %d days (> %d threshold) — fetching in weekly chunks.",
            span_days, _CHUNK_THRESHOLD_DAYS,
        )
        all_records: list[dict] = []
        for chunk_from, chunk_to in _week_chunks(from_date, to_date):
            chunk_records = await _fetch_usage_single(chunk_from, chunk_to)
            logger.info(
                "  chunk %s → %s: %d records", chunk_from, chunk_to, len(chunk_records)
            )
            all_records.extend(chunk_records)
        logger.info("Total records across all chunks: %d", len(all_records))
        return all_records

    return await _fetch_usage_single(from_date, to_date)


async def _fetch_usage_single(from_date: str, to_date: str) -> list[dict]:
    """Perform a single UAS API HTTP request for the given date range."""
    uas_url    = os.environ.get("BTP_UAS_URL", "https://uas-reporting.cfapps.eu10.hana.ondemand.com")
    subaccount = os.environ["BTP_SUBACCOUNT_ID"]
    token      = await _get_token()

    url    = f"{uas_url}/reports/v1/subaccountUsage"
    params = {
        "subaccountId":      subaccount,
        "fromDate":          _to_uas_date(from_date),
        "toDate":            _to_uas_date(to_date),
        "periodPerspective": "DAY",
    }
    logger.info("UAS GET %s  params=%s", url, params)

    timeout = httpx.Timeout(
        connect=15.0,           # time to establish TCP connection
        read=_HTTP_TIMEOUT_SECONDS,  # time to receive the full response body
        write=15.0,
        pool=15.0,
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()

    records = data.get("content", data) if isinstance(data, dict) else data
    return records if isinstance(records, list) else []


# ── Monthly usage API call (GET /reports/v1/monthlyUsage) ────────────────────

async def _fetch_monthly_usage(from_month: str, to_month: str) -> list[dict]:
    """
    Call /reports/v1/monthlyUsage and return the list of records.

    Args:
        from_month: YYYY-MM (e.g. "2026-04")
        to_month:   YYYY-MM (e.g. "2026-06")

    Response record fields (SAP UAS monthly usage API):
      globalAccountId, globalAccountName,
      subaccountId, subaccountName,
      directoryId, directoryName,
      serviceId, serviceName,
      plan, planName,
      reportYearMonth,                ← integer YYYYMM, e.g. 202604
      measureId, metricName, unitSingular, unitPlural,
      usage,
      categoryId, categoryName,
      dataCenter, dataCenterName,
      chargedBlockedStatus            ← "Charged" / "Blocked" etc.

    Note: only fromDate/toDate (YYYYMM format) are accepted as query parameters.
    There is no globalAccountId parameter — the global account scope is determined
    implicitly by the OAuth2 client credentials (verified against the API spec).
    """
    uas_url = os.environ.get("BTP_UAS_URL", "https://uas-reporting.cfapps.eu10.hana.ondemand.com")
    token   = await _get_token()

    url    = f"{uas_url}/reports/v1/monthlyUsage"
    params = {
        "fromDate": _ym_to_api(from_month),   # YYYYMM format
        "toDate":   _ym_to_api(to_month),      # YYYYMM format
    }
    logger.info("Monthly Usage GET %s  params=%s", url, params)

    timeout = httpx.Timeout(connect=15.0, read=_HTTP_TIMEOUT_SECONDS, write=15.0, pool=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()

    records = data.get("content", data) if isinstance(data, dict) else data
    return records if isinstance(records, list) else []


# ── LangChain Tools ───────────────────────────────────────────────────────────

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
            "all"         → all services (default)
            "hana"        → SAP HANA Cloud only
            "aicore"      → SAP AI Core only
            "cf"          → Cloud Foundry Runtime only
            "integration" → SAP Integration Suite only
            "key"         → all 4 key services above

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
            "service":     r.get("serviceName") or r.get("serviceId", ""),
            "serviceId":   r.get("serviceId", ""),
            "plan":        r.get("planName") or r.get("plan", ""),
            "metric":      r.get("metricName") or r.get("measureId", ""),
            "measureId":   r.get("measureId", ""),
            "usage":       r.get("usage", 0),
            "unit":        r.get("unitSingular") or r.get("unitPlural", ""),
            "date":        r.get("startIsoDate", ""),
            "category":    r.get("categoryName", ""),
            "dataCenter":  r.get("dataCenter", ""),
            "space":       r.get("spaceName") or "",
            # For ai-core CU records this field carries the model name,
            # e.g. "gpt-4o-2024-08-06", "anthropic--claude-4.6-opus-1"
            "application": r.get("application") or "",
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


def _time_bucket(iso_date: str, granularity: str) -> str:
    """Return a time-bucket key from a YYYY-MM-DD date string.

    granularity:
      "day"   → "YYYY-MM-DD"  (unchanged)
      "month" → "YYYY-MM"
    """
    if not iso_date:
        return "unknown"
    if granularity == "month":
        return iso_date[:7]   # "YYYY-MM"
    return iso_date           # "YYYY-MM-DD"


@tool
async def get_aicore_model_cu_usage(
    from_date: str,
    to_date: str,
    time_granularity: str = "none",
) -> str:
    """
    Calculate SAP AI Core Capacity Unit (CU) consumption broken down by AI model.

    Filters the UAS data to only records where:
      - serviceId  == "ai-core"
      - measureId  == "capacity_units"

    The UAS API returns an `application` field on these records that carries
    the exact AI model name, e.g. "gpt-4o-2024-08-06",
    "anthropic--claude-4.6-opus-1", "mistralai--mistral-small-instruct-2503".
    This is the correct dimension to group by — NOT spaceName (always None for
    ai-core CU records) and NOT plan (always "extended").

    The optional `time_granularity` parameter controls whether the result is
    collapsed into a single period total or broken down by day / month.

    Args:
        from_date:        Start date in YYYY-MM-DD format (e.g. "2026-05-01")
        to_date:          End date in YYYY-MM-DD format (e.g. "2026-05-31")
        time_granularity: How to bucket time.  One of:
            "none"  → collapse entire date range into one total per model (default)
            "day"   → break down by calendar day   (YYYY-MM-DD)
            "month" → break down by calendar month (YYYY-MM)

    Returns:
        JSON with:
          - by_model:
              time_granularity == "none"  →  list of
                  { model, total_cu }
                  sorted descending by total_cu  (answers "which model uses most CU?")
              time_granularity == "day"|"month"  →  list of
                  { model, total_cu, time_breakdown: [{period, cu}, …] }
                  sorted descending by total_cu; time_breakdown sorted ascending by period
          - by_period  (only when time_granularity != "none"):
              list of { period, total_cu } across all models,
              sorted ascending by period — useful for trend / chart views
          - grand_total_cu:    sum of all CU in the period
          - record_count:      number of raw records matched
          - filtered_records:  raw AI Core CU records (for audit / debugging)
    """
    # ── 1. Validate inputs ───────────────────────────────────────────────────
    from_date = _validate_date(from_date, "from_date")
    to_date   = _validate_date(to_date,   "to_date")
    if from_date > to_date:
        logger.warning("from_date %s > to_date %s — swapping.", from_date, to_date)
        from_date, to_date = to_date, from_date

    tg = (time_granularity or "none").lower()
    if tg not in ("none", "day", "month"):
        logger.warning("Unknown time_granularity=%r — defaulting to 'none'.", tg)
        tg = "none"

    # ── 2. Fetch raw usage ───────────────────────────────────────────────────
    try:
        records = await _fetch_usage(from_date, to_date)
    except Exception as exc:
        return json.dumps({"error": str(exc), "from_date": from_date, "to_date": to_date})

    # ── 3. Filter: serviceId == "ai-core" AND measureId == "capacity_units" ─
    cu_records = [
        r for r in records
        if r.get("serviceId", "").lower() == "ai-core"
        and r.get("measureId", "").lower() == "capacity_units"
    ]

    if not cu_records:
        return json.dumps({
            "from_date":        from_date,
            "to_date":          to_date,
            "time_granularity": tg,
            "message":          "No AI Core capacity_units records found in the given period.",
            "by_model":         [],
            "grand_total_cu":   0,
            "record_count":     0,
            "filtered_records": [],
        }, ensure_ascii=False)

    # ── 4. Normalise raw records ─────────────────────────────────────────────
    # `application` is the field in the live UAS response that carries the
    # AI model name (verified against real API data, May 2026).
    # spaceName is always None and plan is always "extended" for these records.
    filtered_records = [
        {
            "model":       r.get("application") or "unknown",
            "cu":          float(r.get("usage", 0)),
            "date":        r.get("startIsoDate", ""),
            "dataCenter":  r.get("dataCenter", ""),
            "instanceId":  r.get("instanceId", ""),
        }
        for r in cu_records
    ]

    grand_total_cu = round(sum(r["cu"] for r in filtered_records), 6)

    # ── 5. Aggregate by model (+ optional time bucket) ───────────────────────
    # key: (model, period)  where period == "" when tg == "none"
    agg: dict[tuple[str, str], float] = {}
    for r in filtered_records:
        model  = r["model"]
        period = _time_bucket(r["date"], tg) if tg != "none" else ""
        key    = (model, period)
        agg[key] = round(agg.get(key, 0.0) + r["cu"], 6)

    # ── 6. Build by_model view ───────────────────────────────────────────────
    model_totals: dict[str, float] = {}
    model_breakdown: dict[str, dict[str, float]] = {}   # model → period → cu

    for (model, period), cu in agg.items():
        model_totals[model] = round(model_totals.get(model, 0.0) + cu, 6)
        if tg != "none":
            model_breakdown.setdefault(model, {})
            model_breakdown[model][period] = round(
                model_breakdown[model].get(period, 0.0) + cu, 6
            )

    if tg == "none":
        by_model = sorted(
            [{"model": m, "total_cu": total} for m, total in model_totals.items()],
            key=lambda x: -x["total_cu"],
        )
    else:
        by_model = sorted(
            [
                {
                    "model":          m,
                    "total_cu":       total,
                    "time_breakdown": sorted(
                        [{"period": p, "cu": cu}
                         for p, cu in model_breakdown.get(m, {}).items()],
                        key=lambda x: x["period"],
                    ),
                }
                for m, total in model_totals.items()
            ],
            key=lambda x: -x["total_cu"],
        )

    # ── 7. Build by_period view (only when time is not collapsed) ────────────
    result: dict = {
        "from_date":        from_date,
        "to_date":          to_date,
        "time_granularity": tg,
        "record_count":     len(filtered_records),
        "grand_total_cu":   grand_total_cu,
        "by_model":         by_model,
        "filtered_records": filtered_records,
    }

    if tg != "none":
        period_totals: dict[str, float] = {}
        for (_, period), cu in agg.items():
            if period:
                period_totals[period] = round(period_totals.get(period, 0.0) + cu, 6)

        result["by_period"] = sorted(
            [{"period": p, "total_cu": cu} for p, cu in period_totals.items()],
            key=lambda x: x["period"],
        )

    return json.dumps(result, ensure_ascii=False)


@tool
async def simulate_aicore_cu_eom_forecast(
    reference_date: Optional[str] = None,
) -> str:
    """
    Forecast AI Core Capacity Unit (CU) consumption by end of the current month.

    Uses three independent projection methods and combines them into an ensemble:

      1. Linear       — average daily rate over all elapsed days of the month
                        projected forward to fill the remaining days.
      2. Trend (7d)   — same as linear, but the daily rate is computed from the
                        most recent 7 days only.  More responsive to sudden
                        acceleration or deceleration in usage.
      3. Historical   — fetches the previous month's full CU total and its
                        partial total up to the same day-of-month, then applies
                        the ratio  (prev_full / prev_partial)  to the current
                        month's total so far.  Best when usage follows a
                        repeating monthly pattern.

      Ensemble        — weighted average of whichever methods are available.
                        Historical gets higher weight (0.4) when at least 14
                        days of prior-month data exist at the reference point;
                        otherwise methods share equal weight.

    NOTE: The UAS API delivers data with a ~1-day lag, so "today" may not yet
    appear in the records.  The tool automatically detects the last date that
    has data and uses that as the end of the observed window.

    Args:
        reference_date: Treat this date as "today" (YYYY-MM-DD).
                        Defaults to today's UTC date.
                        Pass a past date for what-if / back-testing.

    Returns:
        JSON with:
          context        — calendar info: reference_date, month_start, month_end,
                           days_in_month, last_data_date, data_days_elapsed,
                           days_remaining
          current_month  — cu_so_far, daily_breakdown [{date, cu}],
                           by_model [{model, cu_so_far, forecast_linear_cu}]
          previous_month — prev_month_label, prev_full_cu,
                           prev_partial_cu (up to same day-of-month),
                           prev_partial_days  (null when no prev data)
          forecasts      — linear, trend_7d, historical (null if unavailable),
                           ensemble; each entry has forecast_cu and
                           method_description
          record_count   — number of raw AI Core CU records used
    """
    from calendar import monthrange

    # ── 1. Resolve reference date ────────────────────────────────────────────
    today_utc = datetime.now(tz=timezone.utc).date()
    if reference_date:
        try:
            ref = datetime.strptime(reference_date, "%Y-%m-%d").date()
            if ref > today_utc:
                logger.warning("reference_date %s is in the future — clamping to today.", reference_date)
                ref = today_utc
        except ValueError:
            logger.warning("Invalid reference_date %r — using today.", reference_date)
            ref = today_utc
    else:
        ref = today_utc

    # ── 2. Build current-month window ────────────────────────────────────────
    month_start = ref.replace(day=1)
    days_in_month = monthrange(ref.year, ref.month)[1]
    month_end = ref.replace(day=days_in_month)
    # Fetch up to ref (inclusive); API lag means ref itself may have no data
    fetch_to = min(ref, today_utc)
    month_start_str = month_start.strftime("%Y-%m-%d")
    fetch_to_str    = fetch_to.strftime("%Y-%m-%d")

    # ── 3. Build previous-month window ───────────────────────────────────────
    if month_start.month == 1:
        prev_month_start = month_start.replace(year=month_start.year - 1, month=12, day=1)
    else:
        prev_month_start = month_start.replace(month=month_start.month - 1)
    prev_days_in_month = monthrange(prev_month_start.year, prev_month_start.month)[1]
    prev_month_end = prev_month_start.replace(day=prev_days_in_month)
    prev_month_start_str = prev_month_start.strftime("%Y-%m-%d")
    prev_month_end_str   = prev_month_end.strftime("%Y-%m-%d")

    # ── 4. Fetch data (current month + previous month) ───────────────────────
    try:
        cur_records_raw  = await _fetch_usage(month_start_str, fetch_to_str)
        prev_records_raw = await _fetch_usage(prev_month_start_str, prev_month_end_str)
    except Exception as exc:
        return json.dumps({"error": str(exc), "reference_date": str(ref)})

    def _filter_cu(records: list[dict]) -> list[dict]:
        return [
            r for r in records
            if r.get("serviceId", "").lower() == "ai-core"
            and r.get("measureId", "").lower() == "capacity_units"
        ]

    cur_cu  = _filter_cu(cur_records_raw)
    prev_cu = _filter_cu(prev_records_raw)

    # ── 5. Build per-day aggregates for current month ────────────────────────
    # day_totals: date_str → total CU that day
    day_totals: dict[str, float] = {}
    model_totals: dict[str, float] = {}
    for r in cur_cu:
        d     = r.get("startIsoDate", "")
        cu    = float(r.get("usage", 0))
        model = r.get("application") or "unknown"
        if d:
            day_totals[d]    = round(day_totals.get(d, 0.0)    + cu, 6)
            model_totals[model] = round(model_totals.get(model, 0.0) + cu, 6)

    daily_breakdown = sorted(
        [{"date": d, "cu": cu} for d, cu in day_totals.items()],
        key=lambda x: x["date"],
    )

    cu_so_far = round(sum(day_totals.values()), 6)

    # Last date with actual data
    if daily_breakdown:
        last_data_date = daily_breakdown[-1]["date"]
        data_days_elapsed = datetime.strptime(last_data_date, "%Y-%m-%d").day
    else:
        last_data_date    = None
        data_days_elapsed = 0

    days_remaining = days_in_month - data_days_elapsed

    # ── 6. Recent-7d daily rate ──────────────────────────────────────────────
    recent_window = 7
    recent_days = daily_breakdown[-recent_window:] if len(daily_breakdown) >= 1 else []
    recent_cu   = sum(d["cu"] for d in recent_days)
    recent_n    = len(recent_days)  # actual days with data in the window

    # ── 7. Previous-month partial (up to same day-of-month) ─────────────────
    prev_day_totals: dict[str, float] = {}
    for r in prev_cu:
        d  = r.get("startIsoDate", "")
        cu = float(r.get("usage", 0))
        if d:
            prev_day_totals[d] = round(prev_day_totals.get(d, 0.0) + cu, 6)

    prev_full_cu = round(sum(prev_day_totals.values()), 6)

    # Partial: same day-of-month cutoff as data_days_elapsed
    prev_partial_cu = round(sum(
        cu for d, cu in prev_day_totals.items()
        if datetime.strptime(d, "%Y-%m-%d").day <= data_days_elapsed
    ), 6)
    prev_partial_days = data_days_elapsed if prev_day_totals else None

    # ── 8. Compute forecasts ─────────────────────────────────────────────────
    forecasts: dict = {}

    if data_days_elapsed > 0:
        # Method 1: Linear
        avg_daily = cu_so_far / data_days_elapsed
        f_linear  = round(cu_so_far + avg_daily * days_remaining, 6)
        forecasts["linear"] = {
            "forecast_cu":        f_linear,
            "avg_daily_rate":     round(avg_daily, 6),
            "method_description": (
                f"Average daily CU over the {data_days_elapsed} elapsed day(s) "
                f"({round(avg_daily, 4)} CU/day) projected across the remaining "
                f"{days_remaining} day(s)."
            ),
        }

        # Method 2: Trend 7d
        if recent_n > 0:
            trend_daily = recent_cu / recent_n
            f_trend     = round(cu_so_far + trend_daily * days_remaining, 6)
            forecasts["trend_7d"] = {
                "forecast_cu":          f_trend,
                "recent_7d_daily_rate": round(trend_daily, 6),
                "recent_days_used":     recent_n,
                "method_description": (
                    f"Daily rate from the last {recent_n} day(s) with data "
                    f"({round(trend_daily, 4)} CU/day) projected across the remaining "
                    f"{days_remaining} day(s)."
                ),
            }
        else:
            forecasts["trend_7d"] = None

        # Method 3: Historical ratio
        if prev_partial_cu > 0:
            ratio   = prev_full_cu / prev_partial_cu
            f_hist  = round(cu_so_far * ratio, 6)
            forecasts["historical"] = {
                "forecast_cu":          f_hist,
                "prev_month_label":     prev_month_start.strftime("%Y-%m"),
                "prev_full_cu":         prev_full_cu,
                "prev_partial_cu":      prev_partial_cu,
                "prev_month_ratio":     round(ratio, 6),
                "method_description": (
                    f"Previous month ({prev_month_start.strftime('%Y-%m')}) reached "
                    f"{prev_full_cu} CU total; by day {data_days_elapsed} it had consumed "
                    f"{prev_partial_cu} CU (ratio {round(ratio, 4)}×). "
                    f"Applying that ratio to current {cu_so_far} CU."
                ),
            }
        else:
            forecasts["historical"] = None

        # Ensemble: weighted average of available methods
        available: list[tuple[float, float]] = []  # (forecast, weight)
        if "linear" in forecasts:
            available.append((f_linear, 1.0))
        if forecasts.get("trend_7d"):
            available.append((f_trend, 1.0))
        if forecasts.get("historical"):
            # Give historical higher weight when the prior-month sample is ≥14 days
            hist_weight = 1.4 if data_days_elapsed >= 14 else 1.0
            available.append((f_hist, hist_weight))

        if available:
            total_weight = sum(w for _, w in available)
            f_ensemble   = round(sum(f * w for f, w in available) / total_weight, 6)
            weight_note  = (
                "equal weights" if len({w for _, w in available}) == 1
                else "historical weighted 1.4× (≥14 days of prior-month data available)"
            )
            forecasts["ensemble"] = {
                "forecast_cu":        f_ensemble,
                "methods_combined":   [k for k in ("linear", "trend_7d", "historical") if forecasts.get(k)],
                "method_description": (
                    f"Weighted average of {len(available)} method(s) ({weight_note}). "
                    "This is the recommended single estimate."
                ),
            }
        else:
            forecasts["ensemble"] = None
    else:
        # No data yet — cannot forecast
        for key in ("linear", "trend_7d", "historical", "ensemble"):
            forecasts[key] = None

    # ── 9. Per-model linear forecast ─────────────────────────────────────────
    by_model_forecast = []
    if data_days_elapsed > 0:
        for model, cu in sorted(model_totals.items(), key=lambda x: -x[1]):
            model_daily = cu / data_days_elapsed
            by_model_forecast.append({
                "model":             model,
                "cu_so_far":         round(cu, 6),
                "avg_daily_rate":    round(model_daily, 6),
                "forecast_eom_cu":   round(cu + model_daily * days_remaining, 6),
            })

    # ── 10. Assemble result ───────────────────────────────────────────────────
    return json.dumps({
        "context": {
            "reference_date":    str(ref),
            "month_start":       month_start_str,
            "month_end":         month_end.strftime("%Y-%m-%d"),
            "days_in_month":     days_in_month,
            "last_data_date":    last_data_date,
            "data_days_elapsed": data_days_elapsed,
            "days_remaining":    days_remaining,
        },
        "current_month": {
            "cu_so_far":       cu_so_far,
            "daily_breakdown": daily_breakdown,
            "by_model":        by_model_forecast,
        },
        "previous_month": {
            "prev_month_label":  prev_month_start.strftime("%Y-%m"),
            "prev_full_cu":      prev_full_cu,
            "prev_partial_cu":   prev_partial_cu,
            "prev_partial_days": prev_partial_days,
        },
        "forecasts":    forecasts,
        "record_count": len(cur_cu),
    }, ensure_ascii=False)


# ── Anomaly detection helpers (stdlib only — no numpy/scipy needed) ────────────

def _assess_data_shape(values: list[float]) -> dict:
    """
    Inspect a numeric series and decide the best anomaly-detection algorithm.

    Selection rules (applied in order):
      n < 5              → "insufficient_data"  (too few points)
      5 ≤ n < 14         → "iqr"               (distribution-free, small sample)
      n ≥ 14, CV < 1.5
            & |skew| ≤ 0.5 → "zscore"           (data roughly symmetric/normal)
      n ≥ 14, otherwise  → "mad"               (robust to right-skewed usage data)

    Returns a dict with stats + "recommended_method" + "reason".
    """
    import statistics as _s

    n = len(values)
    if n == 0:
        return {
            "n": 0,
            "recommended_method": "insufficient_data",
            "reason": "No data points.",
        }
    if n == 1:
        return {
            "n": 1,
            "mean": round(values[0], 6),
            "median": round(values[0], 6),
            "std": 0.0,
            "mad": 0.0,
            "cv": 0.0,
            "skewness_proxy": 0.0,
            "q1": round(values[0], 6),
            "q3": round(values[0], 6),
            "iqr": 0.0,
            "recommended_method": "insufficient_data",
            "reason": "Only 1 data point.",
        }

    mean_v   = _s.mean(values)
    std_v    = _s.stdev(values)
    median_v = _s.median(values)
    devs     = [abs(v - median_v) for v in values]
    mad_v    = _s.median(devs)
    cv       = std_v / mean_v if mean_v > 0 else 0.0
    # Pearson's second skewness coefficient proxy: (mean − median) / std
    skew     = (mean_v - median_v) / std_v if std_v > 0 else 0.0

    sorted_v = sorted(values)
    # statistics.quantiles returns [Q1, Q2, Q3] for n=4
    q1, _, q3 = _s.quantiles(sorted_v, n=4)
    iqr_v    = q3 - q1

    if n < 5:
        method = "insufficient_data"
        reason = f"Only {n} data point(s) (need ≥ 5 for reliable detection)."
    elif n < 14:
        method = "iqr"
        reason = (
            f"{n} data points — IQR fences used (small sample, distribution-free)."
        )
    elif cv >= 1.5 or abs(skew) > 0.5:
        method = "mad"
        reason = (
            f"{n} data points; CV={round(cv, 2)}, |skew|={round(abs(skew), 2)} — "
            "right-skewed usage data detected, MAD (Median Absolute Deviation) selected "
            "for robustness."
        )
    else:
        method = "zscore"
        reason = (
            f"{n} data points; CV={round(cv, 2)}, |skew|={round(abs(skew), 2)} — "
            "data roughly symmetric, Z-score appropriate."
        )

    return {
        "n":                n,
        "mean":             round(mean_v,   6),
        "std":              round(std_v,    6),
        "median":           round(median_v, 6),
        "mad":              round(mad_v,    6),
        "cv":               round(cv,       4),
        "skewness_proxy":   round(skew,     4),
        "q1":               round(q1,       6),
        "q3":               round(q3,       6),
        "iqr":              round(iqr_v,    6),
        "recommended_method": method,
        "reason":           reason,
    }


def _run_detection(
    series: list[tuple[str, float]],
    shape:  dict,
    thresholds: dict,
) -> list[dict]:
    """
    Apply the algorithm chosen by _assess_data_shape to a (date, value) series.

    thresholds: {"zscore": float, "iqr_k": float, "mad_k": float}
    Returns a list of anomaly dicts sorted by |score| descending.
    """
    import statistics as _s

    method = shape.get("recommended_method", "insufficient_data")
    if method == "insufficient_data" or len(series) < 2:
        return []

    values   = [v for _, v in series]
    mean_v   = shape["mean"]
    median_v = shape["median"]
    std_v    = shape["std"]
    mad_v    = shape["mad"]
    q1       = shape["q1"]
    q3       = shape["q3"]
    iqr_v    = shape["iqr"]

    anomalies: list[dict] = []

    if method == "zscore":
        t = thresholds["zscore"]
        for date, val in series:
            if std_v > 0:
                z   = (val - mean_v) / std_v
                if abs(z) > t:
                    pct = (val - mean_v) / mean_v * 100 if mean_v > 0 else 0.0
                    anomalies.append({
                        "date":        date,
                        "value":       round(val, 6),
                        "score":       round(z, 4),
                        "score_type":  "z-score",
                        "direction":   "high" if z > 0 else "low",
                        "mean":        mean_v,
                        "std":         std_v,
                        "pct_vs_mean": round(pct, 1),
                        "reason": (
                            f"{round(val, 4)} CU is {round(abs(pct), 1)}% "
                            f"{'above' if z > 0 else 'below'} mean "
                            f"({mean_v} CU); z={round(z, 2)}"
                        ),
                    })

    elif method == "iqr":
        k     = thresholds["iqr_k"]
        upper = q3 + k * iqr_v
        lower = max(0.0, q1 - k * iqr_v)
        for date, val in series:
            if val > upper or val < lower:
                pct = (val - mean_v) / mean_v * 100 if mean_v > 0 else 0.0
                direction = "high" if val > upper else "low"
                # Guard: IQR==0 means all baseline values are equal; use
                # raw distance from fence as score to avoid division by zero.
                _iqr_denom = iqr_v if iqr_v > 0 else max(abs(q3), 1e-10)
                anomalies.append({
                    "date":         date,
                    "value":        round(val, 6),
                    "score":        round(
                        (val - upper) / _iqr_denom if val > upper else (lower - val) / _iqr_denom,
                        4,
                    ),
                    "score_type":   "iqr_distance",
                    "direction":    direction,
                    "upper_fence":  round(upper, 6),
                    "lower_fence":  round(lower, 6),
                    "pct_vs_mean":  round(pct, 1),
                    "reason": (
                        f"{round(val, 4)} CU {'exceeds upper' if direction == 'high' else 'falls below lower'} "
                        f"IQR fence ({round(upper if direction == 'high' else lower, 4)} CU); "
                        f"{round(abs(pct), 1)}% {'above' if pct > 0 else 'below'} mean"
                    ),
                })

    elif method == "mad":
        t       = thresholds["mad_k"]
        # Consistency constant: MAD × 1.4826 ≈ σ for a normal distribution
        mad_std = mad_v * 1.4826 if mad_v > 0 else 1e-10
        for date, val in series:
            mz  = (val - median_v) / mad_std
            if abs(mz) > t:
                pct = (val - median_v) / median_v * 100 if median_v > 0 else 0.0
                anomalies.append({
                    "date":           date,
                    "value":          round(val, 6),
                    "score":          round(mz, 4),
                    "score_type":     "modified_z-score (MAD)",
                    "direction":      "high" if mz > 0 else "low",
                    "median":         median_v,
                    "mad":            mad_v,
                    "pct_vs_median":  round(pct, 1),
                    "reason": (
                        f"{round(val, 4)} CU is {round(abs(pct), 1)}% "
                        f"{'above' if mz > 0 else 'below'} median "
                        f"({median_v} CU); modified-z={round(mz, 2)}"
                    ),
                })

    return sorted(anomalies, key=lambda x: -abs(x["score"]))


# ── Sensitivity presets ───────────────────────────────────────────────────────
_SENSITIVITY_PRESETS: dict[str, dict] = {
    #              z-score  IQR-k  MAD-k
    "low":    {"zscore": 3.0, "iqr_k": 2.50, "mad_k": 3.5},  # fewer alerts
    "medium": {"zscore": 2.5, "iqr_k": 1.75, "mad_k": 2.5},  # balanced (default)
    "high":   {"zscore": 2.0, "iqr_k": 1.50, "mad_k": 2.0},  # more alerts
}


@tool
async def detect_aicore_cu_anomaly(
    lookback_days: int = 30,
    reference_date: Optional[str] = None,
    sensitivity: str = "medium",
) -> str:
    """
    Detect anomalies in SAP AI Core Capacity Unit (CU) daily consumption.

    Automatically selects the best statistical anomaly-detection algorithm
    based on the shape of the actual data (sample size, coefficient of
    variation, skewness):

      < 5 active days    → insufficient data; returns a clear message
      5–13 active days   → IQR fences          (distribution-free, small sample)
      ≥ 14 days, CV < 1.5
           & |skew| ≤ 0.5 → Z-score            (data roughly symmetric/normal)
      ≥ 14 days, otherwise → MAD               (robust to right-skewed usage)

    Detection runs at two levels:
      1. Total daily CU   — sum across all models per calendar day.
      2. Per-model daily CU — independently for each model that has ≥ 5 points.

    Args:
        lookback_days:  Calendar days to look back from reference_date.
                        Default 30. Clamped to [7, 90].
        reference_date: Treat this date as "today" (YYYY-MM-DD).
                        Defaults to today's UTC date.
        sensitivity:    "low" (fewer alerts), "medium" (default), "high" (more alerts).

    Returns:
        JSON with:
          from_date / to_date         — analysis window
          sensitivity                 — sensitivity level used
          data_summary                — record count, active days, daily series
          data_shape                  — n, mean, std, cv, skew, algorithm + rationale
          algorithm_used              — name of the chosen algorithm
          algorithm_rationale         — plain-English explanation
          total_daily_anomalies       — anomalous days for the combined-total series
          per_model_anomalies         — dict: model → anomalous days for that model
          per_model_skipped           — models with < 5 data points (skipped)
          summary_text                — human-readable chat summary
    """
    # ── 1. Resolve dates ─────────────────────────────────────────────────────
    today_utc = datetime.now(tz=timezone.utc).date()
    if reference_date:
        try:
            ref = datetime.strptime(reference_date, "%Y-%m-%d").date()
            if ref > today_utc:
                ref = today_utc
        except ValueError:
            ref = today_utc
    else:
        ref = today_utc

    lookback_days = max(7, min(90, lookback_days))
    from_date     = (ref - timedelta(days=lookback_days - 1)).strftime("%Y-%m-%d")
    to_date       = ref.strftime("%Y-%m-%d")

    # ── 2. Sensitivity thresholds ─────────────────────────────────────────────
    sens       = (sensitivity or "medium").lower()
    if sens not in _SENSITIVITY_PRESETS:
        sens = "medium"
    thresholds = _SENSITIVITY_PRESETS[sens]

    # ── 3. Fetch data ─────────────────────────────────────────────────────────
    try:
        records = await _fetch_usage(from_date, to_date)
    except Exception as exc:
        return json.dumps({"error": str(exc), "from_date": from_date, "to_date": to_date})

    cu_records = [
        r for r in records
        if r.get("serviceId", "").lower() == "ai-core"
        and r.get("measureId", "").lower() == "capacity_units"
    ]

    if not cu_records:
        return json.dumps({
            "from_date":              from_date,
            "to_date":                to_date,
            "sensitivity":            sens,
            "total_daily_anomalies":  [],
            "per_model_anomalies":    {},
            "per_model_skipped":      [],
            "summary_text": (
                f"No AI Core capacity_units records found between {from_date} and "
                f"{to_date}. Cannot detect anomalies."
            ),
        }, ensure_ascii=False)

    # ── 4. Aggregate by day ────────────────────────────────────────────────────
    daily_totals:  dict[str, float]              = {}
    model_daily:   dict[str, dict[str, float]]   = {}   # model → date → CU

    for r in cu_records:
        d     = r.get("startIsoDate", "")
        cu    = float(r.get("usage", 0))
        model = r.get("application") or "unknown"
        if d:
            daily_totals[d]  = round(daily_totals.get(d, 0.0)  + cu, 6)
            model_daily.setdefault(model, {})
            model_daily[model][d] = round(model_daily[model].get(d, 0.0) + cu, 6)

    # Sorted (date, total_cu) series for the total-level detection
    total_series: list[tuple[str, float]] = sorted(daily_totals.items())
    values_total: list[float]             = [v for _, v in total_series]

    # ── 5. Assess data shape & run total-level detection ──────────────────────
    total_shape    = _assess_data_shape(values_total)
    total_anomalies = _run_detection(total_series, total_shape, thresholds)

    # ── 6. Per-model detection ─────────────────────────────────────────────────
    per_model_anomalies: dict[str, list[dict]] = {}
    per_model_skipped:   list[str]             = []

    for model, day_map in model_daily.items():
        model_series = sorted(day_map.items())
        model_vals   = [v for _, v in model_series]
        model_shape  = _assess_data_shape(model_vals)

        if model_shape["recommended_method"] == "insufficient_data":
            per_model_skipped.append(model)
            continue

        anomalies = _run_detection(model_series, model_shape, thresholds)
        if anomalies:
            per_model_anomalies[model] = anomalies

    # ── 7. Build human-readable summary ───────────────────────────────────────
    method_label = {
        "zscore":            "Z-score",
        "iqr":               "IQR fences",
        "mad":               "MAD (robust)",
        "insufficient_data": "N/A",
    }.get(total_shape["recommended_method"], total_shape["recommended_method"])

    n_days         = total_shape["n"]
    total_anom_cnt = len(total_anomalies)
    model_anom_cnt = sum(len(v) for v in per_model_anomalies.values())
    all_anom_cnt   = total_anom_cnt + model_anom_cnt

    if total_shape["recommended_method"] == "insufficient_data":
        summary = (
            f"Insufficient data: only {n_days} active day(s) found between "
            f"{from_date} and {to_date}. At least 5 days are required to detect "
            f"anomalies reliably."
        )
    elif all_anom_cnt == 0:
        summary = (
            f"No anomalies detected between {from_date} and {to_date}.\n"
            f"Algorithm: {method_label} | Sensitivity: {sens} | "
            f"Active days: {n_days} | "
            f"Daily total CU range: {round(min(values_total), 4)} – "
            f"{round(max(values_total), 4)} CU"
        )
    else:
        lines = [
            f"### AI Core CU Anomaly Report",
            f"Period: {from_date} → {to_date} | "
            f"Algorithm: {method_label} | Sensitivity: {sens}",
            f"**{all_anom_cnt} anomaly(-ies) found**",
        ]
        if total_anomalies:
            lines.append(f"\n**Total daily CU anomalies ({total_anom_cnt}):**")
            for a in sorted(total_anomalies, key=lambda x: -x["value"]):
                lines.append(f"  - {a['date']}: **{a['value']} CU** — {a['reason']}")
        if per_model_anomalies:
            lines.append(f"\n**Per-model anomalies ({model_anom_cnt}):**")
            for model, anoms in sorted(per_model_anomalies.items()):
                for a in sorted(anoms, key=lambda x: -x["value"]):
                    lines.append(
                        f"  - [{model}] {a['date']}: **{a['value']} CU** — {a['reason']}"
                    )
        if per_model_skipped:
            lines.append(
                f"\n_Models skipped (< 5 data points): "
                f"{', '.join(per_model_skipped)}_"
            )
        summary = "\n".join(lines)

    # ── 8. Assemble result ─────────────────────────────────────────────────────
    return json.dumps({
        "from_date":   from_date,
        "to_date":     to_date,
        "sensitivity": sens,
        "data_summary": {
            "total_cu_records": len(cu_records),
            "days_with_data":   n_days,
            "min_daily_cu":     round(min(values_total), 6) if values_total else 0.0,
            "max_daily_cu":     round(max(values_total), 6) if values_total else 0.0,
            "daily_series":     [
                {"date": d, "total_cu": round(cu, 6)}
                for d, cu in total_series
            ],
        },
        "data_shape":         total_shape,
        "algorithm_used":     total_shape["recommended_method"],
        "algorithm_rationale": total_shape.get("reason", ""),
        "total_daily_anomalies":  total_anomalies,
        "per_model_anomalies":    per_model_anomalies,
        "per_model_skipped":      per_model_skipped,
        "summary_text":           summary,
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


# ── Global-account monthly usage tools (GET /reports/v1/monthlyUsage) ─────────

@tool
async def list_subaccounts(
    from_month: Optional[str] = None,
    to_month: Optional[str] = None,
) -> str:
    """
    Discover all subaccounts that reported usage under the BTP global account.

    Calls /reports/v1/monthlyUsage and returns the distinct set of subaccount
    IDs and names found within the requested period.  Use this tool as the
    first step when you need to know which subaccounts exist BEFORE querying
    a specific subaccount's daily detail with get_btp_usage.

    Args:
        from_month: Start month in YYYY-MM format (e.g. "2026-04").
                    Defaults to 3 months ago.
        to_month:   End month in YYYY-MM format (e.g. "2026-06").
                    Defaults to last complete month.

    Returns:
        JSON with:
          global_account_id   — global account GUID
          global_account_name — display name
          from_month / to_month
          subaccount_count    — number of distinct subaccounts found
          subaccounts         — sorted list of
              { subaccount_id, subaccount_name,
                directory_id, directory_name,
                service_count, month_count }
              where service_count = distinct services used,
                    month_count  = distinct months with usage
    """
    # ── Resolve / default months ──────────────────────────────────────────────
    default_from, default_to = _last_n_months(3)
    from_month = _validate_month(from_month or default_from, "from_month")
    to_month   = _validate_month(to_month   or default_to,   "to_month")
    if from_month > to_month:
        logger.warning("from_month %s > to_month %s — swapping.", from_month, to_month)
        from_month, to_month = to_month, from_month

    try:
        records = await _fetch_monthly_usage(from_month, to_month)
    except Exception as exc:
        return json.dumps({"error": str(exc), "from_month": from_month, "to_month": to_month})

    if not records:
        return json.dumps({
            "from_month": from_month,
            "to_month":   to_month,
            "message":    "No monthly usage records returned for the given period.",
            "subaccount_count": 0,
            "subaccounts":      [],
        }, ensure_ascii=False)

    # Extract global account info from the first record that has it
    global_account_id   = ""
    global_account_name = ""
    for r in records:
        if r.get("globalAccountId"):
            global_account_id   = r["globalAccountId"]
            global_account_name = r.get("globalAccountName", "")
            break

    # Aggregate per subaccount
    sa_map: dict[str, dict] = {}
    for r in records:
        sa_id    = r.get("subaccountId")   or ""
        sa_name  = r.get("subaccountName") or ""
        dir_id   = r.get("directoryId")    or ""
        dir_name = r.get("directoryName")  or ""

        if not sa_id:
            # Skip records that belong to the global account aggregate (no subaccountId)
            continue

        if sa_id not in sa_map:
            sa_map[sa_id] = {
                "subaccount_id":   sa_id,
                "subaccount_name": sa_name,
                "directory_id":    dir_id,
                "directory_name":  dir_name,
                "services":        set(),
                "months":          set(),
            }
        # Update name in case earlier records had it blank
        if sa_name and not sa_map[sa_id]["subaccount_name"]:
            sa_map[sa_id]["subaccount_name"] = sa_name
        if dir_id and not sa_map[sa_id]["directory_id"]:
            sa_map[sa_id]["directory_id"]    = dir_id
        if dir_name and not sa_map[sa_id]["directory_name"]:
            sa_map[sa_id]["directory_name"]  = dir_name

        service = r.get("serviceId") or r.get("serviceName") or ""
        month   = str(r.get("reportYearMonth", ""))
        if service:
            sa_map[sa_id]["services"].add(service)
        if month:
            sa_map[sa_id]["months"].add(month)

    # Serialise (sets → counts)
    subaccounts = sorted(
        [
            {
                "subaccount_id":   v["subaccount_id"],
                "subaccount_name": v["subaccount_name"],
                "directory_id":    v["directory_id"],
                "directory_name":  v["directory_name"],
                "service_count":   len(v["services"]),
                "month_count":     len(v["months"]),
            }
            for v in sa_map.values()
        ],
        key=lambda x: x["subaccount_name"].lower() or x["subaccount_id"],
    )

    return json.dumps({
        "global_account_id":   global_account_id,
        "global_account_name": global_account_name,
        "from_month":          from_month,
        "to_month":            to_month,
        "subaccount_count":    len(subaccounts),
        "subaccounts":         subaccounts,
    }, ensure_ascii=False)


@tool
async def get_global_account_monthly_usage(
    from_month: str,
    to_month: str,
    service_filter: Optional[str] = "all",
    group_by: Optional[str] = "service",
) -> str:
    """
    Query the BTP global account monthly usage report (/reports/v1/monthlyUsage).

    Returns usage data aggregated across ALL subaccounts under the global
    account for one or more calendar months.  Use this tool for global-account-
    level capacity planning, cost trending, or cross-subaccount comparisons.

    For per-subaccount DAILY detail use get_btp_usage instead (after
    discovering available subaccount IDs with list_subaccounts).

    Args:
        from_month:    Start month, YYYY-MM (e.g. "2026-04").
        to_month:      End month, YYYY-MM (e.g. "2026-06").
        service_filter: Filter results.  One of:
            "all"         → all services (default)
            "hana"        → SAP HANA Cloud only
            "aicore"      → SAP AI Core only
            "cf"          → Cloud Foundry Runtime only
            "integration" → SAP Integration Suite only
            "key"         → the 4 key services above combined
        group_by:      Primary grouping dimension.  One of:
            "service"     → aggregate by service + metric (default)
            "month"       → aggregate by month, then service
            "subaccount"  → aggregate by subaccount, then service
            "directory"   → aggregate by directory (null → "root"), then service

    Returns:
        JSON with:
          global_account_id / global_account_name
          from_month / to_month / service_filter / group_by
          total_records
          monthly_totals        — [{month, total_usage_by_metric}] trend view
          grouped_data          — main result shaped by group_by
          raw_summary           — flat list sorted by total_usage desc
    """
    # ── Validate inputs ───────────────────────────────────────────────────────
    from_month = _validate_month(from_month, "from_month")
    to_month   = _validate_month(to_month,   "to_month")
    if from_month > to_month:
        logger.warning("from_month %s > to_month %s — swapping.", from_month, to_month)
        from_month, to_month = to_month, from_month

    filt = (service_filter or "all").lower()
    grp  = (group_by or "service").lower()
    if grp not in ("service", "month", "subaccount", "directory"):
        logger.warning("Unknown group_by=%r — defaulting to 'service'.", grp)
        grp = "service"

    # ── Fetch ─────────────────────────────────────────────────────────────────
    try:
        records = await _fetch_monthly_usage(from_month, to_month)
    except Exception as exc:
        return json.dumps({"error": str(exc), "from_month": from_month, "to_month": to_month})

    if not records:
        return json.dumps({
            "from_month": from_month,
            "to_month":   to_month,
            "message":    "No monthly usage records returned.",
            "total_records": 0,
            "monthly_totals": [],
            "grouped_data":   [],
            "raw_summary":    [],
        }, ensure_ascii=False)

    # Global account metadata from first populated record
    global_account_id   = ""
    global_account_name = ""
    for r in records:
        if r.get("globalAccountId"):
            global_account_id   = r["globalAccountId"]
            global_account_name = r.get("globalAccountName", "")
            break

    # ── Apply service filter ──────────────────────────────────────────────────
    if filt == "key":
        records = [r for r in records if _classify(r.get("serviceId", "")) != "other"]
    elif filt != "all":
        records = [r for r in records if _classify(r.get("serviceId", "")) == filt]

    # ── Normalise records ─────────────────────────────────────────────────────
    def _to_ym(raw_month) -> str:
        """Convert API's YYYYMM integer/string → YYYY-MM display string."""
        s = str(raw_month).strip()
        if len(s) == 6 and s.isdigit():
            return f"{s[:4]}-{s[4:]}"
        return s or "unknown"

    normalised = [
        {
            "service_id":      r.get("serviceId", ""),
            "service_name":    r.get("serviceName") or r.get("serviceId", ""),
            "plan":            r.get("planName") or r.get("plan", ""),
            "metric_id":       r.get("measureId", ""),
            "metric_name":     r.get("metricName") or r.get("measureId", ""),
            "unit":            r.get("unitSingular") or r.get("unitPlural", ""),
            "usage":           float(r.get("usage", 0)),
            "month":           _to_ym(r.get("reportYearMonth", "")),
            "subaccount_id":   r.get("subaccountId")   or "",
            "subaccount_name": r.get("subaccountName") or "",
            "directory_id":    r.get("directoryId")    or "",
            "directory_name":  r.get("directoryName")  or "",
            "category":        r.get("categoryName", ""),
            "data_center":     r.get("dataCenter", ""),
            "group":           _classify(r.get("serviceId", "")),
        }
        for r in records
    ]

    # ── Monthly trend (always computed) ──────────────────────────────────────
    # month → metric_id → {unit, total}
    month_metric_agg: dict[str, dict[str, dict]] = {}
    for row in normalised:
        m   = row["month"]
        mid = row["metric_id"]
        month_metric_agg.setdefault(m, {})
        month_metric_agg[m].setdefault(mid, {"unit": row["unit"], "total": 0.0})
        month_metric_agg[m][mid]["total"] = round(
            month_metric_agg[m][mid]["total"] + row["usage"], 6
        )

    monthly_totals = sorted(
        [
            {
                "month": m,
                "metrics": sorted(
                    [
                        {"metric_id": mid, "unit": v["unit"], "total_usage": round(v["total"], 6)}
                        for mid, v in metrics.items()
                    ],
                    key=lambda x: -x["total_usage"],
                ),
            }
            for m, metrics in month_metric_agg.items()
        ],
        key=lambda x: x["month"],
    )

    # ── Flat summary: (service, plan, metric) → total ─────────────────────────
    flat_key_fn = lambda row: (
        row["service_name"], row["service_id"], row["plan"],
        row["metric_name"], row["metric_id"], row["unit"], row["group"],
    )
    flat_agg: dict[tuple, float] = {}
    for row in normalised:
        k = flat_key_fn(row)
        flat_agg[k] = round(flat_agg.get(k, 0.0) + row["usage"], 6)

    raw_summary = sorted(
        [
            {
                "service":     k[0],
                "service_id":  k[1],
                "plan":        k[2],
                "metric":      k[3],
                "metric_id":   k[4],
                "unit":        k[5],
                "group":       k[6],
                "total_usage": v,
            }
            for k, v in flat_agg.items()
        ],
        key=lambda x: -x["total_usage"],
    )

    # ── Primary group_by dimension ─────────────────────────────────────────────
    def _dim_key(row: dict) -> str:
        if grp == "month":
            return row["month"]
        if grp == "subaccount":
            return row["subaccount_id"] or "__global__"
        if grp == "directory":
            return row["directory_id"] or "__root__"
        return row["service_id"]   # "service" (default)

    def _dim_label(row: dict) -> str:
        if grp == "month":
            return row["month"]
        if grp == "subaccount":
            name = row["subaccount_name"]
            return f"{name} ({row['subaccount_id']})" if name else row["subaccount_id"] or "(global)"
        if grp == "directory":
            name = row["directory_name"]
            return f"{name} ({row['directory_id']})" if name else row["directory_id"] or "(root)"
        return row["service_name"] or row["service_id"]   # "service"

    # group_dim → secondary → metric_id → {unit, total}
    group_agg: dict[str, dict[str, dict[str, dict]]] = {}
    group_labels: dict[str, str] = {}
    for row in normalised:
        dim   = _dim_key(row)
        label = _dim_label(row)
        group_labels[dim] = label

        sec = row["service_name"] or row["service_id"] if grp == "month" else row["month"]
        mid = row["metric_id"]
        group_agg.setdefault(dim, {})
        group_agg[dim].setdefault(sec, {})
        group_agg[dim][sec].setdefault(mid, {"unit": row["unit"], "total": 0.0})
        group_agg[dim][sec][mid]["total"] = round(
            group_agg[dim][sec][mid]["total"] + row["usage"], 6
        )

    def _sec_label() -> str:
        return "month" if grp != "month" else "service"

    grouped_data = sorted(
        [
            {
                "dimension": dim,
                "label":     group_labels[dim],
                "breakdown": sorted(
                    [
                        {
                            _sec_label(): sec,
                            "metrics": sorted(
                                [
                                    {"metric_id": mid, "unit": v["unit"],
                                     "total_usage": round(v["total"], 6)}
                                    for mid, v in metrics.items()
                                ],
                                key=lambda x: -x["total_usage"],
                            ),
                        }
                        for sec, metrics in sec_map.items()
                    ],
                    key=lambda x: x[_sec_label()],
                ),
            }
            for dim, sec_map in group_agg.items()
        ],
        key=lambda x: x["dimension"],
    )

    return json.dumps({
        "global_account_id":   global_account_id,
        "global_account_name": global_account_name,
        "from_month":          from_month,
        "to_month":            to_month,
        "service_filter":      filt,
        "group_by":            grp,
        "total_records":       len(normalised),
        "monthly_totals":      monthly_totals,
        "grouped_data":        grouped_data,
        "raw_summary":         raw_summary,
    }, ensure_ascii=False)


@tool
async def check_quota_status(
    contract_cu: float,
    contract_start: str,
    contract_end: str,
    reference_date: Optional[str] = None,
) -> str:
    """
    Check whether AI Core CU consumption is on track against an annual contract quota.

    Answers the core business question:
      "Will we exceed our contracted CU limit by year end?"

    Uses three checks:
      1. This month  — used so far vs monthly target (contract_cu / 12)
      2. Cumulative  — total used since contract start vs proportional budget
      3. Year-end    — projected annual spend vs contract limit

    Args:
        contract_cu:    Total CU purchased in the contract (e.g. 100000)
        contract_start: Contract start date in YYYY-MM-DD format (e.g. "2026-01-01")
        contract_end:   Contract end date in YYYY-MM-DD format (e.g. "2026-12-31")
        reference_date: Treat this as today (YYYY-MM-DD). Defaults to today UTC.

    Returns:
        JSON with:
          contract          — contract_cu, contract_start, contract_end, monthly_target
          this_month        — used, target, pct_used, days_elapsed, days_remaining, status
          cumulative        — allowed, used, delta, pct_used, months_elapsed, status
          projection        — avg_monthly_spend, projected_annual, buffer, will_exceed,
                              estimated_breach_date (if at risk), status
          verdict           — SAFE / AT_RISK / WILL_EXCEED
          summary_text      — human-readable one-paragraph summary
    """
    from calendar import monthrange

    # ── 1. Resolve reference date ────────────────────────────────────────────
    today_utc = datetime.now(tz=timezone.utc).date()
    if reference_date:
        try:
            ref = datetime.strptime(reference_date, "%Y-%m-%d").date()
            if ref > today_utc:
                ref = today_utc
        except ValueError:
            ref = today_utc
    else:
        ref = today_utc

    # ── 2. Parse contract dates ──────────────────────────────────────────────
    try:
        c_start = datetime.strptime(contract_start, "%Y-%m-%d").date()
        c_end   = datetime.strptime(contract_end,   "%Y-%m-%d").date()
    except ValueError as exc:
        return json.dumps({"error": f"Invalid contract date format: {exc}"})

    if c_start >= c_end:
        return json.dumps({"error": "contract_start must be before contract_end"})

    # Clamp reference date to contract window
    ref = max(c_start, min(ref, c_end))

    # ── 3. Contract constants ────────────────────────────────────────────────
    contract_days   = (c_end - c_start).days + 1
    monthly_target  = round(contract_cu / 12, 2)

    # ── 4. Fetch AI Core CU since contract start ─────────────────────────────
    fetch_from = contract_start
    fetch_to   = ref.strftime("%Y-%m-%d")

    try:
        records = await _fetch_usage(fetch_from, fetch_to)
    except Exception as exc:
        return json.dumps({"error": str(exc)})

    cu_records = [
        r for r in records
        if r.get("serviceId", "").lower() == "ai-core"
        and r.get("measureId", "").lower() == "capacity_units"
    ]

    # ── 5. Aggregate: total used + per-month + current month ─────────────────
    month_totals: dict[str, float] = {}
    for r in cu_records:
        d  = r.get("startIsoDate", "")
        cu = float(r.get("usage", 0))
        if d:
            ym = d[:7]  # YYYY-MM
            month_totals[ym] = round(month_totals.get(ym, 0.0) + cu, 6)

    total_used = round(sum(month_totals.values()), 6)

    # Current month stats
    current_ym          = ref.strftime("%Y-%m")
    current_month_used  = round(month_totals.get(current_ym, 0.0), 6)
    days_in_month       = monthrange(ref.year, ref.month)[1]
    month_start         = ref.replace(day=1)
    days_elapsed_month  = (ref - month_start).days + 1
    days_remaining_month = days_in_month - days_elapsed_month
    month_pct           = round(current_month_used / monthly_target * 100, 1) if monthly_target > 0 else 0.0

    # ── 6. Cumulative budget check ───────────────────────────────────────────
    # How many months (including partial) have elapsed since contract start
    months_elapsed = (
        (ref.year - c_start.year) * 12 + (ref.month - c_start.month)
        + (days_elapsed_month / days_in_month)
    )
    months_elapsed = round(months_elapsed, 2)

    cumulative_allowed = round(monthly_target * months_elapsed, 2)
    cumulative_delta   = round(total_used - cumulative_allowed, 2)  # positive = overspent
    cumulative_pct     = round(total_used / cumulative_allowed * 100, 1) if cumulative_allowed > 0 else 0.0

    if cumulative_delta > monthly_target * 0.5:
        cumulative_status = "BEHIND"   # overspent by more than half a month's budget
    elif cumulative_delta < -monthly_target * 0.5:
        cumulative_status = "AHEAD"    # underspent by more than half a month's budget
    else:
        cumulative_status = "ON_TRACK"

    # ── 7. Year-end projection ───────────────────────────────────────────────
    days_elapsed_contract  = (ref - c_start).days + 1
    days_remaining_contract = (c_end - ref).days

    if days_elapsed_contract > 0:
        daily_burn_rate    = total_used / days_elapsed_contract
        projected_annual   = round(daily_burn_rate * contract_days, 2)
        avg_monthly_spend  = round(total_used / months_elapsed, 2) if months_elapsed > 0 else 0.0
        buffer             = round(contract_cu - projected_annual, 2)
    else:
        daily_burn_rate   = 0.0
        projected_annual  = 0.0
        avg_monthly_spend = 0.0
        buffer            = contract_cu

    # Estimated breach date (if projected to exceed)
    estimated_breach_date = None
    if daily_burn_rate > 0:
        cu_remaining = contract_cu - total_used
        if cu_remaining > 0:
            days_to_breach = cu_remaining / daily_burn_rate
            breach = ref + timedelta(days=days_to_breach)
            if breach <= c_end:
                estimated_breach_date = breach.strftime("%Y-%m-%d")
        else:
            estimated_breach_date = ref.strftime("%Y-%m-%d")  # already exceeded

    # ── 8. Verdict ───────────────────────────────────────────────────────────
    if projected_annual > contract_cu:
        verdict = "WILL_EXCEED"
    elif projected_annual > contract_cu * 0.90:
        verdict = "AT_RISK"     # within 10% of limit
    else:
        verdict = "SAFE"

    # Month-level status
    if month_pct > 100:
        month_status = "OVER"
    elif month_pct > 85:
        month_status = "AT_RISK"
    else:
        month_status = "ON_TRACK"

    # Projection status
    if verdict == "WILL_EXCEED":
        projection_status = "WILL_EXCEED"
    elif verdict == "AT_RISK":
        projection_status = "AT_RISK"
    else:
        projection_status = "SAFE"

    # ── 9. Human-readable summary ─────────────────────────────────────────────
    verdict_emoji = {"SAFE": "✓", "AT_RISK": "⚠️", "WILL_EXCEED": "⚠️"}[verdict]
    summary_lines = [
        f"{verdict_emoji} {verdict} — Annual contract: {contract_cu:,.0f} CU",
        f"",
        f"This month ({current_ym}):",
        f"  Used: {current_month_used:,.1f} CU of {monthly_target:,.1f} CU target "
        f"({month_pct}%) — {days_remaining_month} days remaining — {month_status}",
        f"",
        f"Cumulative ({contract_start} → {fetch_to}):",
        f"  Budget allowed: {cumulative_allowed:,.1f} CU ({months_elapsed:.1f} months × {monthly_target:,.1f})",
        f"  Actually used:  {total_used:,.1f} CU",
        f"  {'Overspent' if cumulative_delta > 0 else 'Surplus'}: {abs(cumulative_delta):,.1f} CU — {cumulative_status}",
        f"",
        f"Year-end projection:",
        f"  Avg monthly spend: {avg_monthly_spend:,.1f} CU",
        f"  Projected annual:  {projected_annual:,.1f} CU",
        f"  vs Contract:       {contract_cu:,.0f} CU",
        f"  Buffer remaining:  {buffer:,.1f} CU" if buffer >= 0 else f"  Exceeds by:        {abs(buffer):,.1f} CU",
        f"  Status: {projection_status}",
    ]
    if estimated_breach_date:
        summary_lines.append(f"  Estimated breach date: {estimated_breach_date}")

    summary_text = "\n".join(summary_lines)

    return json.dumps({
        "contract": {
            "contract_cu":     contract_cu,
            "contract_start":  contract_start,
            "contract_end":    contract_end,
            "monthly_target":  monthly_target,
        },
        "this_month": {
            "month":              current_ym,
            "used":               current_month_used,
            "target":             monthly_target,
            "pct_used":           month_pct,
            "days_elapsed":       days_elapsed_month,
            "days_remaining":     days_remaining_month,
            "status":             month_status,
        },
        "cumulative": {
            "months_elapsed":   months_elapsed,
            "allowed":          cumulative_allowed,
            "used":             total_used,
            "delta":            cumulative_delta,
            "pct_used":         cumulative_pct,
            "status":           cumulative_status,
        },
        "projection": {
            "avg_monthly_spend":      avg_monthly_spend,
            "daily_burn_rate":        round(daily_burn_rate, 4),
            "projected_annual":       projected_annual,
            "buffer":                 buffer,
            "will_exceed":            projected_annual > contract_cu,
            "estimated_breach_date":  estimated_breach_date,
            "status":                 projection_status,
        },
        "verdict":      verdict,
        "summary_text": summary_text,
    }, ensure_ascii=False)
