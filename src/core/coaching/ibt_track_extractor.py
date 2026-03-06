"""ibt_track_extractor.py – Extract TrackRoadGeometry from an IBT file.

Öffentliche API
---------------
extract_track_geometry(ibt_path, storage_root)
    Öffnet eine IBT-Datei, extrahiert die Streckenmittellinie sowie linke/
    rechte Fahrbahnkante und schreibt track_road_geometry.json unter
    <storage_root>/track_geometries/<track_key>/.

CLI
---
    python src/core/coaching/ibt_track_extractor.py --ibt <Pfad> --storage <storage_root>
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

# ---------------------------------------------------------------------------
# Ensure src/ is on sys.path so that both package import and CLI invocation
# can resolve absolute imports (needed before relative-import fallback below).
# ---------------------------------------------------------------------------
_SRC_DIR = str(Path(__file__).resolve().parent.parent.parent)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

try:
    from .track_geometry import _normalise_xy
    from .storage import sanitize_name
except ImportError:
    from core.coaching.track_geometry import _normalise_xy  # type: ignore[no-redef]
    from core.coaching.storage import sanitize_name  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EDGE_OFFSET_M: float = 5.0          # fallback ±5 m normal offset for edges
_IBT_SAMPLE_DT: float = 1.0 / 60.0  # assumed 60 Hz step when SessionTime unavailable
_MAX_FRAMES: int = 200_000           # safety cap for frame iteration


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_track_geometry(ibt_path: str | Path, storage_root: str | Path) -> Path:
    """Extract TrackRoadGeometry from *ibt_path* and write JSON under *storage_root*.

    Returns the path of the written ``track_road_geometry.json`` file.

    Raises
    ------
    RuntimeError
        If the IBT file cannot be opened.
    """
    ibt_path = Path(ibt_path)
    storage_root = Path(storage_root)

    ir = _open_ibt(ibt_path)
    try:
        track_key = _read_track_key(ir)
        center_m = _extract_centerline_m(ir)
        left_m, right_m = _compute_edges_m(center_m, ir)
    finally:
        _close_ibt(ir)

    center_norm = _to_normalised_list(center_m)
    left_norm = _to_normalised_list(left_m)
    right_norm = _to_normalised_list(right_m)

    out_path = _output_path(storage_root, track_key)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "track_key": track_key,
        "source": "ibt_header",
        "center_line": center_norm,
        "left_edge": left_norm,
        "right_edge": right_norm,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# IBT open / close
# ---------------------------------------------------------------------------


def _open_ibt(ibt_path: Path) -> Any:
    """Open *ibt_path* with pyirsdk and return the IRSDK instance."""
    import irsdk  # type: ignore[import]

    ir = irsdk.IRSDK()
    result = ir.startup(test_file=str(ibt_path))
    # pyirsdk returns False (or None on older versions) when the file is unusable
    if result is False:
        raise RuntimeError(f"irsdk could not open IBT: {ibt_path}")
    return ir


def _close_ibt(ir: Any) -> None:
    try:
        shutdown = getattr(ir, "shutdown", None)
        if callable(shutdown):
            shutdown()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Track-key extraction from session YAML
# ---------------------------------------------------------------------------


def _read_track_key(ir: Any) -> str:
    yaml_text = _get_session_yaml(ir)
    track_display, track_config = _parse_track_names(yaml_text)
    return _build_track_key(track_display, track_config)


def _get_session_yaml(ir: Any) -> str:
    """Return the session info YAML string from *ir*, or empty string."""
    for attr in ("session_info", "sessionInfo", "session_info_yaml", "sessionInfoYaml"):
        val = getattr(ir, attr, None)
        if val is None:
            continue
        text = val() if callable(val) else val
        if isinstance(text, str) and text.strip():
            return text
    # Last-resort subscript access
    try:
        raw = ir["SessionInfo"]
        if isinstance(raw, str):
            return raw
    except Exception:
        pass
    return ""


def _parse_track_names(yaml_text: str) -> tuple[str, str]:
    import re

    m_display = re.search(r"TrackDisplayName\s*:\s*(.+)", yaml_text)
    m_config = re.search(r"TrackConfigName\s*:\s*(.+)", yaml_text)
    display = m_display.group(1).strip() if m_display else "unknown"
    config = m_config.group(1).strip() if m_config else ""
    return display, config


def _build_track_key(track_display: str, track_config: str) -> str:
    parts = [track_display]
    if track_config:
        parts.append(track_config)
    return "__".join(parts)


# ---------------------------------------------------------------------------
# Centerline extraction
# ---------------------------------------------------------------------------


def _extract_centerline_m(ir: Any) -> np.ndarray:
    """Return the center line as an (N, 2) float64 array in metres.

    Priority:
      1. Explicit geometry data in the IBT session header.
      2. VelocityX / VelocityY integration over the first lap (fallback).
    """
    pts = _try_read_header_geometry(ir)
    if pts is not None and len(pts) >= 2:
        return pts

    return _integrate_velocity(ir)


def _try_read_header_geometry(ir: Any) -> np.ndarray | None:
    """Return geometry points from the IBT header, or ``None`` if absent."""
    try:
        weekend = ir["WeekendInfo"]
        if isinstance(weekend, dict):
            for key in ("TrackCenterLine", "CenterLine", "Geometry", "TrackGeometry"):
                val = weekend.get(key)
                if val is not None:
                    pts = _coerce_xy_array(val)
                    if pts is not None and len(pts) >= 2:
                        return pts
    except Exception:
        pass
    return None


def _coerce_xy_array(raw: Any) -> np.ndarray | None:
    """Try to coerce *raw* to an (N, 2) float64 array."""
    try:
        arr = np.asarray(raw, dtype=np.float64)
        if arr.ndim == 2 and arr.shape[1] == 2 and len(arr) >= 2:
            return arr
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# VelocityX/VelocityY dead-reckoning
# ---------------------------------------------------------------------------


def _integrate_velocity(ir: Any) -> np.ndarray:
    """Integrate VelocityX/Y from the first lap into an (N, 2) XY array (m)."""
    frames = _read_first_lap_frames(ir)
    n = len(frames["VelocityX"])
    if n == 0:
        return np.empty((0, 2), dtype=np.float64)

    vx = np.asarray(frames["VelocityX"], dtype=np.float64)
    vy = np.asarray(frames["VelocityY"], dtype=np.float64)
    t_list = frames["SessionTime"]

    if len(t_list) >= 2:
        t_arr = np.asarray(t_list, dtype=np.float64)
        dt = np.diff(t_arr, prepend=t_arr[0])
        median_dt = float(np.nanmedian(np.diff(t_arr)))
        dt = np.where(np.isfinite(dt) & (dt > 0), dt, median_dt)
    else:
        dt = np.full(n, _IBT_SAMPLE_DT, dtype=np.float64)

    vx = np.where(np.isfinite(vx), vx, 0.0)
    vy = np.where(np.isfinite(vy), vy, 0.0)

    x = np.cumsum(vx * dt)
    y = np.cumsum(vy * dt)
    return np.column_stack([x, y])


def _read_first_lap_frames(ir: Any) -> dict[str, list]:
    """Return lists of VelocityX, VelocityY, SessionTime for the first lap."""
    result: dict[str, list] = {
        "VelocityX": [],
        "VelocityY": [],
        "SessionTime": [],
    }
    try:
        _fill_frames_via_parse_to(ir, result)
    except Exception:
        pass
    return result


def _fill_frames_via_parse_to(ir: Any, result: dict[str, list]) -> None:
    """Advance through IBT frames with ``parse_to`` and collect telemetry."""
    parse_to = getattr(ir, "parse_to", None)
    if not callable(parse_to):
        return

    seen_high = False
    n = 0

    while n < _MAX_FRAMES:
        t = _safe_float(ir["SessionTime"])
        if t is None:
            break

        vx = _safe_float(ir["VelocityX"])
        vy = _safe_float(ir["VelocityY"])
        ldp = _safe_float(ir["LapDistPct"])

        result["SessionTime"].append(t)
        result["VelocityX"].append(vx if vx is not None else 0.0)
        result["VelocityY"].append(vy if vy is not None else 0.0)
        n += 1

        # Detect lap wrap-around via LapDistPct
        if ldp is not None:
            if ldp > 0.85:
                seen_high = True
            if seen_high and ldp < 0.15:
                break   # first lap completed

        # Advance to next frame (~60 Hz step)
        try:
            ok = parse_to(t + _IBT_SAMPLE_DT)
        except Exception:
            break
        if not ok:
            break


def _safe_float(val: Any) -> float | None:
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Edge computation
# ---------------------------------------------------------------------------


def _compute_edges_m(
    center_m: np.ndarray,
    ir: Any,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (left_m, right_m) edge arrays in metres.

    Uses track-width metadata from the session YAML when available;
    falls back to ±``_EDGE_OFFSET_M`` along the center-line normal.
    """
    if len(center_m) < 2:
        empty = np.empty((0, 2), dtype=np.float64)
        return empty, empty

    normals = _compute_normals(center_m)
    half_w = _read_track_half_width(ir)
    offset = half_w if half_w is not None else _EDGE_OFFSET_M

    left_m = center_m + normals * offset
    right_m = center_m - normals * offset
    return left_m, right_m


