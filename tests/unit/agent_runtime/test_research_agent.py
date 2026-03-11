from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from job_agent_runtime.agents.research_agent import ResearchAgent
from job_platform.config import clear_settings_cache


SAMPLE_TRACKS = [
    {
        "track_id": "resume_track_python_ml",
        "source_pdf_path": "data/resume-library/python_ml.pdf",
        "display_name": "Python ML Track",
        "raw_text": "Summary\nPython ML engineer\nSkills\nPython AWS LLM\nExperience\nRecent ML role",
        "normalized_text": "Summary\nPython ML engineer\nSkills\nPython AWS LLM\nExperience\nRecent ML role",
        "sections": {
            "summary": "Python ML engineer",
            "skills": "Python\nAWS\nLLM",
            "experience_recent_role": "Built ML services on AWS.",
            "experience_prior_role_1": "Older backend role.",
            "education": "MS Computer Science",
        },
        "role_bias": ["ai_ml", "backend_python", "cloud_platform"],
        "keywords": ["python", "aws", "llm", "machine learning"],
    },
    {
        "track_id": "resume_track_data_platform",
        "source_pdf_path": "data/resume-library/data_platform.pdf",
        "display_name": "Data Platform Track",
        "raw_text": "Summary\nData engineer\nSkills\nPython Spark Airflow AWS\nExperience\nRecent data role",
        "normalized_text": "Summary\nData engineer\nSkills\nPython Spark Airflow AWS\nExperience\nRecent data role",
        "sections": {
            "summary": "Data engineer",
            "skills": "Python\nSpark\nAirflow\nAWS",
            "experience_recent_role": "Built Spark ETL pipelines.",
            "education": "MS Computer Science",
        },
        "role_bias": ["data_platform", "cloud_platform", "backend_python"],
        "keywords": ["python", "spark", "airflow", "aws"],
    },
    {
        "track_id": "resume_track_general_backend",
        "source_pdf_path": "data/resume-library/general_backend.pdf",
        "display_name": "General Backend Track",
        "raw_text": "Summary\nBackend engineer\nSkills\nPython SQL FastAPI AWS\nExperience\nRecent backend role",
        "normalized_text": "Summary\nBackend engineer\nSkills\nPython SQL FastAPI AWS\nExperience\nRecent backend role",
        "sections": {
            "summary": "Backend engineer",
            "skills": "Python\nSQL\nFastAPI\nAWS",
            "experience_recent_role": "Built backend APIs.",
            "education": "MS Computer Science",
        },
        "role_bias": ["backend_python", "cloud_platform"],
        "keywords": ["python", "sql", "fastapi", "aws"],
    },
]


