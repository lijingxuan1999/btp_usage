# Product Requirements Document
## BTP Usage Agent

**Version:** 1.0.0
**Status:** Draft
**Owner:** BTP Platform Operations

---

## 1. Executive Summary

The **BTP Usage Agent** is an AI-powered assistant that enables SAP BTP administrators to query, analyze, and explore daily subaccount usage data across key SAP BTP services using natural language. Deployed as an A2A-protocol-compatible Python service, it integrates with the SAP UAS Reporting API via OAuth2/XSUAA and removes the need for manual API queries.

---

## 2. Problem Statement

BTP administrators need real-time visibility into service consumption (compute, storage, tokens, messages) across multiple cloud services within their subaccount. Manually querying the SAP UAS Reporting API is tedious, requires deep technical knowledge, and is error-prone. An AI agent that understands natural-language queries and automatically handles date parsing, service filtering, authentication, and result formatting dramatically reduces the effort required.

**Pain Points:**
- Manual REST API calls require knowing exact service IDs, date formats, and OAuth2 token flows
- No consolidated view of "what used the most resources this month?"
- Non-technical stakeholders cannot self-serve usage data
- Repetitive queries for daily/weekly reports waste engineering time

---

## 3. Target Users

| Persona | Role | Primary Need |
|---------|------|--------------|
| BTP Platform Administrator | Monitors BTP resource usage and costs | Daily usage reports, anomaly detection |
| Cloud Operations Engineer | Operates SAP HANA Cloud, AI Core, CF Runtime, Integration Suite | Service-specific consumption breakdown |
| Finance/Procurement Stakeholder | Tracks cloud spend | Monthly usage summaries in plain language |

---

## 4. Goals & Success Metrics

### Goals
1. Enable natural-language queries for BTP subaccount usage data
2. Reduce time-to-insight for usage questions from minutes to seconds
3. Support both English and Chinese-speaking administrators
4. Provide a production-ready, deployable agent with full test coverage

### Success Metrics
| Metric | Target |
|--------|--------|
| Query response latency (P95) | < 5 seconds end-to-end |
| Unit test code coverage | ≥ 80% |
| Supported languages | English + Chinese |
| Successful OAuth2 token reuse rate | 100% within expiry window |
| Agent card served at `/.well-known/agent-card.json` | Always valid JSON |

---

## 5. Scope

### In Scope
- Natural-language query interface for BTP subaccount usage via A2A protocol
- OAuth2/XSUAA authentication with token caching
- Support for SAP HANA Cloud, SAP AI Core, Cloud Foundry Runtime, SAP Integration Suite
- Date intelligence (relative dates: "last week", "this month", "yesterday")
- Aggregated usage summaries sorted by consumption
- Bilingual responses (English / Chinese)
- Conversation memory with 1-hour TTL eviction
- Unit and integration test suite (≥ 80% coverage)
- Cloud Foundry deployment artifacts (manifest.yml or Dockerfile)
- README documentation (setup, running locally, testing, deploying)

### Out of Scope
- Multi-subaccount aggregation
- Real-time alerting or threshold-based notifications
- Cost/billing calculation (usage data only, no pricing applied)
- SAP BTP Cockpit UI integration
- Support for services not listed in Section 7

---

## 6. Functional Requirements

### FR-1: Natural Language Usage Query
- **Description:** Users can ask questions about BTP service usage in plain English or Chinese
- **Acceptance:** Agent correctly interprets date ranges (relative and absolute) and service names
- **Example:** "Show HANA usage last week" → returns filtered usage records for `hana-cloud` for the 7-day window ending yesterday

### FR-2: Service Filtering
- **Description:** Agent supports filtering usage records by service type
- **Supported Filters:** `all`, `hana`, `aicore`, `cf`, `integration`, `key`
- **Acceptance:** Records returned match only the requested service category

### FR-3: Usage Summary
- **Description:** Agent can aggregate all usage by (service, metric, unit) group and return group totals sorted descending by consumption
- **Acceptance:** `get_btp_services_summary` returns sorted list of top consumers

### FR-4: Date Intelligence
- **Description:** Agent understands relative date expressions and converts them to `YYYY-MM-DD` format for the UAS API
- **Date Guards:** Rejects future dates; corrects LLM bias toward stale training data dates
- **Acceptance:** "This month" correctly resolves to the first day of the current month through yesterday

