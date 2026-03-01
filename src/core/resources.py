"""Runtime module for core/resources.py."""

from __future__ import annotations

import sys
from pathlib import Path


def get_resource_path(*parts: str) -> Path:
    """Implement get resource path logic."""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        here = Path(__file__).resolve()
        for parent in [here.parent] + list(here.parents):
            if (parent / "requirements.txt").exists():
                base = parent
                break
        if not base:
            base = here.parents[2]
    path = Path(base)
    for part in parts:
        path = path / str(part)
    return path
