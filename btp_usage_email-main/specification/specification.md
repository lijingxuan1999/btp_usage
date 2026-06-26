# BTP Usage Agent – Specification

## Project Overview

A Joule AI agent for BTP administrators to query and analyze daily subaccount usage across SAP HANA Cloud, SAP AI Core, Cloud Foundry Runtime, and SAP Integration Suite via the SAP UAS Reporting API. The agent runs as an A2A-protocol-compatible Python service using LangGraph, LiteLLM, and the SAP Cloud SDK.

---

## Asset: `btp-usage-agent`

### Architecture

```
┌────────────────────────────────────────────────────────┐
│                   A2A Starlette Server                 │
│  main.py → AgentCard + AgentSkill registration         │
│  AgentExecutor → agent_executor.py                     │
│    └─ SampleAgent (LangGraph) → agent.py               │
│         └─ UAS Tools → uas_tool.py / mcp_tools.py      │
│              └─ SAP UAS Reporting API (OAuth2/XSUAA)   │
└────────────────────────────────────────────────────────┘
```

### Directory Structure

```
btp-usage-agent/
├── app/
│   ├── __init__.py
│   ├── main.py              # A2A server entry point (click CLI + uvicorn)
│   ├── agent_executor.py    # A2A AgentExecutor adapter
│   ├── agent.py             # LangGraph SampleAgent with InMemorySaver
│   ├── mcp_tools.py         # Tool loader (wraps UAS tools)
│   ├── uas_tool.py          # get_btp_usage + get_btp_services_summary tools
│   └── util.py              # Shared utilities
├── prebuilt_tests/
│   ├── __init__.py
│   ├── test_server.py       # Server startup + A2A endpoint tests
│   └── test_structure.py    # Project structure validation tests
├── .coveragerc
├── .env                     # Local secrets (not committed)
├── .gitignore
├── conftest.py              # pytest fixtures (start_agent, etc.)
├── pytest.ini
├── README.md
├── requirements-test.txt
├── requirements.txt
└── test_local.py
```

---

## TODO Checklist

### 1. Agent Core (`app/agent.py`)

- [x] `SampleAgent` class with `stream()` and `invoke()` methods
- [x] `ChatLiteLLM` initialization with configurable model and temperature
- [x] `InMemorySaver` checkpointer for conversation history
- [x] Thread TTL eviction (1-hour inactivity timeout)
- [x] `SummarizationMiddleware` triggered at 100k tokens
- [x] `@agent_model`, `@agent_config`, `@prompt_section` decorators from `sap_cloud_sdk`
- [x] System prompt covers: date range parsing, key services table, response format (markdown tables), bilingual (EN/ZH) support
- [ ] **TODO**: Add unit tests for `SampleAgent.invoke()` with mock tools
- [ ] **TODO**: Add unit tests for thread TTL eviction logic
- [ ] **TODO**: Validate that `SummarizationMiddleware` trigger is tunable via `@agent_config`

### 2. UAS API Tool (`app/uas_tool.py`)

- [x] `get_btp_usage` LangChain `@tool` → queries `/reports/v1/subaccountUsage` with date range + optional service filter
- [x] `get_btp_services_summary` LangChain `@tool` → aggregates usage by service+metric, returns group totals
- [x] OAuth2 client-credentials token fetch from XSUAA with in-memory cache (30s expiry buffer)
- [x] Date helpers: `_to_uas_date`, `_last_n_days`, `_yesterday`
- [x] Service classification (`_classify`) for hana / aicore / cf / integration / other
- [x] `service_filter` supports: `all`, `hana`, `aicore`, `cf`, `integration`, `key`
- [x] Response normalized to LLM-friendly JSON with fields: service, serviceId, plan, metric, measureId, usage, unit, date, category, dataCenter, space
- [ ] **TODO**: Add unit tests for `_classify()` with edge-case serviceIds
- [ ] **TODO**: Add unit tests for `get_btp_usage` with mocked `httpx` responses
- [ ] **TODO**: Add unit tests for `get_btp_services_summary` aggregation logic
- [ ] **TODO**: Handle HTTP 4xx/5xx errors from UAS API with descriptive error messages
- [ ] **TODO**: Support pagination if UAS API returns paged results (check `content` vs root list)
- [ ] **TODO**: Add `fromDate` / `toDate` validation (reject future dates, invalid formats)

### 3. Tool Loader (`app/mcp_tools.py`)

- [x] `get_mcp_tools()` returns `[get_btp_usage, get_btp_services_summary]`
- [x] Compatible signature with standard `mcp_tools` contract (supports `use_cache` param)
- [ ] **TODO**: Add error handling if tool import fails at module load time

### 4. Agent Executor (`app/agent_executor.py`)

- [x] `AgentExecutor` extends A2A `A2AAgentExecutor`
- [x] `execute()` method: loads tools → streams agent → emits A2A task events
- [x] Graceful fallback: continues without tools if `get_mcp_tools()` raises
- [x] Task state machine: `working` → `input_required` | `completed`
- [x] Artifacts added on completion via `updater.add_artifact()`
- [x] `cancel()` raises `UnsupportedOperationError`
- [ ] **TODO**: Add integration test for `execute()` with a mock `RequestContext`
- [ ] **TODO**: Ensure `context_id` is propagated correctly to `SampleAgent.stream()`

### 5. Server Entry Point (`app/main.py`)

