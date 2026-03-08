from __future__ import annotations

import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
EXTRA_PATHS = [
    ROOT_DIR / "apps" / "backend" / "src",
    ROOT_DIR / "apps" / "agent-runtime" / "src",
    ROOT_DIR / "packages" / "platform" / "src",
    ROOT_DIR / "packages" / "integrations" / "src",
]

for path in reversed(EXTRA_PATHS):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
