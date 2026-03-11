from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _serialize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _serialize_row(mapping: dict[str, Any]) -> dict[str, Any]:
    return {key: _serialize_value(value) for key, value in mapping.items()}


async def fetch_ops_overview(
    session: AsyncSession,
    *,
    groups_monitored: int,
    polling_enabled: bool,
) -> dict[str, Any]:
    result = await session.execute(
        text(
            """
            WITH pipeline_stats AS (
                SELECT
                    COUNT(*) FILTER (WHERE status = 'review_required')::int AS review_required_count,
                    COUNT(*) FILTER (WHERE status = 'failed')::int AS failed_pipeline_count,
                    COUNT(*) FILTER (
                        WHERE status = 'sent'
                          AND created_at > NOW() - INTERVAL '24 hours'
                    )::int AS sent_pipeline_count_24h,
                    COUNT(*) FILTER (
                        WHERE status = 'discarded'
                          AND created_at > NOW() - INTERVAL '24 hours'
                    )::int AS discarded_pipeline_count_24h,
                    COUNT(*) FILTER (
                        WHERE status = 'review_required'
                          AND created_at > NOW() - INTERVAL '24 hours'
                    )::int AS review_required_count_24h,
                    COUNT(*) FILTER (
                        WHERE status = 'failed'
                          AND created_at > NOW() - INTERVAL '24 hours'
                    )::int AS failed_count_24h
                FROM pipeline_runs
            ),
            polling_summary AS (
                SELECT
                    CASE
                        WHEN COUNT(*) = 0 THEN 'idle'
                        WHEN BOOL_OR(status = 'error') THEN 'error'
                        WHEN BOOL_OR(status = 'running') THEN 'running'
                        WHEN BOOL_OR(status = 'ok') THEN 'ok'
                        ELSE 'idle'
                    END AS polling_status,
                    MAX(last_poll_started_at) AS last_poll_started_at,
                    MAX(last_poll_completed_at) AS last_poll_completed_at
                FROM polling_cursors
            )
            SELECT
                CAST(:groups_monitored AS integer) AS groups_monitored,
                CAST(:polling_enabled AS boolean) AS polling_enabled,
                polling_summary.polling_status,
                polling_summary.last_poll_started_at,
                polling_summary.last_poll_completed_at,
                (
                    SELECT COUNT(*)::int
                    FROM whatsapp_messages
                    WHERE processed = false
                ) AS unprocessed_messages_count,
                pipeline_stats.review_required_count,
                pipeline_stats.failed_pipeline_count,
                pipeline_stats.sent_pipeline_count_24h,
                pipeline_stats.discarded_pipeline_count_24h,
                pipeline_stats.review_required_count_24h,
                pipeline_stats.failed_count_24h
            FROM pipeline_stats, polling_summary
            """
        ),
        {
            "groups_monitored": groups_monitored,
            "polling_enabled": polling_enabled,
        },
    )
    row = result.mappings().one()
    return _serialize_row(dict(row))


async def fetch_review_queue(session: AsyncSession, *, limit: int) -> list[dict[str, Any]]:
    result = await session.execute(
        text(
            """
            SELECT
                pr.trace_id::text AS trace_id,
                pr.created_at,
                o.channel,
                o.recipient,
                pr.job_title,
                pr.company,
                COALESCE(o.status, pr.status) AS status,
                o.attachment_path,
                o.body_preview,
                pr.message_id::text AS message_id
            FROM pipeline_runs pr
            LEFT JOIN LATERAL (
                SELECT channel, recipient, status, attachment_path, body_preview
                FROM outbox
                WHERE trace_id = pr.trace_id
                ORDER BY sent_at DESC, id DESC
                LIMIT 1
            ) o ON true
            WHERE pr.status = 'review_required'
            ORDER BY pr.created_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    return [_serialize_row(dict(row)) for row in result.mappings().all()]


async def fetch_pipeline_runs(
    session: AsyncSession,
    *,
    limit: int,
    status: str | None = None,
) -> list[dict[str, Any]]:
    query = """
        SELECT
            pr.trace_id::text AS trace_id,
            pr.created_at,
            pr.status,
            pr.job_title,
            pr.company,
            o.channel,
            o.status AS outbound_status,
            pr.error_stage,
            pr.error_message,
            rv.attachment_path
        FROM pipeline_runs pr
        LEFT JOIN LATERAL (
            SELECT channel, status
            FROM outbox
            WHERE trace_id = pr.trace_id
            ORDER BY sent_at DESC, id DESC
            LIMIT 1
        ) o ON true
        LEFT JOIN LATERAL (
            SELECT attachment_path
            FROM resume_versions
            WHERE trace_id = pr.trace_id
            ORDER BY version_number DESC, created_at DESC, id DESC
            LIMIT 1
        ) rv ON true
    """
    params: dict[str, Any] = {"limit": limit}
    if status is not None:
        query += "\nWHERE pr.status = :status"
        params["status"] = status
    query += "\nORDER BY pr.created_at DESC\nLIMIT :limit"

    result = await session.execute(
        text(query),
        params,
    )
    return [_serialize_row(dict(row)) for row in result.mappings().all()]


async def fetch_polling_status(session: AsyncSession) -> list[dict[str, Any]]:
    result = await session.execute(
        text(
            """
            SELECT
                group_id,
                status,
                last_successful_message_timestamp,
                last_poll_started_at,
                last_poll_completed_at,
                last_cutoff_timestamp,
                last_error
            FROM polling_cursors
            ORDER BY group_id ASC
            """
        )
    )
    return [_serialize_row(dict(row)) for row in result.mappings().all()]
