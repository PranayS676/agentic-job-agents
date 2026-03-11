# WhatsApp Job Agent

Two-process monorepo split:

1. `job-backend`
Owns HTTP ingest, WAHA polling fallback, migrations, and backend readiness.
2. `job-agent-runtime`
Owns watcher loops, manager orchestration, LLM agents, content generation, tracing, and outbound delivery.
3. Shared packages:
- `job_platform`
- `job_integrations`

Future frontend work should talk only to the backend process. The backend and agent runtime communicate through PostgreSQL state, not direct service calls.

Current artifact policy:
- DOCX is the editable source of truth.
- Per-track resume masters live under `data/resume-docx-tracks/`.
- DOCX is also the active outbound attachment format.

## Repository Layout
```text
apps/
  backend/src/job_backend/
  agent-runtime/src/job_agent_runtime/
  agent-runtime/skills/
packages/
  platform/src/job_platform/
  integrations/src/job_integrations/
alembic/
data/
output/
tests/
```

## Quickstart
1. Install dependencies:
```bash
python -m poetry install
```
2. Copy [`.env.example`](C:/projects/agents/.env.example) to `.env`.
3. Fill the required values, especially:
- `DATABASE_URL`
- `WAHA_BASE_URL`
- `WAHA_SESSION`
- `WAHA_API_KEY`
- `WHATSAPP_GROUP_IDS`
- `ANTHROPIC_API_KEY`
- `GMAIL_CREDENTIALS_PATH`
- `GMAIL_TOKEN_PATH`
- `SKILLS_DIR=apps/agent-runtime/skills`
4. Start PostgreSQL and run WAHA if you want live ingest/outbound.
Python refactors do not require recreating the WAHA or PostgreSQL containers. If they are already running and healthy, reuse them.

## Runtime Model
Start the backend first:
```bash
python -m poetry run job-backend --host 0.0.0.0 --port 8000
```

You can leave the backend running by itself. It will continue ingesting WhatsApp messages into PostgreSQL even if the agent runtime is stopped.

Then start the agent runtime in a second shell:
```bash
python -m poetry run job-agent-runtime
```

Responsibilities:
- Backend:
  - `POST /webhook/waha`
  - `GET /health`
  - WAHA polling fallback every `POLL_INTERVAL_SECONDS`
  - Alembic `upgrade head` on startup
- Agent runtime:
  - watcher queue processing
  - manager orchestration
  - research / resume / outbound agents
  - `pipeline_runs`, `resume_versions`, `outbox`, `agent_traces`

## Durable Polling Behavior
The backend keeps a per-group polling cursor in PostgreSQL.

Behavior:
- First-ever backend poll for a group bootstraps from the last 24 hours.
- Normal cadence uses `POLL_INTERVAL_SECONDS` from `.env` (currently `1800` for 30 minutes).
- After restart, machine sleep, or downtime, the backend catches up from the last successful saved cursor to a fixed poll cutoff.
- The backend can run alone for hours or days while the agent runtime is off. Unprocessed messages accumulate in `whatsapp_messages` and are drained later when `job-agent-runtime` starts.

## Environment Notes
Important path settings:
```env
BASE_RESUME_DOCX=data/base_resume.docx
BASE_RESUME_TEXT=data/base_resume.md
RESUME_LIBRARY_DIR=data/resume-library
RESUME_TRACKS_DIR=data/resume-tracks
RESUME_DOCX_TRACKS_DIR=data/resume-docx-tracks
OUTPUT_DIR=output
SKILLS_DIR=apps/agent-runtime/skills
```

Notes:
1. `data/base_resume.docx` should always be a valid DOCX file.
2. `data/resume-docx-tracks/` is the real operational source for tailored editing.
3. Outbound delivery uses the edited DOCX as the attachment artifact.

Build normalized resume tracks after updating or replacing the PDF resume variants:
```bash
python -m poetry run python scripts/build_resume_tracks.py
```

Bootstrap per-track editable DOCX files after building or changing the track inventory:
```bash
python -m poetry run python scripts/bootstrap_resume_docx_tracks.py
```

## Database Setup
Start PostgreSQL 16 locally:
```bash
docker run -d --name jobagent-db ^
  -e POSTGRES_PASSWORD=password ^
  -e POSTGRES_DB=jobagent ^
  -p 5432:5432 postgres:16
```

Create the dedicated test database:
```bash
python -m poetry run python -c "import sqlalchemy as sa; e=sa.create_engine('postgresql+psycopg2://postgres:password@localhost:5432/postgres', isolation_level='AUTOCOMMIT'); c=e.connect(); c.execute(sa.text('CREATE DATABASE jobagent_test')); c.close(); e.dispose()"
```

Manual migration commands:
```bash
python -m poetry run alembic upgrade head
python -m poetry run alembic downgrade base
```

## WAHA Setup
Start WAHA:
```bash
docker run -d --name waha -p 3000:3000 devlikeapro/waha
```

Authenticate the `default` session:
1. Open `http://localhost:3000/dashboard`
2. Connect to `http://localhost:3000`
3. Use session `default`
4. Scan the QR code with WhatsApp

