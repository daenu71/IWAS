"""CornerMap Builder v1 – Story 2.2.1.

Builds a deterministic corner map (corner_map_v1.json) for a given
Track/Config from a resampled baseline lap parquet.

Storage path: <storage_root>/corner_maps/<track_key>/corner_map_v1.json
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import pyarrow.parquet as pq

from .storage import sanitize_name

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum absolute curvature (1/m) for a region to be treated as a corner.
# 0.003 corresponds to radius < ~333 m.
_CURVATURE_THRESHOLD: float = 0.003

# Grid-point count for the centred moving-average applied to raw curvature.
_SMOOTH_WINDOW: int = 15

# A corner segment must span at least this fraction of LapDistPct.
_MIN_CORNER_WIDTH: float = 0.003

# Two segments closer than this (LapDistPct) are merged into one corner.
_MIN_GAP: float = 0.005

# Corner type radius boundaries (metres).
_HAIRPIN_RADIUS_MAX: float = 30.0
_MEDIUM_RADIUS_MAX: float = 80.0
_SWEEPER_RADIUS_MAX: float = 200.0

# Chicane detection: opposite-sign peaks within this LapDistPct span.
_CHICANE_MAX_DIST: float = 0.05

# Compound detection: two same-sign peaks at least this far apart.
_COMPOUND_MIN_DIST: float = 0.02

# Compression / crest thresholds for VertAccel (m/s²).
# iRacing VertAccel = 9.81 at rest.  Higher = compression, lower = crest.
_COMPRESSION_THRESHOLD: float = 9.81
_CREST_THRESHOLD: float = 8.5

CORNER_MAP_FILENAME = "corner_map_v1.json"

# ---------------------------------------------------------------------------
# Physics-filter defaults (full-throttle corner rejection)
# ---------------------------------------------------------------------------

# Condition A – braking or lift-off
_CORNER_MIN_BRAKE: float = 0.05
_CORNER_MAX_THROTTLE_LIFT: float = 0.97

# Condition B – speed delta (m/s)
_CORNER_MIN_SPEED_DELTA_MS: float = 5.0

# Condition C – mean absolute steering angle (degrees)
_CORNER_MIN_STEERING_DEG: float = 15.0

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_corner_map(
    *,
    parquet_path: Path | str,
    storage_root: Path | str,
    track_key: str,
    curvature_threshold: float = _CURVATURE_THRESHOLD,
    smooth_window: int = _SMOOTH_WINDOW,
    min_corner_width: float = _MIN_CORNER_WIDTH,
    min_gap: float = _MIN_GAP,
    corner_min_brake: float = _CORNER_MIN_BRAKE,
    corner_max_throttle_lift: float = _CORNER_MAX_THROTTLE_LIFT,
    corner_min_speed_delta_ms: float = _CORNER_MIN_SPEED_DELTA_MS,
    corner_min_steering_deg: float = _CORNER_MIN_STEERING_DEG,
    debug_mode: bool = False,
) -> dict[str, Any]:
    """Build the corner map from a resampled lap parquet and save to disk.

    Parameters
    ----------
    parquet_path:
        Path to the resampled lap parquet (output of Story 2.1.1).
    storage_root:
        Root storage directory (same as used for coaching sessions).
    track_key:
        ``TrackDisplayName__TrackConfigName`` identifier.
    curvature_threshold:
        Minimum absolute curvature (1/m) for a region to count as a corner.
    smooth_window:
        Number of grid points for the moving-average smoothing of curvature.
    min_corner_width:
        Minimum corner width in LapDistPct units; shorter segments are discarded.
    min_gap:
        Merge adjacent corner segments separated by less than this in LapDistPct.

    Returns
    -------
    The corner map dict (also written to disk).
    """
    parquet_path = Path(parquet_path)
    storage_root = Path(storage_root)

    if debug_mode:
        print(f"[corner_map DEBUG] build_corner_map track_key={track_key!r}")
        print(f"[corner_map DEBUG] baseline parquet: {parquet_path}")

    data = _read_parquet_as_dict(parquet_path)
    n = len(data.get("LapDistPct", []))

    existing_version = _load_existing_version(storage_root, track_key)
    new_version = (existing_version + 1) if existing_version is not None else 1

    if n == 0:
        corner_map: dict[str, Any] = {
            "track_key": track_key,
            "corner_map_version": new_version,
            "corners": [],
        }
        _write_corner_map(corner_map, storage_root, track_key)
        return corner_map

    lap_dist_pct = np.array(data["LapDistPct"], dtype=np.float64)

    # --- Compute signed curvature (1/m) ---
    curvature = _compute_curvature(data, n)

    # --- Smooth ---
    if smooth_window > 1:
        curvature = _moving_average(curvature, smooth_window)

    # --- Segment corners ---
    abs_curv = np.abs(curvature)
    segments = _segment_corners(
        lap_dist_pct,
        abs_curv,
        curvature,
        threshold=curvature_threshold,
        min_width=min_corner_width,
        min_gap=min_gap,
    )

    # --- Physics filter: reject full-throttle arcs ---
    segments = _filter_fullgas_segments(
        segments,
        brake_arr=_get_float_array(data, "Brake", n),
        throttle_arr=_get_float_array(data, "Throttle", n),
        speed_arr=_get_float_array(data, "Speed", n),
        steering_arr=_get_float_array(data, "SteeringWheelAngle", n),
        min_brake=corner_min_brake,
        max_throttle_lift=corner_max_throttle_lift,
        min_speed_delta=corner_min_speed_delta_ms,
        min_steering_deg=corner_min_steering_deg,
        debug_mode=debug_mode,
    )

    # --- VertAccel for crest/compression detection ---
    vert_accel = _get_float_array(data, "VertAccel", n)

    # --- Build corner entries ---
    corners = [
        _build_corner_entry(
            corner_id=corner_id,
            seg=seg,
            lap_dist_pct=lap_dist_pct,
            curvature=curvature,
            vert_accel=vert_accel,
            corner_map_version=new_version,
        )
        for corner_id, seg in enumerate(segments, start=1)
    ]

    corner_map = {
        "track_key": track_key,
        "corner_map_version": new_version,
        "corners": corners,
    }
    _write_corner_map(corner_map, storage_root, track_key)
    return corner_map


def load_corner_map(
    *,
    storage_root: Path | str,
    track_key: str,
) -> dict[str, Any] | None:
    """Load the corner map for *track_key* from *storage_root*.

    Returns ``None`` if no corner map file exists.
    """
    path = _corner_map_path(Path(storage_root), track_key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Cross-session baseline lap search
# ---------------------------------------------------------------------------

_RUN_DIR_RE = re.compile(r"run_(\d+)", re.IGNORECASE)
_LAP_DIR_RE = re.compile(r"lap_(\d+)", re.IGNORECASE)


def find_best_baseline_lap(
    coaching_storage_dir: Path | str,
    track_key: str,
    debug_mode: bool = False,
) -> Path | None:
    """Find the fastest valid resampled lap across all sessions matching *track_key*.

    Iterates over every session folder in *coaching_storage_dir*, reads
    ``session_meta.json``, builds the track key using the same sanitize logic
    as ``AnalysisCache._infer_track_key``, and collects all valid laps whose
    key matches *track_key*.  Returns the path to the fastest lap's resampled
    parquet, or ``None`` if no valid candidate is found.

    Parameters
    ----------
    coaching_storage_dir:
        Root directory that contains individual session sub-folders.
    track_key:
        ``TrackDisplayName__TrackConfigName__CarClassShortName`` identifier
        (sanitized).
    debug_mode:
        When ``True``, print diagnostic lines prefixed with
        ``[corner_map DEBUG]``.
    """
    coaching_storage_dir = Path(coaching_storage_dir)
    if debug_mode:
        print(
            f"[corner_map DEBUG] find_best_baseline_lap "
            f"coaching_root={coaching_storage_dir} target_key={track_key!r}"
        )

    if not coaching_storage_dir.exists():
        print(
            f"[corner_map WARNING] coaching_storage_dir not found: "
            f"{coaching_storage_dir}"
        )
        return None

    best_path: Path | None = None
    best_duration: float = float("inf")

    for session_dir in sorted(coaching_storage_dir.iterdir()):
        if not session_dir.is_dir():
            continue
        meta_path = session_dir / "session_meta.json"
        if not meta_path.exists():
            if debug_mode:
                print(
                    f"[corner_map WARNING] No session_meta.json in "
                    f"{session_dir.name}, skipping"
                )
            continue

        try:
            meta: dict[str, Any] = json.loads(
                meta_path.read_text(encoding="utf-8")
            )
        except Exception:
            if debug_mode:
                print(
                    f"[corner_map WARNING] Cannot read session_meta.json in "
                    f"{session_dir.name}, skipping"
                )
            continue

        session_key = _session_track_key(meta)
        if session_key != track_key:
            continue

        if debug_mode:
            print(
                f"[corner_map DEBUG] Matching session: {session_dir.name} "
                f"key={session_key!r}"
            )

        for lap_parquet, duration in _iter_valid_laps(session_dir, debug_mode=debug_mode):
            if debug_mode:
                print(
                    f"[corner_map DEBUG]   Candidate: {lap_parquet} "
                    f"duration={duration:.3f}s"
                )
            if duration < best_duration:
                best_duration = duration
                best_path = lap_parquet

    if debug_mode:
        if best_path is not None:
            print(
                f"[corner_map DEBUG] Best baseline lap: {best_path} "
                f"({best_duration:.3f}s)"
            )
        else:
            print("[corner_map DEBUG] No valid baseline lap found")

    if best_path is None:
        print(
            f"[corner_map ERROR] No valid baseline lap found for "
            f"track_key={track_key!r}"
        )

    return best_path


def _session_track_key(meta: dict[str, Any]) -> str:
    """Build a sanitized track key from a session_meta dict."""
    track_name = (
        _meta_str(meta, "TrackDisplayName")
        or _meta_str(meta, "TrackName")
        or "unknown_track"
    )
    config_name = (
        _meta_str(meta, "TrackConfigName")
        or _meta_str(meta, "TrackConfig")
        or "unknown_config"
    )
    car_class = _meta_str(meta, "CarClassShortName") or "unknown_class"
    return (
        f"{sanitize_name(track_name)}"
        f"__{sanitize_name(config_name)}"
        f"__{sanitize_name(car_class)}"
    )


def _meta_str(meta: dict[str, Any], key: str) -> str | None:
    val = meta.get(key)
    if val is None:
        return None
    text = str(val).strip()
    return text or None


def _iter_valid_laps(
    session_dir: Path,
    debug_mode: bool = False,
) -> Iterator[tuple[Path, float]]:
    """Yield ``(resampled_parquet, duration_s)`` for each valid lap in *session_dir*."""
    for run_dir in sorted(session_dir.iterdir()):
        m_run = _RUN_DIR_RE.match(run_dir.name)
        if not run_dir.is_dir() or not m_run:
            continue
        run_id = int(m_run.group(1))

        laps_root = run_dir / "laps"
        if laps_root.is_dir():
            lap_dirs = [d for d in sorted(laps_root.iterdir()) if d.is_dir()]
        else:
            lap_dirs = [
                d
                for d in sorted(run_dir.iterdir())
                if d.is_dir() and _LAP_DIR_RE.match(d.name)
            ]

        for lap_dir in lap_dirs:
            # Prefer analysis sub-dir resampled parquet
            resampled = lap_dir / "analysis" / "lap_resampled.parquet"
            if not resampled.exists():
                resampled = lap_dir / "lap_resampled.parquet"
            if not resampled.exists():
                continue

            if not _is_lap_valid(lap_dir, debug_mode=debug_mode):
                continue

            m_lap = _LAP_DIR_RE.match(lap_dir.name)
            lap_id = int(m_lap.group(1)) if m_lap else None

            duration = _lap_duration_s(
                lap_dir, resampled,
                run_dir=run_dir, run_id=run_id, lap_id=lap_id,
            )
            if not math.isfinite(duration):
                if debug_mode:
                    print(
                        f"[corner_map DEBUG]   Skipping lap {lap_dir.name}: "
                        f"duration unknown (no valid time source)"
                    )
                continue
            yield resampled, duration


def _is_lap_valid(lap_dir: Path, debug_mode: bool = False) -> bool:
    """Return ``True`` when the lap is considered valid / fully computed."""
    status_path = lap_dir / "analysis" / "analysis_status.json"
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            status_val = str(status.get("status", "")).lower()
            if status_val in ("computed", "partial"):
                return True
            if status_val in ("blocked", "not_computed", "stale"):
                if debug_mode:
                    print(
                        f"[corner_map DEBUG]   Skipping lap {lap_dir.name}: "
                        f"status={status_val}"
                    )
                return False
        except Exception:
            pass

    # Fallback: check lap_meta.json for explicit valid_lap flag
    for meta_name in ("lap_meta.json", "meta.json"):
        meta_path = lap_dir / meta_name
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                valid = meta.get("valid_lap")
                if valid is True:
                    return True
                if valid is False:
                    return False
            except Exception:
                pass

    # Default: treat as valid when the resampled parquet exists
    return True


def _lap_duration_s(
    lap_dir: Path,
    resampled_path: Path,
    *,
    run_dir: Path | None = None,
    run_id: int | None = None,
    lap_id: int | None = None,
) -> float:
    """Return lap duration in seconds; ``inf`` when it cannot be determined.

    Priority:
    1. ``duration_s`` from ``run_{id:04d}_meta.json`` → lap entry with matching lap_id.
    2. ``duration_s`` / ``lap_time_s`` / ``lap_time`` from ``lap_meta.json`` / ``meta.json``.
    3. Fallback: ``max(SessionTime) - min(SessionTime)`` from the resampled parquet.
    """
    # --- Priority 1: run_{id:04d}_meta.json ---
    if run_dir is not None and run_id is not None and lap_id is not None:
        run_meta_path = run_dir / f"run_{run_id:04d}_meta.json"
        if run_meta_path.exists():
            try:
                run_meta: dict[str, Any] = json.loads(
                    run_meta_path.read_text(encoding="utf-8")
                )
                for lap_entry in run_meta.get("laps") or []:
                    if int(lap_entry.get("lap_id", -1)) == lap_id:
                        raw = lap_entry.get("duration_s")
                        if raw is not None:
                            try:
                                d = float(raw)
                                if d > 0:
                                    return d
                            except Exception:
                                pass
                        break
            except Exception:
                pass

    # --- Priority 2: lap_meta.json / meta.json ---
    for meta_name in ("lap_meta.json", "meta.json"):
        meta_path = lap_dir / meta_name
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                lap_summary = meta.get("lap_summary") or {}
                for key in ("duration_s", "lap_time_s", "lap_time"):
                    for src in (meta, lap_summary):
                        raw = src.get(key)
                        if raw is not None:
                            try:
                                d = float(raw)
                                if d > 0:
                                    return d
                            except Exception:
                                pass
            except Exception:
                pass

    # --- Priority 3: SessionTime channel in the resampled parquet ---
    try:
        table = pq.read_table(str(resampled_path), columns=["SessionTime"])
        times = [
            float(v)
            for v in table.column("SessionTime").to_pylist()
            if v is not None and math.isfinite(float(v))
        ]
        if len(times) >= 2:
            return max(times) - min(times)
    except Exception:
        pass

    return float("inf")


# ---------------------------------------------------------------------------
# Curvature computation
# ---------------------------------------------------------------------------


def _compute_curvature(data: dict[str, Any], n: int) -> np.ndarray:
    """Return signed curvature array (1/m) for the resampled lap.

    Priority order:
    1. ``YawRate / Speed``  — most reliable for iRacing telemetry.
    2. Parametric curvature from ``X``, ``Y`` position columns.
    3. Zero array (no geometry available).
    """
    yaw_rate = _get_float_array(data, "YawRate", n)
    speed = _get_float_array(data, "Speed", n)

    if yaw_rate is not None and speed is not None:
        # κ = YawRate / Speed  (rad/s) / (m/s) = 1/m
        with np.errstate(invalid="ignore", divide="ignore"):
            curv = np.where(speed > 0.5, yaw_rate / speed, 0.0)
        return np.where(np.isfinite(curv), curv, 0.0).astype(np.float64)

    x = _get_float_array(data, "X", n)
    y = _get_float_array(data, "Y", n)
    if x is not None and y is not None:
        return _parametric_curvature(x, y)

    return np.zeros(n, dtype=np.float64)


def _parametric_curvature(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Signed curvature κ = (x'y'' − y'x'') / (x'^2 + y'^2)^1.5 in 1/m.

    Derivatives are with respect to LapDistPct (uniform grid), so units are
    correct when x, y are in metres.
    """
    dx = np.gradient(x)
    dy = np.gradient(y)
    d2x = np.gradient(dx)
    d2y = np.gradient(dy)
    denom = (dx**2 + dy**2) ** 1.5
    with np.errstate(invalid="ignore", divide="ignore"):
        curv = np.where(denom > 1e-9, (dx * d2y - dy * d2x) / denom, 0.0)
    return np.where(np.isfinite(curv), curv, 0.0).astype(np.float64)