### FR-5: Bilingual Support
- **Description:** Agent detects the user's input language and responds in the same language (EN or ZH)
- **Acceptance:** A query in Chinese receives a Chinese-language markdown table response

### FR-6: Conversation Memory
- **Description:** Agent maintains per-thread conversation context using `InMemorySaver`
- **TTL:** Inactive threads are evicted after 1 hour
- **Acceptance:** Thread state is available for follow-up questions within 1 hour; evicted after inactivity

### FR-7: Health Check Endpoint
- **Description:** `GET /health` returns `{"status": "ok"}` with HTTP 200
- **Acceptance:** Used by Cloud Foundry or Kubernetes health probes

### FR-8: Startup Validation
- **Description:** Agent validates all required environment variables at startup and logs a clear, actionable error if any are missing
- **Required vars:** `BTP_UAS_URL`, `BTP_AUTH_URL`, `BTP_CLIENT_ID`, `BTP_CLIENT_SECRET`, `BTP_SUBACCOUNT_ID`
- **Acceptance:** Missing env var produces a startup error with the variable name; process exits non-zero

### FR-9: Agent Card
- **Description:** Agent serves a valid A2A `AgentCard` at `/.well-known/agent-card.json`
- **Acceptance:** JSON is parseable and includes name, description, URL, version, capabilities, and skills

### FR-10: OAuth2 Token Caching
- **Description:** XSUAA tokens are cached in memory with a 30-second expiry buffer; only fetched when expired
- **Acceptance:** Multiple tool calls within one token lifetime do not re-fetch the token

---

## 7. Key Services & Metrics

| Service | SAP Service ID | Key Metrics Monitored |
|---------|---------------|-----------------------|
| SAP HANA Cloud | `hana-cloud` | Storage (GB-hours), Compute (vCPU-hours, Memory GB-hours), Backup |
| SAP AI Core | `aicore` | Token consumption (input/output) |
| Cloud Foundry Runtime | `linux-container` | Memory (GB-hours) |
| SAP Integration Suite | `IntegrationSuite` | Tenant instances, message count |

---

## 8. Non-Functional Requirements

### NFR-1: Performance
- Agent responds within 5 seconds (P95) for typical single-service date-range queries

### NFR-2: Reliability
- OAuth2 token refresh must not cause request failures; errors fall back gracefully
- HTTP 4xx/5xx from UAS API return a human-readable error message to the user

### NFR-3: Security
- Client credentials (`BTP_CLIENT_ID`, `BTP_CLIENT_SECRET`) are never logged or exposed
- `.env` file is excluded from version control via `.gitignore`

### NFR-4: Maintainability
- All dependencies are pinned to specific versions in `requirements.txt` for reproducible builds
- `sap-cloud-sdk` must be pinned (currently `>=0.17.0` — to be locked to a specific patch version)

### NFR-5: Testability
- Code coverage ≥ 80% measured via `pytest-cov`
- Tests use `respx` for mocking `httpx` HTTP calls

### NFR-6: Deployability
- Agent must deploy to SAP BTP Cloud Foundry via `manifest.yml` or Docker
- `AGENT_PUBLIC_URL` must be set to the deployed CF app URL in production

---

## 9. Technical Architecture

### Technology Stack

| Layer | Technology |
|-------|------------|
| Runtime | Python 3.x |
| Web framework | FastAPI / Starlette + Uvicorn |
| Agent framework | LangGraph with `InMemorySaver` |
| LLM client | LiteLLM (`ChatLiteLLM`), default model: `sap/gpt-4o` |
| Protocol | A2A SDK v0.3.22 |
| HTTP client | `httpx` (async) |
| Auth | OAuth2 client credentials via XSUAA |
| Observability | OpenTelemetry auto-instrumentation via `sap-cloud-sdk` |
| SAP SDK | `sap-cloud-sdk` (decorators, OTel) |

### Architecture Diagram

```
┌────────────────────────────────────────────────────────┐
│                  A2A Starlette Server                  │
│  main.py → AgentCard + AgentSkill registration         │
│  AgentExecutor ← agent_executor.py                     │
│    └─ SampleAgent (LangGraph) ← agent.py               │
│         └─ UAS Tools ← uas_tool.py / mcp_tools.py      │
│              └─ SAP UAS Reporting API (OAuth2/XSUAA)   │
└────────────────────────────────────────────────────────┘
```

