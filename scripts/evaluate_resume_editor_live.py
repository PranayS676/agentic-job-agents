from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from docx import Document

from job_agent_runtime.agents.research_agent import ResearchAgent
from job_agent_runtime.agents.resume_editor_agent import ResumeEditorAgent
from job_agent_runtime.agents.stub_agents import DefaultStubAgentFactory
from job_agent_runtime.orchestration.manager import ManagerAgent
from job_platform.config import clear_settings_cache, get_settings


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT_DIR / "output" / "relevance_review" / "waha_last_24h_relevance_seed_dataset.json"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "output" / "resume_editor_review"


class _NullTracer:
    async def trace(self, **kwargs) -> None:  # noqa: ANN003, ARG002
        return None

    async def update_pipeline_status(self, trace_id, status: str, stage_data: dict) -> None:  # noqa: ANN001, ARG002
        return None


class _FakeResult:
    def __init__(self, rowcount: int = 1) -> None:
        self.rowcount = rowcount


class _FakeSession:
    async def execute(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return _FakeResult()

    async def flush(self) -> None:
        return None


@dataclass(slots=True)
class SeedCase:
    dataset_section: str
    source_candidate_index: int
    expected_decision: str
    group_id: str
    message_id: str
    timestamp: int
    body: str
    rationale: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run live ResearchAgent + ResumeEditorAgent evaluation on curated seed-data cases."
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DEFAULT_DATASET,
        help="Path to the local relevance seed dataset JSON.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory where review artifacts will be written.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=8,
        help="Maximum number of curated cases to run. Default: 8",
    )
    parser.add_argument(
        "--case-timeout-seconds",
        type=int,
        default=300,
        help="Per-case timeout for live evaluation. Default: 300 seconds.",
    )
    return parser.parse_args()


def _load_dataset(dataset_path: Path) -> dict:
    if not dataset_path.is_file():
        raise FileNotFoundError(f"Missing dataset file: {dataset_path}")
    return json.loads(dataset_path.read_text(encoding="utf-8"))


def _normalize_cases(payload: dict, section: str) -> list[SeedCase]:
    items = payload.get(section, [])
    cases: list[SeedCase] = []
    for item in items:
        cases.append(
            SeedCase(
                dataset_section=section,
                source_candidate_index=int(item["source_candidate_index"]),
                expected_decision=str(item["expected_decision"]),
                group_id=str(item["group_id"]),
                message_id=str(item["message_id"]),
                timestamp=int(item["timestamp"]),
                body=str(item["body"]),
                rationale=str(item["rationale"]),
            )
        )
    return cases


def _select_curated_cases(payload: dict, limit: int) -> list[SeedCase]:
    fit_cases = _normalize_cases(payload, "fit_examples")[:4]
    okayish_candidates = _normalize_cases(payload, "okayish_examples")

    cloud_adjacent: list[SeedCase] = []
    mismatch_adjacent: list[SeedCase] = []
    remaining: list[SeedCase] = []

    for case in okayish_candidates:
        text = f"{case.rationale}\n{case.body}".lower()
        is_cloud_adjacent = any(
            token in text for token in ("cloud", "platform", "data", "observability", "sre")
        )
        is_mismatch = any(
            token in text for token in ("java", ".net", "gcp", "azure", "aws", "stack", "adjacent")
        )
        if is_cloud_adjacent:
            cloud_adjacent.append(case)
        if is_mismatch:
            mismatch_adjacent.append(case)
        if not is_cloud_adjacent and not is_mismatch:
            remaining.append(case)

    selected: list[SeedCase] = []
    seen: set[int] = set()

    def add_case(case: SeedCase) -> None:
        if case.source_candidate_index in seen:
            return
        seen.add(case.source_candidate_index)
        selected.append(case)

    for case in fit_cases:
        add_case(case)
    for case in cloud_adjacent[:2]:
        add_case(case)
    for case in mismatch_adjacent[:2]:
        add_case(case)
    for case in okayish_candidates:
        add_case(case)
        if len(selected) >= limit:
            break
    for case in remaining:
        add_case(case)
        if len(selected) >= limit:
            break

    return selected[:limit]


def _default_output_dir() -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return DEFAULT_OUTPUT_DIR / f"run_{timestamp}"


def _load_docx_text(path: Path) -> str:
    document = Document(str(path))
    return "\n".join(paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip())


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_diff(before_text: str, after_text: str, *, from_name: str, to_name: str) -> str:
    diff = difflib.unified_diff(
        before_text.splitlines(),
        after_text.splitlines(),
        fromfile=from_name,
        tofile=to_name,
        lineterm="",
    )
    return "\n".join(diff)


def _ensure_resume_docx_tracks(settings) -> None:
    if settings.resume_docx_tracks_dir is None:
        raise RuntimeError("RESUME_DOCX_TRACKS_DIR is not configured.")
    if settings.base_resume_docx is None:
        raise RuntimeError("BASE_RESUME_DOCX is not configured.")
    if settings.resume_tracks_dir is None:
        raise RuntimeError("RESUME_TRACKS_DIR is not configured.")

    base_resume_docx = settings.resolve_path(settings.base_resume_docx)
    resume_tracks_dir = settings.resolve_path(settings.resume_tracks_dir)
    resume_docx_tracks_dir = settings.resolve_path(settings.resume_docx_tracks_dir)
    resume_docx_tracks_dir.mkdir(parents=True, exist_ok=True)
    for track_path in sorted(resume_tracks_dir.glob("*.json")):
        track_id = track_path.stem
        target_path = resume_docx_tracks_dir / f"{track_id}.docx"
        if not target_path.exists():
            shutil.copyfile(base_resume_docx, target_path)


