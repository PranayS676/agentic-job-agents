from __future__ import annotations

import json
from pathlib import Path

import pytest

from job_agent_runtime.agents import resume_tracks as tracks_module


class _FakePage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakeReader:
    def __init__(self, pages: list[_FakePage]) -> None:
        self.pages = pages


@pytest.fixture
def sample_resume_text() -> str:
    return (
        "SUMMARY\n"
        "Python ML engineer focused on production AI systems.\n\n"
        "TECHNICAL SKILLS\n"
        "Python AWS LLM FastAPI\n\n"
        "RELEVANT EXPERIENCE\n"
        "Senior ML Engineer\n"
        "Built production LLM services on AWS.\n\n"
        "Data Engineer\n"
        "Built Spark pipelines.\n\n"
        "EDUCATION\n"
        "MS Computer Science"
    )


def test_build_resume_track_profile_extracts_sections(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sample_resume_text: str) -> None:
    pdf_path = tmp_path / "resume_track.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 stub")
    monkeypatch.setattr(tracks_module, "PdfReader", lambda _: _FakeReader([_FakePage(sample_resume_text)]))

    profile = tracks_module.build_resume_track_profile(pdf_path)

    assert profile["track_id"] == "resume_track"
    assert "summary" in profile["sections"]
    assert "skills" in profile["sections"]
    assert "experience_recent_role" in profile["sections"]
    assert "education" in profile["sections"]
    assert "python" in profile["keywords"]
    assert "Built production LLM services on AWS." in profile["sections"]["experience_recent_role"]


def test_build_resume_track_profile_raises_on_empty_pdf(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pdf_path = tmp_path / "resume_track.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 stub")
    monkeypatch.setattr(tracks_module, "PdfReader", lambda _: _FakeReader([_FakePage("")]))

    with pytest.raises(ValueError, match="No text extracted"):
        tracks_module.build_resume_track_profile(pdf_path)


def test_write_and_load_resume_tracks_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sample_resume_text: str) -> None:
    output_dir = tmp_path / "tracks"
    pdf_a = tmp_path / "a_resume.pdf"
    pdf_b = tmp_path / "b_resume.pdf"
    pdf_c = tmp_path / "c_resume.pdf"
    for pdf_path in (pdf_a, pdf_b, pdf_c):
        pdf_path.write_bytes(b"%PDF-1.4 stub")

    monkeypatch.setattr(tracks_module, "PdfReader", lambda _: _FakeReader([_FakePage(sample_resume_text)]))

    written = tracks_module.build_and_write_resume_tracks([pdf_c, pdf_a, pdf_b], output_dir)
    loaded = tracks_module.load_resume_tracks(output_dir)

    assert [path.name for path in written] == ["a_resume.json", "b_resume.json", "c_resume.json"]
    assert [item["track_id"] for item in loaded] == ["a_resume", "b_resume", "c_resume"]
    assert json.loads((output_dir / "a_resume.json").read_text(encoding="utf-8"))["track_id"] == "a_resume"


def test_normalize_resume_text_collapses_noise() -> None:
    normalized = tracks_module.normalize_resume_text("A\r\n\r\n\r\nB  C\t\tD")
    assert normalized == "A\n\nB C D"