def _set_required_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    create_tracks: bool = True,
) -> None:
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    skills_dir = tmp_path / "skills"
    resume_library_dir = data_dir / "resume-library"
    resume_tracks_dir = data_dir / "resume-tracks"
    data_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)
    resume_library_dir.mkdir(parents=True, exist_ok=True)
    resume_tracks_dir.mkdir(parents=True, exist_ok=True)

    (data_dir / "base_resume.docx").write_text("docx", encoding="utf-8")
    (data_dir / "base_resume.md").write_text("resume text", encoding="utf-8")
    (data_dir / "credentials.json").write_text("{}", encoding="utf-8")

    if create_tracks:
        for track in SAMPLE_TRACKS:
            (resume_tracks_dir / f"{track['track_id']}.json").write_text(
                json.dumps(track),
                encoding="utf-8",
            )

    env = {
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "MANAGER_MODEL": "claude-opus-4-6",
        "RESEARCH_MODEL": "claude-sonnet-4-6",
        "RESUME_EDITOR_MODEL": "claude-sonnet-4-6",
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
        "RESUME_LIBRARY_DIR": str(resume_library_dir),
        "RESUME_TRACKS_DIR": str(resume_tracks_dir),
        "OUTPUT_DIR": str(output_dir),
        "SKILLS_DIR": str(skills_dir),
        "LOG_LEVEL": "INFO",
        "LOG_FORMAT": "json",
        "OTEL_ENDPOINT": "http://localhost:4317",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def _create_skill_files(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "resume-research"
    references_dir = skill_dir / "references"
    references_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("# Resume Research Agent", encoding="utf-8")
    (references_dir / "research_methodology.md").write_text(
        "1. Read full JD\n2. Prioritize explicit requirements",
        encoding="utf-8",
    )


def _setup_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, create_tracks: bool = True):
    _set_required_env(monkeypatch, tmp_path, create_tracks=create_tracks)
    _create_skill_files(tmp_path)
    monkeypatch.chdir(tmp_path)

    import job_agent_runtime.agents.base_agent as base_agent_module

    monkeypatch.setattr(
        base_agent_module,
        "AsyncAnthropic",
        lambda *args, **kwargs: SimpleNamespace(messages=SimpleNamespace(create=AsyncMock())),
    )

    db_session = SimpleNamespace(execute=AsyncMock(), flush=AsyncMock())
    tracer = SimpleNamespace(trace=AsyncMock())
    agent = ResearchAgent(db_session=db_session, tracer=tracer)
    return agent, db_session, tracer


def _valid_research_payload() -> dict:
    return {
        "selected_resume_track": "resume_track_python_ml",
        "selected_resume_source_pdf": "data/resume-library/python_ml.pdf",
        "selected_resume_match_reason": "Best evidence density for Python and ML requirements.",
        "experience_target_section": "experience_recent_role",
        "summary_focus": "Reframe the summary around Python ML delivery and contract-ready impact.",
        "skills_gap_notes": ["Surface FastAPI and RAG terminology where already grounded."],
        "hard_gaps": ["Exact GCP hands-on evidence is not present; strongest cloud evidence is AWS."],
        "edit_scope": ["summary", "skills", "experience_recent_role"],
        "add_items": [
            {
                "section": "summary",
                "action": "Tighten the summary around Python ML delivery and LLM project impact.",
                "reason": "The JD emphasizes Python, ML, and production impact.",
                "priority": 1,
            },
            {
                "section": "skills",
                "action": "Add FastAPI and RAG terminology where supported by existing work.",
                "reason": "The JD includes API and RAG keywords.",
                "priority": 2,
            },
            {
                "section": "experience_recent_role",
                "action": "Strengthen one recent bullet with measurable ML service impact.",
                "reason": "The JD needs recent production ML evidence.",
                "priority": 3,
            },
        ],
        "remove_items": [
            {
                "section": "skills",
                "action": "Trim older generic tooling if it crowds out role-specific keywords.",
                "reason": "Improves relevance density.",
            }
        ],
        "keywords_to_inject": ["Python", "LLM", "RAG", "FastAPI"],
        "sections_to_edit": ["summary", "skills", "experience_recent_role"],
        "ats_score_estimate_before": 42,
        "ats_score_estimate_after": 76,
        "research_reasoning": "The Python ML track is the closest fit, but the JD still has an exact-cloud gap that must stay explicit.",
    }


@pytest.fixture(autouse=True)
def _clean_settings_cache():
    clear_settings_cache()
    yield
    clear_settings_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_text", "expected_after"),
    [
        (json.dumps(_valid_research_payload()), 76),
        (
            "```json\n" + json.dumps(_valid_research_payload()) + "\n```",
            76,
        ),
    ],
)
async def test_run_success_persists_output_and_traces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    model_text: str,
    expected_after: int,
) -> None:
    agent, db_session, tracer = _setup_agent(monkeypatch, tmp_path)
    trace_id = uuid4()
    db_session.execute.return_value = SimpleNamespace(rowcount=1)
    agent._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value={"text": model_text, "input_tokens": 10, "output_tokens": 20, "latency_ms": 3}
    )

    result = await agent.run(
        job_data={
            "job_summary": "Python LLM role",
            "full_job_text": "Senior Python ML role requiring GCP, FastAPI, and LLM systems.",
            "job_title": "Senior ML Engineer",
            "relevance_decision": "fit",
            "relevance_decision_score": 1.0,
            "relevance_score": 8,
        },
        trace_id=trace_id,
    )

    assert result["ats_score_estimate_after"] == expected_after
    assert result["selected_resume_track"] == "resume_track_python_ml"
    assert result["experience_target_section"] == "experience_recent_role"
    assert result["edit_scope"] == ["summary", "skills", "experience_recent_role"]
    assert db_session.execute.await_count == 1
    assert db_session.flush.await_count == 1
    params = db_session.execute.await_args.args[1]
    assert params["trace_id"] == trace_id
    persisted = json.loads(params["research_output"])
    assert persisted["selected_resume_track"] == "resume_track_python_ml"
    assert tracer.trace.await_count == 1
    assert "track=resume_track_python_ml" in tracer.trace.await_args.kwargs["decision_summary"]


