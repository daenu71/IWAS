"""Lap analysis entry point — bridges flat Sprint-1 storage to AnalysisCache pipeline."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .analysis_cache import AnalysisCache

log = logging.getLogger(__name__)


def _find_flat_lap_meta_path(session_dir: Path, run_id: int, lap_no: int) -> Path:
    """Return the flat lap-meta file whose lap_num matches the given iRacing lap_no.

    The meta files are named by a 1-based sequential write counter that does NOT
    equal the iRacing lap_no (which starts at 0).  We resolve the correct file
    via run_XXXX_meta.json → lap_segments / lap_meta_files mapping.
    Falls back to the direct name-based path when no match is found.
    """
    direct = session_dir / f"run_{run_id:04d}_lap_{lap_no:04d}_meta.json"
    run_meta_path = session_dir / f"run_{run_id:04d}_meta.json"
    if run_meta_path.exists():
        try:
            run_meta = json.loads(run_meta_path.read_text(encoding="utf-8"))
            meta_files: list = run_meta.get("lap_meta_files", [])
            segments: list = run_meta.get("lap_segments", [])
            for idx, seg in enumerate(segments):
                if isinstance(seg, dict) and seg.get("lap_no") == lap_no and idx < len(meta_files):
                    candidate = session_dir / str(meta_files[idx])
                    if candidate.exists():
                        return candidate
        except Exception:
            pass
    return direct


def analyze_lap(session_dir: Path, run_id: int, lap_no: int) -> bool:
    """Run full Sprint-2 analysis pipeline for one lap.

    Reads flat Sprint-1 meta (run_XXXX_lap_YYYY_meta.json), creates the
    expected laps/lap_YYYY/ directory structure, then calls AnalysisCache.compute().

    Returns True if analysis produced a result (computed/partial), False on error.
    """
    session_dir = Path(session_dir)

    # Build the lap directory AnalysisCache expects
    lap_dir = session_dir / "laps" / f"lap_{lap_no:04d}"
    lap_dir.mkdir(parents=True, exist_ok=True)

    # Read the flat Sprint-1 lap meta and translate key names.
    # The meta files use a 1-based sequential counter, not the iRacing lap_no.
    flat_meta_path = _find_flat_lap_meta_path(session_dir, run_id, lap_no)
    adapted_meta: dict = {}
    if flat_meta_path.exists():
        try:
            raw = json.loads(flat_meta_path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        adapted_meta = {
            # index keys AnalysisCache._resolve_start/end_idx recognises
            "start_idx": raw.get("lap_start_sample"),
            "end_idx": raw.get("lap_end_sample"),
            # validity flags AnalysisCache._lap_validity_meta recognises
            "incomplete": not raw.get("lap_complete", True),
            "offtrack": raw.get("offtrack_surface", False),
            # keep originals as well for traceability
            **raw,
        }

    lap_meta_path = lap_dir / "lap_meta.json"
    try:
        lap_meta_path.write_text(json.dumps(adapted_meta, indent=2), encoding="utf-8")
    except Exception:
        pass

    try:
        cache = AnalysisCache()
        result = cache.compute(lap_dir)
        status = result.get("status", "blocked")
        ok = status in {"computed", "partial"}
    except Exception:
        return False

    if ok:
        _try_ensure_track_geometry(session_dir)

    return ok


def _try_ensure_track_geometry(session_dir: Path) -> None:
    """Ensure track_road_geometry.json exists for the session's track.

    Tries (in order):
      1. IBT file scan in ~/Documents/iRacing/telemetry/
      2. Parquet velocity-integration fallback using the session's run_0001.parquet

    Never raises – all errors are logged and silently ignored.
    """
    try:
        from .ibt_track_extractor import (
            extract_track_geometry,
            extract_track_geometry_from_parquet,
            _output_path,
        )
        from .storage import sanitize_name

        storage_root = session_dir.parent
        track_key, _ = _read_track_key_from_session(session_dir)
        if not track_key:
            log.debug("[track_geometry] Cannot determine track key for %s", session_dir)
            return

        out_path = _output_path(storage_root, track_key)
        if out_path.exists():
            log.debug("[track_geometry] Already exists: %s", out_path)
            return

        # 1) Try IBT scan
        ibt_path = _find_ibt_for_track(track_key)
        if ibt_path is not None:
            try:
                written = extract_track_geometry(ibt_path, storage_root)
                log.info("[track_geometry] Extracted from IBT: %s", written)
                return
            except Exception as exc:
                log.warning("[track_geometry] IBT extraction failed: %s", exc)

        # 2) Parquet fallback
        parquet_path = _find_parquet_for_session(session_dir)
        if parquet_path is not None:
            try:
                written = extract_track_geometry_from_parquet(parquet_path, track_key, storage_root)
                log.info("[track_geometry] Extracted from Parquet: %s", written)
                return
            except Exception as exc:
                log.warning("[track_geometry] Parquet extraction failed: %s", exc)

        log.info("[track_geometry] No source available for track key: %s", track_key)

    except Exception as exc:
        log.debug("[track_geometry] Unexpected error in _try_ensure_track_geometry: %s", exc)


def _read_track_key_from_session(session_dir: Path) -> tuple[str, str]:
    """Return (primary_track_key, legacy_track_key) from session_meta.json."""
    try:
        from .storage import sanitize_name

        meta_path = session_dir / "session_meta.json"
        if not meta_path.exists():
            return "", ""
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        track_name = (
            meta.get("TrackDisplayName")
            or meta.get("TrackName")
            or ""
        )
        config_name = (
            meta.get("TrackConfigName")
            or meta.get("TrackConfig")
            or ""
        )
        car_class = meta.get("CarClassShortName") or ""
        if not track_name:
            return "", ""

        primary = f"{sanitize_name(track_name)}__{sanitize_name(config_name)}__{sanitize_name(car_class)}"
        legacy = f"{sanitize_name(track_name)}__{sanitize_name(config_name)}"
        return primary, legacy
    except Exception:
        return "", ""


def _find_ibt_for_track(track_key: str) -> "Path | None":
    """Scan the standard iRacing telemetry folder for an IBT matching *track_key*."""
    import os

    tel_dir = Path(os.path.expanduser("~/Documents/iRacing/telemetry"))
    if not tel_dir.is_dir():
        return None

    # Normalize track_key for fuzzy matching (first segment = track name)
    track_part = track_key.split("__")[0].lower().replace("_", " ")

    for ibt_file in tel_dir.glob("*.ibt"):
        if track_part[:8] in ibt_file.stem.lower():  # compare first 8 chars of track name
            return ibt_file

    return None


def _find_parquet_for_session(session_dir: Path) -> "Path | None":
    """Return the first run_*.parquet in *session_dir*, or None."""
    for p in sorted(session_dir.glob("run_*.parquet")):
        return p
    return None
