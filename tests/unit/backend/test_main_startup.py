from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import job_backend.main as main_module


@pytest.mark.asyncio
async def test_async_main_backend_runtime_path(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(
        whatsapp_group_ids_list=["GROUP1@g.us"],
        waha_base_url="http://localhost:3000",
        waha_session="default",
        waha_api_key="waha-test",
        log_level="INFO",
    )
    args = SimpleNamespace(host="0.0.0.0", port=8000, log_level=None, disable_polling=False)

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "validate_backend_startup_requirements", lambda _s: None)
    monkeypatch.setattr(main_module, "configure_logging", lambda _s: None)
    monkeypatch.setattr(main_module, "_run_migrations", AsyncMock())
    monkeypatch.setattr(main_module, "_db_connectivity_status", AsyncMock(return_value="ok"))
    monkeypatch.setattr(main_module, "_waha_connectivity_status", AsyncMock(return_value="ok"))
    monkeypatch.setattr(main_module, "_print_readiness_table", lambda **kwargs: None)  # noqa: ARG005
    monkeypatch.setattr(main_module, "create_app", lambda enable_polling: object())  # noqa: ARG005

    class _FakeConfig:
        def __init__(self, app, host, port, log_level):  # noqa: ANN001
            self.app = app
            self.host = host
            self.port = port
            self.log_level = log_level

    class _FakeServer:
        def __init__(self, config):  # noqa: ANN001
            self.config = config

        async def serve(self) -> None:
            return None

    monkeypatch.setattr(main_module.uvicorn, "Config", _FakeConfig)
    monkeypatch.setattr(main_module.uvicorn, "Server", _FakeServer)

    exit_code = await main_module._async_main(args)

    assert exit_code == 0
    main_module._run_migrations.assert_awaited_once()

