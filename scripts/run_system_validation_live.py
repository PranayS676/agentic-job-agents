from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from job_agent_runtime.orchestration.manager import ManagerPipelineRunner
from job_agent_runtime.worker.watcher import WatcherService
from job_backend.services.ingest import create_app
from job_platform.config import clear_settings_cache, get_settings


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CASES_PATH = ROOT_DIR / "output" / "system_validation" / "cases.json"
DEFAULT_OUTPUT_ROOT = ROOT_DIR / "output" / "system_validation"


@dataclass(slots=True)
class LiveCase:
    name: str
    payload: dict[str, Any]
    expected_terminal_status: str | None
    allow_live_send: bool
    notes: str


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run supervised end-to-end system validation and save review artifacts."
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CASES_PATH,
        help="JSON file describing live validation cases.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Artifact directory. Defaults to output/system_validation/run_<timestamp>.",
    )
    parser.add_argument(
        "--allow-live-send",
        action="store_true",
        help="Allow cases marked allow_live_send=true to send externally.",
    )
    parser.add_argument(
        "--fail-on-mismatch",
        action="store_true",
        help="Exit non-zero if any case final status differs from expected_terminal_status.",
    )
    return parser.parse_args()


def _async_to_sync_url(database_url: str) -> str:
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    if database_url.startswith("postgres+asyncpg://"):
        return database_url.replace("postgres+asyncpg://", "postgres+psycopg2://", 1)
    return database_url


def _default_output_dir() -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return DEFAULT_OUTPUT_ROOT / f"run_{timestamp}"


def _load_cases(path: Path) -> list[LiveCase]:
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing cases file: {path}. Create a JSON array of live validation cases before running this script."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Cases file must be a JSON array.")

    cases: list[LiveCase] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Case {index} must be a JSON object.")
        case_payload = dict(item.get("payload") or {})
        if not case_payload:
            raise ValueError(f"Case {index} is missing payload.")
        cases.append(
            LiveCase(
                name=str(item.get("name") or f"case_{index:02d}"),
                payload=case_payload,
                expected_terminal_status=(
                    str(item["expected_terminal_status"]).strip()
                    if item.get("expected_terminal_status") is not None
                    else None
                ),
                allow_live_send=bool(item.get("allow_live_send", False)),
                notes=str(item.get("notes") or ""),
            )
        )
    return cases


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _generate_external_message_id(case_name: str) -> str:
    return f"system-validation-{case_name}-{uuid4().hex[:12]}"


