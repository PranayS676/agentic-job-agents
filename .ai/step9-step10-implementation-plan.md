# Step 9 + Step 10 Implementation Plan

This file captures the locked implementation scope for Step 9 and Step 10.

## Step 9
- Implement GmailConnector in src/connectors/gmail.py.
- Implement GmailAgent and WhatsAppMsgAgent.
- Keep outbox writes in ManagerAgent.
- Wire DefaultAgentFactory to real outbound agents.
- Replace placeholder gmail/whatsapp skill docs and add reference templates.
- Add outbound unit/integration tests, including live Gmail coverage.

## Step 10
- Implement src/main.py orchestration entrypoint.
- Startup: load settings, validate startup requirements, run Alembic migrations.
- Run ingest server + watcher loop/cleanup in one process.
- Add --dry-run near-full pipeline mode (skip outbound) with rollback + artifact cleanup.
- Print readiness table with models, groups, DB/WAHA/Gmail status.
- Add runbook + observability SQL + Prompt 11 placeholder files.

## Locked Decisions
1. outbox row ownership remains in ManagerAgent.
2. src.main runs ingest + watcher + cleanup together.
3. --dry-run runs near-full pipeline through quality gate, no outbound.
4. Gmail live integration test runs by default.