# ---------------------------------------------------------------------------
# Signal processing
# ---------------------------------------------------------------------------


def _moving_average(arr: np.ndarray, window: int) -> np.ndarray:
    """Centred moving average; edges are handled by padding with edge values."""
    if window < 2 or len(arr) < window:
        return arr.copy()
    half = window // 2
    padded = np.pad(arr, half, mode="edge")
    kernel = np.ones(window, dtype=np.float64) / window
    smoothed = np.convolve(padded, kernel, mode="valid")
    return smoothed[: len(arr)]


# ---------------------------------------------------------------------------
# Corner segmentation
# ---------------------------------------------------------------------------


def _segment_corners(
    lap_dist: np.ndarray,
    abs_curv: np.ndarray,
    signed_curv: np.ndarray,
    *,
    threshold: float,
    min_width: float,
    min_gap: float,
) -> list[dict[str, Any]]:
    """Segment the curvature trace into corner regions.

    Returns a list of raw segment dicts with keys:
    ``start_idx``, ``end_idx``, ``lap_dist_start``, ``lap_dist_end``,
    ``signed_curv_slice``.
    """
    above = abs_curv >= threshold
    runs = _find_runs(above)

    # Filter by minimum width
    filtered: list[tuple[int, int]] = []
    for s, e in runs:
        width = float(lap_dist[e]) - float(lap_dist[s])
        if width >= min_width:
            filtered.append((s, e))

    # Merge close segments
    merged = _merge_segments(filtered, lap_dist, min_gap)

    return [
        {
            "start_idx": int(s),
            "end_idx": int(e),
            "lap_dist_start": float(lap_dist[s]),
            "lap_dist_end": float(lap_dist[e]),
            "signed_curv_slice": signed_curv[s : e + 1],
        }
        for s, e in merged
    ]


