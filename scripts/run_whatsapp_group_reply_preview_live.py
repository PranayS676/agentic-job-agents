from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from job_agent_runtime.agents.stub_agents import DefaultStubAgentFactory
from job_agent_runtime.agents.whatsapp_msg_agent import WhatsAppMsgAgent
from job_agent_runtime.orchestration.manager import ManagerAgent
from job_integrations.waha import WAHAConnector
from job_platform.config import clear_settings_cache, get_settings


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = ROOT_DIR / "output" / "whatsapp_reply_review"


class _NullTracer:
    async def trace(self, **kwargs) -> None:  # noqa: ANN003, ARG002
        return None

    async def update_pipeline_status(self, trace_id, status: str, stage_data: dict) -> None:  # noqa: ANN001, ARG002
        return None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch the last N hours of configured WhatsApp group messages, save them, "
            "generate live reply drafts, and send the drafts to self-chat for review."
        )
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=24,
        help="How many hours of recent group messages to export. Default: 24",
    )
    parser.add_argument(
        "--group-ids",
        default="",
        help="Optional comma-separated WA group ids. Defaults to WHATSAPP_GROUP_IDS from settings.",
    )
    parser.add_argument(
        "--per-group-limit",
        type=int,
        default=500,
        help="Maximum messages fetched per group from WAHA. Default: 500",
    )
    parser.add_argument(
        "--max-messages",
        type=int,
        default=None,
        help="Optional cap on total fetched messages after sorting.",
    )
    parser.add_argument(
        "--send-decisions",
        default="fit,okayish",
        help="Comma-separated decision labels that should generate self-chat previews. Default: fit,okayish",
    )
    parser.add_argument(
        "--max-send",
        type=int,
        default=None,
        help="Optional cap on the number of preview messages sent to self-chat.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Artifact directory. Defaults to output/whatsapp_reply_review/run_<timestamp>.",
    )
    return parser.parse_args()


def _parse_csv(value: str) -> list[str]:
    seen: set[str] = set()
    items: list[str] = []
    for item in value.split(","):
        cleaned = item.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        items.append(cleaned)
    return items


def _default_output_dir() -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return DEFAULT_OUTPUT_ROOT / f"run_{timestamp}"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _timestamp_to_iso(timestamp: int) -> str | None:
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def _normalize_decision(raw_decision: Any, *, score: int) -> str:
    cleaned = str(raw_decision or "").strip().lower()
    if cleaned in {"fit", "okayish", "reject"}:
        return cleaned
    if cleaned == "relevant":
        return "fit"
    if cleaned == "borderline":
        return "okayish"
    if score <= 4:
        return "reject"
    if score <= 6:
        return "okayish"
    return "fit"


def _truncate(value: str, *, limit: int) -> str:
    cleaned = value.strip()
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: max(0, limit - 3)].rstrip()}..."


def _is_quota_exhausted_error(error_message: str) -> bool:
    lowered = error_message.lower()
    return "key limit exceeded" in lowered or "monthly limit" in lowered


def _extract_self_number(session_payload: dict[str, Any]) -> str | None:
    me = session_payload.get("me")
    if not isinstance(me, dict):
        return None
    me_id = str(me.get("id") or "").strip()
    if not me_id or "@c.us" not in me_id:
        return None
    return me_id.split("@", 1)[0]


def _extract_external_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("id", "message_id", "messageId", "msgId"):
        value = payload.get(key)
        if value:
            return str(value)
    result = payload.get("result")
    if isinstance(result, dict):
        return _extract_external_id(result)
    if isinstance(result, str) and result.strip():
        return result.strip()
    return None


def _message_dedupe_key(message: dict[str, Any]) -> tuple[str, str, int, str]:
    message_id = str(message.get("message_id") or "").strip()
    timestamp = int(message.get("timestamp") or 0)
    if message_id:
        return (str(message.get("group_id") or ""), message_id, timestamp, "")
    return (
        str(message.get("group_id") or ""),
        "",
        timestamp,
        _truncate(str(message.get("text") or ""), limit=120),
    )


def _format_self_chat_preview(index: int, total: int, record: dict[str, Any]) -> str:
    header = " | ".join(
        [
            f"Preview {index}/{total}",
            str(record.get("decision") or "unknown"),
            _truncate(str(record.get("job_title") or "Unknown Title"), limit=50),
            _truncate(str(record.get("company") or "Unknown Company"), limit=40),
        ]
    )
    reply_text = str(record.get("reply_text") or "").strip()
    return f"{header}\n\n{reply_text}".strip()


