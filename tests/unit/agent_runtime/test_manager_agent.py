from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from unittest.mock import AsyncMock

from job_agent_runtime.orchestration.manager import ManagerAgent
from job_platform.config import clear_settings_cache


def _set_required_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    skills_dir = Path.cwd() / "apps" / "agent-runtime" / "skills"
    data_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    (data_dir / "base_resume.docx").write_text("docx", encoding="utf-8")
    (data_dir / "base_resume.md").write_text("resume", encoding="utf-8")
    (data_dir / "credentials.json").write_text("{}", encoding="utf-8")

    env = {
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "MANAGER_MODEL": "claude-opus-4-6",
        "RESEARCH_MODEL": "claude-sonnet-4-6",
        "RESUME_EDITOR_MODEL": "claude-sonnet-4-6",
        "PDF_CONVERTER_MODEL": "claude-haiku-4-5-20251001",
        "GMAIL_AGENT_MODEL": "claude-sonnet-4-6",
        "WHATSAPP_MSG_MODEL": "claude-sonnet-4-6",
        "DATABASE_URL": "postgresql+asyncpg://postgres:password@localhost:5432/jobagent",
        "WAHA_BASE_URL": "http://localhost:3000",
        "WAHA_SESSION": "default",
        "WAHA_API_KEY": "waha-test",
        "WHATSAPP_GROUP_IDS": "GROUP1@g.us",
        "POLL_INTERVAL_SECONDS": "30",
        "GMAIL_CREDENTIALS_PATH": str(data_dir / "credentials.json"),
        "GMAIL_TOKEN_PATH": str(data_dir / "token.json"),
        "SENDER_EMAIL": "sender@example.com",
        "SENDER_NAME": "Pranay",
        "MIN_RELEVANCE_SCORE": "6",
        "MIN_ATS_SCORE": "65",
        "MAX_RESUME_EDIT_ITERATIONS": "2",
        "BASE_RESUME_DOCX": str(data_dir / "base_resume.docx"),
        "BASE_RESUME_TEXT": str(data_dir / "base_resume.md"),
        "OUTPUT_DIR": str(output_dir),
        "SKILLS_DIR": str(skills_dir),
        "LOG_LEVEL": "INFO",
        "LOG_FORMAT": "json",
        "OTEL_ENDPOINT": "http://localhost:4317",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)


class _Tracer:
    def __init__(self) -> None:
        self.statuses: list[str] = []

    async def trace(self, **kwargs) -> None:  # noqa: ANN003, ARG002
        return None

    async def update_pipeline_status(self, trace_id, status: str, stage_data: dict) -> None:  # noqa: ANN001, ARG002
        self.statuses.append(status)


class _FakeAnthropic:
    def __init__(self) -> None:
        self.messages = SimpleNamespace(create=AsyncMock())


@pytest.fixture(autouse=True)
def _clear_settings_cache_fixture():
    clear_settings_cache()
    yield
    clear_settings_cache()


def _build_manager(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    mode: str = "normal",
) -> tuple[ManagerAgent, _Tracer]:
    _set_required_env(monkeypatch, tmp_path)

    import job_agent_runtime.agents.base_agent as base_agent_module

    monkeypatch.setattr(base_agent_module, "AsyncAnthropic", lambda api_key: _FakeAnthropic())  # noqa: ARG005

    tracer = _Tracer()
    db_session = SimpleNamespace(execute=AsyncMock(), flush=AsyncMock())
    manager = ManagerAgent(db_session=db_session, tracer=tracer, mode=mode)
    return manager, tracer


def _sample_message(text: str = "Python ML role") -> SimpleNamespace:
    return SimpleNamespace(
        group_id="GROUP1@g.us",
        sender_number="+15550000001",
        message_text=text,
    )


def _sample_research_output() -> dict:
    return {
        "add_items": [
            {
                "section": "skills",
                "action": "Highlight production Python experience",
                "reason": "Python is a direct requirement",
                "priority": 1,
            }
        ],
        "remove_items": [
            {
                "section": "experience_old_job",
                "action": "Shorten unrelated legacy bullets",
                "reason": "Creates noise for this role",
            }
        ],
        "keywords_to_inject": ["Python"],
        "sections_to_edit": ["Summary"],
        "ats_score_estimate_before": 55,
        "ats_score_estimate_after": 75,
        "research_reasoning": "Strong role alignment after targeted edits.",
    }