def _read_track_half_width(ir: Any) -> float | None:
    """Return half the track width in metres, or ``None`` if unavailable."""
    try:
        weekend = ir["WeekendInfo"]
        if isinstance(weekend, dict):
            for key in ("TrackWidth", "TrackWidthM", "TrackWidthMeters"):
                val = weekend.get(key)
                w = _safe_float(val)
                if w is not None and w > 0.5:
                    return w / 2.0
    except Exception:
        pass

    # Regex fallback on raw YAML
    import re

    yaml_text = _get_session_yaml(ir)
    m = re.search(r"TrackWidth\s*:\s*([\d.]+)", yaml_text)
    if m:
        try:
            w = float(m.group(1))
            if w > 0.5:
                return w / 2.0
        except Exception:
            pass
    return None


def _compute_normals(pts: np.ndarray) -> np.ndarray:
    """Return unit left-perpendicular normals at every point of *pts*."""
    tangents = np.zeros_like(pts)
    tangents[:-1] = pts[1:] - pts[:-1]
    tangents[-1] = tangents[-2]

    lengths = np.hypot(tangents[:, 0], tangents[:, 1])
    lengths = np.where(lengths > 1e-9, lengths, 1.0)
    tangents /= lengths[:, np.newaxis]

    # Rotate 90° CCW: (tx, ty) → (−ty, tx)
    return np.column_stack([-tangents[:, 1], tangents[:, 0]])


