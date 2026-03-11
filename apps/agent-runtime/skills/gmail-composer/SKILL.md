---
name: gmail-composer
description: Compose and send a concise, high-signal outreach email for a specific job with tailored resume attachment.
metadata:
  author: Pranay
  version: 2.0.0
---

# Gmail Composer

## Inputs
- `job_summary`
- `job_title`
- `company`
- `poster_email`
- `attachment_path`

## Rules
1. Maximum 3 short paragraphs.
2. Open with role-specific hook from job summary.
3. Include one measurable fit statement.
4. End with a direct CTA for a short call.
5. Keep tone professional and human.
6. Avoid generic phrases like "I am writing to express my interest".
7. Refer to the attached resume generically; do not claim the file is a PDF.
8. Output must be valid JSON only.

## Subject format
`{Job Title} Application - Pranay`

## Output JSON schema
{
  "subject": "string",
  "body": "string"
}

Load references from `references/email_templates.md` before composing.
