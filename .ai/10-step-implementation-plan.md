# WhatsApp Job Agent v2 - Detailed 10-Step Execution Plan

Source architecture: `C:\Users\prana\Downloads\ARCHITECTURE_V2.md`

## Step 1 - Project Setup and Repository Scaffold
Goal: establish a clean, reproducible Python 3.11+ codebase with the full folder layout from the architecture.

Tasks:
- Initialize Poetry project (`whatsapp-job-agent`) and pin runtime/dev dependencies from the architecture prompt list.
- Create directory structure:
  - `src/core`, `src/agents`, `src/connectors`
  - `skills/*` (all required skill folders with `SKILL.md` placeholders + `references/`)
  - `alembic/`, `data/`, `output/resumes/`, `output/pdfs/`, `tests/unit/`, `tests/integration/`
- Add baseline project files: `pyproject.toml`, `.gitignore`, `.env.example`, `README.md`.
- Add `.gitkeep` for empty output/data directories that should exist in repo.

Definition of done:
- `poetry install` succeeds.
- `tree`/directory listing matches architecture structure.

## Step 2 - Configuration and Environment Contracts
Goal: define and validate all runtime settings before implementing services.

Tasks:
- Implement `src/core/config.py` using Pydantic Settings.
- Include all variables from architecture section 7 and `.env.example`.
- Add computed properties:
  - `whatsapp_group_ids_list`
  - `db_url_sync`
- Add startup validation for required files and secrets paths.
- Standardize environment handling for local/dev/prod.

Definition of done:
- App can load settings without hardcoded constants.
- Missing required env vars fail fast with actionable error messages.

## Step 3 - Database Layer and Migrations
Goal: make PostgreSQL the single source of truth with full schema + migration flow.

Tasks:
- Implement async DB core in `src/core/database.py` (engine, `AsyncSession`, `get_session`).
- Build ORM models in `src/core/models.py` for:
  - `whatsapp_messages`, `pipeline_runs`, `resume_versions`, `outbox`, `candidate_profile`, `agent_traces`
- Configure Alembic for async SQLAlchemy and generate initial migration.
- Add indexes/constraints matching architecture (dedupe hash, status indexes, FK relations).
- Add updated-at behavior for mutable pipeline rows.

Definition of done:
- `alembic upgrade head` creates all required tables locally.
- CRUD smoke test passes for every table.

## Step 4 - Core Agent Framework and Tracing
Goal: provide a reusable execution spine for all agents with structured traceability.

Tasks:
- Implement `BaseAgent` in `src/core/base_agent.py`:
  - Skill loader (`SKILL.md` + `references/` context)
  - Model invocation helper
  - JSON parser with fenced-markdown cleanup
- Implement tracer in `src/core/tracer.py`:
  - `trace(...)` inserts `agent_traces`
  - `update_pipeline_status(...)` updates `pipeline_runs`
- Add standard error envelope and retry boundaries for model calls.
- Instrument latency/tokens/decision summaries.

Definition of done:
- A minimal test agent can run and writes trace rows.
- Failed parsing/model calls are logged and surfaced correctly.

## Step 5 - Ingest Service and WAHA Connector
Goal: reliably ingest incoming WhatsApp group messages into DB with deduplication.

Tasks:
- Implement `src/connectors/waha.py`:
  - group listing, message fetch, send text/file, email extraction helper.
- Implement `src/ingest.py` FastAPI app:
  - `POST /webhook/waha`
  - `GET /health`
- Compute `message_hash` and skip duplicates.
- Persist raw inbound messages with `processed=false`.
- Add polling fallback behavior definition for webhook gaps.

Definition of done:
- Test webhook payload inserts rows correctly.
- Duplicate payload is ignored by hash check.

## Step 6 - Watcher Service and Pipeline Dispatch
Goal: convert unprocessed inbound messages into deterministic pipeline executions.

Tasks:
- Implement `src/watcher.py` polling loop (`processed=false` query).
- Add optimistic processing lock (`processing_started_at`) before dispatch.
- On success: mark message processed.
- On failure: write `processing_error`, still mark processed to avoid infinite loops.
- Add daily deduplication/retention cleanup task.
- Add structured tick metrics logging.

Definition of done:
- Batch of queued messages is consumed in order.
- Failure in one message does not block subsequent rows.

## Step 7 - Manager Orchestrator and Relevance Gate
Goal: centralize execution order, decision logic, and stage-level fault handling.

Tasks:
- Implement `src/agents/manager_agent.py` as orchestrator.
- Step sequence:
  - Relevance evaluation
  - Research
  - Resume edit
  - PDF conversion
  - Quality gate
  - Outbound routing
- Write `pipeline_runs` state transitions and decision JSON.
- Add relevance discard path with explicit reasoning.
- Ensure stage-specific `error_stage` + `error_message` updates.

Definition of done:
- Irrelevant jobs are discarded early.
- Relevant jobs proceed through downstream stages with status transitions recorded.

## Step 8 - Content Pipeline Agents (Research, Resume Edit, PDF)
Goal: produce truthful, tailored resume artifacts per relevant job.

Tasks:
- Implement `ResearchAgent` with base resume loading and structured gap analysis output.
- Implement `ResumeEditorAgent`:
  - read/edit DOCX sections
  - apply add/remove actions
  - persist version metadata and evaluator metrics
- Implement `PDFConverterAgent` with headless LibreOffice conversion.
- Create required skill docs and reference helpers (`research_methodology.md`, `ats_scorer.py`, style rules placeholder).

Definition of done:
- Given sample job input, pipeline produces `.docx` and `.pdf` in output folders.
- `resume_versions` rows capture versioning and evaluation details.

## Step 9 - Outbound Delivery Agents and Routing Completion
Goal: send final tailored application via email or WhatsApp and persist outbound audit records.

Tasks:
- Implement `src/connectors/gmail.py` OAuth/token-based sender.
- Implement `GmailAgent` and `WhatsAppMsgAgent` for generated message composition + send.
- Wire manager routing:
  - if `poster_email` exists -> Gmail
  - else -> WhatsApp with PDF attachment
- Persist outbox records for every send attempt and result.
- Ensure quality-gate pass is required before send.

Definition of done:
- Outbound send succeeds in both routing modes with DB audit trail.
- Pipeline status ends at `sent` for success path.

## Step 10 - Validation, Observability, and Go-Live Runbook
Goal: verify production readiness and provide repeatable operating procedures.

Tasks:
- Implement unit and integration tests for filter, research, resume edit, and full pipeline flow.
- Validate structured logging and SQL observability queries from architecture section 12.
- Build `src/main.py` startup orchestration:
  - migration run
  - ingest server
  - watcher loop
  - cleanup task
  - health/status summary table
- Add `--dry-run` connectivity mode for pre-flight checks.
- Document first-run bootstrap (Postgres, WAHA, Gmail OAuth, LibreOffice, dry run).

Definition of done:
- `python -m src.main --dry-run` passes end-to-end connectivity checks.
- Full run processes live message -> trace -> tailored output -> outbound send.

## Immediate Next Steps
1. Execute Step 1 and Step 2 first; do not start agent coding before config and schema contracts are stable.
2. After Step 3 migration is validated, implement Steps 4 to 6 together (foundation services).
3. Build Steps 7 to 9 as one vertical slice, then finish with Step 10 hardening and runbook.
