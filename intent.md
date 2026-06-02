# Intent: BTP Usage Agent

## What Are We Building?

An AI-powered assistant for **SAP BTP administrators** that enables them to query, analyze, and explore daily subaccount usage data across key SAP BTP services. The agent is deployed as an A2A-protocol-compatible Python service and integrates with the SAP UAS Reporting API via OAuth2/XSUAA.

---

## Problem Statement

BTP administrators need visibility into service consumption (compute, storage, tokens, messages) across multiple cloud services within their subaccount. Manually querying the SAP UAS Reporting API is tedious and requires technical knowledge. An AI agent that understands natural-language questions and automatically handles date parsing, service filtering, authentication, and result formatting dramatically reduces the effort required.

---

## Target Users

- **BTP Platform Administrators** who monitor resource usage and cost
- **Cloud Operations teams** responsible for SAP HANA Cloud, AI Core, Cloud Foundry Runtime, and Integration Suite

---

## Core Capabilities

| Capability | Description |
|------------|-------------|
| Usage query | Ask about usage for any date range (today, last week, this month, etc.) |
| Service filtering | Focus on a specific service: HANA, AI Core, Cloud Foundry, Integration Suite |
| Usage summary | Get aggregated group totals sorted by consumption |
| Date intelligence | Understands relative dates; guards against LLM training-data date bias |
| Bilingual support | Responds in English or Chinese depending on the user's language |
| Conversation memory | Maintains thread context with 1-hour TTL eviction |

---

## Key Services Monitored

| Service | SAP Service ID | Key Metrics |
|---------|---------------|-------------|
| SAP HANA Cloud | `hana-cloud` | Storage (GB-hours), Compute (vCPU-hours, Memory GB-hours), Backup |
| SAP AI Core | `aicore` | Token consumption (input/output) |
| Cloud Foundry Runtime | `linux-container` | Memory (GB-hours) |
| SAP Integration Suite | `IntegrationSuite` | Tenant instances, message count |

---

## Technical Architecture

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

### Technology Stack
- **Runtime**: Python 3.x, FastAPI/Starlette, Uvicorn
- **Agent framework**: LangGraph with `InMemorySaver` checkpointer
- **LLM**: LiteLLM (`ChatLiteLLM`) with configurable model (default: `sap/gpt-4o`)
- **Protocol**: A2A SDK v0.3.22 (Agent-to-Agent protocol)
- **SAP SDK**: `sap-cloud-sdk` for decorators, OTel auto-instrumentation
- **HTTP client**: `httpx` (async)
- **Auth**: OAuth2 client credentials via XSUAA with in-memory token cache

---

## Agent Tools

### `get_btp_usage`
- Queries `/reports/v1/subaccountUsage` for a date range
- Accepts optional `service_filter`: `all | hana | aicore | cf | integration | key`
- Returns normalized JSON records: service, plan, metric, usage, unit, date, dataCenter, space
- Validates and auto-corrects dates (guards against LLM bias toward 2023/2024 dates)

### `get_btp_services_summary`
- Aggregates all usage records by (service, metric, unit) group
- Returns sorted group totals + per-service metric breakdown
- Useful for high-level "what used the most?" questions

---

## Configuration

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
| `AGENT_PUBLIC_URL` | `http://{host}:{port}/` | Public URL reported in the AgentCard |

---

## Data Flow Examples

**Usage Query:**
```
User: "Show HANA usage last week"
→ parse intent → call get_btp_usage(from_date, to_date, service_filter="hana")
→ OAuth2 token fetch → GET /reports/v1/subaccountUsage
→ normalize records → LLM formats as markdown table → A2A artifact response
```

**Summary Query:**
```
User: "What services used the most resources this month?"
→ call get_btp_services_summary(from_date, to_date)
→ aggregate by (service, metric, unit) → sort by total_usage desc
→ LLM formats top services with highlights
```

---

## Current Implementation Status

The agent core is **fully implemented** and functional. The following areas require completion:

### Remaining Work
- Unit tests for `SampleAgent.invoke()`, thread TTL eviction, `_classify()`, `get_btp_usage`, `get_btp_services_summary`
- HTTP error handling (4xx/5xx) with descriptive messages from UAS API
- Pagination support if UAS API returns paged responses
- Startup validation for required environment variables
- Health check endpoint (`GET /health`)
- README: Prerequisites, Setup, Running Locally, Testing, Deploying to BTP sections
- Deployment artifacts: `manifest.yml` or Cloud Foundry `cf push` configuration
- `solution.yaml` configuration for automated deployment via `deploy-solution` skill
- Pin `sap-cloud-sdk` to a specific version in `requirements.txt`

---

## Acceptance Criteria

| # | Criteria |
|---|----------|
| 1 | Agent starts and serves `/.well-known/agent-card.json` with valid JSON |
| 2 | `get_btp_usage` returns records correctly filtered by service type |
| 3 | `get_btp_services_summary` returns sorted group totals |
| 4 | OAuth2 token is cached and reused within expiry window |
| 5 | Agent responds in Chinese when queried in Chinese |
| 6 | Agent handles missing env vars with a clear error at startup |
| 7 | Thread TTL eviction removes inactive sessions after 1 hour |
| 8 | All required env vars documented in README |
| 9 | Code coverage ≥ 80% |
| 10 | Deployment artifacts (Dockerfile or manifest.yml) present |
