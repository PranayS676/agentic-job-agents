from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from unittest.mock import AsyncMock

from job_platform.tracer import AgentTracer


@pytest.mark.asyncio
async def test_trace_executes_insert_and_flush() -> None:
    session = SimpleNamespace(execute=AsyncMock(), flush=AsyncMock())
    tracer = AgentTracer(session)

    trace_id = uuid4()
    await tracer.trace(
        trace_id=trace_id,
        agent_name="DummyAgent",
        model="claude-sonnet-4-6",
        input_data={"prompt": "hello"},
        output_data={"text": "world"},
        tokens_in=10,
        tokens_out=20,
        latency_ms=50,
        decision_summary="ok",
    )

    assert session.execute.await_count == 1
    assert session.flush.await_count == 1
    statement = session.execute.await_args.args[0]
    assert "agent_traces" in str(statement)


@pytest.mark.asyncio
async def test_update_pipeline_status_executes_update_and_flush() -> None:
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(rowcount=1)),
        flush=AsyncMock(),
    )
    tracer = AgentTracer(session)
    trace_id = uuid4()

    await tracer.update_pipeline_status(
        trace_id=trace_id,
        status="research_done",
        stage_data={"step": "research", "ok": True},
    )

    assert session.execute.await_count == 1
    assert session.flush.await_count == 1
    statement = session.execute.await_args.args[0]
    assert "UPDATE pipeline_runs" in str(statement)


@pytest.mark.asyncio
async def test_update_pipeline_status_requires_dict_stage_data() -> None:
    session = SimpleNamespace(execute=AsyncMock(), flush=AsyncMock())
    tracer = AgentTracer(session)

    with pytest.raises(ValueError, match="stage_data must be a dict"):
        await tracer.update_pipeline_status(
            trace_id=uuid4(),
            status="research_done",
            stage_data=["not", "a", "dict"],  # type: ignore[arg-type]
        )

    assert session.execute.await_count == 0
    assert session.flush.await_count == 0


@pytest.mark.asyncio
async def test_update_pipeline_status_rejects_non_json_serializable_stage_data() -> None:
    session = SimpleNamespace(execute=AsyncMock(), flush=AsyncMock())
    tracer = AgentTracer(session)

    with pytest.raises(ValueError, match="JSON-serializable"):
        await tracer.update_pipeline_status(
            trace_id=uuid4(),
            status="research_done",
            stage_data={"bad": {1, 2, 3}},
        )

    assert session.execute.await_count == 0


@pytest.mark.asyncio
async def test_update_pipeline_status_raises_if_trace_missing() -> None:
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(rowcount=0)),
        flush=AsyncMock(),
    )
    tracer = AgentTracer(session)

    with pytest.raises(ValueError, match="pipeline_runs row not found"):
        await tracer.update_pipeline_status(
            trace_id=uuid4(),
            status="research_done",
            stage_data={"step": "research"},
        )

    assert session.flush.await_count == 0


