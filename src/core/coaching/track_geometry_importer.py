"""Offline import service for persistent track geometries from iRacing IBT files."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.persistence import get_iracing_telemetry_dir

from .ibt_track_extractor import TrackSessionMetadata, _output_path, extract_track_geometry, read_ibt_session_metadata
from .storage import get_coaching_storage_dir


_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class IbtTrackCandidate:
    ibt_path: Path
    metadata: TrackSessionMetadata
    modified_ts: float


@dataclass(frozen=True)
class TrackGeometryImportResult:
    status: str
    track_key: str
    geometry_path: Path
    telemetry_dir: Path
    matched_ibt_path: Path | None = None
    existing_source_type: str | None = None
    message: str = ""


def import_track_geometry_from_telemetry(
    track_key: str,
    *,
    telemetry_dir: str | Path | None = None,
    storage_root: str | Path | None = None,
) -> TrackGeometryImportResult:
    """Scan the configured telemetry directory, import IBT geometry, and persist it."""
    requested_track = str(track_key or "").strip()
    if not requested_track:
        raise ValueError("track_key must not be empty")

    storage_path = Path(storage_root) if storage_root is not None else get_coaching_storage_dir()
    telemetry_path = _resolve_telemetry_dir(telemetry_dir)
    requested_geometry_path = _output_path(storage_path, requested_track)
    requested_existing_source_type = _read_saved_source_type(requested_geometry_path)

    if not telemetry_path.is_dir():
        if requested_geometry_path.exists() and requested_existing_source_type in ("ibt", "unknown"):
            message = f"existing geometry kept ({requested_existing_source_type})"
            _LOG.info(
                "[track_geometry_import] skip existing track_key=%s source_type=%s path=%s",
                requested_track,
                requested_existing_source_type,
                requested_geometry_path,
            )
            return TrackGeometryImportResult(
                status="skipped_existing",
                track_key=requested_track,
                geometry_path=requested_geometry_path,
                telemetry_dir=telemetry_path,
                existing_source_type=requested_existing_source_type,
                message=message,
            )
        message = f"telemetry directory not found: {telemetry_path}"
        _LOG.info("[track_geometry_import] %s", message)
        return TrackGeometryImportResult(
            status="telemetry_dir_missing",
            track_key=requested_track,
            geometry_path=requested_geometry_path,
            telemetry_dir=telemetry_path,
            existing_source_type=requested_existing_source_type,
            message=message,
        )

    candidates = find_matching_ibt_files(requested_track, telemetry_dir=telemetry_path)
    if not candidates:
        message = f"no matching ibt found for track_key={requested_track}"
        _LOG.info("[track_geometry_import] %s", message)
        return TrackGeometryImportResult(
            status="no_matching_ibt",
            track_key=requested_track,
            geometry_path=requested_geometry_path,
            telemetry_dir=telemetry_path,
            existing_source_type=requested_existing_source_type,
            message=message,
        )

    selected = candidates[0]
    geometry_path = _output_path(storage_path, selected.metadata.track_key)
    existing_source_type = _read_saved_source_type(geometry_path)
    if geometry_path.exists() and existing_source_type in ("ibt", "unknown"):
        message = f"existing geometry kept ({existing_source_type})"
        _LOG.info(
            "[track_geometry_import] skip existing track_key=%s source_type=%s path=%s",
            selected.metadata.track_key,
            existing_source_type,
            geometry_path,
        )
        return TrackGeometryImportResult(
            status="skipped_existing",
            track_key=selected.metadata.track_key,
            geometry_path=geometry_path,
            telemetry_dir=telemetry_path,
            matched_ibt_path=selected.ibt_path,
            existing_source_type=existing_source_type,
            message=message,
        )

    if existing_source_type == "fallback":
        _LOG.info(
            "[track_geometry_import] replacing fallback geometry track_key=%s existing=%s ibt=%s",
            selected.metadata.track_key,
            geometry_path,
            selected.ibt_path,
        )

    try:
        written = extract_track_geometry(selected.ibt_path, storage_path)
    except Exception as exc:
        message = f"skipped invalid ibt geometry: {exc}"
        _LOG.warning(
            "[track_geometry_import] invalid ibt geometry track_key=%s ibt=%s (%s)",
            selected.metadata.track_key,
            selected.ibt_path,
            exc,
        )
        return TrackGeometryImportResult(
            status="invalid_ibt_geometry",
            track_key=selected.metadata.track_key,
            geometry_path=geometry_path,
            telemetry_dir=telemetry_path,
            matched_ibt_path=selected.ibt_path,
            existing_source_type=existing_source_type,
            message=message,
        )
    message = f"imported from {selected.ibt_path.name}"
    _LOG.info(
        "[track_geometry_import] imported track_key=%s ibt=%s out=%s",
        requested_track,
        selected.ibt_path,
        written,
    )
    return TrackGeometryImportResult(
        status="imported",
        track_key=selected.metadata.track_key,
        geometry_path=written,
        telemetry_dir=telemetry_path,
        matched_ibt_path=selected.ibt_path,
        existing_source_type=existing_source_type,
        message=message,
    )


def find_matching_ibt_files(
    desired_track: str,
    *,
    telemetry_dir: str | Path | None = None,
) -> list[IbtTrackCandidate]:
    """Return matching IBT candidates ordered by most-recent file first."""
    target = str(desired_track or "").strip()
    if not target:
        return []

    telemetry_path = _resolve_telemetry_dir(telemetry_dir)
    if not telemetry_path.is_dir():
        return []

    candidates: list[IbtTrackCandidate] = []
    for ibt_path in sorted(telemetry_path.rglob("*.ibt"), key=lambda path: path.stat().st_mtime, reverse=True):
        try:
            metadata = read_ibt_session_metadata(ibt_path)
        except Exception as exc:
            _LOG.debug("[track_geometry_import] ignoring unreadable ibt=%s (%s)", ibt_path, exc)
            continue
        if _matches_requested_track(target, metadata):
            candidates.append(
                IbtTrackCandidate(
                    ibt_path=ibt_path,
                    metadata=metadata,
                    modified_ts=ibt_path.stat().st_mtime,
                )
            )
    return candidates


def _resolve_telemetry_dir(telemetry_dir: str | Path | None) -> Path:
    if telemetry_dir is None:
        return Path(get_iracing_telemetry_dir())
    return Path(get_iracing_telemetry_dir(telemetry_dir))


def _matches_requested_track(requested_track: str, metadata: TrackSessionMetadata) -> bool:
    requested = _normalize_track_token(requested_track)
    if not requested:
        return False

    aliases = {
        _normalize_track_token(metadata.track_key),
        _normalize_track_token(metadata.track_display_name),
        _normalize_track_token(metadata.track_name),
    }
    if metadata.track_config_name:
        aliases.add(_normalize_track_token(f"{metadata.track_display_name} {metadata.track_config_name}"))
        aliases.add(_normalize_track_token(f"{metadata.track_name} {metadata.track_config_name}"))
    if metadata.track_id is not None:
        aliases.add(str(metadata.track_id))

    return requested in aliases


def _normalize_track_token(value: str) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("__", " ")
    text = text.replace("_", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _read_saved_source_type(geometry_path: Path) -> str | None:
    if not geometry_path.exists():
        return None
    try:
        payload = json.loads(geometry_path.read_text(encoding="utf-8"))
    except Exception:
        return "unknown"
    return _coerce_saved_source_type(payload)


def _coerce_saved_source_type(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "unknown"

    source_type = str(payload.get("source_type") or "").strip().lower()
    if source_type in {"ibt", "fallback"}:
        return source_type

    legacy_source = str(payload.get("source") or payload.get("source_name") or "").strip().lower()
    if legacy_source in {"ibt", "ibt_telemetry"}:
        return "ibt"
    if legacy_source in {"parquet_fallback", "parquet_velocity_integration", "dead_reckoning"}:
        return "fallback"
    if "fallback" in legacy_source:
        return "fallback"
    return "unknown"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Import persistent track geometry from iRacing IBT files.")
    parser.add_argument("--track-key", required=True, help="Canonical coaching track key or track display name")
    parser.add_argument("--telemetry-dir", help="Optional iRacing telemetry directory override")
    parser.add_argument("--storage-root", help="Optional coaching storage root override")
    args = parser.parse_args()

    result = import_track_geometry_from_telemetry(
        args.track_key,
        telemetry_dir=args.telemetry_dir,
        storage_root=args.storage_root,
    )
    print(f"{result.status}: {result.message}")
    print(f"geometry_path={result.geometry_path}")
    if result.matched_ibt_path is not None:
        print(f"matched_ibt={result.matched_ibt_path}")
