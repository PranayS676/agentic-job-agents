# Step 1 Detailed Plan: Project Setup + Architecture Files in `.ai`

## Summary
This plan covers only Step 1 from the 10-step roadmap, expanded to implementation-level detail. Scope includes:
1. Copying selected architecture files into `.ai`.
2. Scaffolding the Python project in-place at `C:\projects\agents` with Poetry.
3. Creating the baseline structure, dependency contracts, and setup files required by the architecture.

## Public Interfaces and Contracts Introduced in Step 1
1. Repository structure contract: required directories and placeholders for later steps.
2. Environment contract: `.env.example` keys define expected runtime configuration.
3. Dependency contract: `pyproject.toml` runtime/dev sets toolchain and imports.
4. Skill filesystem contract: each skill folder has `SKILL.md` and `references/`.

## Detailed Execution Plan
1. Preflight validation (read-only)
- Confirm root path is `C:\projects\agents`.
- Confirm architecture sources exist in Downloads.
- Confirm target `.ai` exists.

2. Copy architecture files into `.ai`
- Copy `ARCHITECTURE_V2.md`.
- Copy `Job_AI_Application_Platform_Architecture.md`.
- Verify both files are non-empty.

3. Initialize Poetry project at repo root
- Create `pyproject.toml` for project `whatsapp-job-agent`.
- Set Python version to `^3.11`.
- Configure source layout for `src` imports.

4. Add runtime dependencies
- Include all architecture dependencies:
  `anthropic`, `asyncpg`, `sqlalchemy[asyncio]`, `alembic`, `httpx`,
  `google-auth`, `google-auth-oauthlib`, `google-auth-httplib2`,
  `google-api-python-client`, `python-docx`, `pydantic`,
  `pydantic-settings`, `python-dotenv`, `schedule`, `structlog`, `rich`,
  `opentelemetry-sdk`, `opentelemetry-exporter-otlp`, `fastapi`, `uvicorn`.

5. Add dev dependency group
- Include `pytest`, `pytest-asyncio`, `ruff`, `black`, `mypy`, `respx`, `factory-boy`.

6. Create required folder tree
- Root dirs: `src`, `skills`, `alembic`, `data`, `output`, `tests`.
- Subdirs: `src/core`, `src/agents`, `src/connectors`, `output/resumes`, `output/pdfs`, `tests/unit`, `tests/integration`, `alembic/versions`.

7. Create all 7 skill skeletons with placeholders
- `job-manager`, `job-relevance-evaluator`, `resume-research`, `resume-editor`, `pdf-converter`, `gmail-composer`, `whatsapp-composer`.
- Each contains `SKILL.md` + `references/`.
- `resume-editor` and `pdf-converter` also include `scripts/`.

8. Create baseline project files
- `.env.example` with all section-7 keys.
- `.gitignore` with required exclusions.
- `README.md` quickstart placeholder.

9. Add `.gitkeep` files for tracked-empty dirs
- `output/resumes/.gitkeep`
- `output/pdfs/.gitkeep`
- `data/.gitkeep`

10. Verification checklist
- Run `poetry check` and `poetry install`.
- Validate mandatory directories exist.
- Validate all required `.env.example` keys exist.
- Validate exactly 7 skills and required parts.
- Validate copied architecture files in `.ai` are non-empty.

## Test Cases and Scenarios
1. Dependency integrity: resolver/install success.
2. Filesystem contract: all required directories/files present.
3. Config completeness: `.env.example` has all required keys.
4. Ignore rules: `.gitignore` includes required exclusions.
5. Architecture copy check: both copied docs open and contain content.

## Assumptions and Defaults
1. Copy exactly two architecture files from Downloads into `.ai`.
2. Keep existing 10-step plan file unchanged.
3. Scaffold in-place under `C:\projects\agents`.
4. Preserve original architecture filenames.
5. Do not modify architecture doc contents while copying.