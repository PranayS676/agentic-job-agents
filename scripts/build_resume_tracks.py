from __future__ import annotations

import argparse
import json
from pathlib import Path

from job_agent_runtime.agents.resume_tracks import build_and_write_resume_tracks, load_resume_tracks
from job_platform.config import get_settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build normalized resume track JSON files from PDF resumes.")
    parser.add_argument(
        "--resume-library-dir",
        type=Path,
        default=None,
        help="Directory containing source PDF resume variants. Defaults to RESUME_LIBRARY_DIR.",
    )
    parser.add_argument(
        "--resume-tracks-dir",
        type=Path,
        default=None,
        help="Directory where normalized resume track JSON files will be written. Defaults to RESUME_TRACKS_DIR.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()

    if args.resume_library_dir is not None:
        resume_library_dir = args.resume_library_dir
    elif settings.resume_library_dir is not None:
        resume_library_dir = settings.resolve_path(settings.resume_library_dir)
    else:
        raise SystemExit("RESUME_LIBRARY_DIR is not configured and --resume-library-dir was not provided.")

    if args.resume_tracks_dir is not None:
        resume_tracks_dir = args.resume_tracks_dir
    elif settings.resume_tracks_dir is not None:
        resume_tracks_dir = settings.resolve_path(settings.resume_tracks_dir)
    else:
        raise SystemExit("RESUME_TRACKS_DIR is not configured and --resume-tracks-dir was not provided.")

    pdf_paths = sorted(resume_library_dir.glob("*.pdf"), key=lambda path: path.name.lower())
    if len(pdf_paths) < 3:
        raise SystemExit(
            f"Expected at least 3 PDF resume variants in {resume_library_dir}, found {len(pdf_paths)}."
        )

    written_paths = build_and_write_resume_tracks(pdf_paths, resume_tracks_dir)
    tracks = load_resume_tracks(resume_tracks_dir)

    summary = {
        "resume_library_dir": str(resume_library_dir),
        "resume_tracks_dir": str(resume_tracks_dir),
        "written_tracks": [path.name for path in written_paths],
        "track_count": len(tracks),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