def _build_markdown_summary(report: dict[str, Any]) -> str:
    lines = [
        "# WhatsApp Reply Preview Live Run",
        "",
        f"- Run started: {report['run_started_at']}",
        f"- Run finished: {report['run_finished_at']}",
        f"- Window hours: {report['hours']}",
        f"- Groups scanned: {len(report['group_summaries'])}",
        f"- Raw messages saved: {report['message_counts']['raw_total']}",
        f"- Raw text messages: {report['message_counts']['text_total']}",
        f"- Text messages evaluated: {report['message_counts']['evaluated_total']}",
        f"- Evaluation errors: {report['message_counts']['error_total']}",
        f"- Not attempted after halt: {report['message_counts']['not_attempted_total']}",
        f"- Reply drafts generated: {report['message_counts']['draft_total']}",
        f"- Draft previews sent to self: {report['message_counts']['sent_ok']}",
        f"- Self-chat send failures: {report['message_counts']['sent_failed']}",
        "",
        "## Decisions",
        "",
    ]

    for decision in ("fit", "okayish", "reject"):
        lines.append(f"- {decision}: {report['decision_counts'].get(decision, 0)}")

    if report["fetch_errors"]:
        lines.extend(["", "## Fetch Errors", ""])
        for item in report["fetch_errors"]:
            lines.append(f"- {item['group_id']}: {item['error']}")

    if report.get("halted_reason"):
        lines.extend(["", "## Halted Reason", "", f"- {report['halted_reason']}"])

    preview_records = [
        item
        for item in report["evaluated_records"]
        if item.get("reply_text") and item.get("send_result", {}).get("ok")
    ][:10]
    if preview_records:
        lines.extend(["", "## Sent Preview Samples", ""])
        for item in preview_records:
            lines.append(
                f"- {item['decision']} | {item['job_title']} | {item['company']} | {item['timestamp_iso'] or 'unknown time'}"
            )
            lines.append(f"  {item['reply_text']}")

    return "\n".join(lines).strip() + "\n"


async def _resolve_self_number(connector: WAHAConnector) -> str:
    session_response = await connector.client.get(f"/api/sessions/{connector.session}")
    session_response.raise_for_status()
    self_number = _extract_self_number(session_response.json())
    if not self_number:
        raise RuntimeError("Unable to resolve self number from WAHA session payload.")
    return self_number


async def _list_group_names(connector: WAHAConnector) -> dict[str, str]:
    groups = await connector.list_groups()
    return {
        str(item.get("chatId") or "").strip(): str(item.get("name") or "").strip()
        for item in groups
        if str(item.get("chatId") or "").strip()
    }


async def _fetch_recent_messages(
    *,
    connector: WAHAConnector,
    group_ids: list[str],
    group_name_map: dict[str, str],
    since_timestamp: int,
    until_timestamp: int,
    per_group_limit: int,
    max_messages: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, Any]]]:
    fetches = await asyncio.gather(
        *[
            connector.get_new_messages(
                group_id=group_id,
                since_timestamp=since_timestamp,
                until_timestamp=until_timestamp,
                limit=per_group_limit,
            )
            for group_id in group_ids
        ],
        return_exceptions=True,
    )

    raw_messages: list[dict[str, Any]] = []
    fetch_errors: list[dict[str, str]] = []
    group_summaries: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int, str]] = set()

    for group_id, result in zip(group_ids, fetches, strict=False):
        group_name = group_name_map.get(group_id) or None
        if isinstance(result, Exception):
            fetch_errors.append({"group_id": group_id, "error": f"{result.__class__.__name__}: {result}"})
            group_summaries.append({"group_id": group_id, "group_name": group_name, "message_count": 0})
            continue

        count_for_group = 0
        for record in result:
            normalized = {
                "group_id": group_id,
                "group_name": group_name,
                "message_id": str(record.get("id") or "").strip(),
                "sender_number": str(record.get("sender_number") or "").strip(),
                "timestamp": int(record.get("timestamp") or 0),
                "timestamp_iso": _timestamp_to_iso(int(record.get("timestamp") or 0)),
                "text": str(record.get("text") or ""),
                "text_preview": _truncate(str(record.get("text") or ""), limit=240),
            }
            dedupe_key = _message_dedupe_key(normalized)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            raw_messages.append(normalized)
            count_for_group += 1
        group_summaries.append(
            {"group_id": group_id, "group_name": group_name, "message_count": count_for_group}
        )

    raw_messages.sort(key=lambda item: (int(item["timestamp"]), item["group_id"], item["message_id"]))
    if max_messages is not None:
        raw_messages = raw_messages[-max_messages:]

    return raw_messages, fetch_errors, group_summaries


