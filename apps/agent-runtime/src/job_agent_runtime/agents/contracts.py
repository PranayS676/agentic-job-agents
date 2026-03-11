from __future__ import annotations

from typing import Protocol, TypedDict
from uuid import UUID


class RelevanceDecision(TypedDict):
    decision: str
    decision_score: float
    relevant: bool
    score: int
    job_title: str
    company: str
    job_summary: str
    poster_email: str | None
    poster_number: str | None
    discard_reason: str | None
    relevance_reason: str | None


class ResearchActionItem(TypedDict, total=False):
    section: str
    action: str
    reason: str
    priority: int


class ResumeTrackProfile(TypedDict):
    track_id: str
    source_pdf_path: str
    display_name: str
    raw_text: str
    normalized_text: str
    sections: dict[str, str]
    role_bias: list[str]
    keywords: list[str]


class ResearchOutput(TypedDict):
    add_items: list[ResearchActionItem]
    remove_items: list[ResearchActionItem]
    keywords_to_inject: list[str]
    sections_to_edit: list[str]
    ats_score_estimate_before: int
    ats_score_estimate_after: int
    research_reasoning: str
    selected_resume_track: str
    selected_resume_source_pdf: str
    selected_resume_match_reason: str
    experience_target_section: str
    summary_focus: str
    skills_gap_notes: list[str]
    hard_gaps: list[str]
    edit_scope: list[str]


class ResumeEditOutput(TypedDict):
    docx_path: str
    attachment_path: str
    source_docx_path: str
    selected_resume_track: str
    changes_made: dict
    ats_score_before: int
    ats_score_after: int
    evaluator_passed: bool
    evaluation_summary: str

class QualityGateDecision(TypedDict):
    passed: bool
    model_pass: bool
    criteria_pass: bool
    feedback: str
    reason: str
    evaluator_passed: bool
    ats_score_after: int


class OutboundResult(TypedDict):
    sent: bool
    channel: str
    recipient: str
    subject: str | None
    body_preview: str
    attachment_path: str | None
    external_id: str | None


class ResearchAgentPort(Protocol):
    async def run(self, job_data: dict, trace_id: UUID) -> ResearchOutput:
        ...


class ResumeEditorAgentPort(Protocol):
    async def run(
        self,
        research_output: ResearchOutput,
        trace_id: UUID,
        job_context: dict,
        version_number: int,
        feedback: str | None = None,
    ) -> ResumeEditOutput:
        ...

class OutboundAgentPort(Protocol):
    async def run(self, context: dict, trace_id: UUID) -> OutboundResult:
        ...


class AgentFactoryPort(Protocol):
    def create_research_agent(self) -> ResearchAgentPort:
        ...

    def create_resume_editor_agent(self) -> ResumeEditorAgentPort:
        ...

    def create_gmail_agent(self) -> OutboundAgentPort:
        ...

    def create_whatsapp_agent(self) -> OutboundAgentPort:
        ...
