# Project Memory & Coding Rules

Rules and lessons learned for this project. Consult this file before writing or changing any code.

---

## 1. Always verify API parameters from the spec before writing code

Before implementing any API call (or accepting inherited code that makes one), look up the actual
API spec via the SAP API Hub (`ibd-mcp-server__sap_knowledge_graph_api_discovery`) to confirm:
- The exact query parameter names and whether they are required or optional
- The date/ID format each parameter expects (e.g. YYYYMM vs YYYYMMDD)
- Which parameters are implicit from OAuth2 credentials vs must be passed explicitly

**Lesson:** `/reports/v1/monthlyUsage` has NO `globalAccountId` parameter. The global account
scope is implicit from the OAuth2 client credentials. We had a stale `globalAccountId` param in
`_fetch_monthly_usage()` that would either be silently ignored or cause a 400 — caught only after
spec verification.

---

## 2. Read raw API response fields and sample data before mapping them

Before writing field-mapping / normalisation code (e.g. building `rows = [...]` dicts), fetch or
review real sample data for that endpoint. Specifically:
- Confirm the exact field names returned (camelCase, snake_case, abbreviations)
- Check which fields are nullable / sometimes absent
- Sample data is in `btp-usage-agent/sample_data/`; add new samples there whenever a new
  endpoint is integrated

**Lesson:** Several field names in the UAS response (e.g. `application` for AI model name,
`startIsoDate` vs `periodStartDate`) were only confirmed by inspecting live responses. Guessing
field names leads to silent `None` values in output.

---

## 3. Copy all production code into the Docker image

Every Python file under `btp-usage-agent/app/` must be present in the Docker image at build time.
Check the `Dockerfile` `COPY` directive whenever a new module is added or an existing one is
removed. A file that exists locally but is not copied will cause an `ImportError` only at runtime
inside the container.

**Lesson:** When `monthly_usage_tool.py` was consolidated into `uas_tool.py`, the `Dockerfile`
`COPY` line had to be verified to ensure no stale references remained and no new files were
accidentally excluded.

---

## 4. Keep the agent system prompt in sync with available tools

`agent.py → get_system_prompt()` contains the authoritative list of tools the LLM knows about.
Whenever a tool is added, removed, or renamed in `uas_tool.py` / `mcp_tools.py`, update the
`## Available Tools` section in the system prompt in the same change. An undocumented tool will
never be called by the LLM even if it is registered.

**Lesson:** `list_subaccounts` and `get_global_account_monthly_usage` were added to `mcp_tools.py`
but were missing from the system prompt until caught in review.

---

## 5. Single-module rule for same-API tools

All tools that call the same base API (same `BTP_UAS_URL`, same OAuth2 credentials) live in a
single module (`uas_tool.py`). Do NOT create a separate `*_tool.py` per endpoint. Shared helpers
(`_get_token`, `_SERVICE_GROUPS`, `_classify`, `_HTTP_TIMEOUT_SECONDS`) are defined once.

---

## API parameter reference (verified against SAP API Hub spec)

| Endpoint | Required params | Format | Notes |
|---|---|---|---|
| `/reports/v1/subaccountUsage` | `subaccountId`, `fromDate`, `toDate` | YYYYMMDD | `periodPerspective` optional (DAY/WEEK/MONTH) |
| `/reports/v1/monthlyUsage` | `fromDate`, `toDate` | YYYYMM | No `globalAccountId` — scope is implicit from OAuth2 creds |

## Environment variables

| Variable | Used by | Notes |
|---|---|---|
| `BTP_UAS_URL` | all UAS tools | defaults to `https://uas-reporting.cfapps.eu10.hana.ondemand.com` |
| `BTP_AUTH_URL` | `_get_token()` | XSUAA token endpoint |
| `BTP_CLIENT_ID` | `_get_token()` | OAuth2 client ID |
| `BTP_CLIENT_SECRET` | `_get_token()` | OAuth2 client secret |
| `BTP_SUBACCOUNT_ID` | `_fetch_usage_single()` | required for subaccountUsage tools |
| `BTP_GLOBAL_ACCOUNT_ID` | reserved | NOT sent to any current endpoint; kept for future use (e.g. `/reports/v1/monthlyDirectoryUsage`) |
