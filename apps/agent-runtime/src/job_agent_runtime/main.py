from __future__ import annotations

import argparse
import asyncio
import hashlib
import signal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from rich.console import Console
from rich.table import Table
from sqlalchemy import text

from job_agent_runtime.orchestration.manager import ManagerAgent, ManagerPipelineRunner
from job_agent_runtime.worker.watcher import WatcherService
from job_integrations.gmail import GmailConnector
from job_integrations.waha import WAHAConnector
from job_platform.config import get_settings, validate_agent_runtime_startup_requirements
from job_platform.database import AsyncSessionLocal, engine
from job_platform.logging import configure_logging
from job_platform.tracer import AgentTracer


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agent runtime entrypoint")
    parser.add_argument("--dry-run", action="store_true", help="Run near-full pipeline checks without outbound")
    parser.add_argument("--log-level", default=None, help="Optional logger level override")
    return parser.parse_args()


async def _db_connectivity_status() -> str:
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc.__class__.__name__}"


async def _waha_connectivity_status(settings) -> str:
    connector = WAHAConnector(
        base_url=settings.waha_base_url,
        session=settings.waha_session,
        api_key=settings.waha_api_key,
    )
    try:
        response = await connector.client.get(
            "/api/server/status",
            params={"session": settings.waha_session},
        )
        if response.status_code == 200:
            return "ok"
        return f"error: HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        return f"error: {exc.__class__.__name__}"
    finally:
        await connector.close()


def _print_readiness_table(*, settings, db_status: str, waha_status: str, gmail_status: str) -> None:
    table = Table(title="Agent Runtime Startup Readiness")
    table.add_column("Check", style="cyan")
    table.add_column("Value", style="white")

    table.add_row("Skills dir", str(settings.resolve_path(settings.skills_dir)))
    table.add_row("Manager model", str(settings.manager_model))
    table.add_row("Research model", str(settings.research_model))
    table.add_row("Resume editor model", str(settings.resume_editor_model))
    table.add_row("Gmail model", str(settings.gmail_agent_model))
    table.add_row("WhatsApp model", str(settings.whatsapp_msg_model))
    table.add_row("DB", db_status)
    table.add_row("WAHA", waha_status)
    table.add_row("Gmail token", gmail_status)

    Console().print(table)


def _snapshot_artifacts(settings) -> set[Path]:
    files: set[Path] = set()
    output_dir = settings.resolve_path(settings.output_dir)
    for subdir in ("resumes",):
        target = output_dir / subdir
        if not target.is_dir():
            continue
        files.update(path.resolve() for path in target.rglob("*") if path.is_file())
    return files


def _cleanup_artifacts(before: set[Path], settings) -> None:
    for path in _snapshot_artifacts(settings) - before:
        try:
            path.unlink()
        except FileNotFoundError:
            continue


async def _fetch_dry_run_samples(settings) -> list[dict[str, Any]]:
    connector = WAHAConnector(
        base_url=settings.waha_base_url,
        session=settings.waha_session,
        api_key=settings.waha_api_key,
    )
    try:
        samples: list[dict[str, Any]] = []
        for group_id in settings.whatsapp_group_ids_list:
            messages = await connector.get_new_messages(group_id=group_id, since_timestamp=0)
            sorted_messages = sorted(
                messages,
                key=lambda row: int(row.get("timestamp") or 0),
                reverse=True,
            )
            samples.extend(sorted_messages[:5])
        return samples
    finally:
        await connector.close()


