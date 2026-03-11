from __future__ import annotations

import json
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from job_platform.config import Settings, get_settings
from job_platform.database import AsyncSessionLocal
from job_platform.models import WhatsAppMessage
from job_platform.tracer import AgentTracer

from ..agents.base_agent import BaseAgent
from ..agents.contracts import (
    AgentFactoryPort,
    OutboundResult,
    QualityGateDecision,
    RelevanceDecision,
    ResearchOutput,
    ResumeEditOutput,
)
from ..agents.factories import DefaultAgentFactory
from ..agents.research_agent import ResearchAgent


class ManagerAgent(BaseAgent):
    def __init__(
        self,
        *,
        db_session: AsyncSession,
        tracer: AgentTracer,
        agent_factory: AgentFactoryPort | None = None,
        settings: Settings | None = None,
        mode: Literal["normal", "dry_run_pre_outbound"] = "normal",
    ) -> None:
        self.settings = settings or get_settings()
        if mode not in {"normal", "dry_run_pre_outbound"}:
            raise ValueError("mode must be 'normal' or 'dry_run_pre_outbound'")
        self.mode: Literal["normal", "dry_run_pre_outbound"] = mode
        self.agent_factory = agent_factory or DefaultAgentFactory(
            settings=self.settings,
            db_session=db_session,
            tracer=tracer,
        )
        super().__init__(
            skill_path="skills/job-manager",
            model=self.settings.manager_model,
            db_session=db_session,
            tracer=tracer,
        )

    async def run(self, message: WhatsAppMessage, trace_id: UUID) -> dict[str, Any]:
        stage = "relevance_evaluation"
        resume_output: ResumeEditOutput | None = None
        quality_attempts: list[QualityGateDecision] = []

        try:
            relevance = await self._evaluate_relevance(message=message, trace_id=trace_id)
            decision_label = self._decision_label_from_relevance(relevance)
            decision_score = self._decision_score_from_label(decision_label)
            await self._persist_relevance(trace_id=trace_id, relevance=relevance)
            await self.tracer.update_pipeline_status(
                trace_id=trace_id,
                status="relevance_done",
                stage_data={
                    "decision": decision_label,
                    "decision_score": decision_score,
                    "score": relevance["score"],
                    "relevant": relevance["relevant"],
                    "job_title": relevance["job_title"],
                    "company": relevance["company"],
                },
            )

            if decision_label == "reject":
                reason = relevance["discard_reason"] or "Below relevance threshold"
                await self.tracer.update_pipeline_status(
                    trace_id=trace_id,
                    status="discarded",
                    stage_data={
                        "reason": reason,
                        "score": relevance["score"],
                        "decision": decision_label,
                        "decision_score": decision_score,
                    },
                )
                return {
                    "trace_id": str(trace_id),
                    "action": "discarded",
                    "reason": reason,
                    "decision": decision_label,
                    "decision_score": decision_score,
                    "relevance_score": relevance["score"],
                }

            stage = "research"
            research_output = await self._run_research(
                message=message,
                relevance=relevance,
                trace_id=trace_id,
            )
            await self.tracer.update_pipeline_status(
                trace_id=trace_id,
                status="research_done",
                stage_data={"add_items": len(research_output["add_items"])},
            )

            stage = "resume_edit"
            resume_output, resume_version_id, version_number = await self._run_resume_edit(
                trace_id=trace_id,
                research_output=research_output,
                job_context={
                    "company": relevance["company"],
                    "job_title": relevance["job_title"],
                    "relevance_decision": decision_label,
                    "relevance_decision_score": decision_score,
                },
                feedback=None,
            )
            attachment_path = self._resume_attachment_path(resume_output)
            await self.tracer.update_pipeline_status(
                trace_id=trace_id,
                status="resume_ready",
                stage_data={
                    "version_number": version_number,
                    "docx_path": resume_output["docx_path"],
                    "attachment_path": attachment_path,
                    "ats_score_after": resume_output["ats_score_after"],
                    "evaluator_passed": resume_output["evaluator_passed"],
                },
            )

            stage = "quality_gate"
            quality_decision = await self._run_quality_gate(
                trace_id=trace_id,
                resume_output=resume_output,
                attempt=0,
            )
            quality_attempts.append(quality_decision)
            retry_count = 0

            if not quality_decision["passed"] and retry_count < 1:
                retry_count += 1
                await self.tracer.update_pipeline_status(
                    trace_id=trace_id,
                    status="quality_retry",
                    stage_data={"retry_count": retry_count, "feedback": quality_decision["feedback"]},
                )

                stage = "resume_edit_retry"
                resume_output, resume_version_id, version_number = await self._run_resume_edit(
                    trace_id=trace_id,
                    research_output=research_output,
                    job_context={
                        "company": relevance["company"],
                        "job_title": relevance["job_title"],
                        "relevance_decision": decision_label,
                        "relevance_decision_score": decision_score,
                    },
                    feedback=quality_decision["feedback"],
                )
                attachment_path = self._resume_attachment_path(resume_output)
                await self.tracer.update_pipeline_status(
                    trace_id=trace_id,
                    status="resume_ready",
                    stage_data={
                        "version_number": version_number,
                        "docx_path": resume_output["docx_path"],
                        "attachment_path": attachment_path,
                        "ats_score_after": resume_output["ats_score_after"],
                        "evaluator_passed": resume_output["evaluator_passed"],
                        "retry_count": retry_count,
                    },
                )

                stage = "quality_gate_retry"
                quality_decision = await self._run_quality_gate(
                    trace_id=trace_id,
                    resume_output=resume_output,
                    attempt=retry_count,
                )
                quality_attempts.append(quality_decision)

            await self._persist_quality_gate_result(
                trace_id=trace_id,
                quality_result={"attempts": quality_attempts, "final": quality_decision},
            )

            if not quality_decision["passed"]:
                error_message = (
                    f"Quality gate failed after retry. feedback={quality_decision['feedback']}, "
                    f"ats_score_after={quality_decision['ats_score_after']}"
                )
                await self._mark_failure(
                    trace_id=trace_id,
                    stage="quality_gate",
                    error_message=error_message,
                )
                return {
                    "trace_id": str(trace_id),
                    "action": "failed",
                    "stage": "quality_gate",
                    "error_message": error_message,
                }

            if self.mode == "dry_run_pre_outbound":
                await self.tracer.update_pipeline_status(
                    trace_id=trace_id,
                    status="dry_run_ready",
                    stage_data={
                        "channel": "skipped",
                        "reason": "dry_run_pre_outbound",
                        "job_title": relevance["job_title"],
                        "company": relevance["company"],
                    },
                )
                return {
                    "trace_id": str(trace_id),
                    "action": "dry_run_ready",
                    "job_title": relevance["job_title"],
                    "company": relevance["company"],
                    "quality_gate": quality_decision,
                }

            stage = "routing"
            routing_context = await self._load_routing_context(trace_id=trace_id)
            routing_context["relevance_decision"] = decision_label
            delivery_mode = self._select_delivery_mode(routing_context)
            try:
                outbound_result = await self._run_routing(
                    trace_id=trace_id,
                    context=routing_context,
                    delivery_mode=delivery_mode,
                )
            except Exception as exc:
                failed_outbound = self._build_failed_outbound_result(
                    context=routing_context,
                    error_message=f"{exc.__class__.__name__}: {exc}",
                )
                await self._persist_outbound_result(
                    trace_id=trace_id,
                    outbound_result=failed_outbound,
                    outbox_status="failed",
                )
                raise

            outbox_status = self._resolve_outbox_status(
                outbound_result=outbound_result,
                delivery_mode=delivery_mode,
            )
            await self._persist_outbound_result(
                trace_id=trace_id,
                outbound_result=outbound_result,
                outbox_status=outbox_status,
            )

            if outbox_status == "review_required":
                await self.tracer.update_pipeline_status(
                    trace_id=trace_id,
                    status="review_required",
                    stage_data={
                        "channel": outbound_result["channel"],
                        "recipient": outbound_result["recipient"],
                        "delivery_mode": delivery_mode,
                    },
                )
                return {
                    "trace_id": str(trace_id),
                    "action": "review_required",
                    "channel": outbound_result["channel"],
                    "recipient": outbound_result["recipient"],
                    "job_title": relevance["job_title"],
                    "company": relevance["company"],
                    "quality_gate": quality_decision,
                }

            if not outbound_result["sent"]:
                raise RuntimeError(
                    "Outbound send failed "
                    f"(channel={outbound_result['channel']}, recipient={outbound_result['recipient']})"
                )

            await self.tracer.update_pipeline_status(
                trace_id=trace_id,
                status="sent",
                stage_data={
                    "channel": outbound_result["channel"],
                    "recipient": outbound_result["recipient"],
                    "external_id": outbound_result["external_id"],
                },
            )

            return {
                "trace_id": str(trace_id),
                "action": "sent",
                "channel": outbound_result["channel"],
                "recipient": outbound_result["recipient"],
                "job_title": relevance["job_title"],
                "company": relevance["company"],
                "quality_gate": quality_decision,
            }
        except Exception as exc:
            await self._mark_failure(
                trace_id=trace_id,
                stage=stage,
                error_message=f"{exc.__class__.__name__}: {exc}",
            )
            raise

    async def _evaluate_relevance(self, message: WhatsAppMessage, trace_id: UUID) -> RelevanceDecision:
        user_prompt = (
            "Evaluate whether this WhatsApp message is relevant to a Python/AI/ML/Data role.\n"
            "Use exactly three decision buckets based on score: reject=0-4, okayish=5-6, fit=7-10.\n"
            "Return strict JSON with keys: decision, relevant, score, job_title, company, job_summary, "
            "poster_email, poster_number, discard_reason, relevance_reason.\n"
            "Keep job_summary concise and factual. Keep relevance_reason concise and evidence-based.\n\n"
            f"group_id: {message.group_id}\n"
            f"sender_number: {message.sender_number}\n"
            f"message_text:\n{message.message_text}"
        )
        model_result = await self._call_model(
            messages=[{"role": "user", "content": user_prompt}],
            trace_id=trace_id,
            max_tokens=512,
        )
        parsed = self._parse_json(model_result["text"])
        return self._coerce_relevance_decision(parsed, message)

    async def _run_research(
        self,
        *,
        message: WhatsAppMessage,
        relevance: RelevanceDecision,
        trace_id: UUID,
    ) -> ResearchOutput:
        research_agent = ResearchAgent(
            db_session=self.db_session,
            tracer=self.tracer,
            settings=self.settings,
        )
        job_data = {
            "job_title": relevance["job_title"],
            "company": relevance["company"],
            "job_summary": relevance["job_summary"],
            "full_job_text": message.message_text,
            "poster_email": relevance["poster_email"],
            "poster_number": relevance["poster_number"],
            "relevance_score": relevance["score"],
            "relevance_decision": self._decision_label_from_relevance(relevance),
            "relevance_decision_score": self._decision_score_from_label(
                self._decision_label_from_relevance(relevance)
            ),
        }
        return await research_agent.run(job_data=job_data, trace_id=trace_id)

    async def _run_resume_edit(
        self,
        trace_id: UUID,
        research_output: ResearchOutput,
        job_context: dict[str, Any],
        feedback: str | None,
    ) -> tuple[ResumeEditOutput, UUID, int]:
        version_number = await self._get_next_resume_version_number(trace_id=trace_id)
        resume_editor = self.agent_factory.create_resume_editor_agent()
        output = await resume_editor.run(
            research_output=research_output,
            trace_id=trace_id,
            job_context=job_context,
            version_number=version_number,
            feedback=feedback,
        )
        version_id = await self._insert_resume_version(
            trace_id=trace_id,
            resume_output=output,
            version_number=version_number,
        )
        return output, version_id, version_number

    async def _run_quality_gate(
        self,
        trace_id: UUID,
        resume_output: ResumeEditOutput,
        attempt: int,
    ) -> QualityGateDecision:
        prompt = (
            "Quality-gate this resume iteration. Return strict JSON: "
            "{pass: boolean, reason: string, feedback: string}.\n"
            f"attempt: {attempt}\n"
            f"evaluator_passed: {resume_output['evaluator_passed']}\n"
            f"ats_score_before: {resume_output['ats_score_before']}\n"
            f"ats_score_after: {resume_output['ats_score_after']}\n"
            f"evaluation_summary: {resume_output['evaluation_summary']}"
        )
        model_result = await self._call_model(
            messages=[{"role": "user", "content": prompt}],
            trace_id=trace_id,
            max_tokens=768,
        )
        parsed = self._parse_json(model_result["text"])

        model_pass = bool(parsed.get("pass", parsed.get("approved", False)))
        evaluator_passed = bool(resume_output["evaluator_passed"])
        ats_after = int(resume_output["ats_score_after"])
        criteria_pass = evaluator_passed and ats_after >= self.settings.min_ats_score
        passed = model_pass and criteria_pass

        feedback = str(
            parsed.get("feedback")
            or parsed.get("reason")
            or "Improve ATS alignment and tighten role-specific impact bullets."
        )
        reason = str(parsed.get("reason") or ("pass" if passed else "quality_gate_not_met"))

        return {
            "passed": passed,
            "model_pass": model_pass,
            "criteria_pass": criteria_pass,
            "feedback": feedback,
            "reason": reason,
            "evaluator_passed": evaluator_passed,
            "ats_score_after": ats_after,
        }

    async def _run_routing(
        self,
        trace_id: UUID,
        context: dict[str, Any],
        delivery_mode: Literal["send", "draft"] = "send",
    ) -> OutboundResult:
        if context.get("poster_email"):
            outbound_agent = self.agent_factory.create_gmail_agent()
            return await outbound_agent.run(
                context=context,
                trace_id=trace_id,
                delivery_mode=delivery_mode,
            )

        poster_number = str(context.get("poster_number") or "").strip()
        if not poster_number:
            raise ValueError("poster_number is required when poster_email is missing")

        outbound_agent = self.agent_factory.create_whatsapp_agent()
        return await outbound_agent.run(
            context=context,
            trace_id=trace_id,
            delivery_mode=delivery_mode,
        )

    async def _mark_failure(self, trace_id: UUID, stage: str, error_message: str) -> None:
        update_stmt = text(
            """
            UPDATE pipeline_runs
            SET status = 'failed',
                error_stage = :error_stage,
                error_message = :error_message
            WHERE trace_id = :trace_id
            """
        )
        result = await self.db_session.execute(
            update_stmt,
            {
                "trace_id": trace_id,
                "error_stage": stage,
                "error_message": error_message[:2000],
            },
        )
        if result.rowcount == 0:
            return
        await self.db_session.flush()
        await self.tracer.update_pipeline_status(
            trace_id=trace_id,
            status="failed",
            stage_data={"error_stage": stage, "error_message": error_message[:500]},
        )

    async def _persist_relevance(self, trace_id: UUID, relevance: RelevanceDecision) -> None:
        update_stmt = text(
            """
            UPDATE pipeline_runs
            SET job_title = :job_title,
                company = :company,
                job_summary = :job_summary,
                poster_email = :poster_email,
                poster_number = :poster_number,
                relevance_score = :relevance_score,
                relevance_reason = :relevance_reason
            WHERE trace_id = :trace_id
            """
        )
        result = await self.db_session.execute(
            update_stmt,
            {
                "trace_id": trace_id,
                "job_title": relevance["job_title"],
                "company": relevance["company"],
                "job_summary": relevance["job_summary"],
                "poster_email": relevance["poster_email"],
                "poster_number": relevance["poster_number"],
                "relevance_score": relevance["score"],
                "relevance_reason": relevance["relevance_reason"],
            },
        )
        self._ensure_row_exists(result.rowcount, trace_id)
        await self.db_session.flush()

    async def _insert_resume_version(
        self,
        trace_id: UUID,
        resume_output: ResumeEditOutput,
        version_number: int,
    ) -> UUID:
        insert_stmt = text(
            """
            INSERT INTO resume_versions (
                trace_id,
                version_number,
                docx_path,
                attachment_path,
                changes_made,
                ats_score_before,
                ats_score_after,
                evaluator_passed
            )
            VALUES (
                :trace_id,
                :version_number,
                :docx_path,
                :attachment_path,
                CAST(:changes_made AS jsonb),
                :ats_score_before,
                :ats_score_after,
                :evaluator_passed
            )
            RETURNING id
            """
        )
        resume_version_id = (
            await self.db_session.execute(
                insert_stmt,
                {
                    "trace_id": trace_id,
                    "version_number": version_number,
                    "docx_path": resume_output["docx_path"],
                    "attachment_path": self._resume_attachment_path(resume_output),
                    "changes_made": self._to_json(resume_output["changes_made"]),
                    "ats_score_before": int(resume_output["ats_score_before"]),
                    "ats_score_after": int(resume_output["ats_score_after"]),
                    "evaluator_passed": bool(resume_output["evaluator_passed"]),
                },
            )
        ).scalar_one()
        await self.db_session.flush()
        return resume_version_id

    async def _get_next_resume_version_number(self, trace_id: UUID) -> int:
        version_stmt = text(
            """
            SELECT COALESCE(MAX(version_number), 0) + 1
            FROM resume_versions
            WHERE trace_id = :trace_id
            """
        )
        version_number = int(
            (
                await self.db_session.execute(
                    version_stmt,
                    {"trace_id": trace_id},
                )
            ).scalar_one()
        )
        return max(1, version_number)

    async def _persist_quality_gate_result(self, trace_id: UUID, quality_result: dict[str, Any]) -> None:
        update_stmt = text(
            """
            UPDATE pipeline_runs
            SET quality_gate_result = CAST(:quality_gate_result AS jsonb)
            WHERE trace_id = :trace_id
            """
        )
        result = await self.db_session.execute(
            update_stmt,
            {
                "trace_id": trace_id,
                "quality_gate_result": self._to_json(quality_result),
            },
        )
        self._ensure_row_exists(result.rowcount, trace_id)
        await self.db_session.flush()

    async def _load_routing_context(self, trace_id: UUID) -> dict[str, Any]:
        query = text(
            """
            SELECT
                pr.job_title,
                pr.company,
                pr.job_summary,
                pr.poster_email,
                pr.poster_number,
                rv.attachment_path
            FROM pipeline_runs pr
            JOIN resume_versions rv ON rv.trace_id = pr.trace_id
            WHERE pr.trace_id = :trace_id
            ORDER BY rv.version_number DESC, rv.created_at DESC
            LIMIT 1
            """
        )
        row = (
            await self.db_session.execute(
                query,
                {"trace_id": trace_id},
            )
        ).mappings().first()
        if row is None:
            raise ValueError(f"Unable to load routing context for trace_id={trace_id}")
        return dict(row)

    async def _persist_outbound_result(
        self,
        trace_id: UUID,
        outbound_result: OutboundResult,
        *,
        outbox_status: str | None = None,
    ) -> None:
        update_stmt = text(
            """
            UPDATE pipeline_runs
            SET outbound_action = :outbound_action
            WHERE trace_id = :trace_id
            """
        )
        result = await self.db_session.execute(
            update_stmt,
            {
                "trace_id": trace_id,
                "outbound_action": outbound_result["channel"],
            },
        )
        self._ensure_row_exists(result.rowcount, trace_id)

        insert_stmt = text(
            """
            INSERT INTO outbox (
                trace_id,
                channel,
                recipient,
                subject,
                body_preview,
                attachment_path,
                external_id,
                status
            )
            VALUES (
                :trace_id,
                :channel,
                :recipient,
                :subject,
                :body_preview,
                :attachment_path,
                :external_id,
                :status
            )
            """
        )
        await self.db_session.execute(
            insert_stmt,
            {
                "trace_id": trace_id,
                "channel": outbound_result["channel"],
                "recipient": outbound_result["recipient"],
                "subject": outbound_result["subject"],
                "body_preview": outbound_result["body_preview"][:200],
                "attachment_path": outbound_result["attachment_path"],
                "external_id": outbound_result["external_id"],
                "status": outbox_status or ("sent" if outbound_result["sent"] else "failed"),
            },
        )
        await self.db_session.flush()

    def _coerce_relevance_decision(
        self,
        payload: dict[str, Any],
        message: WhatsAppMessage,
    ) -> RelevanceDecision:
        score_raw = payload.get("score", 0)
        try:
            score = int(score_raw)
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(10, score))

        decision = self._normalize_decision_label(payload.get("decision"), score=score)
        relevant = decision != "reject"

        job_title = str(payload.get("job_title") or "Unknown Title")
        company = str(payload.get("company") or "Unknown Company")
        job_summary = str(payload.get("job_summary") or message.message_text[:600])

        poster_email = payload.get("poster_email")
        if poster_email is not None:
            poster_email = str(poster_email).strip() or None

        poster_number = payload.get("poster_number")
        if poster_number is not None:
            poster_number = str(poster_number).strip() or None
        if poster_number is None:
            poster_number = message.sender_number

        relevance_reason = str(payload.get("relevance_reason") or payload.get("reason") or "")

        discard_reason = payload.get("discard_reason")
        if discard_reason is not None:
            discard_reason = str(discard_reason).strip() or None
        if discard_reason is None and decision == "reject":
            discard_reason = "Message did not match target profile threshold"

        return {
            "decision": decision,
            "decision_score": self._decision_score_from_label(decision),
            "relevant": relevant,
            "score": score,
            "job_title": job_title,
            "company": company,
            "job_summary": job_summary,
            "poster_email": poster_email,
            "poster_number": poster_number,
            "discard_reason": discard_reason,
            "relevance_reason": relevance_reason,
        }

    def _normalize_decision_label(self, raw_value: Any, *, score: int) -> str:
        cleaned = str(raw_value or "").strip().lower()
        if cleaned in {"fit", "okayish", "reject"}:
            return cleaned
        if cleaned == "relevant":
            return "fit"
        if cleaned == "borderline":
            return "okayish"
        if score <= 4:
            return "reject"
        if score <= 6:
            return "okayish"
        return "fit"

    def _decision_label_from_relevance(self, relevance: RelevanceDecision | dict[str, Any]) -> str:
        score_raw = relevance.get("score", 0)
        try:
            score = int(score_raw)
        except (TypeError, ValueError):
            score = 0
        return self._normalize_decision_label(relevance.get("decision"), score=score)

    def _decision_score_from_label(self, decision: str) -> float:
        if decision == "reject":
            return 0.0
        if decision == "okayish":
            return 0.5
        return 1.0

    def _build_failed_outbound_result(self, context: dict[str, Any], error_message: str) -> OutboundResult:
        poster_email = str(context.get("poster_email") or "").strip()
        poster_number = str(context.get("poster_number") or "").strip()
        channel = "email" if poster_email else "whatsapp"
        recipient = poster_email or poster_number or "unknown"
        return {
            "sent": False,
            "channel": channel,
            "recipient": recipient,
            "subject": None,
            "body_preview": error_message[:500],
            "attachment_path": str(context.get("attachment_path") or "").strip() or None,
            "external_id": None,
        }

    def _select_delivery_mode(self, context: dict[str, Any]) -> Literal["send", "draft"]:
        return "draft" if str(context.get("relevance_decision") or "").strip().lower() == "okayish" else "send"

    def _resolve_outbox_status(
        self,
        *,
        outbound_result: OutboundResult,
        delivery_mode: Literal["send", "draft"],
    ) -> str:
        if delivery_mode == "draft":
            return "review_required"
        return "sent" if outbound_result["sent"] else "failed"

    def _resume_attachment_path(self, resume_output: ResumeEditOutput | dict[str, Any]) -> str:
        attachment_path = str(
            resume_output.get("attachment_path") or resume_output.get("docx_path") or ""
        ).strip()
        if not attachment_path:
            raise ValueError("resume_output must include attachment_path or docx_path")
        return attachment_path

    def _ensure_row_exists(self, rowcount: int | None, trace_id: UUID) -> None:
        if rowcount is None or rowcount == 0:
            raise ValueError(f"pipeline_runs row not found for trace_id={trace_id}")

    def _to_json(self, value: Any) -> str:
        return json.dumps(value)


class ManagerPipelineRunner:
    def __init__(
        self,
        *,
        session_factory=AsyncSessionLocal,
        settings: Settings | None = None,
        agent_factory: AgentFactoryPort | None = None,
        mode: Literal["normal", "dry_run_pre_outbound"] = "normal",
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self.agent_factory = agent_factory
        self.mode = mode

    async def run(self, message: WhatsAppMessage, trace_id: UUID) -> dict[str, Any]:
        async with self.session_factory() as session:
            tracer = AgentTracer(session)
            manager = ManagerAgent(
                db_session=session,
                tracer=tracer,
                agent_factory=self.agent_factory,
                settings=self.settings,
                mode=self.mode,
            )
            try:
                result = await manager.run(message=message, trace_id=trace_id)
                await session.commit()
                return result
            except Exception:
                # Persist manager failure envelope updates before bubbling up to watcher.
                await session.commit()
                raise

