from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path


def _pick_binary() -> str:
    for candidate in ("libreoffice", "soffice"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise RuntimeError(
        "LibreOffice executable not found. Install LibreOffice and ensure either "
        "'libreoffice' or 'soffice' is available on PATH."
    )


def convert_docx_to_pdf(input_path: Path, outdir: Path, timeout: int = 60) -> dict[str, object]:
    if not input_path.is_file():
        raise FileNotFoundError(f"Input DOCX file not found: {input_path}")

    outdir.mkdir(parents=True, exist_ok=True)
    binary = _pick_binary()

    command = [
        binary,
        "--headless",
        "--convert-to",
        "pdf",
        "--outdir",
        str(outdir),
        str(input_path),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"LibreOffice conversion failed: {details}")

    expected_path = outdir / f"{input_path.stem}.pdf"
    if expected_path.is_file():
        pdf_path = expected_path
    else:
        candidates = sorted(outdir.glob(f"{input_path.stem}*.pdf"))
        if not candidates:
            raise RuntimeError("Conversion command succeeded but output PDF was not found.")
        pdf_path = candidates[0]

    return {"pdf_path": str(pdf_path), "status": "success", "error": None}


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert DOCX to PDF using LibreOffice")
    parser.add_argument("--input", required=True, help="Path to input DOCX")
    parser.add_argument("--outdir", required=True, help="Output directory for PDF")
    parser.add_argument("--timeout", type=int, default=60, help="Timeout in seconds")
    args = parser.parse_args()

    payload = convert_docx_to_pdf(
        input_path=Path(args.input).expanduser(),
        outdir=Path(args.outdir).expanduser(),
        timeout=args.timeout,
    )
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
