# WhatsApp Job Agent System v2
## Complete Architecture, Skills, Integrations & Claude Code Prompts

---

## 1. System Philosophy

This system is built on three principles from Anthropic's Skills guide:
- **Progressive Disclosure**: each SKILL.md is lean at the top, detail lives in references/
- **Composability**: skills work independently and chain together via the Manager
- **Observability**: every message, decision, and action is traced end-to-end in PostgreSQL

The database is the backbone. Every agent reads from and writes to it. Nothing is fire-and-forget.

---

## 2. Complete System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         WHATSAPP GROUPS (3x)                                │
│              Job postings arrive as messages in group chats                  │
└────────────────────────────┬────────────────────────────────────────────────┘
                             │ WAHA webhook (HTTP POST on new message)
                             ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    INGEST SERVICE  (src/ingest.py)                           │
│                                                                              │
│  Receives WAHA webhook → writes RAW row to PostgreSQL whatsapp_messages      │
│  Fields: id(uuid), timestamp, group_id, sender_number, message_text,         │
│          message_hash(md5), processed(bool=false), created_at                │
│                                                                              │
│  Also runs polling fallback every 30s if webhook misses anything             │
└────────────────────────────┬────────────────────────────────────────────────┘
                             │ new row inserted → triggers next agent
                             ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│             WATCHER SERVICE  (src/watcher.py)                                │
│                                                                              │
│  Polls DB every 10s: SELECT * FROM whatsapp_messages WHERE processed=false   │
│  For each unprocessed row → dispatches to Pipeline Coordinator               │
│  Marks row processed=true after dispatch                                     │
└────────────────────────────┬────────────────────────────────────────────────┘
                             │ message_id + raw text
                             ▼
╔═════════════════════════════════════════════════════════════════════════════╗
║                    MANAGER AGENT  (Orchestrator)                            ║
║                    Model: claude-opus-4-6                                   ║
║                    Skills: job-manager (has 3 sub-skills loaded)            ║
║                                                                              ║
║  SKILL 1: job-relevance-evaluator  → decides if job suits Pranay's profile  ║
║  SKILL 2: pipeline-coordinator     → decides what agents to call and order  ║
║  SKILL 3: quality-gate-reviewer    → final resume + message review          ║
║                                                                              ║
║  Writes a pipeline_run row to DB with trace_id (uuid) for every job         ║
╚══════════╤══════════════════════════════════════════════════════════════════╝
           │
           │ if job is relevant
           ▼
┌──────────────────────────────────────────────────────────────────┐
│           RESEARCH AGENT  (src/agents/research_agent.py)          │
│           Model: claude-sonnet-4-6                                │
│           Skill: resume-research                                  │
│                                                                   │
│  Reads base resume from DB (candidate_profile table)             │
│  Gap analysis: what to ADD and what to REMOVE from resume        │
│  Writes research_results row to DB (linked to trace_id)          │
└──────────────────────┬───────────────────────────────────────────┘
                       │ structured action items
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│        RESUME EDITOR AGENT  (src/agents/resume_editor_agent.py)  │
│        Model: claude-sonnet-4-6                                   │
│        Skill: resume-editor                                       │
│                                                                   │
│  Applies research action items to base_resume.docx               │
│  Evaluator loop: max 2 refinement passes                         │
│  Saves tailored .docx to output/resumes/                         │
│  Writes resume_versions row to DB                                │
└──────────────────────┬───────────────────────────────────────────┘
                       │ docx_path
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│        PDF CONVERTER AGENT  (src/agents/pdf_converter_agent.py)  │
│        Model: claude-haiku-4-5-20251001                          │
│        Skill: pdf-converter                                       │
│                                                                   │
│  LibreOffice headless: .docx → .pdf                              │
│  Saves to output/pdfs/                                           │
│  Writes to resume_versions row (adds pdf_path)                   │
└──────────────────────┬───────────────────────────────────────────┘
                       │ pdf_path + full context
                       ▼
╔═════════════════════════════════════════════════════════════════╗
║          MANAGER AGENT — QUALITY GATE + ROUTING                 ║
║          (uses SKILL 3: quality-gate-reviewer)                  ║
║                                                                  ║
║  Reviews resume quality (ATS score ≥ 65, no fabrications)       ║
║  Checks: does the job post have an email address?                ║
║                                                                  ║
║  ROUTE A: email present   → COMPOSE + SEND EMAIL                ║
║  ROUTE B: no email        → COMPOSE + SEND WHATSAPP MESSAGE     ║
╚══════════╤══════════════════════════════════════════════════════╝
           │
     ┌─────┴──────┐
     ▼            ▼
┌─────────┐  ┌──────────────────────────────────┐
│  GMAIL  │  │  WHATSAPP MESSAGE AGENT           │
│  AGENT  │  │  (src/agents/whatsapp_msg_agent)  │
│ sonnet  │  │  Model: claude-sonnet-4-6         │
│         │  │  Skill: whatsapp-composer         │
│ Writes  │  │                                  │
│ tailored│  │  Composes professional message    │
│ email   │  │  Attaches PDF via WAHA            │
│ Attaches│  │  Sends to poster_number           │
│ PDF     │  └──────────────────────────────────┘
│ Sends   │
└─────────┘
     │            │
     └─────┬──────┘
           ▼
┌──────────────────────────────────┐
│       TRACE LOGGER               │
│  Writes final outcome to DB      │
│  pipeline_runs: status=complete  │
│  outbox: email/whatsapp sent log │
└──────────────────────────────────┘
```

---

## 3. PostgreSQL Schema (Full)

```sql
-- Every raw WhatsApp message received
CREATE TABLE whatsapp_messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    group_id        TEXT NOT NULL,
    sender_number   TEXT NOT NULL,
    message_text    TEXT NOT NULL,
    message_hash    TEXT NOT NULL UNIQUE,  -- md5 of text, deduplication
    processed       BOOLEAN NOT NULL DEFAULT FALSE,
    processing_started_at TIMESTAMPTZ,
    processing_error TEXT
);
CREATE INDEX idx_whatsapp_unprocessed ON whatsapp_messages(processed, created_at);

