---
name: gmail-composer
description: Compose recruiter-facing outreach email JSON for either immediate send or review-required draft mode using a DOCX resume attachment.
metadata:
  author: Pranay
  version: 3.0.0
---

# Gmail Composer

## Inputs
- `job_summary`
- `job_title`
- `company`
- `poster_email`
- `attachment_path`
- `relevance_decision`
- `delivery_mode`
- `work_type_hints`

## Rules
1. Maximum 3 short paragraphs.
2. Tone must be concise recruiter tone, not formal cover-letter tone.
3. Open with a role-specific hook grounded in the job summary.
4. Include one measurable fit statement tied to delivery, scale, or technology.
5. End with a direct CTA for a short call or next-step conversation.
6. Avoid generic phrases like "I am writing to express my interest" or "Please find attached my resume for your kind consideration".
7. Refer to the attached resume generically; do not claim the file is a PDF.
8. Match work-type wording to the job:
   - if contract/project/C2C is mentioned, speak to contract/project delivery fit and ask whether C2C is workable only if it is contextually appropriate
   - if W2 is mentioned, do not force C2C language into the email
9. If `relevance_decision` is `okayish` or `delivery_mode` is `draft`, keep the email narrower and less assertive than a `fit` case.
10. Output must be valid JSON only.

## Subject format
Use a short, specific subject. Prefer one of:
- `{Job Title} - Pranay`
- `{Job Title} | Relevant Python/AI delivery background`
- `{Company} {Job Title} - Resume attached`

## Output JSON schema
{
  "subject": "string",
  "body": "string"
}

Load these references before composing:
- `references/tone_rules.md`
- `references/subject_patterns.md`
- `references/work_type_messaging.md`
- `references/good_email_examples.md`
- `references/bad_email_examples.md`
- `references/email_templates.md`