@pytest.mark.asyncio
async def test_run_invalid_payload_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent, _, _ = _setup_agent(monkeypatch, tmp_path)
    trace_id = uuid4()
    agent._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value={"text": json.dumps({"add_items": []})}
    )

    with pytest.raises(ValueError, match="missing required keys"):
        await agent.run(job_data={"job_summary": "Python role", "full_job_text": "Python role"}, trace_id=trace_id)


@pytest.mark.asyncio
async def test_run_missing_track_files_raises_file_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent, _, _ = _setup_agent(monkeypatch, tmp_path, create_tracks=False)
    trace_id = uuid4()
    agent._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value={"text": json.dumps(_valid_research_payload())}
    )

    with pytest.raises(FileNotFoundError, match="Expected at least 3 resume track files"):
        await agent.run(job_data={"job_summary": "Python role", "full_job_text": "Python role"}, trace_id=trace_id)


@pytest.mark.asyncio
async def test_run_missing_pipeline_row_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent, db_session, _ = _setup_agent(monkeypatch, tmp_path)
    trace_id = uuid4()
    db_session.execute.return_value = SimpleNamespace(rowcount=0)
    agent._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value={"text": json.dumps(_valid_research_payload())}
    )

    with pytest.raises(ValueError, match="pipeline_runs row not found"):
        await agent.run(job_data={"job_summary": "Python role", "full_job_text": "Python role"}, trace_id=trace_id)


@pytest.mark.asyncio
async def test_run_builds_narrow_prompt_for_okayish_relevance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent, db_session, _ = _setup_agent(monkeypatch, tmp_path)
    trace_id = uuid4()
    db_session.execute.return_value = SimpleNamespace(rowcount=1)
    agent._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value={"text": json.dumps(_valid_research_payload())}
    )

    await agent.run(
        job_data={
            "job_summary": "Cloud-adjacent AI role",
            "full_job_text": "Cloud-adjacent AI role requiring Python and AWS.",
            "job_title": "Platform Engineer",
            "relevance_decision": "okayish",
            "relevance_decision_score": 0.5,
            "relevance_score": 6,
        },
        trace_id=trace_id,
    )

    prompt = agent._call_model.await_args.kwargs["messages"][-1]["content"]
    assert "Keep the edit plan narrower" in prompt


def test_rank_resume_tracks_prefers_exact_keyword_overlap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent, _, _ = _setup_agent(monkeypatch, tmp_path)
    ranked = agent._rank_resume_tracks(
        job_data={
            "job_title": "ML Engineer",
            "job_summary": "Python and LLM role",
            "full_job_text": "Python, LLM, RAG, and AWS experience required.",
        },
        resume_tracks=SAMPLE_TRACKS,
    )

    assert ranked[0]["track_id"] == "resume_track_python_ml"


def test_coerce_research_output_rejects_excess_experience_actions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent, _, _ = _setup_agent(monkeypatch, tmp_path)
    payload = _valid_research_payload()
    payload["add_items"].append(
        {
            "section": "experience_recent_role",
            "action": "Add another role bullet.",
            "reason": "More emphasis.",
            "priority": 4,
        }
    )
    payload["add_items"].append(
        {
            "section": "experience_recent_role",
            "action": "Add a third role bullet.",
            "reason": "Too many role edits.",
            "priority": 5,
        }
    )

    with pytest.raises(ValueError, match="exceeds max 2 actions"):
        agent._coerce_research_output(payload, resume_tracks=SAMPLE_TRACKS, job_data={"full_job_text": "Python role"})


def test_coerce_research_output_derives_exact_cloud_gap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent, _, _ = _setup_agent(monkeypatch, tmp_path)
    payload = _valid_research_payload()
    payload["hard_gaps"] = []

    result = agent._coerce_research_output(
        payload,
        resume_tracks=SAMPLE_TRACKS,
        job_data={"full_job_text": "Senior ML Engineer requiring GCP and Python."},
    )

    assert any("GCP" in gap for gap in result["hard_gaps"])
