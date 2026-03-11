---
name: whatsapp-composer
description: Compose recruiter-chat WhatsApp JSON for either immediate send or review-required draft mode using a DOCX resume attachment.
metadata:
  author: Pranay
  version: 3.0.0
---

# WhatsApp Composer

## Inputs
- `job_summary`
- `job_title`
- `company`
- `poster_number`
- `attachment_path`
- `relevance_decision`
- `delivery_mode`
- `work_type_hints`

## Rules
1. Maximum 3-4 short sentences.
2. Sound like recruiter-chat outreach, not pasted email text.
3. Mention exact role and company when available.
4. Include one measurable or operational fit statement.
5. Mention that the resume is attached, but refer to it generically; do not claim the file is a PDF.
6. End with one clear next-step ask.
7. If `relevance_decision` is `okayish` or `delivery_mode` is `draft`, use narrower and less assertive wording than a `fit` case.
8. Match work-type wording to the posting:
   - if contract/project/C2C is hinted, it is acceptable to mention delivery fit and ask whether C2C is workable when appropriate
   - if W2 is hinted, do not force C2C-only language into the message
9. Avoid greetings or closings that sound like email.
10. Output valid JSON only.

## Output JSON schema
{
  "message_text": "string"
}

Load these references before composing:
- `references/tone_rules.md`
- `references/work_type_messaging.md`
- `references/good_message_examples.md`
- `references/bad_message_examples.md`
- `references/message_templates.md`