@pytest.mark.asyncio
async def test_relevance_discard_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manager, tracer = _build_manager(monkeypatch, tmp_path)
    trace_id = uuid4()

    manager._evaluate_relevance = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "relevant": False,
            "score": 3,
            "job_title": "Unknown",
            "company": "Unknown",
            "job_summary": "not relevant",
            "poster_email": None,
            "poster_number": None,
            "discard_reason": "Not a technical role",
            "relevance_reason": "Not aligned",
        }
    )
    manager._persist_relevance = AsyncMock()  # type: ignore[method-assign]
    manager._run_research = AsyncMock()  # type: ignore[method-assign]

    result = await manager.run(message=_sample_message("marketing role"), trace_id=trace_id)

    assert result["action"] == "discarded"
    assert "discarded" in tracer.statuses
    assert manager._run_research.await_count == 0


@pytest.mark.asyncio
async def test_happy_path_email_routing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manager, tracer = _build_manager(monkeypatch, tmp_path)
    trace_id = uuid4()

    manager._evaluate_relevance = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "relevant": True,
            "score": 8,
            "job_title": "ML Engineer",
            "company": "Acme",
            "job_summary": "Python and ML",
            "poster_email": "recruiter@example.com",
            "poster_number": None,
            "discard_reason": None,
            "relevance_reason": "Strong match",
        }
    )
    manager._persist_relevance = AsyncMock()  # type: ignore[method-assign]
    manager._run_research = AsyncMock(  # type: ignore[method-assign]
        return_value=_sample_research_output()
    )
    manager._run_resume_edit = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            {
                "docx_path": "output/resumes/resume.docx",
                "changes_made": {"Summary": "updated"},
                "ats_score_before": 55,
                "ats_score_after": 75,
                "evaluator_passed": True,
                "evaluation_summary": "good",
            },
            uuid4(),
            1,
        )
    )
    manager._run_pdf_conversion = AsyncMock(  # type: ignore[method-assign]
        return_value={"pdf_path": "output/pdfs/resume.pdf", "status": "success"}
    )
    manager._run_quality_gate = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "passed": True,
            "model_pass": True,
            "criteria_pass": True,
            "feedback": "",
            "reason": "pass",
            "evaluator_passed": True,
            "ats_score_after": 75,
        }
    )
    manager._persist_quality_gate_result = AsyncMock()  # type: ignore[method-assign]
    manager._load_routing_context = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "job_title": "ML Engineer",
            "company": "Acme",
            "job_summary": "Python and ML",
            "poster_email": "recruiter@example.com",
            "poster_number": None,
            "pdf_path": "output/pdfs/resume.pdf",
        }
    )
    manager._run_routing = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "sent": True,
            "channel": "email",
            "recipient": "recruiter@example.com",
            "subject": "Application - ML Engineer",
            "body_preview": "hello",
            "attachment_path": "output/pdfs/resume.pdf",
            "external_id": "stub-email-123",
        }
    )
    manager._persist_outbound_result = AsyncMock()  # type: ignore[method-assign]
    manager._mark_failure = AsyncMock()  # type: ignore[method-assign]

    result = await manager.run(message=_sample_message(), trace_id=trace_id)

    assert result["action"] == "sent"
    assert result["channel"] == "email"
    assert "sent" in tracer.statuses
    assert manager._mark_failure.await_count == 0