# ---------------------------------------------------------------------------
# Normalisation – must be identical to _normalise_xy in track_geometry.py
# ---------------------------------------------------------------------------


def _to_normalised_list(pts_m: np.ndarray) -> list[list[float]]:
    """Normalise *pts_m* to [0, 1] (identical to track_geometry._normalise_xy).

    Returns a list of [x, y] pairs, or an empty list when *pts_m* has
    fewer than two points.
    """
    if len(pts_m) < 2:
        return []
    x = pts_m[:, 0].astype(np.float64)
    y = pts_m[:, 1].astype(np.float64)
    normed = _normalise_xy(x, y)
    if len(normed) == 0:
        return []
    return normed.tolist()


# ---------------------------------------------------------------------------
# Storage path
# ---------------------------------------------------------------------------


def _output_path(storage_root: Path, track_key: str) -> Path:
    safe_key = sanitize_name(track_key)
    return storage_root / "track_geometries" / safe_key / "track_road_geometry.json"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Extract TrackRoadGeometry from an IBT file.",
    )
    parser.add_argument("--ibt", required=True, help="Path to the IBT file")
    parser.add_argument(
        "--storage", required=True, help="Storage root directory"
    )
    args = parser.parse_args()

    written = extract_track_geometry(args.ibt, args.storage)
    print(f"Written: {written}")
