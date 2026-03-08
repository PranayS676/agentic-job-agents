from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import job_agent_runtime.main as main_module


@pytest.mark.asyncio
async def test_async_main_dry_run_path(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(
        whatsapp_group_ids_list=["GROUP1@g.us"],
        manager_model="m1",
        research_model="m2",
        resume_editor_model="m3",
        pdf_converter_model="m4",
        gmail_agent_model="m5",
        whatsapp_msg_model="m6",
        log_level="INFO",
        skills_dir="apps/agent-runtime/skills",
        resolve_path=lambda value: value,
    )
    args = SimpleNamespace(dry_run=True, log_level=None)

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "validate_agent_runtime_startup_requirements", lambda _s: None)
    monkeypatch.setattr(main_module, "configure_logging", lambda _s: None)
    monkeypatch.setattr(main_module, "_db_connectivity_status", AsyncMock(return_value="ok"))
    monkeypatch.setattr(main_module, "_waha_connectivity_status", AsyncMock(return_value="ok"))
    monkeypatch.setattr(main_module, "_print_readiness_table", lambda **kwargs: None)  # noqa: ARG005
    monkeypatch.setattr(main_module, "_run_dry_run", AsyncMock(return_value=1))
    monkeypatch.setattr(main_module, "_run_runtime", AsyncMock(return_value=0))
    monkeypatch.setattr(main_module, "engine", SimpleNamespace(dispose=AsyncMock()))

    class _FakeGmailConnector:
        def __init__(self, *, settings=None):  # noqa: ANN001
            _ = settings

        def token_status(self) -> str:
            return "expired"

    monkeypatch.setattr(main_module, "GmailConnector", _FakeGmailConnector)

    exit_code = await main_module._async_main(args)

    assert exit_code == 1
    main_module._run_dry_run.assert_awaited_once()
    assert main_module._run_runtime.await_count == 0