@pytest.mark.asyncio
async def test_whatsapp_routing_when_email_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manager, _ = _build_manager(monkeypatch, tmp_path)
    trace_id = uuid4()

    manager._evaluate_relevance = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "relevant": True,
            "score": 8,
            "job_title": "Backend Engineer",
            "company": "Acme",
            "job_summary": "Python backend",
            "poster_email": None,
            "poster_number": "+15550000009",
            "discard_reason": None,
            "relevance_reason": "Match",
        }
    )
    manager._persist_relevance = AsyncMock()  # type: ignore[method-assign]
    manager._run_research = AsyncMock(  # type: ignore[method-assign]
        return_value=_sample_research_output()
    )
    manager._run_resume_edit = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            {
                "docx_path": "output/resumes/resume.docx",
                "changes_made": {},
                "ats_score_before": 50,
                "ats_score_after": 70,
                "evaluator_passed": True,
                "evaluation_summary": "good",
            },
            uuid4(),
            1,
        )
    )
    manager._run_pdf_conversion = AsyncMock(  # type: ignore[method-assign]
        return_value={"pdf_path": "output/pdfs/resume.pdf", "status": "success"}
    )
    manager._run_quality_gate = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "passed": True,
            "model_pass": True,
            "criteria_pass": True,
            "feedback": "",
            "reason": "pass",
            "evaluator_passed": True,
            "ats_score_after": 70,
        }
    )
    manager._persist_quality_gate_result = AsyncMock()  # type: ignore[method-assign]
    manager._load_routing_context = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "job_title": "Backend Engineer",
            "company": "Acme",
            "job_summary": "Python backend",
            "poster_email": None,
            "poster_number": "+15550000009",
            "pdf_path": "output/pdfs/resume.pdf",
        }
    )
    manager._run_routing = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "sent": True,
            "channel": "whatsapp",
            "recipient": "+15550000009",
            "subject": None,
            "body_preview": "hello",
            "attachment_path": "output/pdfs/resume.pdf",
            "external_id": "stub-whatsapp-123",
        }
    )
    manager._persist_outbound_result = AsyncMock()  # type: ignore[method-assign]

    result = await manager.run(message=_sample_message(), trace_id=trace_id)

    assert result["action"] == "sent"
    assert result["channel"] == "whatsapp"


@pytest.mark.asyncio
async def test_quality_gate_retry_then_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manager, tracer = _build_manager(monkeypatch, tmp_path)
    trace_id = uuid4()

    manager._evaluate_relevance = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "relevant": True,
            "score": 9,
            "job_title": "ML Engineer",
            "company": "Acme",
            "job_summary": "Python and ML",
            "poster_email": "recruiter@example.com",
            "poster_number": None,
            "discard_reason": None,
            "relevance_reason": "Strong match",
        }
    )
    manager._persist_relevance = AsyncMock()  # type: ignore[method-assign]
    manager._run_research = AsyncMock(  # type: ignore[method-assign]
        return_value=_sample_research_output()
    )
    manager._run_resume_edit = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            (
                {
                    "docx_path": "output/resumes/resume_v1.docx",
                    "changes_made": {},
                    "ats_score_before": 52,
                    "ats_score_after": 62,
                    "evaluator_passed": False,
                    "evaluation_summary": "first try failed",
                },
                uuid4(),
                1,
            ),
            (
                {
                    "docx_path": "output/resumes/resume_v2.docx",
                    "changes_made": {},
                    "ats_score_before": 62,
                    "ats_score_after": 76,
                    "evaluator_passed": True,
                    "evaluation_summary": "retry passed",
                },
                uuid4(),
                2,
            ),
        ]
    )
    manager._run_pdf_conversion = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"pdf_path": "output/pdfs/resume_v1.pdf", "status": "success"},
            {"pdf_path": "output/pdfs/resume_v2.pdf", "status": "success"},
        ]
    )
    manager._run_quality_gate = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {
                "passed": False,
                "model_pass": False,
                "criteria_pass": False,
                "feedback": "Need stronger impact",
                "reason": "fail",
                "evaluator_passed": False,
                "ats_score_after": 62,
            },
            {
                "passed": True,
                "model_pass": True,
                "criteria_pass": True,
                "feedback": "",
                "reason": "pass",
                "evaluator_passed": True,
                "ats_score_after": 76,
            },
        ]
    )
    manager._persist_quality_gate_result = AsyncMock()  # type: ignore[method-assign]
    manager._load_routing_context = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "job_title": "ML Engineer",
            "company": "Acme",
            "job_summary": "Python and ML",
            "poster_email": "recruiter@example.com",
            "poster_number": None,
            "pdf_path": "output/pdfs/resume_v2.pdf",
        }
    )
    manager._run_routing = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "sent": True,
            "channel": "email",
            "recipient": "recruiter@example.com",
            "subject": "Application",
            "body_preview": "hello",
            "attachment_path": "output/pdfs/resume_v2.pdf",
            "external_id": "stub-email-456",
        }
    )
    manager._persist_outbound_result = AsyncMock()  # type: ignore[method-assign]

    result = await manager.run(message=_sample_message(), trace_id=trace_id)

    assert result["action"] == "sent"
    assert manager._run_resume_edit.await_count == 2
    assert manager._run_quality_gate.await_count == 2
    assert "quality_retry" in tracer.statuses


