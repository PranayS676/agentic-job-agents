-- Jobs processed recently
SELECT job_title, company, relevance_score, status, created_at
FROM pipeline_runs
ORDER BY created_at DESC
LIMIT 20;

-- Agent traces for a pipeline run
SELECT agent_name, decision, latency_ms, input_tokens, output_tokens, created_at
FROM agent_traces
WHERE trace_id = :trace_id
ORDER BY created_at ASC;

-- Outbound audit this week
SELECT channel, recipient, subject, status, sent_at
FROM outbox
WHERE sent_at > NOW() - INTERVAL '7 days'
ORDER BY sent_at DESC;

-- Token usage by pipeline
SELECT pr.trace_id, pr.job_title, pr.company,
       SUM(COALESCE(at.input_tokens,0) + COALESCE(at.output_tokens,0)) AS total_tokens
FROM pipeline_runs pr
JOIN agent_traces at ON at.trace_id = pr.trace_id
GROUP BY pr.trace_id, pr.job_title, pr.company
ORDER BY total_tokens DESC;

-- Discarded jobs and reason
SELECT trace_id, job_title, company, relevance_reason, created_at
FROM pipeline_runs
WHERE status = 'discarded'
ORDER BY created_at DESC;
