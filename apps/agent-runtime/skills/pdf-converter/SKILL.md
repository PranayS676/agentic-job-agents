---
name: pdf-converter
description: Converts a resume .docx file to PDF using LibreOffice headless mode. Use when a tailored DOCX resume is ready for outbound attachment.
metadata:
  author: Pranay
  version: 2.0.0
---

# PDF Converter

## Process
1. Receive `docx_path`.
2. Run `python scripts/convert.py --input {docx_path} --outdir output/pdfs/`.
3. Return `pdf_path` in JSON.
4. Fail explicitly if conversion tooling is unavailable.

## Output
{
  "pdf_path": "output/pdfs/file.pdf",
  "status": "success|error",
  "error": null
}

## Fallback Policy
- If `libreoffice` is unavailable, try `soffice`.
- If neither exists, fail with install/PATH instructions.
- Do not silently fail or use low-fidelity HTML conversion fallbacks.