def _find_runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Return ``(start_idx, end_idx)`` for each contiguous ``True`` run."""
    if not np.any(mask):
        return []
    runs: list[tuple[int, int]] = []
    in_run = False
    start = 0
    for i, v in enumerate(mask):
        if v and not in_run:
            start = i
            in_run = True
        elif not v and in_run:
            runs.append((start, i - 1))
            in_run = False
    if in_run:
        runs.append((start, len(mask) - 1))
    return runs


def _merge_segments(
    segments: list[tuple[int, int]],
    lap_dist: np.ndarray,
    min_gap: float,
) -> list[tuple[int, int]]:
    """Merge adjacent segments whose gap is smaller than *min_gap*."""
    if not segments:
        return []
    merged = [segments[0]]
    for s, e in segments[1:]:
        gap = float(lap_dist[s]) - float(lap_dist[merged[-1][1]])
        if gap < min_gap:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return merged


# ---------------------------------------------------------------------------
# Full-throttle corner filter
# ---------------------------------------------------------------------------


def _filter_fullgas_segments(
    segments: list[dict[str, Any]],
    *,
    brake_arr: np.ndarray | None,
    throttle_arr: np.ndarray | None,
    speed_arr: np.ndarray | None,
    steering_arr: np.ndarray | None,
    min_brake: float,
    max_throttle_lift: float,
    min_speed_delta: float,
    min_steering_deg: float,
    debug_mode: bool = False,
) -> list[dict[str, Any]]:
    """Reject corner candidates that show no meaningful braking, speed drop, or steering.

    A candidate is kept if at least one *applicable* condition passes:
    - A: max(Brake) > min_brake  OR  min(Throttle) < max_throttle_lift
    - B: max(Speed) - min(Speed) > min_speed_delta
    - C: mean(|SteeringWheelAngle|) > min_steering_deg

    A condition is "applicable" only when the required channel(s) are present.
    If no conditions are applicable, the candidate is kept (benefit of the doubt).
    """
    kept: list[dict[str, Any]] = []
    for candidate_id, seg in enumerate(segments, start=1):
        s = seg["start_idx"]
        e = seg["end_idx"] + 1  # exclusive slice end

        # --- Condition A ---
        passes_a: bool | None = None
        if brake_arr is not None or throttle_arr is not None:
            a_result = False
            if brake_arr is not None:
                max_brake = float(np.nanmax(brake_arr[s:e]))
                if max_brake > min_brake:
                    a_result = True
            if not a_result and throttle_arr is not None:
                min_throttle = float(np.nanmin(throttle_arr[s:e]))
                if min_throttle < max_throttle_lift:
                    a_result = True
            passes_a = a_result

        # --- Condition B ---
        passes_b: bool | None = None
        if speed_arr is not None:
            seg_speed = speed_arr[s:e]
            finite_speed = seg_speed[np.isfinite(seg_speed)]
            if len(finite_speed) > 0:
                speed_delta = float(np.max(finite_speed)) - float(np.min(finite_speed))
                passes_b = speed_delta > min_speed_delta

        # --- Condition C ---
        passes_c: bool | None = None
        if steering_arr is not None:
            seg_steer = np.abs(steering_arr[s:e])
            finite_steer = seg_steer[np.isfinite(seg_steer)]
            if len(finite_steer) > 0:
                mean_steer_deg = float(np.nanmean(finite_steer)) * 57.2958
                passes_c = mean_steer_deg > min_steering_deg

        if debug_mode:
            curv_slice = seg["signed_curv_slice"]
            peak_curv = float(np.nanmax(np.abs(curv_slice))) if len(curv_slice) else 0.0
            min_spd = (
                float(np.nanmin(speed_arr[s:e])) if speed_arr is not None else float("nan")
            )
            max_thr = (
                float(np.nanmax(throttle_arr[s:e]))
                if throttle_arr is not None
                else float("nan")
            )
            max_brk = (
                float(np.nanmax(brake_arr[s:e])) if brake_arr is not None else float("nan")
            )
            mean_stw = (
                float(np.nanmean(np.abs(steering_arr[s:e]))) * 57.2958
                if steering_arr is not None
                else float("nan")
            )
            print(
                f"[corner_map DEBUG] candidate={candidate_id} "
                f"start={seg['lap_dist_start']:.4f} end={seg['lap_dist_end']:.4f} "
                f"curv_peak={peak_curv:.5f} min_speed={min_spd:.2f}m/s "
                f"max_throttle={max_thr:.3f} max_brake={max_brk:.3f} "
                f"mean_steering={mean_stw:.2f}deg "
                f"A={passes_a} B={passes_b} C={passes_c}"
            )

        applicable = [p for p in (passes_a, passes_b, passes_c) if p is not None]
        if not applicable or any(applicable):
            kept.append(seg)
        elif debug_mode:
            print(
                f"[corner_map DEBUG] candidate={candidate_id} REJECTED "
                f"(all applicable conditions failed)"
            )

    return kept


# ---------------------------------------------------------------------------
# Corner entry building
# ---------------------------------------------------------------------------


def _build_corner_entry(
    *,
    corner_id: int,
    seg: dict[str, Any],
    lap_dist_pct: np.ndarray,
    curvature: np.ndarray,
    vert_accel: np.ndarray | None,
    corner_map_version: int,
) -> dict[str, Any]:
    """Build a single corner dict from a raw segment."""
    s = seg["start_idx"]
    e = seg["end_idx"]
    curv_slice: np.ndarray = seg["signed_curv_slice"]
    abs_slice = np.abs(curv_slice)

    peak_curv = float(np.nanmax(abs_slice)) if len(abs_slice) else 0.0
    mean_curv = float(np.nanmean(abs_slice)) if len(abs_slice) else 0.0
    radius_est = round(1.0 / peak_curv, 2) if peak_curv > 1e-9 else None

    corner_type = _classify_corner_type(curv_slice, abs_slice, radius_est, seg)
    curvature_trend = _classify_trend(abs_slice)

    crest_present: bool | None = None
    compression_present: bool | None = None
    if vert_accel is not None:
        va_slice = vert_accel[s : e + 1]
        finite_va = va_slice[np.isfinite(va_slice)]
        if len(finite_va) > 0:
            compression_present = bool(np.any(finite_va > _COMPRESSION_THRESHOLD))
            crest_present = bool(np.any(finite_va < _CREST_THRESHOLD))

    return {
        "corner_id": corner_id,
        "start_lapdist_pct": round(seg["lap_dist_start"], 6),
        "end_lapdist_pct": round(seg["lap_dist_end"], 6),
        "corner_type": corner_type,
        "radius_est": radius_est,
        "curvature_peak": round(peak_curv, 6),
        "curvature_mean": round(mean_curv, 6),
        "curvature_trend": curvature_trend,
        "crest_present": crest_present,
        "compression_present": compression_present,
        "corner_map_version": corner_map_version,
    }


def _classify_corner_type(
    curv_slice: np.ndarray,
    abs_slice: np.ndarray,
    radius_est: float | None,
    seg: dict[str, Any],
) -> str:
    """Classify corner type from curvature profile."""
    if len(curv_slice) == 0:
        return "unknown"

    # --- Chicane: meaningful portions of both curvature signs ---
    significant = curv_slice[abs_slice > 1e-6]
    if len(significant) >= 4:
        n_pos = int(np.sum(significant > 0))
        n_neg = int(np.sum(significant < 0))
        minority = min(n_pos, n_neg) / len(significant)
        width = seg["lap_dist_end"] - seg["lap_dist_start"]
        if minority > 0.2 and width <= _CHICANE_MAX_DIST:
            return "chicane"

    # --- Compound: two curvature peaks of same sign far apart ---
    peaks = _find_local_maxima(abs_slice)
    if len(peaks) >= 2:
        n_pts = len(abs_slice)
        width = seg["lap_dist_end"] - seg["lap_dist_start"]
        first_p = seg["lap_dist_start"] + (peaks[0] / max(n_pts - 1, 1)) * width
        last_p = seg["lap_dist_start"] + (peaks[-1] / max(n_pts - 1, 1)) * width
        if last_p - first_p > _COMPOUND_MIN_DIST:
            return "compound"

    return _classify_by_radius(radius_est)


def _classify_by_radius(radius_est: float | None) -> str:
    """Map radius estimate to corner type string."""
    if radius_est is None:
        return "unknown"
    if radius_est < _HAIRPIN_RADIUS_MAX:
        return "hairpin"
    if radius_est < _MEDIUM_RADIUS_MAX:
        return "medium"
    if radius_est < _SWEEPER_RADIUS_MAX:
        return "sweeper"
    return "unknown"


def _classify_trend(abs_slice: np.ndarray) -> str:
    """Classify curvature trend as 'inc' (increasing), 'dec', or 'sym'."""
    n = len(abs_slice)
    if n < 4:
        return "sym"
    half = n // 2
    first_mean = float(np.nanmean(abs_slice[:half]))
    second_mean = float(np.nanmean(abs_slice[half:]))
    peak_val = float(np.nanmax(abs_slice))
    if peak_val < 1e-9:
        return "sym"
    ratio = abs(first_mean - second_mean) / peak_val
    if ratio < 0.15:
        return "sym"
    return "inc" if second_mean > first_mean else "dec"


def _find_local_maxima(arr: np.ndarray) -> list[int]:
    """Return indices of local maxima in *arr*."""
    peaks = []
    for i in range(1, len(arr) - 1):
        if arr[i] >= arr[i - 1] and arr[i] >= arr[i + 1]:
            peaks.append(i)
    return peaks


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _read_parquet_as_dict(parquet_path: Path) -> dict[str, Any]:
    table = pq.read_table(str(parquet_path))
    return table.to_pydict()


def _get_float_array(data: dict[str, Any], col: str, n: int) -> np.ndarray | None:
    """Return column as float64 array, or ``None`` if absent or all-NaN."""
    if col not in data:
        return None
    vals = data[col]
    arr = np.empty(n, dtype=np.float64)
    for i, v in enumerate(vals[:n]):
        if v is None:
            arr[i] = np.nan
        else:
            try:
                f = float(v)
                arr[i] = f if math.isfinite(f) else np.nan
            except Exception:
                arr[i] = np.nan
    if not np.any(np.isfinite(arr)):
        return None
    return arr


def _corner_map_path(storage_root: Path, track_key: str) -> Path:
    return storage_root / "corner_maps" / track_key / CORNER_MAP_FILENAME


def _load_existing_version(storage_root: Path, track_key: str) -> int | None:
    path = _corner_map_path(storage_root, track_key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data.get("corner_map_version", 1))
    except Exception:
        return None


def _write_corner_map(
    corner_map: dict[str, Any],
    storage_root: Path,
    track_key: str,
) -> None:
    path = _corner_map_path(storage_root, track_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(corner_map, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
