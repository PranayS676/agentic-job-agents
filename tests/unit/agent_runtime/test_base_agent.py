from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from job_platform.config import clear_settings_cache


def _set_required_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    skills_dir = tmp_path / "skills"
    data_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)

    (data_dir / "base_resume.docx").write_text("docx", encoding="utf-8")
    (data_dir / "base_resume.md").write_text("resume", encoding="utf-8")
    (data_dir / "credentials.json").write_text("{}", encoding="utf-8")

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
        "OUTPUT_DIR": str(output_dir),
        "SKILLS_DIR": str(skills_dir),
        "LOG_LEVEL": "INFO",
        "LOG_FORMAT": "json",
        "OTEL_ENDPOINT": "http://localhost:4317",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)


class DummyTracer:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def trace(self, **kwargs) -> None:
        self.calls.append(kwargs)


def _make_skill_folder(base: Path) -> Path:
    skill_dir = base / "skills" / "dummy-agent"
    refs = skill_dir / "references"
    refs.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("You are a test agent.", encoding="utf-8")
    (refs / "b.md").write_text("B second", encoding="utf-8")
    (refs / "a.md").write_text("A first", encoding="utf-8")
    return skill_dir


@pytest.fixture(autouse=True)
def _clean_settings_cache():
    clear_settings_cache()
    yield
    clear_settings_cache()


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeUsage:
    def __init__(self, input_tokens: int = 11, output_tokens: int = 22) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeResponse:
    def __init__(self, text: str = "hello from model") -> None:
        self.content = [_FakeTextBlock(text)]
        self.usage = _FakeUsage()

    def model_dump(self, mode: str = "json"):  # noqa: ARG002
        return {
            "content": [{"type": "text", "text": self.content[0].text}],
            "usage": {"input_tokens": self.usage.input_tokens, "output_tokens": self.usage.output_tokens},
        }


class _FakeAnthropic:
    def __init__(self, api_key: str, messages_impl=None) -> None:
        self.api_key = api_key
        if messages_impl is None:
            messages_impl = _DefaultFakeMessages()
        self.messages = messages_impl


class _DefaultFakeMessages:
    async def create(self, **kwargs):  # noqa: ARG002
        return _FakeResponse()


def _build_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, messages_impl=None):
    _set_required_env(monkeypatch, tmp_path)
    skill_dir = _make_skill_folder(tmp_path)

    import job_agent_runtime.agents.base_agent as base_agent

    monkeypatch.setattr(
        base_agent,
        "AsyncAnthropic",
        lambda *args, **kwargs: _FakeAnthropic(
            api_key=kwargs.get("api_key"),
            messages_impl=messages_impl,
        ),
    )

    class DummyAgent(base_agent.BaseAgent):
        async def run(self, input_data: dict, trace_id):  # noqa: ARG002
            return {}

    tracer = DummyTracer()
    agent = DummyAgent(skill_path=str(skill_dir), model="claude-sonnet-4-6", db_session=None, tracer=tracer)
    return agent, tracer, base_agent


def test_loads_skill_and_references_deterministically(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    agent, _, _ = _build_agent(monkeypatch, tmp_path)
    assert "You are a test agent." in agent.system_prompt
    assert "[a.md]" in agent.reference_context
    assert "[b.md]" in agent.reference_context
    assert agent.reference_context.index("[a.md]") < agent.reference_context.index("[b.md]")


def test_missing_skill_file_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_required_env(monkeypatch, tmp_path)
    skill_dir = tmp_path / "skills" / "broken-agent"
    (skill_dir / "references").mkdir(parents=True, exist_ok=True)

    import job_agent_runtime.agents.base_agent as base_agent

    monkeypatch.setattr(
        base_agent,
        "AsyncAnthropic",
        lambda *args, **kwargs: _FakeAnthropic(api_key=kwargs.get("api_key")),
    )

    class DummyAgent(base_agent.BaseAgent):
        async def run(self, input_data: dict, trace_id):  # noqa: ARG002
            return {}

    with pytest.raises(FileNotFoundError, match="SKILL.md"):
        DummyAgent(skill_path=str(skill_dir), model="claude-sonnet-4-6", db_session=None, tracer=DummyTracer())


def test_parse_json_plain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    agent, _, _ = _build_agent(monkeypatch, tmp_path)
    parsed = agent._parse_json('{"ok": true, "n": 1}')
    assert parsed == {"ok": True, "n": 1}


def test_parse_json_fenced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    agent, _, _ = _build_agent(monkeypatch, tmp_path)
    parsed = agent._parse_json("```json\n{\"ok\": true}\n```")
    assert parsed == {"ok": True}


def test_parse_json_invalid_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    agent, _, _ = _build_agent(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="not-json"):
        agent._parse_json("not-json")


@pytest.mark.asyncio
async def test_call_model_success_traces(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    agent, tracer, _ = _build_agent(monkeypatch, tmp_path, messages_impl=_DefaultFakeMessages())
    trace_id = uuid4()

    result = await agent._call_model(
        messages=[{"role": "user", "content": "hello"}],
        trace_id=trace_id,
        max_tokens=123,
    )

    assert result["text"] == "hello from model"
    assert result["input_tokens"] == 11
    assert result["output_tokens"] == 22
    assert result["model"] == "claude-sonnet-4-6"
    assert isinstance(result["latency_ms"], int)
    assert len(tracer.calls) == 1
    assert tracer.calls[0]["trace_id"] == trace_id


class _RetryableError(Exception):
    status_code = 503


class _FlakyMessages:
    def __init__(self) -> None:
        self.calls = 0

    async def create(self, **kwargs):  # noqa: ARG002
        self.calls += 1
        if self.calls < 3:
            raise _RetryableError("temporary failure")
        return _FakeResponse("eventual success")


@pytest.mark.asyncio
async def test_call_model_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    flaky = _FlakyMessages()
    agent, tracer, base_agent = _build_agent(monkeypatch, tmp_path, messages_impl=flaky)
    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(base_agent.asyncio, "sleep", _fake_sleep)
    trace_id = uuid4()
    result = await agent._call_model(messages=[{"role": "user", "content": "hello"}], trace_id=trace_id)

    assert result["text"] == "eventual success"
    assert flaky.calls == 3
    assert sleep_calls == [0.5, 1.0]
    assert len(tracer.calls) == 1


class _HardFailureMessages:
    async def create(self, **kwargs):  # noqa: ARG002
        raise ValueError("bad request")


@pytest.mark.asyncio
async def test_call_model_hard_failure_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent, tracer, _ = _build_agent(monkeypatch, tmp_path, messages_impl=_HardFailureMessages())
    trace_id = uuid4()

    with pytest.raises(RuntimeError, match="Model call failed"):
        await agent._call_model(messages=[{"role": "user", "content": "hello"}], trace_id=trace_id)

    assert len(tracer.calls) == 0


