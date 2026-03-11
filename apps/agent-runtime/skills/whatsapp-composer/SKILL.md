---
name: whatsapp-composer
description: Compose a concise WhatsApp outreach message when no email is available, with resume attachment.
metadata:
  author: Pranay
  version: 2.0.0
---

# WhatsApp Composer

## Inputs
- `job_summary`
- `job_title`
- `company`
- `poster_number`
- `attachment_path`

## Rules
1. Maximum 5 sentences.
2. Mention exact role + company.
3. Include one measurable fit statement.
4. Mention that resume is attached.
5. End with clear next-step ask.
6. Refer to the attached resume generically; do not claim the file is a PDF.
7. Output valid JSON only.

## Output JSON schema
{
  "message_text": "string"
}

Load references from `references/message_templates.md` before composing.
