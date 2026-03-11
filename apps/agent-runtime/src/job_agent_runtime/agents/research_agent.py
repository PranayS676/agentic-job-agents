from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from job_agent_runtime.agents.base_agent import BaseAgent
from job_platform.config import Settings, get_settings
from job_platform.tracer import AgentTracer

from .contracts import ResearchActionItem, ResearchOutput, ResumeTrackProfile
from .resume_tracks import KEYWORD_TAXONOMY, load_resume_tracks, normalize_resume_text, slugify


class ResearchAgent(BaseAgent):
    SHORTLIST_SIZE = 2
    MIN_TRACK_COUNT = 3

    def __init__(
        self,
        *,
        db_session: AsyncSession,
        tracer: AgentTracer,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        super().__init__(
            skill_path="skills/resume-research",
            model=self.settings.research_model,
            db_session=db_session,
            tracer=tracer,
        )

    async def run(self, job_data: dict, trace_id: UUID) -> ResearchOutput:
        job_summary = str(job_data.get("job_summary") or "").strip()
        full_job_text = str(job_data.get("full_job_text") or job_summary).strip()
        if not full_job_text:
            raise ValueError("job_data.full_job_text or job_data.job_summary is required for research")

        resume_tracks = self._load_resume_tracks()
        ranked_tracks = self._rank_resume_tracks(job_data=job_data, resume_tracks=resume_tracks)
        shortlisted_tracks = ranked_tracks[: self.SHORTLIST_SIZE]
        prompt = self._build_prompt(
            job_data=job_data,
            ranked_tracks=ranked_tracks,
            shortlisted_tracks=shortlisted_tracks,
        )
        model_result = await self._call_model(
            messages=[{"role": "user", "content": prompt}],
            trace_id=trace_id,
            max_tokens=4096,
        )
        parsed = self._parse_json(model_result["text"])
        research_output = self._coerce_research_output(
            payload=parsed,
            resume_tracks=resume_tracks,
            job_data=job_data,
        )

        await self._persist_research_output(trace_id=trace_id, research_output=research_output)

        decision_summary = (
            f"track={research_output['selected_resume_track']}; "
            f"adds={len(research_output['add_items'])}; "
            f"gaps={len(research_output['hard_gaps'])}; ATS "
            f"{research_output['ats_score_estimate_before']} -> {research_output['ats_score_estimate_after']}"
        )
        await self.tracer.trace(
            trace_id=trace_id,
            agent_name=self.__class__.__name__,
            model=self.model,
            input_data={"job_data": job_data, "track_shortlist": shortlisted_tracks},
            output_data={"research_output": research_output},
            tokens_in=model_result.get("input_tokens"),
            tokens_out=model_result.get("output_tokens"),
            latency_ms=model_result.get("latency_ms"),
            decision_summary=decision_summary,
        )

        return research_output

    def _load_resume_tracks(self) -> list[ResumeTrackProfile]:
        if self.settings.resume_tracks_dir is not None:
            tracks_dir = self.settings.resolve_path(self.settings.resume_tracks_dir)
            tracks = load_resume_tracks(tracks_dir)
            if len(tracks) < self.MIN_TRACK_COUNT:
                raise FileNotFoundError(
                    f"Expected at least {self.MIN_TRACK_COUNT} resume track files in {tracks_dir}, found {len(tracks)}"
                )
            return tracks

        return [self._build_base_resume_fallback_track()]

    def _build_base_resume_fallback_track(self) -> ResumeTrackProfile:
        resume_text = self._load_base_resume_text()
        normalized_text = normalize_resume_text(resume_text)
        return {
            "track_id": "base_resume",
            "source_pdf_path": str(self.settings.resolve_path(self.settings.base_resume_text)),
            "display_name": "Base Resume",
            "raw_text": resume_text,
            "normalized_text": normalized_text,
            "sections": {
                "summary": normalized_text,
                "skills": normalized_text,
                "experience_recent_role": normalized_text,
                "education": normalized_text,
            },
            "role_bias": ["fallback"],
            "keywords": [keyword for keyword in KEYWORD_TAXONOMY if keyword in normalized_text.lower()],
        }

    def _load_base_resume_text(self) -> str:
        if self.settings.base_resume_text is None:
            raise FileNotFoundError("BASE_RESUME_TEXT is not configured")
        resume_path = self.settings.resolve_path(self.settings.base_resume_text)
        if not resume_path.is_file():
            raise FileNotFoundError(f"BASE_RESUME_TEXT file not found: {resume_path}")
        return resume_path.read_text(encoding="utf-8")

    def _rank_resume_tracks(
        self,
        *,
        job_data: dict[str, Any],
        resume_tracks: list[ResumeTrackProfile],
    ) -> list[dict[str, Any]]:
        full_job_text = str(job_data.get("full_job_text") or job_data.get("job_summary") or "")
        job_title = str(job_data.get("job_title") or "")
        job_lower = full_job_text.lower()
        title_tokens = self._tokenize(job_title)
        jd_keywords = [keyword for keyword in KEYWORD_TAXONOMY if keyword in job_lower]
        required_clouds = [cloud for cloud in ("gcp", "azure", "aws") if re.search(rf"\b{cloud}\b", job_lower)]

        ranked: list[dict[str, Any]] = []
        for track in resume_tracks:
            track_text = track["normalized_text"].lower()
            track_keywords = set(track.get("keywords", []))
            title_overlap = sum(1 for token in title_tokens if token in track_text)
            keyword_overlap = sum(1 for keyword in jd_keywords if keyword in track_keywords)
            seniority_score = self._seniority_score(job_lower, track_text)
            role_bias_score = self._role_bias_score(job_lower, track.get("role_bias", []))
            cloud_score = self._cloud_score(required_clouds, track_keywords)
            domain_overlap = self._domain_overlap_score(job_lower, track_text)
            total_score = (
                title_overlap * 10
                + keyword_overlap * 6
                + seniority_score
                + role_bias_score
                + cloud_score
                + domain_overlap
            )
            ranked.append(
                {
                    "track_id": track["track_id"],
                    "display_name": track["display_name"],
                    "source_pdf_path": track["source_pdf_path"],
                    "available_sections": sorted(track["sections"].keys()),
                    "keywords": track.get("keywords", []),
                    "role_bias": track.get("role_bias", []),
                    "heuristic_score": total_score,
                    "score_breakdown": {
                        "title_overlap": title_overlap,
                        "keyword_overlap": keyword_overlap,
                        "seniority_score": seniority_score,
                        "role_bias_score": role_bias_score,
                        "cloud_score": cloud_score,
                        "domain_overlap": domain_overlap,
                    },
                    "summary": track["sections"].get("summary", "")[:600],
                    "skills": track["sections"].get("skills", "")[:600],
                    "experience_recent_role": track["sections"].get("experience_recent_role", "")[:900],
                }
            )

        return sorted(ranked, key=lambda item: (-item["heuristic_score"], item["track_id"]))

    def _build_prompt(
        self,
        *,
        job_data: dict[str, Any],
        ranked_tracks: list[dict[str, Any]],
        shortlisted_tracks: list[dict[str, Any]],
    ) -> str:
        job_summary = str(job_data.get("job_summary") or "").strip()
        full_job_text = str(job_data.get("full_job_text") or job_summary).strip()
        relevance_decision = str(job_data.get("relevance_decision") or "fit").strip().lower()

        aggressiveness_instruction = (
            "This role is only okayish. Keep the edit plan narrower, avoid stretching claims, and only suggest the most defensible changes."
            if relevance_decision == "okayish"
            else "This role is a fit. You may suggest the strongest truthful alignment within the constrained edit scope."
        )

        prompt_payload = {
            "job_context": {
                "job_title": job_data.get("job_title"),
                "company": job_data.get("company"),
                "job_summary": job_summary,
                "full_job_text": full_job_text,
                "relevance_score": job_data.get("relevance_score"),
                "relevance_decision": relevance_decision,
                "relevance_decision_score": job_data.get("relevance_decision_score"),
            },
            "ranked_tracks": [
                {
                    "track_id": item["track_id"],
                    "display_name": item["display_name"],
                    "heuristic_score": item["heuristic_score"],
                    "score_breakdown": item["score_breakdown"],
                }
                for item in ranked_tracks
            ],
            "shortlisted_tracks": shortlisted_tracks,
            "rules": {
                "edit_scope": [
                    "summary",
                    "skills",
                    "one experience target section from the selected track",
                ],
                "experience_actions_max": 2,
                "cloud_rule": "Do not rewrite AWS evidence as GCP or Azure. Exact-cloud mismatches must appear in hard_gaps.",
                "summary_rule": "Return one clear summary_focus.",
                "skills_rule": "Suggest only missing or under-emphasized skills that are grounded in the selected track.",
                "truth_rule": "Do not invent unsupported experience, tools, or cloud platforms.",
                "aggressiveness_instruction": aggressiveness_instruction,
            },
            "required_output_schema": {
                "selected_resume_track": "one of the shortlisted track_id values",
                "selected_resume_source_pdf": "path of the selected source PDF",
                "selected_resume_match_reason": "concise evidence-based explanation",
                "experience_target_section": "one experience_* section key from the selected track",
                "summary_focus": "single sentence describing how to realign the summary",
                "skills_gap_notes": ["list of missing or under-emphasized skills to address"],
                "hard_gaps": ["list exact deficits that must not be faked"],
                "edit_scope": ["summary", "skills", "experience_*"],
                "add_items": [
                    {
                        "section": "summary|skills|experience_*",
                        "action": "exact change request",
                        "reason": "JD-grounded reason",
                        "priority": 1,
                    }
                ],
                "remove_items": [
                    {
                        "section": "summary|skills|experience_*",
                        "action": "what to shorten or de-emphasize",
                        "reason": "why it should be reduced",
                    }
                ],
                "keywords_to_inject": ["ATS keywords grounded in the selected track"],
                "sections_to_edit": ["summary", "skills", "experience_*"],
                "ats_score_estimate_before": 0,
                "ats_score_estimate_after": 0,
                "research_reasoning": "one paragraph with the reasoning",
            },
        }

        return (
            "Select the best resume variant for this job and produce a constrained, truthful edit plan.\n"
            "Return strict JSON only.\n"
            "You must operate within exactly three edit areas: summary, skills, and one selected experience section.\n"
            f"Input:\n{json.dumps(prompt_payload, ensure_ascii=True, indent=2)}"
        )

    def _coerce_research_output(
        self,
        payload: dict[str, Any],
        *,
        resume_tracks: list[ResumeTrackProfile],
        job_data: dict[str, Any],
    ) -> ResearchOutput:
        required_keys = {
            "add_items",
            "remove_items",
            "keywords_to_inject",
            "ats_score_estimate_before",
            "ats_score_estimate_after",
            "research_reasoning",
            "selected_resume_track",
            "selected_resume_match_reason",
            "experience_target_section",
            "summary_focus",
            "skills_gap_notes",
            "hard_gaps",
            "edit_scope",
        }
        missing = sorted(required_keys - set(payload))
        if missing:
            raise ValueError(f"Research output missing required keys: {', '.join(missing)}")

        tracks_by_id = {track["track_id"]: track for track in resume_tracks}
        selected_track_id = str(payload.get("selected_resume_track") or "").strip()
        if selected_track_id not in tracks_by_id:
            raise ValueError(f"selected_resume_track must match a known track id: {selected_track_id!r}")
        selected_track = tracks_by_id[selected_track_id]

        experience_target_section = self._normalize_experience_target_section(
            payload.get("experience_target_section"),
            selected_track,
        )
        allowed_sections = {"summary", "skills", experience_target_section}

        add_items = self._normalize_action_items(
            payload.get("add_items"),
            field_name="add_items",
            max_items=6,
            require_priority=True,
            allowed_sections=allowed_sections,
            experience_target_section=experience_target_section,
            max_experience_items=2,
        )
        remove_items = self._normalize_action_items(
            payload.get("remove_items"),
            field_name="remove_items",
            max_items=2,
            require_priority=False,
            allowed_sections=allowed_sections,
            experience_target_section=experience_target_section,
            max_experience_items=1,
        )
        keywords = self._normalize_string_list(payload.get("keywords_to_inject"), "keywords_to_inject")
        summary_focus = str(payload.get("summary_focus") or "").strip()
        if not summary_focus:
            raise ValueError("summary_focus must be a non-empty string")

        skills_gap_notes = self._normalize_string_list(
            payload.get("skills_gap_notes"),
            "skills_gap_notes",
            allow_empty=True,
        )
        hard_gaps = self._merge_hard_gaps(
            self._normalize_string_list(payload.get("hard_gaps"), "hard_gaps", allow_empty=True),
            self._derive_hard_gaps(job_data=job_data, selected_track=selected_track),
        )
        before_score = self._normalize_score(
            payload.get("ats_score_estimate_before"),
            "ats_score_estimate_before",
        )
        after_score = self._normalize_score(
            payload.get("ats_score_estimate_after"),
            "ats_score_estimate_after",
        )
        reasoning = str(payload.get("research_reasoning") or "").strip()
        if not reasoning:
            raise ValueError("research_reasoning must be a non-empty string")
        selected_resume_match_reason = str(payload.get("selected_resume_match_reason") or "").strip()
        if not selected_resume_match_reason:
            raise ValueError("selected_resume_match_reason must be a non-empty string")

        edit_scope = self._normalize_edit_scope(
            payload.get("edit_scope"),
            allowed_sections=allowed_sections,
            field_name="edit_scope",
        )
        sections_to_edit = self._normalize_edit_scope(
            payload.get("sections_to_edit", edit_scope),
            allowed_sections=allowed_sections,
            field_name="sections_to_edit",
        )

        return {
            "add_items": add_items,
            "remove_items": remove_items,
            "keywords_to_inject": keywords,
            "sections_to_edit": sections_to_edit,
            "ats_score_estimate_before": before_score,
            "ats_score_estimate_after": after_score,
            "research_reasoning": reasoning,
            "selected_resume_track": selected_track_id,
            "selected_resume_source_pdf": selected_track["source_pdf_path"],
            "selected_resume_match_reason": selected_resume_match_reason,
            "experience_target_section": experience_target_section,
            "summary_focus": summary_focus,
            "skills_gap_notes": skills_gap_notes,
            "hard_gaps": hard_gaps,
            "edit_scope": edit_scope,
        }

    def _normalize_action_items(
        self,
        value: Any,
        *,
        field_name: str,
        max_items: int,
        require_priority: bool,
        allowed_sections: set[str],
        experience_target_section: str,
        max_experience_items: int,
    ) -> list[ResearchActionItem]:
        if not isinstance(value, list):
            raise ValueError(f"{field_name} must be a list")
        if len(value) > max_items:
            raise ValueError(f"{field_name} exceeds max {max_items} items")

        normalized: list[ResearchActionItem] = []
        experience_item_count = 0
        for index, raw in enumerate(value):
            if not isinstance(raw, dict):
                raise ValueError(f"{field_name}[{index}] must be an object")

            section = self._normalize_item_section(
                raw.get("section"),
                experience_target_section=experience_target_section,
            )
            action = str(raw.get("action") or "").strip()
            reason = str(raw.get("reason") or "").strip()
            if not section or not action or not reason:
                raise ValueError(f"{field_name}[{index}] must include section/action/reason")
            if section not in allowed_sections:
                raise ValueError(
                    f"{field_name}[{index}] section must stay within summary/skills/{experience_target_section}"
                )
            if section == experience_target_section:
                experience_item_count += 1
                if experience_item_count > max_experience_items:
                    raise ValueError(
                        f"{field_name} exceeds max {max_experience_items} actions for {experience_target_section}"
                    )

            item: ResearchActionItem = {
                "section": section,
                "action": action,
                "reason": reason,
            }

            priority_raw = raw.get("priority")
            if require_priority:
                if priority_raw is None:
                    raise ValueError(f"{field_name}[{index}] priority is required")
                item["priority"] = self._normalize_priority(priority_raw, f"{field_name}[{index}].priority")
            elif priority_raw is not None:
                item["priority"] = self._normalize_priority(priority_raw, f"{field_name}[{index}].priority")

            normalized.append(item)

        return normalized

    def _normalize_item_section(self, value: Any, *, experience_target_section: str) -> str:
        cleaned = slugify(str(value or ""))
        if cleaned in {"experience", "experience_recent", "recent_experience", "current_role"}:
            return experience_target_section
        if cleaned.startswith("experience_"):
            return cleaned
        if cleaned in {"summary", "skills"}:
            return cleaned
        return cleaned

    def _normalize_edit_scope(
        self,
        value: Any,
        *,
        allowed_sections: set[str],
        field_name: str,
    ) -> list[str]:
        sections = self._normalize_string_list(value, field_name)
        normalized = [self._normalize_item_section(section, experience_target_section=self._experience_section(allowed_sections)) for section in sections]
        if set(normalized) != allowed_sections or len(normalized) != 3:
            raise ValueError(
                f"{field_name} must contain exactly summary, skills, and one selected experience section"
            )
        return ["summary", "skills", self._experience_section(allowed_sections)]

    def _experience_section(self, sections: set[str]) -> str:
        for section in sections:
            if section.startswith("experience_"):
                return section
        raise ValueError("Expected an experience_* section in allowed sections")

    def _normalize_experience_target_section(
        self,
        value: Any,
        selected_track: ResumeTrackProfile,
    ) -> str:
        section = self._normalize_item_section(value, experience_target_section="experience_recent_role")
        if section not in selected_track["sections"]:
            raise ValueError(
                f"experience_target_section must exist in selected track {selected_track['track_id']}: {section!r}"
            )
        if not section.startswith("experience_"):
            raise ValueError("experience_target_section must be an experience_* section")
        return section

    def _normalize_string_list(
        self,
        value: Any,
        field_name: str,
        allow_empty: bool = False,
    ) -> list[str]:
        if not isinstance(value, list):
            raise ValueError(f"{field_name} must be a list")
        seen: set[str] = set()
        result: list[str] = []
        for index, raw in enumerate(value):
            cleaned = str(raw or "").strip()
            if not cleaned:
                if allow_empty:
                    continue
                raise ValueError(f"{field_name}[{index}] must be a non-empty string")
            if cleaned in seen:
                continue
            seen.add(cleaned)
            result.append(cleaned)
        return result

    def _normalize_score(self, value: Any, field_name: str) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be an integer") from exc
        if parsed < 0 or parsed > 100:
            raise ValueError(f"{field_name} must be between 0 and 100")
        return parsed

    def _normalize_priority(self, value: Any, field_name: str) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be an integer") from exc
        if parsed < 1:
            raise ValueError(f"{field_name} must be >= 1")
        return parsed

    def _merge_hard_gaps(self, model_hard_gaps: list[str], derived_hard_gaps: list[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for gap in [*model_hard_gaps, *derived_hard_gaps]:
            cleaned = gap.strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            merged.append(cleaned)
        return merged

    def _derive_hard_gaps(
        self,
        *,
        job_data: dict[str, Any],
        selected_track: ResumeTrackProfile,
    ) -> list[str]:
        full_job_text = str(job_data.get("full_job_text") or job_data.get("job_summary") or "").lower()
        selected_keywords = set(selected_track.get("keywords", []))
        gaps: list[str] = []
        for cloud in ("gcp", "azure"):
            if re.search(rf"\b{cloud}\b", full_job_text) and cloud not in selected_keywords:
                if "aws" in selected_keywords:
                    gaps.append(f"Exact {cloud.upper()} hands-on evidence is not present; strongest cloud evidence is AWS.")
                else:
                    gaps.append(f"Exact {cloud.upper()} hands-on evidence is not present in the selected resume track.")
        return gaps

    def _tokenize(self, value: str) -> list[str]:
        return [token for token in re.findall(r"[a-z0-9]+", value.lower()) if len(token) > 2]

    def _seniority_score(self, job_lower: str, track_text: str) -> int:
        requested = [token for token in ("lead", "principal", "architect", "senior", "staff") if token in job_lower]
        if not requested:
            return 2
        matched = sum(1 for token in requested if token in track_text)
        return (matched * 4) - (4 if matched == 0 else 0)

    def _role_bias_score(self, job_lower: str, role_bias: list[str]) -> int:
        bias_score = 0
        if any(token in job_lower for token in ("machine learning", "ml", "llm", "rag")) and "ai_ml" in role_bias:
            bias_score += 8
        if any(token in job_lower for token in ("data engineer", "spark", "airflow", "snowflake", "databricks")) and "data_platform" in role_bias:
            bias_score += 6
        if any(token in job_lower for token in ("aws", "gcp", "azure", "terraform", "kubernetes")) and "cloud_platform" in role_bias:
            bias_score += 5
        if "python" in job_lower and "backend_python" in role_bias:
            bias_score += 4
        return bias_score

    def _cloud_score(self, required_clouds: list[str], track_keywords: set[str]) -> int:
        if not required_clouds:
            return 0
        score = 0
        for cloud in required_clouds:
            if cloud in track_keywords:
                score += 8
            else:
                score -= 6
        return score

    def _domain_overlap_score(self, job_lower: str, track_text: str) -> int:
        domain_terms = [
            term
            for term in ("llm", "rag", "machine learning", "data engineering", "analytics", "microservices")
            if term in job_lower and term in track_text
        ]
        return len(domain_terms) * 3

    async def _persist_research_output(self, trace_id: UUID, research_output: ResearchOutput) -> None:
        update_stmt = text(
            """
            UPDATE pipeline_runs
            SET research_output = CAST(:research_output AS jsonb)
            WHERE trace_id = :trace_id
            """
        )
        result = await self.db_session.execute(
            update_stmt,
            {
                "trace_id": trace_id,
                "research_output": json.dumps(research_output),
            },
        )
        if result.rowcount is None or result.rowcount == 0:
            raise ValueError(f"pipeline_runs row not found for trace_id={trace_id}")
        await self.db_session.flush()