### Agent Tools

#### `get_btp_usage`
- Endpoint: `GET /reports/v1/subaccountUsage`
- Parameters: `from_date`, `to_date`, `service_filter` (`all|hana|aicore|cf|integration|key`)
- Output: Normalized JSON records with fields: service, serviceId, plan, metric, measureId, usage, unit, date, category, dataCenter, space

#### `get_btp_services_summary`
- Aggregates all records by `(service, metric, unit)` group
- Output: Sorted group totals + per-service metric breakdown (descending by `total_usage`)

---

## 10. Configuration

### Required Environment Variables

| Variable | Description |
|----------|-------------|
| `BTP_UAS_URL` | UAS Reporting API base URL (e.g. `https://uas-reporting.cfapps.eu10.hana.ondemand.com`) |
| `BTP_AUTH_URL` | XSUAA token endpoint |
| `BTP_CLIENT_ID` | OAuth2 client ID |
| `BTP_CLIENT_SECRET` | OAuth2 client secret |
| `BTP_SUBACCOUNT_ID` | Target BTP subaccount ID |

### Optional Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `0.0.0.0` | Server bind address |
| `PORT` | `8080` | Server port |
| `AGENT_PUBLIC_URL` | `http://{host}:{port}/` | Public URL in AgentCard |

---

## 11. Data Flows

### Usage Query Flow
```
User: "Show HANA usage last week"
  → agent.py: parse intent → resolve dates → call get_btp_usage(from_date, to_date, service_filter="hana")
  → uas_tool.py: OAuth2 token fetch (cached) → GET /reports/v1/subaccountUsage
  → normalize records → return JSON
  → LLM formats as markdown table
  → A2A artifact response returned to user
```

### Summary Flow
```
User: "What services used the most resources this month?"
  → agent.py: call get_btp_services_summary(from_date, to_date)
  → uas_tool.py: fetch all records → aggregate by (service, metric, unit)
  → return group_summary + detail sorted by total_usage desc
  → LLM formats top services with highlights
  → A2A artifact response returned to user
```

---

## 12. Acceptance Criteria

| # | Criteria | Priority |
|---|----------|----------|
| AC-1 | Agent starts and serves `/.well-known/agent-card.json` with valid JSON | Must Have |
| AC-2 | `get_btp_usage` returns records correctly filtered by service type | Must Have |
| AC-3 | `get_btp_services_summary` returns sorted group totals | Must Have |
| AC-4 | OAuth2 token is cached and reused within expiry window | Must Have |
| AC-5 | Agent responds in Chinese when queried in Chinese | Must Have |
| AC-6 | Agent handles missing env vars with a clear error at startup | Must Have |
| AC-7 | Thread TTL eviction removes inactive sessions after 1 hour | Should Have |
| AC-8 | All required env vars documented in README | Must Have |
| AC-9 | Code coverage ≥ 80% | Should Have |
| AC-10 | Deployment artifacts (Dockerfile or manifest.yml) present | Must Have |
| AC-11 | `GET /health` returns `{"status": "ok"}` with HTTP 200 | Should Have |
| AC-12 | HTTP 4xx/5xx from UAS API returns human-readable error message | Should Have |
| AC-13 | All dependencies pinned to specific versions | Should Have |

---

## 13. Open Items / Decisions Required

| # | Item | Owner | Status |
|---|------|-------|--------|
| OI-1 | Pin `sap-cloud-sdk` to a specific version | Engineering | Open |
| OI-2 | Choose deployment method: `manifest.yml` (CF push) or `Dockerfile` (container) | DevOps | Open |
| OI-3 | Confirm UAS API pagination behavior (single vs. paged responses) | Engineering | Open |
| OI-4 | Set `AGENT_PUBLIC_URL` for the production CF deployment | DevOps | Open |
| OI-5 | Define target BTP region and CF organization for deployment | Platform Admin | Open |

---

## 14. Out-of-Scope Items (Future Considerations)

- Multi-subaccount aggregation across global account
- Cost/billing calculation with pricing data overlay
- Threshold-based alerting and anomaly detection
- Slack / Microsoft Teams integration
- Dashboard UI (Fiori / React)
- Support for additional BTP services (e.g., Document Management, Workflow)