- [x] `AgentCard` registration: name, description, URL, version, capabilities, skills
- [x] `AgentSkill` with tags and natural-language examples (EN + ZH)
- [x] OpenTelemetry auto-instrumentation via `sap_cloud_sdk` (before framework imports)
- [x] `StarletteInstrumentor` applied to the built app
- [x] `HOST` / `PORT` configurable via environment variables
- [x] `AGENT_PUBLIC_URL` used for agent card URL (falls back to `http://{host}:{port}/`)
- [ ] **TODO**: Add health check endpoint (`GET /health`) returning `{"status": "ok"}`
- [ ] **TODO**: Validate `AGENT_PUBLIC_URL` is set in production; warn if using localhost fallback

### 6. Configuration & Environment

- [x] `.env` file support via `python-dotenv`
- [x] Required env vars: `BTP_UAS_URL`, `BTP_AUTH_URL`, `BTP_CLIENT_ID`, `BTP_CLIENT_SECRET`, `BTP_SUBACCOUNT_ID`
- [x] Optional env vars: `HOST`, `PORT`, `AGENT_PUBLIC_URL`
- [ ] **TODO**: Add startup validation that checks all required env vars are present and logs a clear error if missing
- [ ] **TODO**: Document all required env vars in README with example values

### 7. Dependencies (`requirements.txt`)

- [x] `litellm==1.86.1`
- [x] `langchain==1.2.15`, `langchain-core==1.3.3`, `langchain-litellm==0.3.5`
- [x] `langgraph==1.1.9`
- [x] `a2a-sdk[all]==0.3.22`
- [x] `fastapi!=0.136.3`, `uvicorn==0.40.0`
- [x] `httpx==0.28.1`, `python-dotenv==1.2.2`, `click==8.1.8`
- [x] `mcp>=1.0.0`, `opentelemetry-instrumentation-starlette`
- [x] `sap-cloud-sdk>=0.17.0`
- [ ] **TODO**: Pin `sap-cloud-sdk` to a specific version for reproducible builds
- [ ] **TODO**: Add `requirements-test.txt` entries for `pytest`, `pytest-asyncio`, `respx` (httpx mock)

### 8. Testing

- [x] `prebuilt_tests/test_server.py` → server startup + `/.well-known/agent-card.json` validation
- [x] `prebuilt_tests/test_structure.py` → project structure validation
- [x] `conftest.py` with `start_agent` fixture
- [ ] **TODO**: Add `pytest-asyncio` tests for `get_btp_usage` with mocked HTTP
- [ ] **TODO**: Add `pytest-asyncio` tests for `get_btp_services_summary` aggregation
- [ ] **TODO**: Add test for bilingual response — verify agent responds in Chinese when queried in Chinese
- [ ] **TODO**: Add test for OAuth2 token refresh logic (expiry cache behavior)
- [ ] **TODO**: Achieve ≥80% code coverage (configure `.coveragerc`)

### 9. README & Documentation

- [x] Overview section
- [x] Technology stack listed (A2A, LangGraph, LiteLLM, SAP Cloud SDK)
- [x] Directory structure documented
- [ ] **TODO**: Add **Prerequisites** section (Python version, BTP subaccount access)
- [ ] **TODO**: Add **Setup** section with `pip install -r requirements.txt` and `.env` setup steps
- [ ] **TODO**: Add **Running Locally** section with `python app/main.py` command
- [ ] **TODO**: Add **Testing** section with `pytest` commands
- [ ] **TODO**: Document the UAS API reference link and permission requirements
- [ ] **TODO**: Add **Deploying to BTP** section (Cloud Foundry `cf push` or Docker)

### 10. Deployment Readiness

- [ ] **TODO**: Create `manifest.yml` or `Dockerfile` for Cloud Foundry / containerized deployment
- [ ] **TODO**: Set `AGENT_PUBLIC_URL` to the deployed CF app URL
- [ ] **TODO**: Bind XSUAA service instance for production OAuth2 credentials
- [ ] **TODO**: Configure `solution.yaml` for automated deployment via `deploy-solution` skill

---

## Key Data Flows

### Usage Query Flow
```
User: "Show HANA usage last week"
  → agent.py: parse intent → call get_btp_usage(from_date, to_date, service_filter="hana")
  → uas_tool.py: OAuth2 token fetch → GET /reports/v1/subaccountUsage
  → normalize records → return JSON
  → LLM formats as markdown table
  → A2A artifact response
```

### Summary Flow
```
User: "What services used the most resources this month?"
  → agent.py: call get_btp_services_summary(from_date, to_date)
  → uas_tool.py: fetch all records → aggregate by (service, metric, unit)
  → return group_summary + detail sorted by total_usage desc
  → LLM formats top services with highlights
```

---

## Acceptance Criteria

| # | Criteria | Verified |
|---|----------|----------|
| 1 | Agent starts and serves `/.well-known/agent-card.json` with valid JSON | ✅ (prebuilt_tests) |
| 2 | `get_btp_usage` returns records filtered by service type | ⬜ |
| 3 | `get_btp_services_summary` returns sorted group totals | ⬜ |
| 4 | OAuth2 token is cached and reused within expiry window | ⬜ |
| 5 | Agent responds in Chinese when queried in Chinese | ⬜ |
| 6 | Agent handles missing env vars with a clear error at startup | ⬜ |
| 7 | Thread TTL eviction removes inactive sessions after 1 hour | ⬜ |
| 8 | All required env vars documented in README | ⬜ |
| 9 | Code coverage ≥ 80% | ⬜ |
| 10 | Deployment artifacts (Dockerfile or manifest.yml) present | ⬜ |
