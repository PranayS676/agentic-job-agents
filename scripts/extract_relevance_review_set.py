from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


JOB_KEYWORDS = (
    "c2c",
    "w2",
    "contract",
    "contract-to-hire",
    "c2h",
    "project",
    "python",
    "ai",
    "ml",
    "llm",
    "machine learning",
    "data scientist",
    "data engineer",
    "ml engineer",
    "ai engineer",
    "developer",
    "engineer",
    "fastapi",
    "rag",
    "databricks",
    "pyspark",
    "remote",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract a manager-relevance review set from a WAHA export JSON file."
    )
    parser.add_argument("--input", required=True, help="Path to WAHA export JSON")
    parser.add_argument(
        "--output",
        required=True,
        help="Path to output JSON file containing flattened review candidates",
    )
    parser.add_argument(
        "--per-group-limit",
        type=int,
        default=30,
        help="Maximum candidate messages to keep per WhatsApp group",
    )
    parser.add_argument(
        "--min-keyword-score",
        type=int,
        default=2,
        help="Minimum heuristic keyword score to keep a message as a candidate",
    )
    return parser.parse_args()


def load_export(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("WAHA export must be a JSON object")
    if not isinstance(payload.get("messages"), dict):
        raise ValueError("WAHA export must contain a 'messages' mapping")
    return payload


def keyword_score(text: str) -> int:
    lower_text = text.lower()
    return sum(1 for keyword in JOB_KEYWORDS if keyword in lower_text)


def flatten_candidates(
    payload: dict[str, Any],
    *,
    per_group_limit: int,
    min_keyword_score: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    type_counts: Counter[str] = Counter()
    per_group_kept: Counter[str] = Counter()
    nonempty_count = 0
    skipped_empty = 0

    for group_id, messages in payload["messages"].items():
        if not isinstance(messages, list):
            continue

        for item in messages:
            if not isinstance(item, dict):
                continue

            body = str(item.get("body") or "").strip()
            if not body:
                skipped_empty += 1
                continue

            nonempty_count += 1
            raw_data = item.get("_data") or {}
            msg_type = str(raw_data.get("type") or item.get("source") or "unknown")
            type_counts[msg_type] += 1

            score = keyword_score(body)
            if score < min_keyword_score:
                continue

            if per_group_kept[group_id] >= per_group_limit:
                continue

            per_group_kept[group_id] += 1
            candidates.append(
                {
                    "group_id": group_id,
                    "message_id": item.get("id"),
                    "timestamp": item.get("timestamp"),
                    "message_type": msg_type,
                    "sender": ((raw_data.get("author") or raw_data.get("sender")) if isinstance(raw_data, dict) else None),
                    "body": body,
                    "heuristic_keyword_score": score,
                    "label": None,
                    "label_reason": None,
                }
            )

    candidates.sort(
        key=lambda item: (
            item["group_id"],
            -(item["heuristic_keyword_score"] or 0),
            -(item["timestamp"] or 0),
        )
    )

    summary = {
        "exported_at": payload.get("exported_at"),
        "window_hours": payload.get("window_hours"),
        "groups_in_export": len(payload["messages"]),
        "nonempty_messages": nonempty_count,
        "candidate_messages": len(candidates),
        "skipped_empty_messages": skipped_empty,
        "message_type_counts": dict(type_counts.most_common()),
        "per_group_candidate_counts": dict(per_group_kept),
        "min_keyword_score": min_keyword_score,
        "per_group_limit": per_group_limit,
    }
    return candidates, summary


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)

    payload = load_export(input_path)
    candidates, summary = flatten_candidates(
        payload,
        per_group_limit=args.per_group_limit,
        min_keyword_score=args.min_keyword_score,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "summary": summary,
        "candidates": candidates,
    }
    output_path.write_text(json.dumps(output, ensure_ascii=True, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=True, indent=2))
    print(f"wrote_review_set={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