async def _run_dry_run(settings) -> int:
    samples = await _fetch_dry_run_samples(settings)
    if not samples:
        return 1

    rows: list[dict[str, str]] = []
    failures = 0

    for index, sample in enumerate(samples):
        artifact_snapshot = _snapshot_artifacts(settings)
        sender = str(sample.get("sender_number") or "").strip()
        group_id = str(sample.get("group_id") or "").strip()
        message_text = str(sample.get("text") or sample.get("message_text") or "").strip()
        timestamp = int(sample.get("timestamp") or 0)
        message_hash = hashlib.md5(
            f"{group_id}|{sender}|{message_text}|{timestamp}|dry_run|{index}".encode("utf-8")
        ).hexdigest()

        action = "error"
        detail = ""
        trace_id: str | None = None

        async with AsyncSessionLocal() as session:
            try:
                message_id = (
                    await session.execute(
                        text(
                            """
                            INSERT INTO whatsapp_messages (
                                group_id,
                                sender_number,
                                message_text,
                                message_hash,
                                processed
                            )
                            VALUES (
                                :group_id,
                                :sender_number,
                                :message_text,
                                :message_hash,
                                false
                            )
                            RETURNING id
                            """
                        ),
                        {
                            "group_id": group_id,
                            "sender_number": sender,
                            "message_text": message_text,
                            "message_hash": message_hash,
                        },
                    )
                ).scalar_one()

                dry_trace_id = (
                    await session.execute(
                        text(
                            """
                            INSERT INTO pipeline_runs (message_id, status)
                            VALUES (:message_id, 'started')
                            RETURNING trace_id
                            """
                        ),
                        {"message_id": message_id},
                    )
                ).scalar_one()
                trace_id = str(dry_trace_id)

                message_obj = SimpleNamespace(
                    group_id=group_id,
                    sender_number=sender,
                    message_text=message_text,
                )
                manager = ManagerAgent(
                    db_session=session,
                    tracer=AgentTracer(session),
                    settings=settings,
                    mode="dry_run_pre_outbound",
                )
                result = await manager.run(message=message_obj, trace_id=dry_trace_id)
                action = str(result.get("action") or "unknown")
                detail = str(result.get("reason") or result.get("stage") or "")
                if action not in {"dry_run_ready", "discarded"}:
                    failures += 1
            except Exception as exc:  # noqa: BLE001
                failures += 1
                action = "error"
                detail = f"{exc.__class__.__name__}: {exc}"
            finally:
                await session.rollback()
                _cleanup_artifacts(artifact_snapshot, settings)

        rows.append(
            {
                "group": group_id,
                "sender": sender,
                "trace_id": trace_id or "n/a",
                "action": action,
                "detail": detail[:120],
            }
        )

    table = Table(title="Agent Runtime Dry-Run Summary")
    table.add_column("Group")
    table.add_column("Sender")
    table.add_column("Trace")
    table.add_column("Action")
    table.add_column("Detail")
    for row in rows:
        table.add_row(row["group"], row["sender"], row["trace_id"], row["action"], row["detail"])
    Console().print(table)

    return 1 if failures else 0


async def _run_runtime(settings, log_level_override: str | None) -> int:
    watcher = WatcherService(settings=settings)
    runner = ManagerPipelineRunner(settings=settings)

    watcher_task = asyncio.create_task(
        watcher.run_forever(runner),
        name="agent_runtime_watcher",
    )
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    shutdown_wait_task = asyncio.create_task(stop_event.wait(), name="agent_runtime_shutdown_wait")
    try:
        done, _ = await asyncio.wait(
            {watcher_task, shutdown_wait_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if watcher_task in done and watcher_task.exception():
            raise watcher_task.exception()
        return 0
    finally:
        watcher_task.cancel()
        shutdown_wait_task.cancel()
        await asyncio.gather(watcher_task, shutdown_wait_task, return_exceptions=True)


async def _async_main(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.log_level:
        settings.log_level = args.log_level.upper()

    validate_agent_runtime_startup_requirements(settings)
    configure_logging(settings)

    db_status = await _db_connectivity_status()
    waha_status = await _waha_connectivity_status(settings)
    gmail_status = GmailConnector(settings=settings).token_status()
    _print_readiness_table(
        settings=settings,
        db_status=db_status,
        waha_status=waha_status,
        gmail_status=gmail_status,
    )

    try:
        if args.dry_run:
            return await _run_dry_run(settings)
        return await _run_runtime(settings, args.log_level)
    finally:
        await engine.dispose()


def main() -> int:
    return asyncio.run(_async_main(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
