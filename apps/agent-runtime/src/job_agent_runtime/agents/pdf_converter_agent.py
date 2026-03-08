from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from job_agent_runtime.agents.base_agent import BaseAgent
from job_platform.config import Settings, get_settings
from job_platform.tracer import AgentTracer

from .contracts import PDFOutput


class PDFConverterAgent(BaseAgent):
    def __init__(
        self,
        *,
        db_session: AsyncSession,
        tracer: AgentTracer,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        super().__init__(
            skill_path="skills/pdf-converter",
            model=self.settings.pdf_converter_model,
            db_session=db_session,
            tracer=tracer,
        )

    async def run(self, input_data: dict, trace_id: UUID) -> PDFOutput:  # noqa: ARG002
        docx_path = Path(str(input_data.get("docx_path") or "")).expanduser()
        if not docx_path.is_file():
            raise FileNotFoundError(f"docx_path not found: {docx_path}")

        if self.settings.output_dir is None:
            raise RuntimeError("OUTPUT_DIR is not configured")
        out_dir = self.settings.resolve_path(self.settings.output_dir) / "pdfs"
        out_dir.mkdir(parents=True, exist_ok=True)

        if self.settings.skills_dir is None:
            raise FileNotFoundError("SKILLS_DIR is not configured")
        script_path = self.settings.resolve_path(self.settings.skills_dir) / "pdf-converter" / "scripts" / "convert.py"
        if not script_path.is_file():
            raise FileNotFoundError(f"PDF converter script not found: {script_path}")

        command = [
            sys.executable,
            str(script_path),
            "--input",
            str(docx_path),
            "--outdir",
            str(out_dir),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=90,
        )
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"PDF conversion failed: {details}")

        payload = json.loads(result.stdout.strip() or "{}")
        pdf_path = Path(str(payload.get("pdf_path") or "")).expanduser()
        status = str(payload.get("status") or "").strip().lower()
        if status != "success":
            error_message = str(payload.get("error") or "unknown conversion error")
            raise RuntimeError(f"PDF conversion returned non-success status: {error_message}")
        if not pdf_path.is_file():
            raise RuntimeError(f"PDF conversion reported success but file was not found: {pdf_path}")

        return {
            "pdf_path": str(pdf_path),
            "status": "success",
        }

