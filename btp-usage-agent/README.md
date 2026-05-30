# BTP Usage Agent

A Joule AI agent that helps BTP administrators query and analyze daily subaccount usage for SAP HANA Cloud, AI Core, Cloud Foundry Runtime, and Integration Suite via the SAP UAS Reporting API

## Overview

Uses A2A Protocol, LangGraph, LiteLLM, and SAP Cloud SDK.

## Structure

- `app/main.py` - A2A server entry
- `app/agent_executor.py` - Request handling
- `app/agent.py` - Agent logic