@pytest.mark.asyncio
async def test_quality_gate_fails_after_retry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manager, _ = _build_manager(monkeypatch, tmp_path)
    trace_id = uuid4()

    manager._evaluate_relevance = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "relevant": True,
            "score": 9,
            "job_title": "ML Engineer",
            "company": "Acme",
            "job_summary": "Python and ML",
            "poster_email": "recruiter@example.com",
            "poster_number": None,
            "discard_reason": None,
            "relevance_reason": "Strong match",
        }
    )
    manager._persist_relevance = AsyncMock()  # type: ignore[method-assign]
    manager._run_research = AsyncMock(  # type: ignore[method-assign]
        return_value=_sample_research_output()
    )
    manager._run_resume_edit = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            (
                {
                    "docx_path": "output/resumes/resume_v1.docx",
                    "changes_made": {},
                    "ats_score_before": 50,
                    "ats_score_after": 60,
                    "evaluator_passed": False,
                    "evaluation_summary": "first fail",
                },
                uuid4(),
                1,
            ),
            (
                {
                    "docx_path": "output/resumes/resume_v2.docx",
                    "changes_made": {},
                    "ats_score_before": 60,
                    "ats_score_after": 61,
                    "evaluator_passed": False,
                    "evaluation_summary": "retry fail",
                },
                uuid4(),
                2,
            ),
        ]
    )
    manager._run_pdf_conversion = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"pdf_path": "output/pdfs/resume_v1.pdf", "status": "success"},
            {"pdf_path": "output/pdfs/resume_v2.pdf", "status": "success"},
        ]
    )
    manager._run_quality_gate = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {
                "passed": False,
                "model_pass": False,
                "criteria_pass": False,
                "feedback": "Need better ATS",
                "reason": "fail",
                "evaluator_passed": False,
                "ats_score_after": 60,
            },
            {
                "passed": False,
                "model_pass": False,
                "criteria_pass": False,
                "feedback": "Still low ATS",
                "reason": "fail",
                "evaluator_passed": False,
                "ats_score_after": 61,
            },
        ]
    )
    manager._persist_quality_gate_result = AsyncMock()  # type: ignore[method-assign]
    manager._mark_failure = AsyncMock()  # type: ignore[method-assign]

    result = await manager.run(message=_sample_message(), trace_id=trace_id)

    assert result["action"] == "failed"
    assert result["stage"] == "quality_gate"
    assert manager._mark_failure.await_count == 1


@pytest.mark.asyncio
async def test_stage_exception_marks_failure_and_reraises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manager, _ = _build_manager(monkeypatch, tmp_path)
    trace_id = uuid4()

    manager._evaluate_relevance = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "relevant": True,
            "score": 8,
            "job_title": "ML Engineer",
            "company": "Acme",
            "job_summary": "Python and ML",
            "poster_email": "recruiter@example.com",
            "poster_number": None,
            "discard_reason": None,
            "relevance_reason": "Strong match",
        }
    )
    manager._persist_relevance = AsyncMock()  # type: ignore[method-assign]
    manager._run_research = AsyncMock(side_effect=RuntimeError("research boom"))  # type: ignore[method-assign]
    manager._mark_failure = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="research boom"):
        await manager.run(message=_sample_message(), trace_id=trace_id)

    assert manager._mark_failure.await_count == 1
    failure_kwargs = manager._mark_failure.await_args.kwargs
    assert failure_kwargs["stage"] == "research"


@pytest.mark.asyncio
async def test_run_research_delegates_to_research_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manager, _ = _build_manager(monkeypatch, tmp_path)
    trace_id = uuid4()
    relevance = {
        "relevant": True,
        "score": 9,
        "job_title": "ML Engineer",
        "company": "Acme",
        "job_summary": "Python and ML",
        "poster_email": "recruiter@example.com",
        "poster_number": "+15550001234",
        "discard_reason": None,
        "relevance_reason": "Strong match",
    }
    expected = _sample_research_output()
    captured: dict = {}

    class _FakeResearchAgent:
        def __init__(self, *, db_session, tracer, settings) -> None:  # noqa: ANN001
            captured["db_session"] = db_session
            captured["tracer"] = tracer
            captured["settings"] = settings

        async def run(self, job_data: dict, trace_id):  # noqa: ANN001
            captured["job_data"] = job_data
            captured["trace_id"] = trace_id
            return expected

    import job_agent_runtime.orchestration.manager as manager_module

    monkeypatch.setattr(manager_module, "ResearchAgent", _FakeResearchAgent)
    result = await manager._run_research(relevance=relevance, trace_id=trace_id)

    assert result == expected
    assert captured["db_session"] is manager.db_session
    assert captured["tracer"] is manager.tracer
    assert captured["settings"] is manager.settings
    assert captured["trace_id"] == trace_id
    assert captured["job_data"]["job_title"] == "ML Engineer"
    assert captured["job_data"]["company"] == "Acme"


