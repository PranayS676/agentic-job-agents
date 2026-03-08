from __future__ import annotations

import argparse
import json
from pathlib import Path


def _normalize_text(value: str) -> str:
    return " ".join(value.lower().split())


def calculate_ats_score(resume_text: str, keywords: list[str]) -> dict[str, object]:
    normalized_resume = _normalize_text(resume_text)
    normalized_keywords = []
    for keyword in keywords:
        cleaned = _normalize_text(str(keyword or ""))
        if cleaned and cleaned not in normalized_keywords:
            normalized_keywords.append(cleaned)

    if not normalized_keywords:
        return {"score": 0, "matched_keywords": [], "total_keywords": 0}

    matched_keywords: list[str] = []
    total_occurrences = 0
    for keyword in normalized_keywords:
        count = normalized_resume.count(keyword)
        if count > 0:
            matched_keywords.append(keyword)
            total_occurrences += count

    presence_ratio = len(matched_keywords) / len(normalized_keywords)
    density_ratio = min(total_occurrences / max(len(normalized_keywords), 1), 1.5) / 1.5
    score = int(round(((presence_ratio * 0.8) + (density_ratio * 0.2)) * 100))
    score = max(0, min(100, score))
    return {
        "score": score,
        "matched_keywords": matched_keywords,
        "total_keywords": len(normalized_keywords),
    }


def _load_keywords(raw_keywords: str | None, keywords_file: Path | None) -> list[str]:
    if raw_keywords:
        return [item.strip() for item in raw_keywords.split(",") if item.strip()]

    if keywords_file is None:
        return []
    if not keywords_file.is_file():
        raise FileNotFoundError(f"keywords file not found: {keywords_file}")

    content = keywords_file.read_text(encoding="utf-8").strip()
    if not content:
        return []
    if content.startswith("["):
        parsed = json.loads(content)
        if not isinstance(parsed, list):
            raise ValueError("keywords file JSON must be a list")
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [line.strip() for line in content.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Simple ATS keyword scorer")
    parser.add_argument("--resume-file", required=True, help="Path to plain-text resume file")
    parser.add_argument("--keywords", help="Comma-separated keywords")
    parser.add_argument("--keywords-file", help="Path to keywords list file (json array or newline list)")
    args = parser.parse_args()

    resume_file = Path(args.resume_file)
    if not resume_file.is_file():
        raise FileNotFoundError(f"resume file not found: {resume_file}")

    keywords_file = Path(args.keywords_file) if args.keywords_file else None
    keywords = _load_keywords(args.keywords, keywords_file)
    resume_text = resume_file.read_text(encoding="utf-8")
    result = calculate_ats_score(resume_text=resume_text, keywords=keywords)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
