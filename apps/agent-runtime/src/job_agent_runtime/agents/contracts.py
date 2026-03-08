from __future__ import annotations

from typing import Protocol, TypedDict
from uuid import UUID


class RelevanceDecision(TypedDict):
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


class ResearchOutput(TypedDict):
    add_items: list[ResearchActionItem]
    remove_items: list[ResearchActionItem]
    keywords_to_inject: list[str]
    sections_to_edit: list[str]
    ats_score_estimate_before: int
    ats_score_estimate_after: int
    research_reasoning: str


class ResumeEditOutput(TypedDict):
    docx_path: str
    changes_made: dict
    ats_score_before: int
    ats_score_after: int
    evaluator_passed: bool
    evaluation_summary: str


class PDFOutput(TypedDict):
    pdf_path: str
    status: str


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


class PDFConverterAgentPort(Protocol):
    async def run(self, input_data: dict, trace_id: UUID) -> PDFOutput:
        ...


class OutboundAgentPort(Protocol):
    async def run(self, context: dict, trace_id: UUID) -> OutboundResult:
        ...


class AgentFactoryPort(Protocol):
    def create_research_agent(self) -> ResearchAgentPort:
        ...

    def create_resume_editor_agent(self) -> ResumeEditorAgentPort:
        ...

    def create_pdf_converter_agent(self) -> PDFConverterAgentPort:
        ...

    def create_gmail_agent(self) -> OutboundAgentPort:
        ...

    def create_whatsapp_agent(self) -> OutboundAgentPort:
        ...