@pytest.mark.asyncio
async def test_run_resume_edit_passes_version_and_job_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager, _ = _build_manager(monkeypatch, tmp_path)
    trace_id = uuid4()
    research_output = _sample_research_output()
    expected_version = 3
    expected_resume_version_id = uuid4()
    fake_editor = SimpleNamespace(
        run=AsyncMock(
            return_value={
                "docx_path": "output/resumes/acme_ml_engineer_abcd1234_v3.docx",
                "changes_made": {},
                "ats_score_before": 60,
                "ats_score_after": 78,
                "evaluator_passed": True,
                "evaluation_summary": "ok",
            }
        )
    )

    manager._get_next_resume_version_number = AsyncMock(  # type: ignore[method-assign]
        return_value=expected_version
    )
    manager._insert_resume_version = AsyncMock(  # type: ignore[method-assign]
        return_value=expected_resume_version_id
    )
    manager.agent_factory = SimpleNamespace(create_resume_editor_agent=lambda: fake_editor)

    output, resume_version_id, version_number = await manager._run_resume_edit(
        trace_id=trace_id,
        research_output=research_output,
        job_context={"company": "Acme", "job_title": "ML Engineer"},
        feedback="tighten summary",
    )

    assert version_number == expected_version
    assert resume_version_id == expected_resume_version_id
    assert output["docx_path"].endswith("_v3.docx")
    assert fake_editor.run.await_count == 1
    run_kwargs = fake_editor.run.await_args.kwargs
    assert run_kwargs["version_number"] == expected_version
    assert run_kwargs["job_context"] == {"company": "Acme", "job_title": "ML Engineer"}
    manager._insert_resume_version.assert_awaited_once()
    assert manager._insert_resume_version.await_args.kwargs["version_number"] == expected_version


@pytest.mark.asyncio
async def test_dry_run_mode_skips_routing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manager, tracer = _build_manager(monkeypatch, tmp_path, mode="dry_run_pre_outbound")
    trace_id = uuid4()

    manager._evaluate_relevance = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "relevant": True,
            "score": 8,
            "job_title": "ML Engineer",
            "company": "Acme",
            "job_summary": "Python and ML",
            "poster_email": "recruiter@example.com",
            "poster_number": None,
            "discard_reason": None,
            "relevance_reason": "Strong match",
        }
    )
    manager._persist_relevance = AsyncMock()  # type: ignore[method-assign]
    manager._run_research = AsyncMock(return_value=_sample_research_output())  # type: ignore[method-assign]
    manager._run_resume_edit = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            {
                "docx_path": "output/resumes/resume.docx",
                "changes_made": {},
                "ats_score_before": 55,
                "ats_score_after": 75,
                "evaluator_passed": True,
                "evaluation_summary": "good",
            },
            uuid4(),
            1,
        )
    )
    manager._run_pdf_conversion = AsyncMock(  # type: ignore[method-assign]
        return_value={"pdf_path": "output/pdfs/resume.pdf", "status": "success"}
    )
    manager._run_quality_gate = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "passed": True,
            "model_pass": True,
            "criteria_pass": True,
            "feedback": "",
            "reason": "pass",
            "evaluator_passed": True,
            "ats_score_after": 75,
        }
    )
    manager._persist_quality_gate_result = AsyncMock()  # type: ignore[method-assign]
    manager._run_routing = AsyncMock()  # type: ignore[method-assign]

    result = await manager.run(message=_sample_message(), trace_id=trace_id)

    assert result["action"] == "dry_run_ready"
    assert "dry_run_ready" in tracer.statuses
    assert manager._run_routing.await_count == 0


