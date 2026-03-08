# Backend / Agent Runtime Separation

## Summary
The repo is now split into two deployable apps plus shared packages:

1. `job-backend`
- HTTP ingest
- WAHA polling fallback
- migrations
- health/readiness
2. `job-agent-runtime`
- watcher loop
- manager orchestration
- research / resume / PDF / outbound agents
- trace and outbox writes
3. Shared packages
- `job_platform`
- `job_integrations`

Backend and agent runtime communicate only through PostgreSQL tables.

## Package Layout
```text
apps/
  backend/src/job_backend/
  agent-runtime/src/job_agent_runtime/
  agent-runtime/skills/
packages/
  platform/src/job_platform/
  integrations/src/job_integrations/
tests/
  unit/backend/
  unit/agent_runtime/
  unit/platform/
  unit/integrations/
  integration/backend/
  integration/agent_runtime/
  integration/system/
```

## Queue Boundary
1. Backend inserts `whatsapp_messages(processed=false)`.
2. Agent runtime claims rows using `processing_started_at`.
3. Agent runtime creates/updates:
- `pipeline_runs`
- `resume_versions`
- `outbox`
- `agent_traces`

## Entry Points
Start backend:
```bash
python -m poetry run job-backend --host 0.0.0.0 --port 8000
```

Start agent runtime:
```bash
python -m poetry run job-agent-runtime
```

## Config Notes
Use:
```env
SKILLS_DIR=apps/agent-runtime/skills
```

## Current Import Surface
The active codebase now uses only:

1. `job_backend.*`
2. `job_agent_runtime.*`
3. `job_platform.*`
4. `job_integrations.*`

## Frontend Direction
Future frontend and observability APIs should attach to the backend side only. They should not import or call agent modules directly.
