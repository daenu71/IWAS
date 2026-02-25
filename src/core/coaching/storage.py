from __future__ import annotations

from datetime import datetime
import re
import time
from pathlib import Path
from typing import Any


ACTIVE_SESSION_LOCK_FILENAME = ".active_session.lock"
SESSION_FINALIZED_FILENAME = ".finalized"

_SANITIZE_INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_SANITIZE_WHITESPACE_RE = re.compile(r"\s+")
_SANITIZE_REPEAT_UNDERSCORE_RE = re.compile(r"_+")


def get_coaching_storage_dir() -> Path:
    try:
        from core.persistence import resolve_coaching_storage_dir

        return Path(resolve_coaching_storage_dir())
    except Exception:
        fallback = Path(r"C:\iWAS\data\coaching")
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return fallback


def sanitize_name(s: str) -> str:
    text = str(s or "").strip()
    if not text:
        return "unknown"
    text = _SANITIZE_WHITESPACE_RE.sub("_", text)
    text = _SANITIZE_INVALID_CHARS_RE.sub("_", text)
    text = text.replace(".", "_")
    text = _SANITIZE_REPEAT_UNDERSCORE_RE.sub("_", text).strip("_")
    return text or "unknown"


def build_session_folder_name(
    ts: Any,
    track: Any,
    car: Any,
    session_type: Any,
    session_id: Any,
) -> str:
    dt = _coerce_datetime(ts)
    ts_part = dt.strftime("%Y-%m-%d__%H%M%S")
    return "__".join(
        (
            ts_part,
            sanitize_name(str(track or "unknown")),
            sanitize_name(str(car or "unknown")),
            sanitize_name(str(session_type or "unknown")),
            sanitize_name(str(session_id or "unknown")),
        )
    )


def ensure_session_dir(
    ts: Any,
    track: Any,
    car: Any,
    session_type: Any,
    session_id: Any,
    *,
    base_dir: Path | None = None,
) -> Path:
    root_dir = Path(base_dir) if base_dir is not None else get_coaching_storage_dir()
    session_dir = root_dir / build_session_folder_name(ts, track, car, session_type, session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def mark_session_active(session_dir: Path, *, payload: dict[str, Any] | None = None) -> Path:
    lock_path = Path(session_dir) / ACTIVE_SESSION_LOCK_FILENAME
    content = ""
    if payload:
        lines: list[str] = []
        for key, value in payload.items():
            lines.append(f"{key}={value}")
        content = "\n".join(lines) + "\n"
    lock_path.write_text(content, encoding="utf-8")
    return lock_path


def mark_session_finalized(session_dir: Path, *, remove_lock: bool = False) -> Path:
    session_dir = Path(session_dir)
    lock_path = session_dir / ACTIVE_SESSION_LOCK_FILENAME
    if remove_lock:
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass
    finalized_path = session_dir / SESSION_FINALIZED_FILENAME
    finalized_path.write_text("", encoding="utf-8")
    return finalized_path


def _coerce_datetime(ts: Any) -> datetime:
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, time.struct_time):
        return datetime.fromtimestamp(time.mktime(ts))
    try:
        return datetime.fromtimestamp(float(ts))
    except Exception:
        return datetime.now()
