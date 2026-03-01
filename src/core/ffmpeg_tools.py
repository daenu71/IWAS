"""FFmpeg/FFprobe binary resolution helpers."""

from __future__ import annotations

import os
from pathlib import Path
from shutil import which

from core.resources import get_resource_path


def _tool_filename(base_name: str) -> str:
    """Implement tool filename logic."""
    name = str(base_name or "").strip()
    if os.name == "nt" and not name.lower().endswith(".exe"):
        return f"{name}.exe"
    return name


def _find_bundled_tool(base_name: str) -> Path | None:
    """Find bundled tool."""
    fn = _tool_filename(base_name)
    # PyInstaller one-folder layout -> _internal/tools/ffmpeg/*
    try:
        p = get_resource_path("tools", "ffmpeg", fn)
        if p.exists():
            return p
    except Exception:
        pass
    # Developer layout (authoritative FFmpeg source for local runs).
    try:
        p2 = get_resource_path("third_party", "ffmpeg", "lgpl_shared", "bin", fn)
        if p2.exists():
            return p2
    except Exception:
        pass
    return None


def resolve_media_tool(base_name: str) -> str:
    """Resolve media tool."""
    bundled = _find_bundled_tool(base_name)
    if bundled is not None:
        return str(bundled)

    name = str(base_name or "").strip() or "ffmpeg"
    hit = which(name)
    if hit:
        return str(hit)

    fn = _tool_filename(name)
    hit2 = which(fn)
    if hit2:
        return str(hit2)
    return name


def media_tool_exists(base_name: str) -> bool:
    """Implement media tool exists logic."""
    bundled = _find_bundled_tool(base_name)
    if bundled is not None:
        return True
    name = str(base_name or "").strip() or "ffmpeg"
    return (which(name) is not None) or (which(_tool_filename(name)) is not None)


def resolve_ffmpeg_bin() -> str:
    """Resolve ffmpeg bin."""
    return resolve_media_tool("ffmpeg")


def resolve_ffprobe_bin() -> str:
    """Resolve ffprobe bin."""
    return resolve_media_tool("ffprobe")


def ffmpeg_exists() -> bool:
    """Implement ffmpeg exists logic."""
    return media_tool_exists("ffmpeg")


def ffprobe_exists() -> bool:
    """Implement ffprobe exists logic."""
    return media_tool_exists("ffprobe")
