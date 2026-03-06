"""Diagnostics bundle export and environment risk detection."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import zipfile
from pathlib import Path, PureWindowsPath
from typing import Callable, Iterable, Sequence

from core import persistence

_SECRET_KEY_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|auth[_-]?key|access[_-]?key|client[_-]?secret)"
)
_RUN_META_RE = re.compile(r"^run_\d{4}_meta\.json$", re.IGNORECASE)
_URL_SECRET_RE = re.compile(r"(?i)([?&](?:token|apikey|auth)=)([^&#\s\"']+)")
_INLINE_SECRET_RE = re.compile(r"(?i)\b(token|apikey|auth)\b(\s*[:=]\s*)(['\"]?)([^'\"\s,&]+)(\3)")
_WINDOWS_DRIVE_RE = re.compile(r"(?i)^[a-z]:[\\/]")
_USER_PREFIX_TEXT_RE = re.compile(r"(?i)([a-z]:\\users\\)([^\\/\r\n]+)")


def _looks_like_windows_abs_path(raw: str) -> bool:
    text = str(raw or "").strip()
    return bool(_WINDOWS_DRIVE_RE.match(text))


def _mask_path_text(path_text: str, *, ffmpeg_style: bool = False) -> str:
    text = str(path_text or "").strip().replace("/", "\\")
    if not _looks_like_windows_abs_path(text):
        return text

    try:
        parts = list(PureWindowsPath(text).parts)
    except Exception:
        parts = []
    if len(parts) < 2:
        return "<abs_path>"

    lower_parts = [str(part).lower() for part in parts]
    if len(parts) >= 3 and lower_parts[1] == "users":
        remainder = "\\".join(parts[3:])
        if ffmpeg_style:
            return "<user_path>" + (f"\\{remainder}" if remainder else "")
        return r"C:\Users\<redacted>" + (f"\\{remainder}" if remainder else "\\")

    tail_parts = [part for part in parts[1:] if part not in ("\\", "/")]
    if ffmpeg_style:
        remainder = "\\".join(tail_parts[-2:] if len(tail_parts) >= 2 else tail_parts[-1:])
        return "<abs_path>" + (f"\\{remainder}" if remainder else "")

    remainder = "\\".join(tail_parts[-2:] if len(tail_parts) >= 2 else tail_parts[-1:])
    return "<abs_path>" + (f"\\{remainder}" if remainder else "")


def _scrub_url_tokens(text: str) -> str:
    out = _URL_SECRET_RE.sub(r"\1<redacted>", str(text or ""))
    out = _INLINE_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}<redacted>{m.group(5)}", out)
    return out


def _scrub_semicolon_path_list(text: str) -> str:
    value = str(text or "")
    parts = value.split(";")
    if len(parts) <= 1:
        return value
    if not any(_looks_like_windows_abs_path(part) for part in parts):
        return value
    return ";".join(
        _mask_path_text(part, ffmpeg_style=True) if _looks_like_windows_abs_path(part) else part
        for part in parts
    )


def _scrub_embedded_windows_paths(text: str) -> str:
    out = str(text or "")
    out = re.sub(
        r'(?i)"([a-z]:\\[^"\r\n]+)"',
        lambda m: f'"{_mask_path_text(m.group(1), ffmpeg_style=True)}"',
        out,
    )
    out = re.sub(
        r"(?i)(^|[\s=:(\[])([a-z]:\\[^\s,\"'\]\}\r\n]+)",
        lambda m: f"{m.group(1)}{_mask_path_text(m.group(2), ffmpeg_style=True)}",
        out,
    )
    return out


def scrub_diagnostics_value(value: str) -> str:
    raw = str(value or "")
    leading = raw[: len(raw) - len(raw.lstrip())]
    trailing = raw[len(raw.rstrip()):]
    text = _scrub_url_tokens(raw)
    stripped = text.strip()
    if ";" in text:
        scrubbed_paths = _scrub_semicolon_path_list(text)
        if scrubbed_paths != text:
            return scrubbed_paths
    if _looks_like_windows_abs_path(stripped):
        return f"{leading}{_mask_path_text(stripped, ffmpeg_style=True)}{trailing}"
    return _USER_PREFIX_TEXT_RE.sub(r"\1<redacted>", _scrub_embedded_windows_paths(text))


def mask_ffmpeg_command_args(args: Sequence[str]) -> list[str]:
    masked: list[str] = []
    for arg in args:
        text = str(arg)
        scrubbed = _scrub_url_tokens(text)
        if _looks_like_windows_abs_path(scrubbed):
            scrubbed = _mask_path_text(scrubbed, ffmpeg_style=True)
        masked.append(scrubbed)
    return masked


def format_masked_ffmpeg_command(args: Sequence[str]) -> str:
    return subprocess.list2cmdline(mask_ffmpeg_command_args(args))


def scrub_ffmpeg_command_text(command_text: str) -> str:
    out = _scrub_url_tokens(str(command_text or ""))
    out = re.sub(
        r'(?i)"([a-z]:\\[^"\r\n]+)"',
        lambda m: f'"{_mask_path_text(m.group(1), ffmpeg_style=True)}"',
        out,
    )
    out = re.sub(
        r"(?i)(^|[\s=])([a-z]:\\[^\s\"'\r\n]+)",
        lambda m: f"{m.group(1)}{_mask_path_text(m.group(2), ffmpeg_style=True)}",
        out,
    )
    return _USER_PREFIX_TEXT_RE.sub(r"\1<redacted>", out)


def scrub_diagnostics_text(text: str) -> str:
    out_lines: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = _scrub_url_tokens(raw_line)
        if "[FFMPEG-CMD]" in line:
            prefix, marker, rest = line.partition("[FFMPEG-CMD]")
            out_lines.append(f"{prefix}{marker} {scrub_ffmpeg_command_text(rest.strip())}".rstrip())
            continue
        if "=" in line:
            key, sep, value = line.partition("=")
            out_lines.append(f"{key}{sep}{scrub_diagnostics_value(value)}")
            continue
        out_lines.append(scrub_diagnostics_value(line))
    return "\n".join(out_lines) + ("\n" if str(text or "").endswith("\n") or out_lines else "")


def _sanitize_manifest_value(value):
    if isinstance(value, dict):
        return {str(k): _sanitize_manifest_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_manifest_value(v) for v in value]
    if isinstance(value, str):
        return scrub_diagnostics_value(value)
    return value


def _read_scrubbed_text(path: Path) -> str:
    return scrub_diagnostics_text(path.read_text(encoding="utf-8", errors="replace"))


def _safe_resolve(path: Path) -> Path:
    """Implement safe resolve logic."""
    try:
        return path.expanduser().resolve()
    except Exception:
        try:
            return path.expanduser()
        except Exception:
            return path


def _norm_case_path_text(path: Path) -> str:
    """Implement norm case path text logic."""
    raw = str(_safe_resolve(path))
    return raw.replace("/", "\\").rstrip("\\").lower()


def _path_within(path: Path, root: Path) -> bool:
    """Implement path within logic."""
    path_text = _norm_case_path_text(path)
    root_text = _norm_case_path_text(root)
    if not root_text:
        return False
    return path_text == root_text or path_text.startswith(root_text + "\\")


def collect_onedrive_roots() -> list[Path]:
    """Collect onedrive roots."""
    roots: list[Path] = []
    seen: set[str] = set()

    def _add(candidate: Path | None) -> None:
        """Implement add logic."""
        if candidate is None:
            return
        resolved = _safe_resolve(candidate)
        try:
            if not resolved.is_dir():
                return
        except Exception:
            return
        key = _norm_case_path_text(resolved)
        if not key or key in seen:
            return
        seen.add(key)
        roots.append(resolved)

    user_profile = Path(os.environ.get("UserProfile") or str(Path.home()))
    for env_key in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        raw = str(os.environ.get(env_key) or "").strip()
        if raw:
            _add(Path(raw))

    _add(user_profile / "OneDrive")
    try:
        for child in sorted(user_profile.glob("OneDrive*")):
            _add(child)
    except Exception:
        pass

    # Known folder redirection targets that can be under OneDrive.
    for folder_name in ("Desktop", "Documents", "Pictures", "Videos"):
        folder_path = _safe_resolve(user_profile / folder_name)
        folder_text = _norm_case_path_text(folder_path)
        if ("\\onedrive" in folder_text) or any(_path_within(folder_path, root) for root in roots):
            _add(folder_path)

    return roots


def is_onedrive_sync_path(path: str | Path | None, *, roots: Sequence[Path] | None = None) -> bool:
    """Return whether onedrive sync path."""
    raw = str(path or "").strip()
    if not raw:
        return False
    target = _safe_resolve(Path(raw))
    target_text = _norm_case_path_text(target)
    if "\\onedrive" in target_text:
        return True
    scan_roots = list(roots) if roots is not None else collect_onedrive_roots()
    return any(_path_within(target, root) for root in scan_roots)


def detect_onedrive_risky_paths(paths: Iterable[str | Path | None]) -> list[Path]:
    """Detect onedrive risky paths."""
    roots = collect_onedrive_roots()
    risky: list[Path] = []
    seen: set[str] = set()
    for item in paths:
        raw = str(item or "").strip()
        if not raw:
            continue
        target = _safe_resolve(Path(raw))
        key = _norm_case_path_text(target)
        if key in seen:
            continue
        if is_onedrive_sync_path(target, roots=roots):
            risky.append(target)
            seen.add(key)
    return risky


def _candidate_log_dirs(project_root: Path) -> list[Path]:
    """Implement candidate log dirs logic."""
    out: list[Path] = []
    seen: set[str] = set()

    def _add(path: Path) -> None:
        """Implement add logic."""
        resolved = _safe_resolve(path)
        key = _norm_case_path_text(resolved)
        if key in seen:
            return
        try:
            if not resolved.is_dir():
                return
        except Exception:
            return
        seen.add(key)
        out.append(resolved)

    _add(project_root / "_logs")
    for env_key in ("LOCALAPPDATA", "APPDATA"):
        base = str(os.environ.get(env_key) or "").strip()
        if not base:
            continue
        base_path = Path(base)
        for rel in ("iWAS/logs", "IWAS/logs", "iWAS/_logs", "IWAS/_logs"):
            _add(base_path / rel)
    return out


def _collect_recent_logs(project_root: Path, *, max_files: int = 16) -> list[Path]:
    """Collect recent logs."""
    candidates: list[Path] = []
    seen: set[str] = set()
    for log_dir in _candidate_log_dirs(project_root):
        try:
            children = list(log_dir.iterdir())
        except Exception:
            continue
        for child in children:
            try:
                if not child.is_file():
                    continue
            except Exception:
                continue
            suffix = str(child.suffix).lower()
            if suffix not in {".log", ".txt", ".jsonl"}:
                continue
            key = _norm_case_path_text(child)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(child)

    def _mtime(path: Path) -> float:
        """Implement mtime logic."""
        try:
            return float(path.stat().st_mtime)
        except Exception:
            return 0.0

    candidates.sort(key=_mtime, reverse=True)
    return candidates[: max(1, int(max_files))]


def _find_latest_session_dir(storage_dir: Path) -> Path | None:
    """Find latest session dir."""
    try:
        if not storage_dir.is_dir():
            return None
    except Exception:
        return None
    latest_dir: Path | None = None
    latest_mtime = -1.0
    try:
        children = list(storage_dir.iterdir())
    except Exception:
        return None
    for child in children:
        try:
            if not child.is_dir():
                continue
            mtime = float(child.stat().st_mtime)
        except Exception:
            continue
        if mtime > latest_mtime:
            latest_mtime = mtime
            latest_dir = child
    return latest_dir


def _read_redacted_text(path: Path) -> str:
    """Read redacted text."""
    text = path.read_text(encoding="utf-8", errors="replace")
    out_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            out_lines.append(line)
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            if _SECRET_KEY_RE.search(key):
                out_lines.append(f"{key.rstrip()} = <redacted>")
                continue
            out_lines.append(f"{key}={value}".rstrip())
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            if _SECRET_KEY_RE.search(key):
                out_lines.append(f"{key.rstrip()}: <redacted>")
                continue
            out_lines.append(f"{key}:{value}".rstrip())
            continue
        out_lines.append(line)
    return scrub_diagnostics_text("\n".join(out_lines) + "\n")


def _unique_arcname(base_name: str, used: set[str]) -> str:
    """Implement unique arcname logic."""
    candidate = base_name
    idx = 2
    while candidate in used:
        stem, dot, suffix = base_name.rpartition(".")
        if dot:
            candidate = f"{stem}_{idx}.{suffix}"
        else:
            candidate = f"{base_name}_{idx}"
        idx += 1
    used.add(candidate)
    return candidate


def export_diagnostics_bundle(
    bundle_path: str | Path,
    *,
    project_root: str | Path,
    coaching_storage_dir: str | Path | None = None,
    output_video_dir: str | Path | None = None,
    progress_cb: Callable[[str], None] | None = None,
    redact_sensitive: bool = True,
) -> Path:
    """Implement export diagnostics bundle logic."""
    def _progress(text: str) -> None:
        """Implement progress logic."""
        if callable(progress_cb):
            try:
                progress_cb(str(text))
            except Exception:
                pass

    project_root_path = _safe_resolve(Path(project_root))
    bundle_path_obj = _safe_resolve(Path(bundle_path))
    bundle_path_obj.parent.mkdir(parents=True, exist_ok=True)
    tmp_bundle = bundle_path_obj.with_name(bundle_path_obj.name + ".tmp")

    if coaching_storage_dir is None:
        try:
            coaching_storage_dir = persistence.resolve_coaching_storage_dir()
        except Exception:
            coaching_storage_dir = ""

    storage_path = _safe_resolve(Path(str(coaching_storage_dir or "").strip() or str(project_root_path / "data" / "coaching")))
    output_path = _safe_resolve(Path(str(output_video_dir or "").strip() or str(project_root_path / "output" / "video")))
    risky_paths = detect_onedrive_risky_paths((storage_path, output_path))

    manifest: dict[str, object] = {
        "generated_at_epoch": time.time(),
        "project_root": str(project_root_path),
        "coaching_storage_dir": str(storage_path),
        "output_video_dir": str(output_path),
        "onedrive_risky_paths": [str(p) for p in risky_paths],
        "included_logs": [],
        "included_configs": [],
        "included_session_meta": [],
        "notes": [],
    }
    used_arcnames: set[str] = set()

    try:
        with zipfile.ZipFile(tmp_bundle, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            _progress("Collecting recent iWAS logs...")
            for log_file in _collect_recent_logs(project_root_path):
                arcname = _unique_arcname(f"logs/{log_file.name}", used_arcnames)
                try:
                    log_text = _read_scrubbed_text(log_file) if redact_sensitive else log_file.read_text(encoding="utf-8", errors="replace")
                    zf.writestr(arcname, log_text)
                    cast_list = manifest["included_logs"]
                    if isinstance(cast_list, list):
                        cast_list.append(str(log_file))
                except Exception as exc:
                    notes = manifest.get("notes")
                    if isinstance(notes, list):
                        notes.append(f"log skipped: {log_file} ({type(exc).__name__}: {exc})")

            _progress("Collecting latest coaching session metadata...")
            latest_session_dir = _find_latest_session_dir(storage_path)
            if latest_session_dir is not None:
                session_meta = latest_session_dir / "session_meta.json"
                if session_meta.exists():
                    arcname = _unique_arcname(f"coaching/{latest_session_dir.name}/{session_meta.name}", used_arcnames)
                    try:
                        session_text = _read_scrubbed_text(session_meta) if redact_sensitive else session_meta.read_text(encoding="utf-8", errors="replace")
                        zf.writestr(arcname, session_text)
                        cast_list = manifest["included_session_meta"]
                        if isinstance(cast_list, list):
                            cast_list.append(str(session_meta))
                    except Exception as exc:
                        notes = manifest.get("notes")
                        if isinstance(notes, list):
                            notes.append(
                                f"session meta skipped: {session_meta} ({type(exc).__name__}: {exc})"
                            )
                run_meta_files: list[Path] = []
                try:
                    for child in latest_session_dir.iterdir():
                        if child.is_file() and _RUN_META_RE.match(child.name):
                            run_meta_files.append(child)
                except Exception:
                    run_meta_files = []
                run_meta_files.sort(
                    key=lambda p: float(p.stat().st_mtime) if p.exists() else 0.0,
                    reverse=True,
                )
                if run_meta_files:
                    run_meta = run_meta_files[0]
                    arcname = _unique_arcname(f"coaching/{latest_session_dir.name}/{run_meta.name}", used_arcnames)
                    try:
                        run_meta_text = _read_scrubbed_text(run_meta) if redact_sensitive else run_meta.read_text(encoding="utf-8", errors="replace")
                        zf.writestr(arcname, run_meta_text)
                        cast_list = manifest["included_session_meta"]
                        if isinstance(cast_list, list):
                            cast_list.append(str(run_meta))
                    except Exception as exc:
                        notes = manifest.get("notes")
                        if isinstance(notes, list):
                            notes.append(f"run meta skipped: {run_meta} ({type(exc).__name__}: {exc})")

            _progress("Writing config snapshots (redacted)...")
            config_candidates = [
                project_root_path / "config" / "defaults.ini",
                project_root_path / "config" / "user_settings.ini",
                project_root_path / "config" / "user.ini",
            ]
            for config_path in config_candidates:
                try:
                    if not config_path.exists() or not config_path.is_file():
                        continue
                except Exception:
                    continue
                try:
                    redacted = _read_redacted_text(config_path) if redact_sensitive else config_path.read_text(encoding="utf-8", errors="replace")
                    arcname = _unique_arcname(f"config/{config_path.name}", used_arcnames)
                    zf.writestr(arcname, redacted)
                    cast_list = manifest["included_configs"]
                    if isinstance(cast_list, list):
                        cast_list.append(str(config_path))
                except Exception as exc:
                    notes = manifest.get("notes")
                    if isinstance(notes, list):
                        notes.append(f"config skipped: {config_path} ({type(exc).__name__}: {exc})")

            _progress("Writing Windows dump pointers...")
            dump_instructions = (
                "Windows crash dump pointers\n"
                "Minidumps folder: <windows_minidump_path>\n"
                "Memory dump file: <windows_memory_dump_path>\n"
                "\n"
                "Please attach the newest .dmp file(s) manually when reporting a crash.\n"
                "iWAS does not auto-copy .dmp files (admin/system access may be required).\n"
            )
            zf.writestr("windows_dump_paths.txt", dump_instructions)

            _progress("Finalizing diagnostics bundle...")
            final_manifest = _sanitize_manifest_value(manifest) if redact_sensitive else manifest
            zf.writestr("diagnostics_manifest.json", json.dumps(final_manifest, indent=2))
    except Exception:
        try:
            tmp_bundle.unlink(missing_ok=True)
        except Exception:
            pass
        raise

    tmp_bundle.replace(bundle_path_obj)
    _progress(f"Diagnostics export complete: {bundle_path_obj}")
    return bundle_path_obj