async def _evaluate_and_send(
    *,
    settings,
    connector: WAHAConnector,
    self_number: str,
    messages: list[dict[str, Any]],
    send_decisions: set[str],
    max_send: int | None,
    output_dir: Path,
) -> tuple[list[dict[str, Any]], str | None]:
    tracer = _NullTracer()
    manager = ManagerAgent(
        db_session=SimpleNamespace(),
        tracer=tracer,
        settings=settings,
        agent_factory=DefaultStubAgentFactory(settings=settings),
    )
    whatsapp_agent = WhatsAppMsgAgent(
        db_session=SimpleNamespace(),
        tracer=tracer,
        settings=settings,
        connector=connector,
    )

    placeholder_path = output_dir / "_draft_only_placeholder.docx"
    if not placeholder_path.exists():
        placeholder_path.write_text("placeholder", encoding="utf-8")

    evaluated_records: list[dict[str, Any]] = []
    send_queue: list[dict[str, Any]] = []
    halted_reason: str | None = None

    for index, message in enumerate(messages, start=1):
        record = dict(message)
        record["source_index"] = index
        text = str(message.get("text") or "").strip()

        if not text:
            record.update(
                {
                    "status": "skipped_empty",
                    "decision": None,
                    "score": None,
                    "job_title": None,
                    "company": None,
                    "job_summary": None,
                    "poster_number": None,
                    "relevance_reason": None,
                    "reply_text": None,
                    "send_result": {"ok": False, "skipped": "empty_text"},
                }
            )
            evaluated_records.append(record)
            continue

        try:
            relevance = await manager._evaluate_relevance(
                message=SimpleNamespace(
                    group_id=record["group_id"],
                    sender_number=record["sender_number"],
                    message_text=text,
                ),
                trace_id=uuid4(),
            )
            score = int(relevance.get("score") or 0)
            decision = _normalize_decision(relevance.get("decision"), score=score)
            record.update(
                {
                    "status": "evaluated",
                    "decision": decision,
                    "score": score,
                    "job_title": str(relevance.get("job_title") or "").strip() or None,
                    "company": str(relevance.get("company") or "").strip() or None,
                    "job_summary": str(relevance.get("job_summary") or "").strip() or None,
                    "poster_number": str(relevance.get("poster_number") or "").strip() or None,
                    "poster_email": str(relevance.get("poster_email") or "").strip() or None,
                    "discard_reason": str(relevance.get("discard_reason") or "").strip() or None,
                    "relevance_reason": str(relevance.get("relevance_reason") or "").strip() or None,
                    "relevance": relevance,
                    "reply_text": None,
                    "send_result": {"ok": False, "skipped": "decision_not_selected"},
                }
            )

            if decision in send_decisions:
                draft_result = await whatsapp_agent.run(
                    context={
                        "job_title": record["job_title"],
                        "company": record["company"],
                        "job_summary": record["job_summary"],
                        "poster_number": record["poster_number"] or record["sender_number"],
                        "attachment_path": str(placeholder_path),
                        "relevance_decision": decision,
                    },
                    trace_id=uuid4(),
                    delivery_mode="draft",
                )
                reply_text = str(draft_result.get("body_preview") or "").strip()
                record["reply_text"] = reply_text
                send_queue.append(record)
        except Exception as exc:  # noqa: BLE001
            error_message = f"{exc.__class__.__name__}: {exc}"
            record.update(
                {
                    "status": "error",
                    "decision": None,
                    "score": None,
                    "job_title": None,
                    "company": None,
                    "job_summary": None,
                    "poster_number": None,
                    "relevance_reason": None,
                    "reply_text": None,
                    "error": error_message,
                    "send_result": {"ok": False, "skipped": "evaluation_error"},
                }
            )
            if _is_quota_exhausted_error(error_message):
                halted_reason = error_message
        evaluated_records.append(record)
        if halted_reason is not None:
            break

    if halted_reason is not None and len(evaluated_records) < len(messages):
        for index, message in enumerate(messages[len(evaluated_records):], start=len(evaluated_records) + 1):
            evaluated_records.append(
                {
                    **message,
                    "source_index": index,
                    "status": "not_attempted",
                    "decision": None,
                    "score": None,
                    "job_title": None,
                    "company": None,
                    "job_summary": None,
                    "poster_number": None,
                    "relevance_reason": None,
                    "reply_text": None,
                    "error": halted_reason,
                    "send_result": {"ok": False, "skipped": "batch_halted"},
                }
            )

    total_to_send = len(send_queue) if max_send is None else min(len(send_queue), max_send)
    for send_index, record in enumerate(send_queue[:total_to_send], start=1):
        self_chat_message = _format_self_chat_preview(send_index, total_to_send, record)
        send_result = await connector.send_message(self_number, self_chat_message)
        record["self_chat_message"] = self_chat_message
        record["send_result"] = {
            "ok": bool(send_result.get("ok")),
            "external_id": _extract_external_id(send_result.get("data")),
            "error": send_result.get("error"),
        }
        await asyncio.sleep(0.25)

    for record in send_queue[total_to_send:]:
        record["send_result"] = {"ok": False, "skipped": "max_send_limit"}

    return evaluated_records, halted_reason