@pytest.mark.asyncio
async def test_outbound_exception_persists_failed_outbox(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager, _ = _build_manager(monkeypatch, tmp_path)
    trace_id = uuid4()

    manager._evaluate_relevance = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "relevant": True,
            "score": 8,
            "job_title": "ML Engineer",
            "company": "Acme",
            "job_summary": "Python and ML",
            "poster_email": "recruiter@example.com",
            "poster_number": None,
            "discard_reason": None,
            "relevance_reason": "Strong match",
        }
    )
    manager._persist_relevance = AsyncMock()  # type: ignore[method-assign]
    manager._run_research = AsyncMock(return_value=_sample_research_output())  # type: ignore[method-assign]
    manager._run_resume_edit = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            {
                "docx_path": "output/resumes/resume.docx",
                "changes_made": {},
                "ats_score_before": 55,
                "ats_score_after": 75,
                "evaluator_passed": True,
                "evaluation_summary": "good",
            },
            uuid4(),
            1,
        )
    )
    manager._run_pdf_conversion = AsyncMock(  # type: ignore[method-assign]
        return_value={"pdf_path": "output/pdfs/resume.pdf", "status": "success"}
    )
    manager._run_quality_gate = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "passed": True,
            "model_pass": True,
            "criteria_pass": True,
            "feedback": "",
            "reason": "pass",
            "evaluator_passed": True,
            "ats_score_after": 75,
        }
    )
    manager._persist_quality_gate_result = AsyncMock()  # type: ignore[method-assign]
    manager._load_routing_context = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "job_title": "ML Engineer",
            "company": "Acme",
            "job_summary": "Python and ML",
            "poster_email": "recruiter@example.com",
            "poster_number": None,
            "pdf_path": "output/pdfs/resume.pdf",
        }
    )
    manager._run_routing = AsyncMock(side_effect=RuntimeError("send boom"))  # type: ignore[method-assign]
    manager._persist_outbound_result = AsyncMock()  # type: ignore[method-assign]
    manager._mark_failure = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="send boom"):
        await manager.run(message=_sample_message(), trace_id=trace_id)

    manager._persist_outbound_result.assert_awaited_once()
    outbound = manager._persist_outbound_result.await_args.kwargs["outbound_result"]
    assert outbound["sent"] is False
    assert outbound["channel"] == "email"
    assert outbound["recipient"] == "recruiter@example.com"
    assert manager._mark_failure.await_count == 1


@pytest.mark.asyncio
async def test_outbound_sent_false_marks_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager, _ = _build_manager(monkeypatch, tmp_path)
    trace_id = uuid4()

    manager._evaluate_relevance = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "relevant": True,
            "score": 8,
            "job_title": "ML Engineer",
            "company": "Acme",
            "job_summary": "Python and ML",
            "poster_email": "recruiter@example.com",
            "poster_number": None,
            "discard_reason": None,
            "relevance_reason": "Strong match",
        }
    )
    manager._persist_relevance = AsyncMock()  # type: ignore[method-assign]
    manager._run_research = AsyncMock(return_value=_sample_research_output())  # type: ignore[method-assign]
    manager._run_resume_edit = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            {
                "docx_path": "output/resumes/resume.docx",
                "changes_made": {},
                "ats_score_before": 55,
                "ats_score_after": 75,
                "evaluator_passed": True,
                "evaluation_summary": "good",
            },
            uuid4(),
            1,
        )
    )
    manager._run_pdf_conversion = AsyncMock(  # type: ignore[method-assign]
        return_value={"pdf_path": "output/pdfs/resume.pdf", "status": "success"}
    )
    manager._run_quality_gate = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "passed": True,
            "model_pass": True,
            "criteria_pass": True,
            "feedback": "",
            "reason": "pass",
            "evaluator_passed": True,
            "ats_score_after": 75,
        }
    )
    manager._persist_quality_gate_result = AsyncMock()  # type: ignore[method-assign]
    manager._load_routing_context = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "job_title": "ML Engineer",
            "company": "Acme",
            "job_summary": "Python and ML",
            "poster_email": "recruiter@example.com",
            "poster_number": None,
            "pdf_path": "output/pdfs/resume.pdf",
        }
    )
    manager._run_routing = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "sent": False,
            "channel": "email",
            "recipient": "recruiter@example.com",
            "subject": "Apply",
            "body_preview": "failed",
            "attachment_path": "output/pdfs/resume.pdf",
            "external_id": None,
        }
    )
    manager._persist_outbound_result = AsyncMock()  # type: ignore[method-assign]
    manager._mark_failure = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Outbound send failed"):
        await manager.run(message=_sample_message(), trace_id=trace_id)

    manager._persist_outbound_result.assert_awaited_once()
    assert manager._mark_failure.await_count == 1


