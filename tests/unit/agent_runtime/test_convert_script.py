from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_convert_module():
    script_path = Path("apps/agent-runtime/skills/pdf-converter/scripts/convert.py").resolve()
    spec = importlib.util.spec_from_file_location("convert_script_module", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_convert_docx_to_pdf_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load_convert_module()
    input_path = tmp_path / "resume.docx"
    out_dir = tmp_path / "pdfs"
    input_path.write_text("docx", encoding="utf-8")
    out_dir.mkdir(parents=True, exist_ok=True)
    expected_pdf = out_dir / "resume.pdf"
    expected_pdf.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(module, "_pick_binary", lambda: "libreoffice")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),  # noqa: ARG005
    )

    payload = module.convert_docx_to_pdf(input_path=input_path, outdir=out_dir, timeout=10)
    assert payload["status"] == "success"
    assert payload["pdf_path"] == str(expected_pdf)


def test_convert_docx_to_pdf_missing_binary_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load_convert_module()
    input_path = tmp_path / "resume.docx"
    out_dir = tmp_path / "pdfs"
    input_path.write_text("docx", encoding="utf-8")
    out_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(module.shutil, "which", lambda name: None)  # noqa: ARG005
    with pytest.raises(RuntimeError, match="LibreOffice executable not found"):
        module.convert_docx_to_pdf(input_path=input_path, outdir=out_dir)


def test_convert_docx_to_pdf_subprocess_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load_convert_module()
    input_path = tmp_path / "resume.docx"
    out_dir = tmp_path / "pdfs"
    input_path.write_text("docx", encoding="utf-8")
    out_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(module, "_pick_binary", lambda: "libreoffice")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="boom"),  # noqa: ARG005
    )

    with pytest.raises(RuntimeError, match="conversion failed"):
        module.convert_docx_to_pdf(input_path=input_path, outdir=out_dir)

