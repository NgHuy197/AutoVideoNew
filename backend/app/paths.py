from __future__ import annotations

import os
from pathlib import Path


def confined(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    root_resolved = root.resolve()
    if os.path.commonpath([str(resolved), str(root_resolved)]) != str(root_resolved):
        raise ValueError("path is outside the configured data directory")
    return resolved


def safe_filename(name: str, fallback: str = "video") -> str:
    value = Path(name).name
    value = "".join(ch if ch.isalnum() or ch in " ._()-" else "_" for ch in value).strip(" .")
    return value or fallback
