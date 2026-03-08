from __future__ import annotations

from pathlib import Path
from uuid import UUID

from job_platform.config import Settings, get_settings

from .contracts import (
    AgentFactoryPort,
    OutboundResult,
    PDFOutput,
    ResearchOutput,
    ResumeEditOutput,
)


class StubResearchAgent:
    async def run(self, job_data: dict, trace_id: UUID) -> ResearchOutput:
        job_summary = str(job_data.get("job_summary") or "")
        add_items: list[dict[str, object]] = []
        if "python" in job_summary.lower():
            add_items.append(
                {
                    "section": "skills",
                    "action": "Highlight production Python experience",
                    "reason": "Python appears as a direct requirement in job summary",
                    "priority": 1,
                }
            )
        if "ml" in job_summary.lower() or "ai" in job_summary.lower():
            add_items.append(
                {
                    "section": "experience",
                    "action": "Emphasize machine learning project outcomes with metrics",
                    "reason": "AI/ML keywords appear in job summary",
                    "priority": 2,
                }
            )
        if not add_items:
            add_items.append(
                {
                    "section": "summary",
                    "action": "Align resume summary to role requirements",
                    "reason": "No direct keyword hits; improve role alignment",
                    "priority": 1,
                }
            )

        return {
            "add_items": add_items,
            "remove_items": [
                {
                    "section": "education",
                    "action": "De-emphasize unrelated legacy coursework bullet",
                    "reason": "Creates noise for the target role",
                }
            ],
            "keywords_to_inject": ["Python", "FastAPI", "LLM"],
            "sections_to_edit": ["Summary", "Experience", "Projects"],
            "ats_score_estimate_before": 58,
            "ats_score_estimate_after": 74,
            "research_reasoning": f"Stub research for trace {str(trace_id)[:8]}",
        }


class StubResumeEditorAgent:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._run_counter = 0

    async def run(
        self,
        research_output: ResearchOutput,
        trace_id: UUID,
        job_context: dict,
        version_number: int,
        feedback: str | None = None,
    ) -> ResumeEditOutput:
        self._run_counter += 1
        version = version_number if version_number >= 1 else self._run_counter

        out_dir = Path(self.settings.output_dir) / "resumes"
        out_dir.mkdir(parents=True, exist_ok=True)

        company = str(job_context.get("company") or "unknown")
        job_title = str(job_context.get("job_title") or "unknown")
        company_slug = _slugify(company)
        job_slug = _slugify(job_title)
        docx_path = out_dir / f"{company_slug}_{job_slug}_{str(trace_id)[:8]}_v{version}.docx"
        docx_path.write_text(
            (
                f"Stub resume content for trace={trace_id}, version={version}, "
                f"company={company}, job_title={job_title}, feedback={feedback}"
            ),
            encoding="utf-8",
        )

        research_map = dict(research_output)
        before_score = int(
            research_map.get(
                "ats_score_estimate_before",
                research_map.get("ats_score_before", 58),
            )
        )
        after_score = int(
            research_map.get(
                "ats_score_estimate_after",
                research_map.get("ats_score_after", before_score + 10),
            )
        )
        if feedback:
            after_score = max(after_score, 78)

        return {
            "docx_path": str(docx_path),
            "changes_made": {
                "applied_sections": research_map.get("sections_to_edit", []),
                "feedback": feedback,
            },
            "ats_score_before": before_score,
            "ats_score_after": after_score,
            "evaluator_passed": after_score >= 65,
            "evaluation_summary": "Stub evaluation complete",
        }


class StubPDFConverterAgent:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def run(self, input_data: dict, trace_id: UUID) -> PDFOutput:
        docx_path = Path(str(input_data["docx_path"]))
        out_dir = Path(self.settings.output_dir) / "pdfs"
        out_dir.mkdir(parents=True, exist_ok=True)

        pdf_path = out_dir / f"{docx_path.stem}_{str(trace_id)[:8]}.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n% Stub PDF output\n")

        return {
            "pdf_path": str(pdf_path),
            "status": "success",
        }


class StubGmailAgent:
    async def run(self, context: dict, trace_id: UUID) -> OutboundResult:
        body = (
            "Hello, I am interested in this opportunity and have attached my tailored resume. "
            "Please let me know if we can connect."
        )
        return {
            "sent": True,
            "channel": "email",
            "recipient": str(context["poster_email"]),
            "subject": f"Application - {context.get('job_title') or 'Role'}",
            "body_preview": body,
            "attachment_path": context.get("pdf_path"),
            "external_id": f"stub-email-{str(trace_id)[:8]}",
        }


class StubWhatsAppMsgAgent:
    async def run(self, context: dict, trace_id: UUID) -> OutboundResult:
        body = (
            f"Hi, I'm interested in the {context.get('job_title') or 'role'} opportunity. "
            "Sharing my resume for your review."
        )
        return {
            "sent": True,
            "channel": "whatsapp",
            "recipient": str(context["poster_number"]),
            "subject": None,
            "body_preview": body,
            "attachment_path": context.get("pdf_path"),
            "external_id": f"stub-whatsapp-{str(trace_id)[:8]}",
        }


class DefaultStubAgentFactory(AgentFactoryPort):
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._research = StubResearchAgent()
        self._resume_editor = StubResumeEditorAgent(self.settings)
        self._pdf_converter = StubPDFConverterAgent(self.settings)
        self._gmail = StubGmailAgent()
        self._whatsapp = StubWhatsAppMsgAgent()

    def create_research_agent(self) -> StubResearchAgent:
        return self._research

    def create_resume_editor_agent(self) -> StubResumeEditorAgent:
        return self._resume_editor

    def create_pdf_converter_agent(self) -> StubPDFConverterAgent:
        return self._pdf_converter

    def create_gmail_agent(self) -> StubGmailAgent:
        return self._gmail

    def create_whatsapp_agent(self) -> StubWhatsAppMsgAgent:
        return self._whatsapp


def _slugify(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in value)
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_") or "unknown"

