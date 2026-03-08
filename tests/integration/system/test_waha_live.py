from __future__ import annotations

import os

import httpx
import pytest
from pydantic import ValidationError

from job_integrations.waha import WAHAConnector
from job_platform.config import clear_settings_cache, get_settings


@pytest.mark.asyncio
@pytest.mark.live_waha
async def test_waha_live_api_and_group_fetch() -> None:
    if os.getenv("RUN_LIVE_WAHA_TESTS") != "1":
        pytest.skip("Set RUN_LIVE_WAHA_TESTS=1 to enable live WAHA tests.")

    clear_settings_cache()
    try:
        settings = get_settings()
    except ValidationError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Live WAHA settings are incomplete: {exc}")

    connector = WAHAConnector()
    try:
        status_response = await connector.client.get("/api/server/status", params={"session": connector.session})
        assert status_response.status_code == 200, "WAHA API is not reachable on /api/server/status"

        groups = await connector.list_groups()
        if not groups:
            pytest.skip("WAHA returned no groups. Authenticate session in WAHA dashboard and retry.")

        available_group_ids = {group.get("chatId") for group in groups if group.get("chatId")}
        configured_group = next(
            (group_id for group_id in settings.whatsapp_group_ids_list if group_id in available_group_ids),
            None,
        )
        if configured_group is None:
            pytest.skip(
                "No overlap between WHATSAPP_GROUP_IDS and WAHA groups. "
                "Update .env with valid group ids from WAHA dashboard."
            )

        messages = await connector.get_new_messages(configured_group, since_timestamp=0)
        assert isinstance(messages, list)
    except httpx.HTTPError as exc:  # pragma: no cover - environment dependent
        pytest.fail(f"Live WAHA request failed: {exc}")
    finally:
        await connector.close()