def _fetch_unprocessed_count(sync_database_url: str) -> int:
    engine = create_engine(sync_database_url)
    try:
        with engine.connect() as conn:
            return int(
                conn.execute(
                    text("SELECT COUNT(*) FROM whatsapp_messages WHERE processed = false")
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _fetch_case_snapshot(sync_database_url: str, *, external_message_id: str) -> dict[str, Any]:
    engine = create_engine(sync_database_url)
    try:
        with engine.connect() as conn:
            message_row = conn.execute(
                text(
                    """
                    SELECT id, group_id, sender_number, message_text, processed, processing_error,
                           source_timestamp, external_message_id, created_at
                    FROM whatsapp_messages
                    WHERE external_message_id = :external_message_id
                    """
                ),
                {"external_message_id": external_message_id},
            ).mappings().first()
            if message_row is None:
                raise RuntimeError(
                    f"Unable to find whatsapp_messages row for external_message_id={external_message_id}"
                )

            pipeline_row = conn.execute(
                text(
                    """
                    SELECT trace_id::text AS trace_id, status, message_id, job_title, company, job_summary,
                           relevance_score, error_stage, error_message, outbound_action,
                           quality_gate_result, manager_decision, created_at
                    FROM pipeline_runs
                    WHERE message_id = :message_id
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"message_id": message_row["id"]},
            ).mappings().first()

            resume_rows = conn.execute(
                text(
                    """
                    SELECT id, trace_id::text AS trace_id, version_number, docx_path, attachment_path,
                           ats_score_before, ats_score_after, evaluator_passed, created_at
                    FROM resume_versions
                    WHERE trace_id = CAST(:trace_id AS uuid)
                    ORDER BY version_number ASC, created_at ASC
                    """
                ),
                {"trace_id": pipeline_row["trace_id"] if pipeline_row else None},
            ).mappings().all() if pipeline_row else []

            outbox_rows = conn.execute(
                text(
                    """
                    SELECT id, trace_id::text AS trace_id, channel, recipient, subject, body_preview,
                           attachment_path, external_id, status, sent_at, created_at
                    FROM outbox
                    WHERE trace_id = CAST(:trace_id AS uuid)
                    ORDER BY created_at ASC, id ASC
                    """
                ),
                {"trace_id": pipeline_row["trace_id"] if pipeline_row else None},
            ).mappings().all() if pipeline_row else []

            trace_rows = conn.execute(
                text(
                    """
                    SELECT id, trace_id::text AS trace_id, agent_name, decision, input_tokens,
                           output_tokens, latency_ms, created_at
                    FROM agent_traces
                    WHERE trace_id = CAST(:trace_id AS uuid)
                    ORDER BY created_at ASC, id ASC
                    """
                ),
                {"trace_id": pipeline_row["trace_id"] if pipeline_row else None},
            ).mappings().all() if pipeline_row else []

            return {
                "whatsapp_message": dict(message_row),
                "pipeline_run": dict(pipeline_row) if pipeline_row else None,
                "resume_versions": [dict(row) for row in resume_rows],
                "outbox": [dict(row) for row in outbox_rows],
                "agent_traces": [dict(row) for row in trace_rows],
            }
    finally:
        engine.dispose()


async def _run_case(
    *,
    client: TestClient,
    watcher: WatcherService,
    pipeline_runner: ManagerPipelineRunner,
    sync_database_url: str,
    case: LiveCase,
    artifact_root: Path,
) -> dict[str, Any]:
    if _fetch_unprocessed_count(sync_database_url) != 0:
        raise RuntimeError("Refusing to run live validation with a non-empty unprocessed backlog.")

    payload = dict(case.payload)
    payload.setdefault("group_id", "")
    payload.setdefault("sender_number", "")
    payload.setdefault("message_text", "")
    payload.setdefault("timestamp", int(datetime.now(UTC).timestamp()))
    payload.setdefault("external_message_id", _generate_external_message_id(case.name))

    response = client.post("/webhook/waha", json=payload)
    if response.status_code != 200:
        raise RuntimeError(
            f"Webhook ingest failed for {case.name}: status={response.status_code} body={response.text}"
        )

    tick_summary = await watcher.run_tick(pipeline_runner=pipeline_runner)
    snapshot = _fetch_case_snapshot(
        sync_database_url,
        external_message_id=str(payload["external_message_id"]),
    )
    overview_snapshot = client.get("/api/ops/overview").json()
    review_queue_snapshot = client.get("/api/ops/review-queue", params={"limit": 20}).json()
    pipeline_runs_snapshot = client.get("/api/ops/pipeline-runs", params={"limit": 20}).json()
    polling_status_snapshot = client.get("/api/ops/polling-status").json()

    final_status = str((snapshot["pipeline_run"] or {}).get("status") or "")
    case_dir = artifact_root / case.name
    case_dir.mkdir(parents=True, exist_ok=True)

    _write_json(case_dir / "input_message.json", payload)
    _write_json(case_dir / "tick_summary.json", tick_summary)
    _write_json(case_dir / "snapshot.json", snapshot)
    _write_json(case_dir / "ops_overview.json", overview_snapshot)
    _write_json(case_dir / "ops_review_queue.json", review_queue_snapshot)
    _write_json(case_dir / "ops_pipeline_runs.json", pipeline_runs_snapshot)
    _write_json(case_dir / "ops_polling_status.json", polling_status_snapshot)

    return {
        "name": case.name,
        "expected_terminal_status": case.expected_terminal_status,
        "actual_terminal_status": final_status,
        "allow_live_send": case.allow_live_send,
        "notes": case.notes,
        "message_external_id": payload["external_message_id"],
        "artifact_dir": str(case_dir),
        "tick_summary": tick_summary,
        "matched_expectation": (
            final_status == case.expected_terminal_status if case.expected_terminal_status else None
        ),
    }


def _build_summary_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# System Validation Summary",
        "",
        f"- Evaluated at: `{summary['evaluated_at']}`",
        f"- Cases file: `{summary['cases_path']}`",
        f"- Completed cases: `{summary['completed_cases']}`",
        f"- Errors: `{summary['error_count']}`",
        "",
        "## Cases",
    ]
    for item in summary["results"]:
        lines.extend(
            [
                f"### {item['name']}",
                f"- expected: `{item['expected_terminal_status']}`",
                f"- actual: `{item['actual_terminal_status']}`",
                f"- matched expectation: `{item['matched_expectation']}`",
                f"- allow live send: `{item['allow_live_send']}`",
                f"- artifact dir: `{item['artifact_dir']}`",
                "",
            ]
        )
    if summary["errors"]:
        lines.append("## Errors")
        for item in summary["errors"]:
            lines.append(f"- `{item['name']}`: {item['error']}")
    return "\n".join(lines)


async def _main() -> int:
    args = _parse_args()
    cases = _load_cases(args.cases)
    if not cases:
        raise RuntimeError("No cases found.")
    if any(case.allow_live_send for case in cases) and not args.allow_live_send:
        raise RuntimeError(
            "At least one case is marked allow_live_send=true. Re-run with --allow-live-send after reviewing content targets."
        )

    clear_settings_cache()
    settings = get_settings()
    sync_database_url = _async_to_sync_url(settings.database_url)

    async_engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    pipeline_runner = ManagerPipelineRunner(session_factory=session_factory, settings=settings)
    watcher = WatcherService(settings=settings, session_factory=session_factory)
    app = create_app(enable_polling=False)
    output_dir = args.output_dir or _default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    try:
        with TestClient(app) as client:
            for case in cases:
                try:
                    results.append(
                        await _run_case(
                            client=client,
                            watcher=watcher,
                            pipeline_runner=pipeline_runner,
                            sync_database_url=sync_database_url,
                            case=case,
                            artifact_root=output_dir,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        {
                            "name": case.name,
                            "error": f"{exc.__class__.__name__}: {exc}",
                        }
                    )
    finally:
        await async_engine.dispose()
        clear_settings_cache()

    summary = {
        "evaluated_at": datetime.now(UTC).isoformat(),
        "cases_path": str(args.cases),
        "completed_cases": len(results),
        "error_count": len(errors),
        "results": results,
        "errors": errors,
    }
    _write_json(output_dir / "summary.json", summary)
    _write_markdown(output_dir / "summary.md", _build_summary_markdown(summary))

    mismatches = [
        item for item in results if item["matched_expectation"] is False
    ]
    if errors:
        return 1
    if args.fail_on_mismatch and mismatches:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