-- One row per job posting that passed initial filter
CREATE TABLE pipeline_runs (
    trace_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id      UUID NOT NULL REFERENCES whatsapp_messages(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Job details extracted by Manager
    job_title       TEXT,
    company         TEXT,
    job_summary     TEXT,
    poster_number   TEXT,
    poster_email    TEXT,           -- null if not found in message
    relevance_score INTEGER,        -- 0-10
    relevance_reason TEXT,

    -- Pipeline status
    status          TEXT NOT NULL DEFAULT 'started',
    -- Values: started | research_done | resume_ready | pdf_ready | sent | failed | discarded

    -- Agent decisions (JSONB for full trace)
    manager_decision    JSONB,      -- routing decision + reasoning
    research_output     JSONB,      -- gap analysis results
    resume_eval         JSONB,      -- evaluator scores
    quality_gate_result JSONB,      -- final manager quality check
    outbound_action     TEXT,       -- 'email' | 'whatsapp' | null

    -- Errors
    error_stage     TEXT,
    error_message   TEXT
);
CREATE INDEX idx_pipeline_status ON pipeline_runs(status, created_at);

-- Version history for every tailored resume
CREATE TABLE resume_versions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id        UUID NOT NULL REFERENCES pipeline_runs(trace_id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    version_number  INTEGER NOT NULL DEFAULT 1,
    docx_path       TEXT,
    pdf_path        TEXT,
    changes_made    JSONB,          -- list of edits applied
    ats_score_before INTEGER,
    ats_score_after  INTEGER,
    evaluator_passed BOOLEAN
);

-- Every outbound communication (email or whatsapp)
CREATE TABLE outbox (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id        UUID NOT NULL REFERENCES pipeline_runs(trace_id),
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    channel         TEXT NOT NULL,  -- 'email' | 'whatsapp'
    recipient       TEXT NOT NULL,  -- email address or whatsapp number
    subject         TEXT,           -- email only
    body_preview    TEXT,           -- first 200 chars
    attachment_path TEXT,
    external_id     TEXT,           -- gmail message_id or waha msg id
    status          TEXT NOT NULL DEFAULT 'sent'  -- 'sent' | 'failed'
);

-- Your candidate profile (loaded by agents)
CREATE TABLE candidate_profile (
    id              SERIAL PRIMARY KEY,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    full_name       TEXT NOT NULL,
    email           TEXT NOT NULL,
    phone           TEXT,
    linkedin_url    TEXT,
    resume_text     TEXT NOT NULL,  -- full resume as plain text
    target_roles    TEXT[],         -- ['ML Engineer', 'AI Engineer', ...]
    target_stack    TEXT[],         -- ['Python', 'LLMs', 'RAG', ...]
    location_pref   TEXT
);

-- Agent decision log (every LLM call traced here)
CREATE TABLE agent_traces (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id        UUID REFERENCES pipeline_runs(trace_id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    agent_name      TEXT NOT NULL,
    model           TEXT NOT NULL,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    latency_ms      INTEGER,
    decision        TEXT,           -- human readable summary of decision
    full_input      JSONB,          -- full prompt context (for debugging)
    full_output     JSONB           -- full model response
);
CREATE INDEX idx_traces_trace_id ON agent_traces(trace_id, created_at);
```

---

## 4. Anthropic Agent Skills — Complete Set

### Folder Structure
```
skills/
├── job-manager/
│   ├── SKILL.md                          ← orchestrator with 3 sub-skills
│   └── references/
│       ├── candidate_profile.md          ← your profile (filled by you)
│       └── routing_rules.md              ← decision logic reference
│
├── job-relevance-evaluator/
│   ├── SKILL.md
│   └── references/
│       └── target_profile.md             ← roles + stack + dealbreakers
│
├── resume-research/
│   ├── SKILL.md
│   └── references/
│       ├── base_resume.md                ← your resume as plain text
│       └── research_methodology.md       ← how to do gap analysis
│
├── resume-editor/
│   ├── SKILL.md
│   ├── references/
│   │   ├── base_resume.md                ← master resume (same file)
│   │   └── style_rules.md                ← format + tone rules
│   └── scripts/
│       └── ats_scorer.py                 ← keyword density checker
│
├── pdf-converter/
│   ├── SKILL.md
│   └── scripts/
│       └── convert.py
│
├── gmail-composer/
│   ├── SKILL.md
│   └── references/
│       └── email_templates.md            ← 3 outreach templates
│
└── whatsapp-composer/
    ├── SKILL.md
    └── references/
        └── message_templates.md          ← WhatsApp message templates
```

---

### SKILL: job-manager/SKILL.md
```markdown
---
name: job-manager
description: Master orchestrator for the job application pipeline. Coordinates three sub-responsibilities: (1) evaluating if a job is relevant using job-relevance-evaluator skill, (2) deciding which agents to invoke and in what order using pipeline-coordinator logic, (3) performing final quality gate review before outbound communication. Use when a new job message has been received from WhatsApp and a pipeline_run trace_id has been created in the database.
metadata:
  author: Pranay
  version: 2.0.0
  sub-skills: job-relevance-evaluator, resume-research, resume-editor
---

# Manager Agent — Orchestrator

## Your Three Responsibilities

### Responsibility 1: Job Relevance Evaluation
Load references/candidate_profile.md. Evaluate the incoming job message.
Delegate detailed classification rules to job-relevance-evaluator skill.

Output:
```json
{
  "relevant": true|false,
  "score": 0-10,
  "job_title": "...",
  "company": "...",
  "job_summary": "2-3 sentences",
  "poster_email": "email or null",
  "discard_reason": "reason or null"
}
```

If score < 6: set pipeline status = 'discarded', log reason, STOP.
If score >= 6: continue to Responsibility 2.

### Responsibility 2: Pipeline Coordination
Consult references/routing_rules.md.
Always run agents in this exact order:
1. research_agent → gets gap analysis
2. resume_editor_agent → applies edits, produces docx
3. pdf_converter_agent → converts to pdf
4. quality_gate (Responsibility 3)

Write each stage result to pipeline_runs.manager_decision JSONB as you go.

### Responsibility 3: Quality Gate + Routing
Review resume_versions row for this trace_id.
PASS criteria: evaluator_passed = true AND ats_score_after >= 65
FAIL: return resume_editor with specific feedback (max 1 retry)

After PASS:
- poster_email is NOT null → route to gmail-composer skill
- poster_email is null     → route to whatsapp-composer skill

## Output Contract
Every response from this agent MUST be valid JSON. No preamble.
Always include trace_id in every response.
```

---

### SKILL: job-relevance-evaluator/SKILL.md
```markdown
---
name: job-relevance-evaluator
description: Classifies a WhatsApp message to determine if it is a relevant job posting for a Python/ML/AI/data science engineer. Use when the job-manager skill needs to evaluate a new message. Loads target profile from references/target_profile.md. Returns structured relevance assessment with score, extracted details, and contact information.
metadata:
  author: Pranay
  version: 2.0.0
---

# Job Relevance Evaluator

## Target Profile
Load from references/target_profile.md before evaluating.

## Classification Logic

### RELEVANT (score 6-10) — message mentions ANY of:
Technical: Python, ML, machine learning, data science, AI, LLM, agents, NLP,
RAG, vector databases, embeddings, fine-tuning, MLOps, AI engineering
Frameworks: LangChain, LlamaIndex, Hugging Face, PyTorch, TensorFlow, scikit-learn,
FastAPI, Anthropic, OpenAI
Roles: ML Engineer, AI Engineer, Data Scientist, Python Developer, Backend Python,
Data Engineer, AI Researcher, Software Engineer (Python)

Score breakdown:
- 9-10: Perfect match — exact role + exact stack + good seniority fit
- 7-8:  Strong match — right domain, some stack overlap
- 6:    Marginal match — Python/engineering but unclear AI component

### NOT RELEVANT (score 0-5) — discard if:
- Java/.NET/PHP/Ruby only, no Python mentioned
- Pure frontend only (React/Vue/Angular with no backend)
- Non-tech: sales, marketing, HR, finance, design
- Message is not a job post: it is chat, a question, news, or spam
- Role requires physical presence in a location you cannot work from
- Already processed (check message_hash)

## Email Extraction
Scan message text for email pattern: [word]@[domain].[tld]
Return first match found, or null.

## Output — ONLY valid JSON, no other text:
{
  "relevant": true|false,
  "score": 0-10,
  "job_title": "extracted or best-guess title",
  "company": "company name or Unknown",
  "job_summary": "2-3 sentence summary of role and requirements",
  "poster_email": "email@domain.com or null",
  "discard_reason": "specific reason if not relevant, null if relevant",
  "confidence": "high|medium|low"
}
```

---

### SKILL: resume-research/SKILL.md
```markdown
---
name: resume-research
description: Performs gap analysis between a job description and a candidate's resume. Identifies what skills and keywords to ADD to the resume and what to REMOVE or de-emphasise to better match the target role. Use after a job has been classified as relevant. Loads the candidate's base resume from references/base_resume.md. Returns structured action items with clear priorities.
metadata:
  author: Pranay
  version: 2.0.0
---

# Resume Research Agent

## Role
Technical career strategist. Gap analysis specialist. You identify exactly what
to change in a resume — additions AND removals — to maximise match with a job.

## Methodology
Load from references/research_methodology.md before starting.

## Step 1: Deconstruct the Job Description
Extract from the job summary:
- Hard requirements (must-have)
- Soft requirements (nice-to-have)
- ATS keywords (exact phrases, tool names, frameworks)
- Seniority signals (years of experience, leadership)
- Domain knowledge required

## Step 2: Audit the Current Resume
Load references/base_resume.md.
For each requirement:
- STRONG MATCH: clearly evidenced with metrics → no change needed
- WEAK MATCH: present but vague or unquantified → strengthen
- MISSING BUT APPLICABLE: candidate has this but resume doesn't show it → add
- MISSING NOT APPLICABLE: candidate genuinely lacks this → note gap only

## Step 3: What to REMOVE or De-emphasise
Identify resume content that:
- Is irrelevant to this specific role (e.g. Java work on a Python-only JD)
- Takes space from more relevant content
- Dates poorly (tech from 5+ years ago not relevant here)

## Step 4: Prioritise
Max 5 ADD items. Max 3 REMOVE/de-emphasise items.
Each must be specific: name the section, the exact change, the reason.

## Output — ONLY valid JSON:
{
  "add_items": [
    {
      "section": "skills",
      "action": "Add 'LLM orchestration with Anthropic SDK' to technical skills",
      "reason": "JD mentions Anthropic API 4 times — direct keyword match",
      "priority": 1
    }
  ],
  "remove_items": [
    {
      "section": "experience_old_job",
      "action": "Remove or shorten Java microservices bullet — not relevant here",
      "reason": "This is a Python-only role, Java experience is noise"
    }
  ],
  "keywords_to_inject": ["LLM", "RAG pipeline", "multi-agent", "Anthropic SDK"],
  "sections_to_edit": ["summary", "skills", "experience_lexisnexis"],
  "ats_score_estimate_before": 42,
  "ats_score_estimate_after": 76,
  "research_reasoning": "One paragraph explanation of main gaps and strategy"
}
```

---

### SKILL: resume-editor/SKILL.md
```markdown
---
name: resume-editor
description: Applies targeted edits to a resume based on research agent action items. Rewrites specific sections only — never the entire resume. Adds keywords naturally, strengthens metrics, removes irrelevant content. Includes a built-in self-evaluation pass. Use after the research agent has produced structured add/remove action items. Loads base resume from references/base_resume.md.
metadata:
  author: Pranay
  version: 2.0.0
---

# Resume Editor Agent

## Role
Precision resume editor. You apply specific, targeted changes. You never rewrite from scratch.
You never fabricate experience, tools, or companies the candidate has not worked with.

## Pre-Edit Checklist (REQUIRED before any edits)
Read ALL of references/base_resume.md — understand structure and voice.
Read ALL of references/style_rules.md — understand formatting constraints.
Read the research action items in full.
Only then begin editing.

## Editing Rules

### Adding content:
- Insert keywords WITHIN existing bullets — do not create new bullet points
  unless explicitly in action items
- Maintain the candidate's existing voice and verb tense
- Every added keyword must be justified by the research action items
- Quantify where candidate has real data — never invent numbers

### Removing content:
- For 'remove' items: shorten the bullet to one line or delete if instructed
- Never remove contact info, job titles, companies, or dates
- Only remove content explicitly listed in remove_items

### Prohibited:
- Adding tools, frameworks, certifications the candidate has never used
- Changing companies, titles, or dates
- Creating new jobs or projects that don't exist
- Increasing years of experience claims

## Self-Evaluation Pass (REQUIRED before returning output)
Run scripts/ats_scorer.py mentally or call it if available.
Check:
[ ] All keywords from keywords_to_inject are present?
[ ] No fabricated content?
[ ] ATS score improved vs before estimate?
[ ] Reads naturally — no keyword stuffing?
[ ] Style matches references/style_rules.md?
[ ] Sections not in sections_to_edit are UNCHANGED?

If any check fails → fix. Maximum 2 self-correction iterations.

## Output — ONLY valid JSON:
{
  "edited_sections": {
    "summary": "new text for this section",
    "skills": "new text for this section",
    "experience_lexisnexis": "updated bullets as plain text"
  },
  "changes_applied": [
    "Added 'LLM orchestration' to summary — priority 1 from research",
    "Removed Java bullet from early career — priority 1 remove item"
  ],
  "evaluation": {
    "keywords_injected": ["LLM", "RAG pipeline", "multi-agent"],
    "ats_score_before": 42,
    "ats_score_after": 79,
    "checklist_passed": true,
    "iterations": 1
  }
}
```

---

### SKILL: pdf-converter/SKILL.md
```markdown
---
name: pdf-converter
description: Converts a resume .docx file to PDF using LibreOffice headless. Use when a tailored resume docx has been produced and needs to be converted to PDF for attachment. Runs a subprocess command and returns the output PDF path. Simple deterministic task requiring no LLM reasoning.
metadata:
  author: Pranay
  version: 2.0.0
---

# PDF Converter

## Process
1. Receive docx_path from input
2. Run: python scripts/convert.py --input {docx_path} --outdir output/pdfs/
3. Return the pdf_path
4. Update resume_versions row in DB with pdf_path

## Output:
{ "pdf_path": "output/pdfs/...", "status": "success"|"error", "error": null|"message" }

## Fallback
If LibreOffice not available: raise explicit error with install instructions.
Do NOT silently fail. Do NOT attempt HTML-based conversion — quality is unacceptable.
```

---

### SKILL: gmail-composer/SKILL.md
```markdown
---
name: gmail-composer
description: Composes and sends a professional cold outreach email for a job application, with the tailored resume PDF attached. Use when a job posting has an email address, the resume is ready, and the quality gate has passed. Loads email templates from references/email_templates.md. Sends via Gmail API OAuth2.
metadata:
  author: Pranay
  version: 2.0.0
---

# Gmail Composer Agent

## Email Writing Rules

Structure (3 paragraphs maximum):
1. Hook: One specific thing about the role that is genuinely interesting.
   Reference something from the actual job description. Never generic.
2. Match: Your single strongest match to their requirements.
   One or two sentences. Must include a metric.
3. CTA: Ask for a 15-minute call. Simple. No begging.

Tone: Direct. Confident. Human. Not corporate.

Forbidden phrases (never use):
- "I am writing to express my interest"
- "I would be a great fit"
- "Please find attached my resume"
- "I am passionate about"
- "I look forward to hearing from you"

Subject line format: [Job Title] Application — [Your Name]

Sign-off: First name + LinkedIn URL + Phone number

## Load templates from references/email_templates.md before writing.

## Output — ONLY valid JSON:
{
  "to": "recipient@email.com",
  "subject": "ML Engineer Application — Pranay",
  "body": "full email body as plain text",
  "attachment_path": "output/pdfs/company_jobtitle_timestamp.pdf"
}
```

---

### SKILL: whatsapp-composer/SKILL.md
```markdown
---
name: whatsapp-composer
description: Composes a professional WhatsApp message for a job application when no email address was found in the posting. Attaches the resume PDF via WAHA API. Use when the quality gate has passed but poster_email is null. Message should be concise, professional, and appropriate for WhatsApp messaging etiquette.
metadata:
  author: Pranay
  version: 2.0.0
---

# WhatsApp Message Composer

## Message Rules
- Maximum 5 sentences
- WhatsApp tone: professional but conversational (not corporate email tone)
- Sentence 1: Greet + reference the specific role they posted
- Sentence 2: One-line why you're a fit (with one metric)
- Sentence 3: State you're sending your resume
- Sentence 4: Offer a call / ask about next steps
- No bullet points. No formal sign-off headers.
- End with: name + phone number inline

## Load templates from references/message_templates.md.

## Output — ONLY valid JSON:
{
  "to_number": "+1234567890",
  "message_text": "full WhatsApp message",
  "attachment_path": "output/pdfs/company_jobtitle_timestamp.pdf"
}
```

---

## 5. Integration Map — Every External Connection

```
┌──────────────────────────────────────────────────────────────────┐
│                    INTEGRATION REGISTRY                          │
├─────────────────┬────────────────┬──────────────┬───────────────┤
│ Integration     │ Library        │ Auth Method  │ Used By       │
├─────────────────┼────────────────┼──────────────┼───────────────┤
│ WAHA (WhatsApp) │ httpx (REST)   │ WAHA_API_KEY │ Ingest,       │
│                 │                │              │ WA Composer   │
├─────────────────┼────────────────┼──────────────┼───────────────┤
│ PostgreSQL      │ asyncpg +      │ DB URL       │ All agents    │
│                 │ SQLAlchemy 2.0 │              │               │
├─────────────────┼────────────────┼──────────────┼───────────────┤
│ Anthropic API   │ anthropic SDK  │ API Key      │ All LLM calls │
├─────────────────┼────────────────┼──────────────┼───────────────┤
│ Gmail API       │ google-api-    │ OAuth2       │ Gmail         │
│                 │ python-client  │ (token.json) │ Composer      │
├─────────────────┼────────────────┼──────────────┼───────────────┤
│ LibreOffice     │ subprocess     │ None (local) │ PDF Converter │
├─────────────────┼────────────────┼──────────────┼───────────────┤
│ Alembic         │ alembic        │ DB URL       │ DB migrations │
└─────────────────┴────────────────┴──────────────┴───────────────┘
```

---

## 6. Full Dependencies (pyproject.toml)

```toml
[project]
name = "whatsapp-job-agent"
version = "2.0.0"
description = "Automated WhatsApp job monitoring, resume tailoring, and outreach system"
requires-python = "^3.11"

dependencies = [
    # ── Anthropic ──────────────────────────────────────────
    "anthropic>=0.84.0",

    # ── Database ───────────────────────────────────────────
    "asyncpg>=0.29.0",                    # async PostgreSQL driver
    "sqlalchemy[asyncio]>=2.0.30",        # ORM + async session
    "alembic>=1.13.0",                    # DB migrations

    # ── WhatsApp (WAHA via REST) ────────────────────────────
    "httpx>=0.27.0",                      # HTTP client for WAHA REST API

    # ── Gmail ──────────────────────────────────────────────
    "google-auth>=2.28.0",
    "google-auth-oauthlib>=1.2.0",
    "google-auth-httplib2>=0.2.0",
    "google-api-python-client>=2.120.0",

    # ── Resume editing ──────────────────────────────────────
    "python-docx>=1.1.0",

    # ── Config + Runtime ───────────────────────────────────
    "pydantic>=2.7.0",
    "pydantic-settings>=2.3.0",
    "python-dotenv>=1.0.0",
    "schedule>=1.2.0",                    # polling loop
    "structlog>=24.0.0",                  # structured JSON logging
    "rich>=13.7.0",                       # terminal output

    # ── PDF conversion (LibreOffice via subprocess) ─────────
    # System dep: apt install libreoffice (or brew install libreoffice)
    # No pip package needed — subprocess call only

    # ── Observability ──────────────────────────────────────
    "opentelemetry-sdk>=1.24.0",          # traces + spans
    "opentelemetry-exporter-otlp>=1.24.0",
]

[dependency-groups]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
    "ruff>=0.4.0",
    "black>=24.0.0",
    "mypy>=1.10.0",
    "factory-boy>=3.3.0",                 # test data factories
    "respx>=0.21.0",                      # mock httpx for WAHA tests
]
```

---

## 7. Environment Variables (.env.example)

```bash
# ── Anthropic ──────────────────────────────────────────────────
ANTHROPIC_API_KEY=sk-ant-...

# ── Model assignments ──────────────────────────────────────────
MANAGER_MODEL=claude-opus-4-6
RESEARCH_MODEL=claude-sonnet-4-6
RESUME_EDITOR_MODEL=claude-sonnet-4-6
PDF_CONVERTER_MODEL=claude-haiku-4-5-20251001
GMAIL_AGENT_MODEL=claude-sonnet-4-6
WHATSAPP_MSG_MODEL=claude-sonnet-4-6

# ── PostgreSQL ─────────────────────────────────────────────────
DATABASE_URL=postgresql+asyncpg://postgres:password@localhost:5432/jobagent
# For local dev: docker run -e POSTGRES_PASSWORD=password -p 5432:5432 postgres:16

# ── WAHA (WhatsApp) ────────────────────────────────────────────
WAHA_BASE_URL=http://localhost:3000
WAHA_SESSION=default
WAHA_API_KEY=your-waha-api-key
WHATSAPP_GROUP_IDS=GROUPID1@g.us,GROUPID2@g.us,GROUPID3@g.us
POLL_INTERVAL_SECONDS=30

# ── Gmail OAuth2 ───────────────────────────────────────────────
GMAIL_CREDENTIALS_PATH=data/credentials.json
GMAIL_TOKEN_PATH=data/token.json
SENDER_EMAIL=your@gmail.com
SENDER_NAME=Pranay

# ── Quality gate ───────────────────────────────────────────────
MIN_RELEVANCE_SCORE=6
MIN_ATS_SCORE=65
MAX_RESUME_EDIT_ITERATIONS=2

# ── Paths ──────────────────────────────────────────────────────
BASE_RESUME_DOCX=data/base_resume.docx
BASE_RESUME_TEXT=data/base_resume.md
OUTPUT_DIR=output
SKILLS_DIR=skills

# ── Observability ──────────────────────────────────────────────
LOG_LEVEL=INFO
LOG_FORMAT=json                           # 'json' for prod, 'console' for dev
OTEL_ENDPOINT=http://localhost:4317       # optional, for traces
```

---

## 8. Project Folder Structure

```
whatsapp-job-agent/
│
├── pyproject.toml
├── .env.example
├── .gitignore
├── alembic.ini
│
├── skills/                              ← Anthropic Agent Skills (7 skills)
│   ├── job-manager/
│   │   ├── SKILL.md
│   │   └── references/
│   │       ├── candidate_profile.md     ← FILL IN: your profile
│   │       └── routing_rules.md
│   ├── job-relevance-evaluator/
│   │   ├── SKILL.md
│   │   └── references/
│   │       └── target_profile.md        ← FILL IN: target roles + stack
│   ├── resume-research/
│   │   ├── SKILL.md
│   │   └── references/
│   │       ├── base_resume.md           ← FILL IN: your resume as text
│   │       └── research_methodology.md
│   ├── resume-editor/
│   │   ├── SKILL.md
│   │   ├── references/
│   │   │   ├── base_resume.md           ← same file (symlink)
│   │   │   └── style_rules.md           ← FILL IN: your resume style
│   │   └── scripts/
│   │       └── ats_scorer.py
│   ├── pdf-converter/
│   │   ├── SKILL.md
│   │   └── scripts/
│   │       └── convert.py
│   ├── gmail-composer/
│   │   ├── SKILL.md
│   │   └── references/
│   │       └── email_templates.md       ← FILL IN: 3 template examples
│   └── whatsapp-composer/
│       ├── SKILL.md
│       └── references/
│           └── message_templates.md     ← FILL IN: WA message templates
│
├── src/
│   ├── main.py                          ← entry point: starts all services
│   ├── ingest.py                        ← WAHA webhook receiver (FastAPI)
│   ├── watcher.py                       ← DB polling loop → pipeline trigger
│   │
│   ├── core/
│   │   ├── base_agent.py                ← BaseAgent ABC
│   │   ├── config.py                    ← Pydantic settings
│   │   ├── database.py                  ← SQLAlchemy async engine + session
│   │   ├── models.py                    ← SQLAlchemy ORM models (all tables)
│   │   └── tracer.py                    ← agent trace logger (writes agent_traces)
│   │
│   ├── agents/
│   │   ├── manager_agent.py             ← orchestrator + quality gate
│   │   ├── research_agent.py
│   │   ├── resume_editor_agent.py
│   │   ├── pdf_converter_agent.py
│   │   ├── gmail_agent.py
│   │   └── whatsapp_msg_agent.py
│   │
│   └── connectors/
│       ├── waha.py                      ← WAHA REST API wrapper
│       └── gmail.py                     ← Gmail OAuth2 wrapper
│
├── alembic/
│   └── versions/                        ← DB migration files
│
├── data/
│   ├── base_resume.docx                 ← REQUIRED: your master resume
│   ├── base_resume.md                   ← REQUIRED: same resume as text
│   ├── credentials.json                 ← Gmail OAuth2 (gitignored)
│   └── token.json                       ← Gmail token (gitignored, auto-created)
│
├── output/
│   ├── resumes/                         ← tailored .docx outputs
│   └── pdfs/                            ← converted PDFs
│
└── tests/
    ├── unit/
    │   ├── test_job_filter.py
    │   ├── test_research.py
    │   └── test_resume_editor.py
    └── integration/
        └── test_pipeline.py
```

---

## 9. Tracing & Observability Design

Every agent call writes to two places:

**1. pipeline_runs table** — the high-level trace:
- Status transitions: `started → research_done → resume_ready → pdf_ready → sent`
- Each stage appends to `manager_decision` JSONB column
- Errors write to `error_stage` + `error_message`

**2. agent_traces table** — the low-level LLM call trace:
- Every single `_call_model()` call writes one row
- Records: agent name, model, input/output tokens, latency, the full input + output
- Linked to trace_id so you can replay any pipeline run

**Structured logging** via `structlog`:
```
{"timestamp": "2026-03-05T10:30:00Z", "trace_id": "uuid", "agent": "research_agent",
 "event": "gap_analysis_complete", "ats_before": 42, "ats_after": 76, "level": "info"}
```

This means for every job that comes through you can answer:
- Was it relevant? What score and why?
- What did the research agent suggest to change?
- How many iterations did the resume editor take?
- Was the email/WhatsApp sent? What was the message?
- How many tokens did the whole pipeline cost?

---

## 10. Claude Code Prompts — Run These In Order

---

### [PROMPT 1] Project Scaffold

```
Create a Python project called "whatsapp-job-agent" using Poetry with Python 3.11+.

Add these pip dependencies:
anthropic>=0.84.0, asyncpg>=0.29.0, sqlalchemy[asyncio]>=2.0.30,
alembic>=1.13.0, httpx>=0.27.0, google-auth>=2.28.0,
google-auth-oauthlib>=1.2.0, google-auth-httplib2>=0.2.0,
google-api-python-client>=2.120.0, python-docx>=1.1.0,
pydantic>=2.7.0, pydantic-settings>=2.3.0, python-dotenv>=1.0.0,
schedule>=1.2.0, structlog>=24.0.0, rich>=13.7.0,
opentelemetry-sdk>=1.24.0, fastapi>=0.111.0, uvicorn>=0.30.0

Dev: pytest, pytest-asyncio, ruff, black, mypy, respx, factory-boy

Create the full folder structure from the architecture document including:
- src/core/, src/agents/, src/connectors/
- skills/ with all 7 skill subfolders (each with SKILL.md placeholder + references/ folder)
- alembic/ for migrations
- data/, output/resumes/, output/pdfs/ (with .gitkeep)
- tests/unit/, tests/integration/

Create .env.example with all variables from section 7 of the architecture doc.
Create .gitignore excluding: .env, data/*.json, data/*.db, output/, __pycache__
```

---

### [PROMPT 2] Database Models + Migration

```
In src/core/database.py, create:
- Async SQLAlchemy engine using DATABASE_URL from settings
- AsyncSession factory
- Base declarative class

In src/core/models.py, create SQLAlchemy ORM models for ALL tables from the
architecture schema (section 3):
- WhatsAppMessage
- PipelineRun
- ResumeVersion
- Outbox
- CandidateProfile
- AgentTrace

All UUID primary keys use server_default=text("gen_random_uuid()").
All JSONB columns use postgresql JSONB type.
All timestamps use TIMESTAMPTZ with server_default=func.now().
Add update_at trigger logic for PipelineRun.

In alembic/, initialise alembic with async support (alembic.ini pointing to
DATABASE_URL env var). Create the initial migration from the models.

Add a src/core/database.py function: async def get_session() as an
async context manager.
```

---

### [PROMPT 3] BaseAgent + Config + Tracer

```
In src/core/config.py:
Create a Pydantic BaseSettings class reading from .env with all fields from
.env.example. Add computed properties:
- whatsapp_group_ids_list: list[str] — splits WHATSAPP_GROUP_IDS on comma
- db_url_sync: str — replaces asyncpg with psycopg2 for alembic sync operations

In src/core/base_agent.py:
Create abstract BaseAgent(ABC) that:
- __init__(self, skill_path: str, model: str, db_session, tracer)
- Loads SKILL.md from skill_path as self.system_prompt (full file content)
- Also loads all files in skill_path/references/ and appends to context
- abstract async run(input_data: dict, trace_id: UUID) -> dict
- protected async _call_model(messages, tools=None, max_tokens=2048) -> dict
  * calls anthropic client
  * records start time
  * on response: writes one row to agent_traces table via tracer
  * returns parsed response
- protected _parse_json(response_text: str) -> dict
  * strips markdown fences if present
  * json.loads
  * raises ValueError with full text if fails

In src/core/tracer.py:
Create AgentTracer with method:
  async trace(trace_id, agent_name, model, input_data, output_data,
              tokens_in, tokens_out, latency_ms, decision_summary)
that inserts one row into agent_traces.
Also add method: async update_pipeline_status(trace_id, status, stage_data)
that updates pipeline_runs.status and appends to manager_decision JSONB.
```

---

### [PROMPT 4] WAHA Connector + Ingest Service

```
In src/connectors/waha.py create WAHAConnector(httpx.AsyncClient):
- Base URL + session from settings
- async get_new_messages(group_id: str, since_timestamp: int) -> list[dict]
  GET /api/messages?chatId={group_id}&limit=100&session={session}
  Filter to messages newer than since_timestamp
  Return list of: {id, text, sender_number, timestamp, group_id}
- async send_message(to_number: str, text: str) -> dict
  POST /api/sendText with body {chatId, text, session}
- async send_message_with_file(to_number: str, text: str, file_path: str) -> dict
  POST /api/sendFile — multipart form with file attachment
- async list_groups() -> list[dict]
  GET /api/chats — filter for @g.us chatIds only
- static extract_email(text: str) -> str | None
  Regex: r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
- All methods: catch httpx errors, log with structlog, never raise

In src/ingest.py create a FastAPI app with:
- POST /webhook/waha endpoint that receives WAHA webhook payload
- Extracts: group_id, sender_number, message_text, timestamp from payload
- Computes message_hash = md5(message_text + sender_number)
- Checks DB for duplicate hash — skip if exists
- Inserts new WhatsAppMessage row with processed=False
- Returns 200 immediately (async fire-and-forget insert)
- GET /health endpoint returns {"status": "ok", "groups_monitored": count}
```

---

### [PROMPT 5] Watcher Service

```
In src/watcher.py create WatcherService:

async def watch_loop(db_session, pipeline_runner):
  Runs every POLL_INTERVAL_SECONDS seconds.
  Each tick:
    1. SELECT * FROM whatsapp_messages
       WHERE processed = false
       ORDER BY created_at ASC
       LIMIT 10
    2. For each message:
       a. UPDATE processing_started_at = NOW() (optimistic lock)
       b. await pipeline_runner.run(message)
       c. UPDATE processed = true on success
       d. On exception: UPDATE processing_error = str(e), processed = true
          (mark processed to avoid infinite retry — log the error)
    3. Log tick summary with structlog: {processed_count, errors, latency}

Add a separate async def deduplication_cleanup() that runs daily:
  DELETE FROM whatsapp_messages
  WHERE processed = true AND created_at < NOW() - INTERVAL '30 days'

Use asyncio.gather to run both loops concurrently.
```

---

### [PROMPT 6] Manager Agent — Full Orchestrator

```
In src/agents/manager_agent.py create ManagerAgent(BaseAgent):
skill_path = "skills/job-manager"
model = settings.MANAGER_MODEL

async def run(message: WhatsAppMessage, trace_id: UUID) -> dict:

  STEP 1 — Relevance evaluation:
  Call _call_model with message.message_text + sender info.
  Parse JSON relevance response.
  Write to pipeline_runs: job_title, company, relevance_score, poster_email.
  If score < MIN_RELEVANCE_SCORE:
    update pipeline status = 'discarded'
    return {"action": "discarded", "reason": discard_reason}

  STEP 2 — Research:
  Instantiate ResearchAgent, call agent.run(job_data, trace_id)
  Write research_output to pipeline_runs.research_output JSONB
  Update status = 'research_done'

  STEP 3 — Resume editing:
  Instantiate ResumeEditorAgent, call agent.run(research_output, trace_id)
  Write to resume_versions table (new row)
  Update status = 'resume_ready'

  STEP 4 — PDF conversion:
  Instantiate PDFConverterAgent, call agent.run({docx_path}, trace_id)
  Update resume_versions row: add pdf_path
  Update status = 'pdf_ready'

  STEP 5 — Quality gate:
  Load resume_versions row. Check evaluator_passed and ats_score_after.
  Call _call_model with resume eval summary for quality gate decision.
  If FAIL and retry_count < 1:
    Go back to STEP 3 with quality_feedback included
  If FAIL after retry:
    Update status = 'failed', error_stage = 'quality_gate'
    Return failure

  STEP 6 — Routing:
  If poster_email is NOT null:
    Instantiate GmailAgent, call agent.run(context, trace_id)
    outbound_action = 'email'
  Else:
    Instantiate WhatsAppMsgAgent, call agent.run(context, trace_id)
    outbound_action = 'whatsapp'

  Update pipeline status = 'sent'
  Write outbox row
  Return full pipeline summary

Wrap every step in try/except. On error: update pipeline_runs with
error_stage and error_message. Re-raise for watcher to catch.
```

---

### [PROMPT 7] Research Agent

```
In src/agents/research_agent.py create ResearchAgent(BaseAgent):
skill_path = "skills/resume-research"
model = settings.RESEARCH_MODEL

async def run(job_data: dict, trace_id: UUID) -> dict:
  1. Load base resume text from settings.BASE_RESUME_TEXT (read file)
  2. Build user message:
     "Job Summary:\n{job_summary}\n\nCandidate Resume:\n{resume_text}"
  3. Call _call_model
  4. Parse JSON response
  5. Write result to pipeline_runs.research_output
  6. Trace: write agent_traces row with decision_summary =
     f"Found {len(add_items)} additions, {len(remove_items)} removals.
      ATS estimate: {before} -> {after}"
  7. Return research output dict

Create skills/resume-research/SKILL.md with the full content from
architecture section 4 (resume-research skill).

Create skills/resume-research/references/research_methodology.md:
"# Research Methodology
1. Always read the full job description before analysing
2. Prioritise explicit requirements over implied ones
3. ATS keywords = exact phrases, not paraphrases
4. Maximum 5 ADD items — quality over quantity
5. Only suggest truthful additions
6. Score ATS before/after honestly — do not inflate estimates"
```

---

### [PROMPT 8] Resume Editor Agent

```
In src/agents/resume_editor_agent.py create ResumeEditorAgent(BaseAgent):
skill_path = "skills/resume-editor"
model = settings.RESUME_EDITOR_MODEL

async def run(research_output: dict, trace_id: UUID, feedback: str = None) -> dict:
  1. Load base resume docx using python-docx
     Extract each section as a dict: {section_name: paragraph_text}
     Sections identified by heading style or bold text
  2. Build prompt with:
     - research action items (add_items, remove_items, keywords_to_inject)
     - current section texts for sections in sections_to_edit only
     - feedback string if this is a retry (append: "Previous attempt failed: {feedback}")
  3. Call _call_model (max_tokens=4096)
  4. Parse edited_sections JSON
  5. Apply edits back to the loaded docx:
     For each key in edited_sections:
       Find paragraph(s) in docx matching that section
       Replace the run text
  6. Save to: output/resumes/{company}_{job_title}_{trace_id[:8]}_v{version}.docx
  7. Insert resume_versions row:
     docx_path, changes_made, ats_score_before, ats_score_after, evaluator_passed
  8. Return: {docx_path, version_id, evaluation}

Create skills/resume-editor/SKILL.md with full content from architecture section 4.
Create skills/resume-editor/references/style_rules.md placeholder.
Create skills/resume-editor/scripts/ats_scorer.py:
  A simple Python script that takes a text file and a keywords list,
  returns a score 0-100 based on keyword presence and density.
```

---

### [PROMPT 9] Gmail + WhatsApp Outbound Agents

```
In src/connectors/gmail.py create GmailConnector:
- Loads credentials from settings.GMAIL_CREDENTIALS_PATH
- Saves/refreshes token to settings.GMAIL_TOKEN_PATH
- Scopes: gmail.send, gmail.compose
- async send(to, subject, body, attachment_path=None) -> str (returns message_id)
  Build MIMEMultipart, attach PDF if path provided, encode base64, send via
  users().messages().send()

In src/agents/gmail_agent.py create GmailAgent(BaseAgent):
skill_path = "skills/gmail-composer"
model = settings.GMAIL_AGENT_MODEL

async def run(context: dict, trace_id: UUID) -> dict:
  context contains: job_summary, job_title, company, poster_email, pdf_path
  1. Call _call_model to generate email subject + body JSON
  2. Call GmailConnector.send(to=poster_email, subject, body, attachment_path=pdf_path)
  3. Write outbox row: channel='email', recipient, subject, body[:200], attachment_path, external_id=message_id
  4. Return {sent: true, message_id, subject}

In src/agents/whatsapp_msg_agent.py create WhatsAppMsgAgent(BaseAgent):
skill_path = "skills/whatsapp-composer"
model = settings.WHATSAPP_MSG_MODEL

async def run(context: dict, trace_id: UUID) -> dict:
  context contains: job_summary, job_title, company, poster_number, pdf_path
  1. Call _call_model to generate WhatsApp message JSON
  2. Call WAHAConnector.send_message_with_file(
       to_number=poster_number, text=message_text, file_path=pdf_path)
  3. Write outbox row: channel='whatsapp', recipient=poster_number, body[:200]
  4. Return {sent: true, to: poster_number}
```

---

### [PROMPT 10] PDF Converter + Main Entry Point

```
In src/agents/pdf_converter_agent.py create PDFConverterAgent(BaseAgent):
skill_path = "skills/pdf-converter"
model = settings.PDF_CONVERTER_MODEL  # haiku — minimal reasoning

async def run(input_data: dict, trace_id: UUID) -> dict:
  docx_path = input_data["docx_path"]
  out_dir = Path(settings.OUTPUT_DIR) / "pdfs"
  out_dir.mkdir(parents=True, exist_ok=True)
  result = subprocess.run(
    ["libreoffice", "--headless", "--convert-to", "pdf",
     "--outdir", str(out_dir), docx_path],
    capture_output=True, text=True, timeout=60
  )
  if result.returncode != 0:
    raise RuntimeError(f"LibreOffice failed: {result.stderr}")
  pdf_path = str(out_dir / Path(docx_path).with_suffix(".pdf").name)
  return {"pdf_path": pdf_path, "status": "success"}

In src/main.py create the application entry point that:
1. Loads settings + validates required files exist (base_resume.docx, credentials.json)
2. Runs alembic migrations on startup: alembic upgrade head
3. Starts three concurrent asyncio tasks:
   a. FastAPI ingest server on port 8000 (via uvicorn.Server)
   b. WatcherService polling loop
   c. Daily cleanup coroutine
4. On startup: print a rich table showing:
   - All 3 group IDs being monitored
   - Model assigned to each agent
   - DB connection status
   - WAHA connection status (ping /api/server/status)
   - Gmail token status (valid/expired/missing)
5. Handle SIGINT gracefully: stop all loops, close DB connections

Add CLI: python -m src.main --dry-run
  Fetches last 5 messages from each group and runs them through
  Job Filter only (no resume editing) to verify connectivity.
```

---

### [PROMPT 11] Fill-In Files You Must Complete Before Running

```
Create these placeholder files with clear instructions on what to fill in:

1. skills/job-relevance-evaluator/references/target_profile.md
   Content template:
   # My Target Profile — Fill This In Before Running

   ## Target Roles (I want to apply to these)
   - ML Engineer
   - AI Engineer
   - Data Scientist
   - Python Developer (AI/ML focus)
   - [ADD YOUR OWN]

   ## Tech Stack I Know
   - Languages: Python (primary), [add others]
   - ML/AI: LLMs, RAG, Anthropic SDK, [add others]
   - Infra: AWS Lambda, [add others]
   - [ADD MORE]

   ## Dealbreakers (never apply even if Python)
   - Requires relocation to [CITY] — I cannot relocate
   - Requires clearance
   - [ADD YOUR OWN]

2. skills/job-manager/references/candidate_profile.md
   Content template:
   # Candidate Profile
   Name: Pranay
   Current Role: AI Engineer at LexisNexis
   Email: [YOUR EMAIL]
   LinkedIn: [YOUR LINKEDIN URL]
   Phone: [YOUR PHONE]
   Location: [YOUR CITY] — open to remote

3. data/base_resume.md
   Paste your full resume here as plain text.
   This is what the research and editor agents will read.
   Structure as: SUMMARY / SKILLS / EXPERIENCE / EDUCATION

4. skills/gmail-composer/references/email_templates.md
   Content template:
   # Email Templates
   ## Template A — Direct application
   [Example of a good cold email — write 3 real examples you would send]

5. skills/whatsapp-composer/references/message_templates.md
   Content template:
   # WhatsApp Message Templates
   ## Template A
   [Example of a good WhatsApp message — write 2 real examples]
```

---

## 11. Setup Sequence (First Run)

```bash
# 1. Start PostgreSQL
docker run -d --name jobagent-db \
  -e POSTGRES_PASSWORD=password \
  -e POSTGRES_DB=jobagent \
  -p 5432:5432 postgres:16

# 2. Start WAHA
docker run -d --name waha \
  -p 3000:3000 \
  devlikeapro/waha
# Open http://localhost:3000/dashboard → scan QR code → get group IDs

# 3. Fill in .env (copy from .env.example, add your keys + group IDs)

# 4. Fill in the 5 placeholder files (Prompt 11 above)

# 5. Set up Gmail OAuth2
#    Go to Google Cloud Console → Enable Gmail API → Download credentials.json
#    Place in data/credentials.json
#    First run will open browser for OAuth consent → creates token.json

# 6. Install LibreOffice
apt install libreoffice  # Linux
brew install libreoffice  # Mac

# 7. Install + run
poetry install
poetry run playwright install chromium  # backup only
poetry run alembic upgrade head         # creates all DB tables
poetry run python -m src.main --dry-run # test connectivity
poetry run python -m src.main           # start full system
```

---

## 12. Observability — What You Can Query

```sql
-- See all jobs that came through today
SELECT job_title, company, relevance_score, status, created_at
FROM pipeline_runs
ORDER BY created_at DESC
LIMIT 20;

-- See the full agent trace for a specific job
SELECT agent_name, decision, latency_ms, input_tokens + output_tokens AS total_tokens
FROM agent_traces
WHERE trace_id = 'your-trace-id'
ORDER BY created_at;

-- See all emails/whatsapp sent this week
SELECT channel, recipient, subject, sent_at
FROM outbox
WHERE sent_at > NOW() - INTERVAL '7 days'
ORDER BY sent_at DESC;

-- Cost estimate (tokens used per pipeline run)
SELECT pr.job_title, pr.company,
       SUM(at.input_tokens + at.output_tokens) AS total_tokens
FROM pipeline_runs pr
JOIN agent_traces at ON at.trace_id = pr.trace_id
GROUP BY pr.trace_id, pr.job_title, pr.company
ORDER BY total_tokens DESC;

-- Jobs discarded and why
SELECT job_title, manager_decision->>'discard_reason' AS reason, created_at
FROM pipeline_runs
WHERE status = 'discarded'
ORDER BY created_at DESC;
```

