from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from pypdf import PdfReader

from .contracts import ResumeTrackProfile


SECTION_ALIASES = {
    "summary": {"summary", "professional summary", "profile", "career summary"},
    "skills": {
        "skills",
        "technical skills",
        "technical skill",
        "core skills",
        "competencies",
        "technologies",
        "tools technologies",
        "tools and technologies",
        "technical expertise",
        "tech stack",
    },
    "experience": {
        "experience",
        "professional experience",
        "work experience",
        "relevant experience",
        "relevant professional experience",
    },
    "education": {"education", "academic background"},
}

KEYWORD_TAXONOMY = [
    "python",
    "java",
    "sql",
    "fastapi",
    "django",
    "flask",
    "aws",
    "gcp",
    "azure",
    "terraform",
    "kubernetes",
    "docker",
    "spark",
    "airflow",
    "dbt",
    "snowflake",
    "databricks",
    "pandas",
    "numpy",
    "scikit-learn",
    "machine learning",
    "deep learning",
    "llm",
    "rag",
    "anthropic",
    "openai",
    "langchain",
    "vector database",
    "postgresql",
    "microservices",
]


def extract_resume_text_from_pdf(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    parts: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        text = text.strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip()


def normalize_resume_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\u2022", "- ").replace("\uf0b7", "- ")
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def slugify(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in value)
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_") or "resume_track"


def detect_sections(normalized_text: str) -> dict[str, str]:
    lines = [line.strip() for line in normalized_text.splitlines()]
    sections: dict[str, list[str]] = {}
    current = "body"
    sections[current] = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            if sections[current] and sections[current][-1] != "":
                sections[current].append("")
            continue

        heading = match_heading(line)
        if heading:
            current = heading
            sections.setdefault(current, [])
            continue

        sections.setdefault(current, []).append(line)

    finalized = {
        section: "\n".join(item for item in content).strip()
        for section, content in sections.items()
        if any(item.strip() for item in content)
    }

    experience_text = finalized.get("experience")
    if experience_text:
        finalized.update(extract_experience_role_sections(experience_text))
    return finalized


def match_heading(line: str) -> str | None:
    lowered = _normalize_heading(line)
    for canonical, aliases in SECTION_ALIASES.items():
        if lowered in aliases:
            return canonical
    if len(line) <= 40 and line.isupper():
        lowered = _normalize_heading(line)
        for canonical, aliases in SECTION_ALIASES.items():
            if lowered in aliases:
                return canonical
    return None


def _normalize_heading(line: str) -> str:
    lowered = line.lower().strip(" :")
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return " ".join(lowered.split())


def extract_experience_role_sections(experience_text: str) -> dict[str, str]:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", experience_text) if block.strip()]
    if len(blocks) <= 1:
        role_blocks = _split_experience_roles_by_headers(experience_text)
        if role_blocks:
            blocks = role_blocks
    sections: dict[str, str] = {}
    for index, block in enumerate(blocks[:5]):
        if index == 0:
            section_key = "experience_recent_role"
        else:
            section_key = f"experience_prior_role_{index}"
        sections[section_key] = block
    if "experience_recent_role" not in sections:
        sections["experience_recent_role"] = experience_text
    return sections


def _split_experience_roles_by_headers(experience_text: str) -> list[str]:
    lines = [line.rstrip() for line in experience_text.splitlines()]
    blocks: list[list[str]] = []
    current: list[str] = []

    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            if current and current[-1] != "":
                current.append("")
            continue

        next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if _looks_like_role_header(line=line, next_line=next_line) and current:
            blocks.append(current)
            current = []
        current.append(line)

    if current:
        blocks.append(current)

    normalized_blocks = ["\n".join(item for item in block if item).strip() for block in blocks]
    return [block for block in normalized_blocks if block]


def _looks_like_role_header(*, line: str, next_line: str) -> bool:
    line_lower = line.lower()
    if any(token in line_lower for token in ("technologies used:", "description:", "tools/technologies:")):
        return False
    if re.search(r"\b(19|20)\d{2}\b", line) and len(line) <= 120:
        return True
    if next_line and re.search(r"\b(remote|usa|india|hyderabad|va|tx|ny|ca|present)\b", next_line.lower()):
        return True
    return False


def infer_role_bias(display_name: str, normalized_text: str) -> list[str]:
    lowered = f"{display_name}\n{normalized_text}".lower()
    bias: list[str] = []
    if any(token in lowered for token in ("ml", "machine learning", "llm", "rag", "anthropic")):
        bias.append("ai_ml")
    if any(token in lowered for token in ("data engineer", "spark", "snowflake", "airflow", "dbt")):
        bias.append("data_platform")
    if any(token in lowered for token in ("aws", "gcp", "azure", "terraform", "kubernetes")):
        bias.append("cloud_platform")
    if any(token in lowered for token in ("python", "fastapi", "django", "flask", "microservices")):
        bias.append("backend_python")
    return bias


def extract_keywords(normalized_text: str) -> list[str]:
    lowered = normalized_text.lower()
    found = [keyword for keyword in KEYWORD_TAXONOMY if keyword in lowered]
    return sorted(dict.fromkeys(found))


def build_resume_track_profile(pdf_path: Path) -> ResumeTrackProfile:
    raw_text = extract_resume_text_from_pdf(pdf_path)
    if not raw_text:
        raise ValueError(f"No text extracted from PDF: {pdf_path}")

    normalized_text = normalize_resume_text(raw_text)
    sections = detect_sections(normalized_text)
    display_name = pdf_path.stem.replace("_", " ")

    return {
        "track_id": slugify(pdf_path.stem),
        "source_pdf_path": str(pdf_path),
        "display_name": display_name,
        "raw_text": raw_text,
        "normalized_text": normalized_text,
        "sections": sections,
        "role_bias": infer_role_bias(display_name, normalized_text),
        "keywords": extract_keywords(normalized_text),
    }


def write_resume_track(profile: ResumeTrackProfile, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{profile['track_id']}.json"
    output_path.write_text(json.dumps(profile, indent=2, ensure_ascii=True), encoding="utf-8")
    return output_path


def build_and_write_resume_tracks(pdf_paths: Iterable[Path], output_dir: Path) -> list[Path]:
    written_paths: list[Path] = []
    for pdf_path in sorted(pdf_paths, key=lambda path: path.name.lower()):
        profile = build_resume_track_profile(pdf_path)
        written_paths.append(write_resume_track(profile, output_dir))
    return written_paths


def load_resume_tracks(tracks_dir: Path) -> list[ResumeTrackProfile]:
    track_files = sorted(tracks_dir.glob("*.json"), key=lambda path: path.name.lower())
    tracks: list[ResumeTrackProfile] = []
    for track_file in track_files:
        payload = json.loads(track_file.read_text(encoding="utf-8"))
        tracks.append(payload)
    return tracks
