# Job AI Application Platform

## Complete Architecture & Execution Plan

Version 1.0

------------------------------------------------------------------------

# 1. Project Overview

## Goal

Build a web application that:

-   Aggregates jobs from multiple sources (Dice, RSS feeds, Gmail
    alerts, company career pages, CSV uploads)
-   Displays all jobs in a unified dashboard
-   Scores job relevance against a user's resume
-   Automatically tailors a resume per job using AI
-   Generates cover letters
-   Creates an "Apply Pack"
-   Tracks application status
-   Assists in applying (without violating portal Terms of Service)

------------------------------------------------------------------------

# 2. Core Principles

1.  Do NOT violate job portal Terms of Service.
2.  Use assisted apply instead of auto-apply bots.
3.  Keep human review before submission.
4.  Never fabricate resume experience.
5.  Build scalable architecture from day one.
6.  Keep modular connector design.

------------------------------------------------------------------------

# 3. High-Level Architecture

## 3.1 Frontend (User Interface)

Built with: - React + TypeScript

Responsibilities: - User authentication - Source connection setup - Job
feed display - Resume upload - Tailored resume preview - Application
tracker - Apply Pack generation

------------------------------------------------------------------------

## 3.2 Backend API (Core Engine)

Built with: - FastAPI - PostgreSQL - Redis - Background workers

Responsibilities: - Authentication (JWT) - Job normalization -
Deduplication - Resume storage - AI orchestration - Application
tracking - Export services (PDF/DOCX)

------------------------------------------------------------------------

## 3.3 Connectors (Job Ingestion Engine)

Plugin-based architecture.

Each connector implements: - search_jobs() - get_job_details() -
get_apply_link()

Supported source types: - Gmail job alerts - RSS feeds - Career sites
(Greenhouse, Lever, Workday patterns) - CSV uploads - Future portal
integrations (if permitted)

------------------------------------------------------------------------

## 3.4 AI Service Layer

Uses ChatGPT/OpenAI API.

Responsibilities: 1. Extract job requirements 2. Extract skills 3.
Compare resume vs job 4. Generate match score 5. Tailor resume bullets
6. Rewrite summary 7. Generate cover letter 8. Generate application Q&A
responses

Safety Rules: - Only rewrite existing resume facts - No new experience
fabrication - Maintain structured fact inventory

------------------------------------------------------------------------

## 3.5 Storage Layer

Primary database: PostgreSQL

Tables:

### Users

-   id
-   email
-   name
-   preferences

### Sources

-   id
-   user_id
-   type
-   config
-   last_fetched_at

### Jobs

-   id
-   source
-   title
-   company
-   location
-   description
-   apply_url
-   fingerprint_hash

### Resumes

-   id
-   user_id
-   structured_data

### ResumeVersions

-   id
-   resume_id
-   job_id
-   tailored_content
-   created_at

### Applications

-   id
-   job_id
-   resume_version_id
-   status
-   notes
-   follow_up_date

------------------------------------------------------------------------

# 4. Job Data Flow

1.  Connector fetches jobs
2.  Data is normalized
3.  Duplicate jobs removed
4.  Match score calculated
5.  Jobs displayed in UI
6.  User clicks "Tailor"
7.  AI generates tailored resume
8.  Resume version saved
9.  Apply Pack generated
10. User clicks Apply (external link opens)

------------------------------------------------------------------------

# 5. Phased Development Plan

## Phase 0 -- Infrastructure Setup (Week 1)

-   Repo structure
-   Docker setup
-   Auth system
-   Database schema
-   CI setup

## Phase 1 -- Unified Job Feed (Weeks 2--3)

-   RSS integration
-   CSV upload
-   Career site parser
-   Deduplication logic
-   Job feed with filters

## Phase 2 -- Resume Profile + Match Scoring (Weeks 4--5)

-   Resume upload
-   Structured parsing
-   Skill extraction
-   Match scoring
-   "Why Matched" explanation

## Phase 3 -- AI Tailored Resume (Weeks 6--8)

-   JD requirement extraction
-   Resume mapping
-   Bullet rewriting
-   Cover letter generation
-   PDF/DOCX export

## Phase 4 -- Assisted Apply + Tracker (Weeks 9--10)

-   Application tracking
-   Apply Pack generation
-   Status workflow
-   Follow-up reminders

## Phase 5 -- Advanced Connectors (Optional)

-   Gmail integration
-   Browser autofill extension
-   Semantic embedding matching
-   Smart recommendations

------------------------------------------------------------------------

# 6. Team Split (2-Person Execution)

## Person A -- Backend & Infrastructure

-   Database schema
-   Connectors
-   Deduplication logic
-   Match scoring
-   AI orchestration
-   Export services
-   Background workers
-   Security implementation

## Person B -- Frontend & UX

-   React UI
-   Job dashboard
-   Resume upload flow
-   Tailored resume preview
-   Application tracker UI
-   API integration
-   User experience optimization

------------------------------------------------------------------------

# 7. Folder Structure

/frontend /src /pages /components /api /backend /app /api /models
/services /connectors /workers /infra README.md

------------------------------------------------------------------------

# 8. Definition of Done

-   Works end-to-end
-   Data stored correctly
-   Error handling implemented
-   Basic tests written
-   Code reviewed
-   Documentation updated

------------------------------------------------------------------------

# Final Outcome

A scalable AI-powered job aggregation and application assistance
platform that:

-   Centralizes job search
-   Automates resume tailoring
-   Assists in job applications
-   Tracks career progress
-   Maintains compliance with job portals

------------------------------------------------------------------------

End of Document