Then set:
```env
WAHA_BASE_URL=http://localhost:3000
WAHA_SESSION=default
WAHA_API_KEY=your-waha-api-key
WHATSAPP_GROUP_IDS=group1@g.us,group2@g.us
```

Check backend health:
```bash
curl http://localhost:8000/health
```

Health now includes:
- `groups_monitored`
- `polling_enabled`
- `polling_status`
- `last_poll_started_at`
- `last_poll_completed_at`
- `last_webhook_at`

## Backend Ops API
The backend now exposes a small read-only operator API for future dashboard work.

Available endpoints:
- `GET /api/ops/overview`
- `GET /api/ops/review-queue?limit=20`
- `GET /api/ops/pipeline-runs?limit=50`
- `GET /api/ops/polling-status`

Examples:
```bash
curl http://localhost:8000/api/ops/overview
curl "http://localhost:8000/api/ops/review-queue?limit=20"
curl "http://localhost:8000/api/ops/pipeline-runs?limit=50"
curl http://localhost:8000/api/ops/polling-status
```

Use these endpoints to confirm:
- backlog size
- review queue size
- recent pipeline terminal states
- per-group polling cursor health

## Gmail Setup
1. Enable Gmail API in Google Cloud.
2. Put OAuth client credentials at `data/credentials.json`.
3. Set:
```env
GMAIL_CREDENTIALS_PATH=data/credentials.json
GMAIL_TOKEN_PATH=data/token.json
SENDER_EMAIL=your@gmail.com
SENDER_NAME=Pranay
```
4. Bootstrap the token:
```bash
python -m poetry run python -c "from job_integrations.gmail import GmailConnector; import asyncio; c=GmailConnector(); print(c.token_status()); print(asyncio.run(c.send('your@gmail.com','Token Bootstrap','Bootstrap test email')))"
```

## Common Commands
Run the full test suite:
```bash
python -m poetry run pytest -q
```

Run backend-focused tests:
```bash
python -m poetry run pytest tests/unit/backend/test_ingest_webhook.py tests/unit/backend/test_waha_polling.py tests/integration/backend/test_ingest_integration.py tests/integration/backend/test_polling_catchup_integration.py tests/integration/backend/test_ops_api_integration.py -q
```

Run agent-runtime-focused tests:
```bash
python -m poetry run pytest tests/unit/agent_runtime/test_watcher.py tests/unit/agent_runtime/test_manager_agent.py tests/integration/agent_runtime/test_watcher_manager_integration.py -q
```

Run live WAHA tests:
```bash
set RUN_LIVE_WAHA_TESTS=1
python -m poetry run pytest tests/integration/system/test_waha_live.py -q -m live_waha
```

Run live manager relevance smoke tests:
```bash
set RUN_LIVE_ANTHROPIC_TESTS=1
python -m poetry run pytest tests/integration/system/test_manager_relevance_live.py -q -m live_anthropic
```

Run the full local seed dataset against the live manager relevance prompt:
```bash
python -m poetry run python scripts/evaluate_manager_relevance_live.py --dataset output/relevance_review/waha_last_24h_relevance_seed_dataset.json
```

Run the curated live ResumeEditorAgent review batch:
```bash
python -m poetry run python scripts/evaluate_resume_editor_live.py --dataset output/relevance_review/waha_last_24h_relevance_seed_dataset.json --limit 8
```

Run the live ResumeEditorAgent smoke test:
```bash
set RUN_LIVE_ANTHROPIC_TESTS=1
python -m poetry run pytest tests/integration/system/test_resume_editor_live.py -q -m live_anthropic
```

Run live Gmail integration:
```bash
python -m poetry run pytest tests/integration/system/test_gmail_live.py -q
```

Run Step 8 deterministic system validation:
```bash
python -m poetry run pytest tests/integration/system/test_system_validation_integration.py -q
```

Run the supervised live system validation harness:
```bash
python -m poetry run python scripts/run_system_validation_live.py --cases output/system_validation/cases.json
```

If any case is allowed to send externally, require the explicit flag:
```bash
python -m poetry run python scripts/run_system_validation_live.py --cases output/system_validation/cases.json --allow-live-send
```

The live runner writes review artifacts under:
- `output/system_validation/run_<timestamp>/summary.json`
- `output/system_validation/run_<timestamp>/summary.md`
- one subdirectory per case with DB and ops API snapshots

Recommended startup order for Step 8:
1. Start WAHA and PostgreSQL
2. Start `job-backend`
3. Start `job-agent-runtime` only when you want backlog drained
4. Use `/api/ops/overview` and `/api/ops/review-queue` to confirm system state

## Observability SQL
Recent pipeline runs:
```sql
SELECT job_title, company, relevance_score, status, created_at
FROM pipeline_runs
ORDER BY created_at DESC
LIMIT 20;
```

Per-trace agent timing:
```sql
SELECT agent_name, decision, latency_ms, input_tokens, output_tokens, created_at
FROM agent_traces
WHERE trace_id = :trace_id
ORDER BY created_at ASC;
```

Outbound audit:
```sql
SELECT channel, recipient, subject, status, sent_at
FROM outbox
WHERE sent_at > NOW() - INTERVAL '7 days'
ORDER BY sent_at DESC;
```
