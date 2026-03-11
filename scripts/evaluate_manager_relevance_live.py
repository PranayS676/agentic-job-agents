from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from job_agent_runtime.agents.stub_agents import DefaultStubAgentFactory
from job_agent_runtime.orchestration.manager import ManagerAgent
from job_platform.config import clear_settings_cache, get_settings


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT_DIR / "output" / "relevance_review" / "waha_last_24h_relevance_seed_dataset.json"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "output" / "relevance_review"
DATASET_SECTIONS = (
    "fit_examples",
    "reject_examples",
    "okayish_examples",
    "employment_policy_examples",
)
SECTION_ALIASES = {
    "fit_examples": ("fit_examples", "relevant_examples"),
    "reject_examples": ("reject_examples",),
    "okayish_examples": ("okayish_examples", "borderline_examples"),
    "employment_policy_examples": ("employment_policy_examples",),
}


class _NullTracer:
    async def trace(self, **kwargs) -> None:  # noqa: ANN003, ARG002
        return None

    async def update_pipeline_status(self, trace_id, status: str, stage_data: dict) -> None:  # noqa: ANN001, ARG002
        return None


@dataclass(slots=True)
class EvalCase:
    dataset_section: str
    source_candidate_index: int
    expected_decision: str
    expected_decision_score: float
    group_id: str
    message_id: str
    timestamp: int
    body: str
    rationale: str
    employment_type_hint: str


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run live Anthropic relevance evaluation against the local WAHA seed dataset."
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DEFAULT_DATASET,
        help="Path to the local relevance seed dataset JSON.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output report path. Defaults to output/relevance_review/manager_relevance_live_eval_<timestamp>.json",
    )
    parser.add_argument(
        "--sections",
        default="all",
        help="Comma-separated dataset sections to evaluate. Default: all",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional total-case limit after section filtering.",
    )
    parser.add_argument(
        "--min-accuracy",
        type=float,
        default=None,
        help="Optional minimum exact-match accuracy. Exit non-zero if not met.",
    )
    return parser.parse_args()


def _load_cases(dataset_path: Path, sections: set[str] | None, limit: int | None) -> tuple[dict, list[EvalCase]]:
    if not dataset_path.is_file():
        raise FileNotFoundError(f"Missing dataset file: {dataset_path}")

    payload = json.loads(dataset_path.read_text(encoding="utf-8"))
    cases: list[EvalCase] = []
    for section in DATASET_SECTIONS:
        if sections is not None and section not in sections:
            continue
        source_items: list[dict] = []
        for source_key in SECTION_ALIASES[section]:
            source_items.extend(payload.get(source_key, []))
        for item in source_items:
            expected_decision = _normalize_expected_decision(item)
            cases.append(
                EvalCase(
                    dataset_section=section,
                    source_candidate_index=int(item["source_candidate_index"]),
                    expected_decision=expected_decision,
                    expected_decision_score=float(
                        item.get("expected_decision_score", _decision_score_for_decision(expected_decision))
                    ),
                    group_id=str(item["group_id"]),
                    message_id=str(item["message_id"]),
                    timestamp=int(item["timestamp"]),
                    body=str(item["body"]),
                    rationale=str(item["rationale"]),
                    employment_type_hint=str(item.get("employment_type_hint") or "unknown"),
                )
            )
    if limit is not None:
        cases = cases[:limit]
    return payload, cases


def _parse_sections(value: str) -> set[str] | None:
    if value.strip().lower() == "all":
        return None
    sections = {part.strip() for part in value.split(",") if part.strip()}
    unknown = sections.difference(DATASET_SECTIONS)
    if unknown:
        raise ValueError(f"Unknown sections: {', '.join(sorted(unknown))}")
    return sections


def _normalize_expected_decision(item: dict) -> str:
    raw = str(item.get("expected_decision") or item.get("expected_label") or "").strip().lower()
    if raw == "relevant":
        return "fit"
    if raw == "borderline":
        return "okayish"
    if raw in {"fit", "okayish", "reject"}:
        return raw
    raise ValueError(f"Unsupported expected decision label: {raw!r}")


def _decision_score_for_decision(decision: str) -> float:
    if decision == "reject":
        return 0.0
    if decision == "okayish":
        return 0.5
    if decision == "fit":
        return 1.0
    raise ValueError(f"Unsupported decision: {decision}")


def _decision_score_for_prediction(decision: str) -> float:
    if decision == "reject":
        return 0.0
    if decision == "okayish":
        return 0.5
    if decision == "fit":
        return 1.0
    raise ValueError(f"Unsupported decision: {decision}")


def _bucket_decision(decision: dict) -> str:
    score = int(decision["score"])
    if score <= 4 or not bool(decision["relevant"]):
        return "reject"
    if score <= 6:
        return "okayish"
    return "fit"


