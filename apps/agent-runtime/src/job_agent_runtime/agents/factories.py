from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from job_platform.config import Settings, get_settings
from job_platform.tracer import AgentTracer

from .contracts import AgentFactoryPort
from .gmail_agent import GmailAgent
from .pdf_converter_agent import PDFConverterAgent
from .resume_editor_agent import ResumeEditorAgent
from .whatsapp_msg_agent import WhatsAppMsgAgent


class DefaultAgentFactory(AgentFactoryPort):
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        db_session: AsyncSession,
        tracer: AgentTracer,
    ) -> None:
        self.settings = settings or get_settings()
        self.db_session = db_session
        self.tracer = tracer
        self._resume_editor = ResumeEditorAgent(
            db_session=self.db_session,
            tracer=self.tracer,
            settings=self.settings,
        )
        self._pdf_converter = PDFConverterAgent(
            db_session=self.db_session,
            tracer=self.tracer,
            settings=self.settings,
        )
        self._gmail = GmailAgent(
            db_session=self.db_session,
            tracer=self.tracer,
            settings=self.settings,
        )
        self._whatsapp = WhatsAppMsgAgent(
            db_session=self.db_session,
            tracer=self.tracer,
            settings=self.settings,
        )

    def create_research_agent(self):
        # ManagerAgent currently instantiates ResearchAgent directly.
        raise NotImplementedError("Research agent is managed directly by ManagerAgent")

    def create_resume_editor_agent(self) -> ResumeEditorAgent:
        return self._resume_editor

    def create_pdf_converter_agent(self) -> PDFConverterAgent:
        return self._pdf_converter

    def create_gmail_agent(self) -> GmailAgent:
        return self._gmail

    def create_whatsapp_agent(self) -> WhatsAppMsgAgent:
        return self._whatsapp

