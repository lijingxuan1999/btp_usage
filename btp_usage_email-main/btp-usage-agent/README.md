# BTP Usage Agent

A Joule AI agent that helps BTP administrators query and analyze daily subaccount usage for SAP HANA Cloud, AI Core, Cloud Foundry Runtime, and Integration Suite via the SAP UAS Reporting API.

## Overview

Uses A2A Protocol, LangGraph, LiteLLM, and SAP Cloud SDK.

## Technology Stack

- **A2A Protocol** (`a2a-sdk`) — Agent-to-Agent communication standard
- **LangGraph** — Agent orchestration with conversation checkpointing
- **LiteLLM** — Multi-provider LLM abstraction (SAP AI Core / GPT-4o)
- **SAP UAS Reporting API** — BTP usage data source (OAuth2/XSUAA)
- **FastAPI / Uvicorn** — ASGI web server

## Structure

```
btp-usage-agent/
├── app/
│   ├── main.py              # A2A server entry point
│   ├── agent_executor.py    # A2A AgentExecutor adapter
│   ├── agent.py             # LangGraph SampleAgent
│   ├── mcp_tools.py         # Tool loader
│   ├── uas_tool.py          # UAS API tools (get_btp_usage, get_btp_services_summary)
│   └── util.py              # Shared utilities
├── prebuilt_tests/          # Structure & server tests
├── .env.example             # Template for credentials
├── Dockerfile               # Container build
├── requirements.txt         # Runtime dependencies
└── requirements-test.txt    # Test dependencies
```

## Prerequisites

- Python 3.11+
- Access to an SAP BTP subaccount with UAS Reporting API enabled
- SAP AI Core service instance (for LLM backend)
- BTP service key with OAuth2 client credentials

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure credentials
cp .env.example .env
# Edit .env with your actual BTP service key values
```

## Running Locally

```bash
python app/main.py
# Server starts at http://0.0.0.0:5000
```

The agent card is available at: `http://localhost:5000/.well-known/agent-card.json`

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `BTP_UAS_URL` | ✅ | UAS Reporting API base URL |
| `BTP_AUTH_URL` | ✅ | XSUAA token endpoint |
| `BTP_CLIENT_ID` | ✅ | OAuth2 client ID |
| `BTP_CLIENT_SECRET` | ✅ | OAuth2 client secret |
| `BTP_SUBACCOUNT_ID` | ✅ | BTP subaccount GUID |
| `HOST` | ❌ | Server bind host (default: `0.0.0.0`) |
| `PORT` | ❌ | Server port (default: `5000`) |
| `AGENT_PUBLIC_URL` | ❌ | Public URL for agent card (production) |

## Testing

```bash
pip install -r requirements-test.txt
pytest
```

## Credentials / Security Notes

- **`.env` is listed in `.gitignore`** — never commit it to Git
- For production, inject credentials via **BTP environment bindings** or **Kubernetes Secrets**
- Rotate service keys immediately if accidentally exposed

## UAS API Reference

- [SAP UAS Reporting API](https://api.sap.com/api/APIUasReportingService/path/dailySubaccountUsage)
- Endpoint: `GET /reports/v1/subaccountUsage`
- Authentication: OAuth2 client credentials (XSUAA)

## Deploying to BTP

Build and push the Docker image:

```bash
docker build -t btp-usage-agent .
docker run -p 5000:5000 --env-file .env btp-usage-agent
```

For Cloud Foundry deployment, set `AGENT_PUBLIC_URL` to the deployed CF app URL and bind the XSUAA service instance.