async def _evaluate_cases(cases: list[EvalCase]) -> dict:
    clear_settings_cache()
    settings = get_settings()
    if settings.anthropic_api_key is None and settings.anthropic_auth_token is None:
        raise RuntimeError(
            "ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is required for live relevance evaluation."
        )
    if not settings.manager_model:
        raise RuntimeError("MANAGER_MODEL is required for live relevance evaluation.")

    manager = ManagerAgent(
        db_session=SimpleNamespace(),
        tracer=_NullTracer(),
        settings=settings,
        agent_factory=DefaultStubAgentFactory(settings=settings),
    )

    results: list[dict] = []
    for index, case in enumerate(cases, start=1):
        started = datetime.now(UTC)
        finished = datetime.now(UTC)
        try:
            decision = await manager._evaluate_relevance(
                message=SimpleNamespace(
                    group_id=case.group_id,
                    sender_number="+15550000001",
                    message_text=case.body,
                ),
                trace_id=uuid4(),
            )
            predicted_decision = _bucket_decision(decision)
            results.append(
                {
                    "case_number": index,
                    "dataset_section": case.dataset_section,
                    "source_candidate_index": case.source_candidate_index,
                    "message_id": case.message_id,
                    "group_id": case.group_id,
                    "timestamp": case.timestamp,
                    "expected_decision": case.expected_decision,
                    "expected_decision_score": case.expected_decision_score,
                    "predicted_decision": predicted_decision,
                    "predicted_decision_score": _decision_score_for_prediction(predicted_decision),
                    "exact_match": predicted_decision == case.expected_decision,
                    "employment_type_hint": case.employment_type_hint,
                    "rationale": case.rationale,
                    "decision": decision,
                    "error": None,
                    "started_at": started.isoformat(),
                    "finished_at": finished.isoformat(),
                }
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "case_number": index,
                    "dataset_section": case.dataset_section,
                    "source_candidate_index": case.source_candidate_index,
                    "message_id": case.message_id,
                    "group_id": case.group_id,
                    "timestamp": case.timestamp,
                    "expected_decision": case.expected_decision,
                    "expected_decision_score": case.expected_decision_score,
                    "predicted_decision": None,
                    "predicted_decision_score": None,
                    "exact_match": False,
                    "employment_type_hint": case.employment_type_hint,
                    "rationale": case.rationale,
                    "decision": None,
                    "error": f"{exc.__class__.__name__}: {exc}",
                    "started_at": started.isoformat(),
                    "finished_at": finished.isoformat(),
                }
            )

    total = len(results)
    exact_matches = sum(1 for item in results if item["exact_match"])
    error_cases = sum(1 for item in results if item["error"] is not None)
    completed_cases = total - error_cases
    by_section: dict[str, dict[str, int]] = {}
    for section in DATASET_SECTIONS:
        section_results = [item for item in results if item["dataset_section"] == section]
        if not section_results:
            continue
        by_section[section] = {
            "count": len(section_results),
            "exact_matches": sum(1 for item in section_results if item["exact_match"]),
            "error_cases": sum(1 for item in section_results if item["error"] is not None),
        }

    return {
        "evaluated_at": datetime.now(UTC).isoformat(),
        "model": settings.manager_model,
        "min_relevance_score": settings.min_relevance_score,
        "total_cases": total,
        "completed_cases": completed_cases,
        "error_cases": error_cases,
        "exact_matches": exact_matches,
        "accuracy": (exact_matches / total) if total else 0.0,
        "accuracy_completed_only": (exact_matches / completed_cases) if completed_cases else 0.0,
        "by_section": by_section,
        "results": results,
    }


def _default_output_path() -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return DEFAULT_OUTPUT_DIR / f"manager_relevance_live_eval_{timestamp}.json"


async def _main() -> int:
    args = _parse_args()
    sections = _parse_sections(args.sections)
    dataset_meta, cases = _load_cases(args.dataset, sections, args.limit)
    if not cases:
        raise RuntimeError("No cases selected for evaluation.")

    report = await _evaluate_cases(cases)
    report["dataset"] = {
        "path": str(args.dataset),
        "requires_human_review": bool(dataset_meta.get("requires_human_review", False)),
        "notes": dataset_meta.get("notes", []),
    }

    output_path = args.output or _default_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Model: {report['model']}")
    print(f"Cases: {report['total_cases']}")
    print(f"Completed: {report['completed_cases']}")
    print(f"Errors: {report['error_cases']}")
    print(f"Exact matches: {report['exact_matches']}")
    print(f"Accuracy: {report['accuracy']:.2%}")
    print(f"Report: {output_path}")

    if args.min_accuracy is not None and report["accuracy"] < args.min_accuracy:
        print(
            f"Accuracy {report['accuracy']:.2%} is below required minimum {args.min_accuracy:.2%}",
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
