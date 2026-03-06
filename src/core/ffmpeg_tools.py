"""FFmpeg/FFprobe binary resolution helpers."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from shutil import which

from core.resources import get_resource_path


def _tool_filename(base_name: str) -> str:
    """Implement tool filename logic."""
    name = str(base_name or "").strip()
    if os.name == "nt" and not name.lower().endswith(".exe"):
        return f"{name}.exe"
    return name


def is_packaged_app() -> bool:
    """Return True when running from a PyInstaller packaged app."""
    return bool(getattr(sys, "frozen", False))


def _find_packaged_bundled_tool(base_name: str) -> Path | None:
    """Find the bundled tool inside the packaged app."""
    fn = _tool_filename(base_name)
    # PyInstaller one-folder layout -> _internal/tools/ffmpeg/*
    try:
        p = get_resource_path("tools", "ffmpeg", fn)
        if p.exists():
            return p
    except Exception:
        pass
    return None


def _find_source_bundled_tool(base_name: str) -> Path | None:
    """Find the developer-bundled tool for local source runs."""
    fn = _tool_filename(base_name)
    # Developer layout (authoritative FFmpeg source for local runs).
    try:
        p2 = get_resource_path("third_party", "ffmpeg", "lgpl_shared", "bin", fn)
        if p2.exists():
            return p2
    except Exception:
        pass
    return None


def _find_bundled_tool(base_name: str) -> Path | None:
    """Find the authoritative bundled tool for the current runtime mode."""
    if is_packaged_app():
        return _find_packaged_bundled_tool(base_name)
    return _find_source_bundled_tool(base_name)


def _find_path_tool(base_name: str) -> str | None:
    """Resolve a tool via PATH."""
    name = str(base_name or "").strip() or "ffmpeg"
    hit = which(name)
    if hit:
        return str(hit)

    fn = _tool_filename(name)
    hit2 = which(fn)
    if hit2:
        return str(hit2)
    return None


def _missing_bundled_tool_error(base_name: str) -> RuntimeError:
    """Return the packaged-mode missing-binary error."""
    name = str(base_name or "").strip() or "ffmpeg"
    return RuntimeError(f"Bundled {name} missing")


def classify_media_tool_source(base_name: str, resolved_path: str | os.PathLike[str]) -> str:
    """Classify the resolved media tool as bundled or PATH."""
    bundled = _find_bundled_tool(base_name)
    if bundled is None:
        return "PATH"
    try:
        lhs = os.path.normcase(os.path.abspath(str(resolved_path)))
        rhs = os.path.normcase(os.path.abspath(str(bundled)))
        if lhs == rhs:
            return "bundled"
    except Exception:
        pass
    return "PATH"


def resolve_media_tool(base_name: str) -> str:
    """Resolve media tool."""
    bundled = _find_bundled_tool(base_name)
    if bundled is not None:
        return str(bundled)

    if is_packaged_app():
        raise _missing_bundled_tool_error(base_name)

    hit = _find_path_tool(base_name)
    if hit is not None:
        return hit
    return str(base_name or "").strip() or "ffmpeg"


def media_tool_exists(base_name: str) -> bool:
    """Implement media tool exists logic."""
    bundled = _find_bundled_tool(base_name)
    if bundled is not None:
        return True
    if is_packaged_app():
        return False
    return _find_path_tool(base_name) is not None


def resolve_ffmpeg_bin() -> str:
    """Resolve ffmpeg bin."""
    return resolve_media_tool("ffmpeg")


def resolve_ffprobe_bin() -> str:
    """Resolve ffprobe bin."""
    return resolve_media_tool("ffprobe")


def classify_ffmpeg_source(resolved_path: str | os.PathLike[str]) -> str:
    """Classify the resolved ffmpeg binary source."""
    return classify_media_tool_source("ffmpeg", resolved_path)


def ffmpeg_exists() -> bool:
    """Implement ffmpeg exists logic."""
    return media_tool_exists("ffmpeg")


def ffprobe_exists() -> bool:
    """Implement ffprobe exists logic."""
    return media_tool_exists("ffprobe")
