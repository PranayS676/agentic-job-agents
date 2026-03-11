from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts.evaluate_resume_editor_live import (
    _default_output_dir,
    _load_dataset,
    _select_curated_cases,
    _main as run_live_editor_eval,
)
from job_platform.config import clear_settings_cache, get_settings


ROOT_DIR = Path(__file__).resolve().parents[3]
DATASET_PATH = ROOT_DIR / "output" / "relevance_review" / "waha_last_24h_relevance_seed_dataset.json"


@pytest.mark.asyncio
@pytest.mark.live_anthropic
async def test_resume_editor_live_smoke_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.getenv("RUN_LIVE_ANTHROPIC_TESTS") != "1":
        pytest.skip("Set RUN_LIVE_ANTHROPIC_TESTS=1 to enable live Anthropic tests.")
    if not DATASET_PATH.is_file():
        pytest.skip("Local relevance seed dataset is missing under output/relevance_review/.")

    clear_settings_cache()
    try:
        settings = get_settings()
    except ValidationError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Live Anthropic settings are incomplete: {exc}")

    if settings.anthropic_api_key is None and settings.anthropic_auth_token is None:
        pytest.skip("ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN must be configured.")
    if not settings.resume_editor_model or not settings.research_model or not settings.manager_model:
        pytest.skip("Manager, research, and resume-editor models must be configured.")

    payload = _load_dataset(DATASET_PATH)
    cases = _select_curated_cases(payload, 2)
    if len(cases) < 2:
        pytest.skip("Need at least 2 curated fit/okayish cases for live editor smoke test.")

    output_dir = _default_output_dir()
    monkeypatch.setattr(
        "scripts.evaluate_resume_editor_live.parse_args",
        lambda: type(
            "Args",
            (),
            {"dataset": DATASET_PATH, "output_dir": output_dir, "limit": 2},
        )(),
    )

    exit_code = await run_live_editor_eval()
    assert exit_code == 0
    summary_path = output_dir / "summary.json"
    assert summary_path.is_file()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["completed_cases"] == 2
    assert not summary["errors"]
