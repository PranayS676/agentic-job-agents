from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


def _load_ats_module():
    script_path = Path("apps/agent-runtime/skills/resume-editor/scripts/ats_scorer.py").resolve()
    spec = importlib.util.spec_from_file_location("ats_scorer_module", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module, script_path


def test_calculate_ats_score_keyword_presence() -> None:
    module, _ = _load_ats_module()
    payload = module.calculate_ats_score(
        resume_text="Built Python and FastAPI services. Designed RAG pipeline.",
        keywords=["Python", "FastAPI", "RAG pipeline", "Anthropic SDK"],
    )
    assert isinstance(payload, dict)
    assert payload["total_keywords"] == 4
    assert "python" in payload["matched_keywords"]
    assert payload["score"] > 0


def test_ats_scorer_cli_returns_json(tmp_path: Path) -> None:
    _, script_path = _load_ats_module()
    resume_file = tmp_path / "resume.txt"
    resume_file.write_text("Python FastAPI RAG", encoding="utf-8")

    command = [
        sys.executable,
        str(script_path),
        "--resume-file",
        str(resume_file),
        "--keywords",
        "Python,FastAPI,Anthropic SDK",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0
    payload = json.loads(result.stdout.strip())
    assert "score" in payload
    assert payload["total_keywords"] == 3