async def _run_case(
    *,
    case: SeedCase,
    case_index: int,
    manager: ManagerAgent,
    research_agent: ResearchAgent,
    resume_editor: ResumeEditorAgent,
    output_dir: Path,
) -> dict:
    case_dir = output_dir / f"case_{case_index:02d}_{case.source_candidate_index}"
    case_dir.mkdir(parents=True, exist_ok=True)

    message = SimpleNamespace(
        group_id=case.group_id,
        sender_number="+15550000001",
        message_text=case.body,
    )

    relevance = await manager._evaluate_relevance(message=message, trace_id=uuid4())
    research_output = await research_agent.run(
        job_data={
            "job_title": relevance["job_title"],
            "company": relevance["company"],
            "job_summary": relevance["job_summary"],
            "full_job_text": case.body,
            "poster_email": relevance["poster_email"],
            "poster_number": relevance["poster_number"],
            "relevance_score": relevance["score"],
            "relevance_decision": relevance["decision"],
            "relevance_decision_score": relevance["decision_score"],
        },
        trace_id=uuid4(),
    )
    resume_output = await resume_editor.run(
        research_output=research_output,
        trace_id=uuid4(),
        job_context={
            "company": relevance["company"],
            "job_title": relevance["job_title"],
            "relevance_decision": relevance["decision"],
            "relevance_decision_score": relevance["decision_score"],
        },
        version_number=1,
    )

    source_docx_path = Path(resume_output["source_docx_path"])
    edited_docx_path = Path(resume_output["docx_path"])
    before_text = _load_docx_text(source_docx_path)
    after_text = _load_docx_text(edited_docx_path)
    diff_text = _make_diff(
        before_text,
        after_text,
        from_name=source_docx_path.name,
        to_name=edited_docx_path.name,
    )

    _write_text(case_dir / "case_input.json", json.dumps(asdict(case), indent=2))
    _write_text(case_dir / "relevance_output.json", json.dumps(relevance, indent=2))
    _write_text(case_dir / "research_output.json", json.dumps(research_output, indent=2))
    _write_text(case_dir / "resume_output.json", json.dumps(resume_output, indent=2))
    _write_text(case_dir / "before.txt", before_text)
    _write_text(case_dir / "after.txt", after_text)
    _write_text(case_dir / "diff.md", diff_text)

    return {
        "case_number": case_index,
        "source_candidate_index": case.source_candidate_index,
        "expected_decision": case.expected_decision,
        "predicted_decision": relevance["decision"],
        "job_title": relevance["job_title"],
        "company": relevance["company"],
        "selected_resume_track": research_output["selected_resume_track"],
        "source_docx_path": str(source_docx_path),
        "edited_docx_path": str(edited_docx_path),
        "changed_sections": sorted(resume_output["changes_made"]["applied_sections"].keys()),
        "hard_gaps": research_output["hard_gaps"],
        "ats_before": resume_output["ats_score_before"],
        "ats_after": resume_output["ats_score_after"],
        "manual_review": {
            "selected_track_ok": None,
            "summary_ok": None,
            "skills_ok": None,
            "scope_ok": None,
            "hard_gaps_respected": None,
            "sendable_after_review": None,
            "notes": "",
        },
        "artifact_dir": str(case_dir),
    }


async def _main() -> int:
    args = parse_args()
    payload = _load_dataset(args.dataset)
    cases = _select_curated_cases(payload, args.limit)
    if not cases:
        raise RuntimeError("No curated fit/okayish cases available in the seed dataset.")

    clear_settings_cache()
    settings = get_settings()
    if settings.anthropic_api_key is None and settings.anthropic_auth_token is None:
        raise RuntimeError("ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is required for live editor evaluation.")

    _ensure_resume_docx_tracks(settings)

    fake_session = _FakeSession()
    tracer = _NullTracer()
    manager = ManagerAgent(
        db_session=fake_session,
        tracer=tracer,
        settings=settings,
        agent_factory=DefaultStubAgentFactory(settings=settings),
    )
    research_agent = ResearchAgent(
        db_session=fake_session,
        tracer=tracer,
        settings=settings,
    )
    resume_editor = ResumeEditorAgent(
        db_session=fake_session,
        tracer=tracer,
        settings=settings,
    )

    output_dir = args.output_dir or _default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    summary_items: list[dict] = []
    errors: list[dict] = []

    def write_summary() -> None:
        summary = {
            "evaluated_at": datetime.now(UTC).isoformat(),
            "dataset_path": str(args.dataset),
            "model_family": {
                "manager_model": settings.manager_model,
                "research_model": settings.research_model,
                "resume_editor_model": settings.resume_editor_model,
            },
            "case_count": len(cases),
            "completed_cases": len(summary_items),
            "errors": errors,
            "results": summary_items,
        }
        _write_text(output_dir / "summary.json", json.dumps(summary, indent=2))

    for index, case in enumerate(cases, start=1):
        try:
            summary_items.append(
                await asyncio.wait_for(
                    _run_case(
                        case=case,
                        case_index=index,
                        manager=manager,
                        research_agent=research_agent,
                        resume_editor=resume_editor,
                        output_dir=output_dir,
                    ),
                    timeout=args.case_timeout_seconds,
                )
            )
            write_summary()
        except TimeoutError:
            errors.append(
                {
                    "case_number": index,
                    "source_candidate_index": case.source_candidate_index,
                    "error": f"TimeoutError: case exceeded {args.case_timeout_seconds} seconds",
                }
            )
            write_summary()
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {
                    "case_number": index,
                    "source_candidate_index": case.source_candidate_index,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
            write_summary()
    write_summary()

    print(f"Cases: {len(cases)}")
    print(f"Completed: {len(summary_items)}")
    print(f"Errors: {len(errors)}")
    print(f"Review artifacts: {output_dir}")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
