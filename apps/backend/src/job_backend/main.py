from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import httpx
import uvicorn
from alembic import command
from alembic.config import Config
from rich.console import Console
from rich.table import Table
from sqlalchemy import text

from job_backend.app import create_app
from job_integrations.waha import WAHAConnector
from job_platform.config import get_settings, validate_backend_startup_requirements
from job_platform.database import AsyncSessionLocal
from job_platform.logging import configure_logging


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backend service entrypoint")
    parser.add_argument("--host", default="0.0.0.0", help="Host for FastAPI ingest server")
    parser.add_argument("--port", type=int, default=8000, help="Port for FastAPI ingest server")
    parser.add_argument("--log-level", default=None, help="Optional uvicorn log level override")
    parser.add_argument("--disable-polling", action="store_true", help="Disable WAHA polling fallback")
    return parser.parse_args()


def _alembic_upgrade_head_sync() -> None:
    root_dir = Path(__file__).resolve().parents[4]
    config = Config(str(root_dir / "alembic.ini"))
    config.set_main_option("script_location", str(root_dir / "alembic"))
    command.upgrade(config, "head")


async def _run_migrations() -> None:
    await asyncio.to_thread(_alembic_upgrade_head_sync)


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


def _print_readiness_table(*, settings, db_status: str, waha_status: str, polling_enabled: bool) -> None:
    table = Table(title="Backend Startup Readiness")
    table.add_column("Check", style="cyan")
    table.add_column("Value", style="white")

    table.add_row("Groups", ", ".join(settings.whatsapp_group_ids_list))
    table.add_row("WAHA polling fallback", "enabled" if polling_enabled else "disabled")
    table.add_row("DB", db_status)
    table.add_row("WAHA", waha_status)

    Console().print(table)


async def _async_main(args: argparse.Namespace) -> int:
    settings = get_settings()
    validate_backend_startup_requirements(settings)
    configure_logging(settings)

    await _run_migrations()
    db_status = await _db_connectivity_status()
    waha_status = await _waha_connectivity_status(settings)
    _print_readiness_table(
        settings=settings,
        db_status=db_status,
        waha_status=waha_status,
        polling_enabled=not args.disable_polling,
    )

    app = create_app(enable_polling=not args.disable_polling)
    config = uvicorn.Config(
        app=app,
        host=args.host,
        port=args.port,
        log_level=(args.log_level or settings.log_level).lower(),
    )
    server = uvicorn.Server(config)
    await server.serve()
    return 0


def main() -> int:
    return asyncio.run(_async_main(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
