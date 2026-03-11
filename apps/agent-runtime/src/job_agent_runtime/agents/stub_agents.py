from __future__ import annotations

from pathlib import Path
from uuid import UUID

from job_platform.config import Settings, get_settings

from .contracts import (
    AgentFactoryPort,
    OutboundResult,
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
                    "section": "experience_recent_role",
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
                    "section": "skills",
                    "action": "De-emphasize unrelated legacy coursework bullet",
                    "reason": "Creates noise for the target role",
                }
            ],
            "keywords_to_inject": ["Python", "FastAPI", "LLM"],
            "sections_to_edit": ["summary", "skills", "experience_recent_role"],
            "ats_score_estimate_before": 58,
            "ats_score_estimate_after": 74,
            "research_reasoning": f"Stub research for trace {str(trace_id)[:8]}",
            "selected_resume_track": "stub_track_python_ml",
            "selected_resume_source_pdf": "data/resume-library/stub_track_python_ml.pdf",
            "selected_resume_match_reason": "Stub selected the strongest Python/ML-oriented profile.",
            "experience_target_section": "experience_recent_role",
            "summary_focus": "Align the summary to emphasize Python delivery, AI/ML work, and contract readiness.",
            "skills_gap_notes": ["Highlight FastAPI and LLM orchestration where already evidenced."],
            "hard_gaps": [],
            "edit_scope": ["summary", "skills", "experience_recent_role"],
        }


class StubResumeEditorAgent:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._run_counter = 0
        self._track_docx_dir = self._ensure_track_docx_dir()

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
        research_map = dict(research_output)

        out_dir = Path(self.settings.resolve_path(self.settings.output_dir)) / "resumes"
        out_dir.mkdir(parents=True, exist_ok=True)

        company = str(job_context.get("company") or "unknown")
        job_title = str(job_context.get("job_title") or "unknown")
        company_slug = _slugify(company)
        job_slug = _slugify(job_title)
        docx_path = out_dir / f"{company_slug}_{job_slug}_{str(trace_id)[:8]}_v{version}.docx"
        selected_track = str(research_map.get("selected_resume_track") or "stub_track_python_ml")
        source_docx_path = self._track_docx_dir / f"{selected_track}.docx"
        if not source_docx_path.exists():
            source_docx_path.write_text("stub source docx", encoding="utf-8")
        docx_path.write_text(
            (
                f"Stub resume content for trace={trace_id}, version={version}, "
                f"company={company}, job_title={job_title}, feedback={feedback}"
            ),
            encoding="utf-8",
        )
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
            "attachment_path": str(docx_path),
            "source_docx_path": str(source_docx_path),
            "selected_resume_track": selected_track,
            "changes_made": {
                "applied_sections": research_map.get("sections_to_edit", []),
                "feedback": feedback,
                "source_docx_bootstrapped_from_base": True,
            },
            "ats_score_before": before_score,
            "ats_score_after": after_score,
            "evaluator_passed": after_score >= 65,
            "evaluation_summary": "Stub evaluation complete",
        }

    def _ensure_track_docx_dir(self) -> Path:
        if self.settings.resume_docx_tracks_dir is not None:
            track_docx_dir = Path(self.settings.resolve_path(self.settings.resume_docx_tracks_dir))
        else:
            track_docx_dir = Path(self.settings.resolve_path(self.settings.output_dir)) / "_stub_resume_docx_tracks"
        track_docx_dir.mkdir(parents=True, exist_ok=True)
        return track_docx_dir


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
            "attachment_path": context.get("attachment_path"),
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
            "attachment_path": context.get("attachment_path"),
            "external_id": f"stub-whatsapp-{str(trace_id)[:8]}",
        }


class DefaultStubAgentFactory(AgentFactoryPort):
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._research = StubResearchAgent()
        self._resume_editor = StubResumeEditorAgent(self.settings)
        self._gmail = StubGmailAgent()
        self._whatsapp = StubWhatsAppMsgAgent()

    def create_research_agent(self) -> StubResearchAgent:
        return self._research

    def create_resume_editor_agent(self) -> StubResumeEditorAgent:
        return self._resume_editor

    def create_gmail_agent(self) -> StubGmailAgent:
        return self._gmail

    def create_whatsapp_agent(self) -> StubWhatsAppMsgAgent:
        return self._whatsapp


def _slugify(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in value)
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_") or "unknown"