async def _main() -> int:
    args = _parse_args()
    run_started_at = datetime.now(UTC)
    clear_settings_cache()
    settings = get_settings()

    group_ids = _parse_csv(args.group_ids) or settings.whatsapp_group_ids_list
    send_decisions = set(_parse_csv(args.send_decisions))
    invalid_send_decisions = send_decisions.difference({"fit", "okayish", "reject"})
    if invalid_send_decisions:
        invalid = ", ".join(sorted(invalid_send_decisions))
        raise ValueError(f"Unsupported send decision labels: {invalid}")

    output_dir = args.output_dir or _default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    until_dt = datetime.now(UTC)
    since_dt = until_dt - timedelta(hours=args.hours)
    since_timestamp = int(since_dt.timestamp())
    until_timestamp = int(until_dt.timestamp())

    connector = WAHAConnector(
        base_url=settings.waha_base_url,
        session=settings.waha_session,
        api_key=settings.waha_api_key,
    )
    try:
        self_number = await _resolve_self_number(connector)
        group_name_map = await _list_group_names(connector)
        raw_messages, fetch_errors, group_summaries = await _fetch_recent_messages(
            connector=connector,
            group_ids=group_ids,
            group_name_map=group_name_map,
            since_timestamp=since_timestamp,
            until_timestamp=until_timestamp,
            per_group_limit=args.per_group_limit,
            max_messages=args.max_messages,
        )
        _write_json(
            output_dir / "raw_messages.json",
            {
                "saved_at": datetime.now(UTC).isoformat(),
                "hours": args.hours,
                "since_timestamp": since_timestamp,
                "until_timestamp": until_timestamp,
                "since_iso": since_dt.isoformat(),
                "until_iso": until_dt.isoformat(),
                "group_summaries": group_summaries,
                "fetch_errors": fetch_errors,
                "messages": raw_messages,
            },
        )

        evaluated_records, halted_reason = await _evaluate_and_send(
            settings=settings,
            connector=connector,
            self_number=self_number,
            messages=raw_messages,
            send_decisions=send_decisions,
            max_send=args.max_send,
            output_dir=output_dir,
        )
    finally:
        await connector.close()
        clear_settings_cache()

    decision_counts: dict[str, int] = {}
    for item in evaluated_records:
        decision = item.get("decision")
        if not decision:
            continue
        decision_counts[decision] = decision_counts.get(decision, 0) + 1

    draft_total = sum(1 for item in evaluated_records if item.get("reply_text"))
    sent_ok = sum(1 for item in evaluated_records if item.get("send_result", {}).get("ok"))
    sent_failed = sum(
        1
        for item in evaluated_records
        if item.get("reply_text") and not item.get("send_result", {}).get("ok")
    )
    text_total = sum(1 for item in raw_messages if str(item.get("text") or "").strip())
    evaluated_total = sum(1 for item in evaluated_records if item.get("status") == "evaluated")
    error_total = sum(1 for item in evaluated_records if item.get("status") == "error")
    not_attempted_total = sum(1 for item in evaluated_records if item.get("status") == "not_attempted")

    run_finished_at = datetime.now(UTC)
    report = {
        "run_started_at": run_started_at.isoformat(),
        "run_finished_at": run_finished_at.isoformat(),
        "hours": args.hours,
        "self_number": self_number,
        "halted_reason": halted_reason,
        "group_ids": group_ids,
        "group_summaries": group_summaries,
        "fetch_errors": fetch_errors,
        "decision_counts": decision_counts,
        "message_counts": {
            "raw_total": len(raw_messages),
            "text_total": text_total,
            "evaluated_total": evaluated_total,
            "draft_total": draft_total,
            "error_total": error_total,
            "not_attempted_total": not_attempted_total,
            "sent_ok": sent_ok,
            "sent_failed": sent_failed,
        },
        "paths": {
            "output_dir": str(output_dir),
            "raw_messages": str(output_dir / "raw_messages.json"),
            "evaluated_replies": str(output_dir / "evaluated_replies.json"),
            "summary_json": str(output_dir / "summary.json"),
            "summary_markdown": str(output_dir / "summary.md"),
        },
        "evaluated_records": evaluated_records,
    }

    _write_json(output_dir / "evaluated_replies.json", evaluated_records)
    _write_json(output_dir / "summary.json", report)
    _write_markdown(output_dir / "summary.md", _build_markdown_summary(report))

    print(f"Output directory: {output_dir}")
    print(f"Raw messages: {len(raw_messages)}")
    print(f"Evaluated messages: {evaluated_total}")
    print(f"Decision counts: {json.dumps(decision_counts, sort_keys=True)}")
    print(f"Draft replies: {draft_total}")
    print(f"Sent to self: {sent_ok}")
    print(f"Send failures: {sent_failed}")
    print(f"Summary: {output_dir / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
