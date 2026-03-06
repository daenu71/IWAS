"""Event Engine v1 – Story 2.3.1.

Extracts deterministic driving events from a resampled lap parquet and
writes them as ``lap_events.json``.  All thresholds are read from
``config/coaching/event_config_v1.json`` (versioned).

Storage path: <session>/<run>/laps/<lap_id>/analysis/lap_events.json

Channel conventions
-------------------
- LapDistPct  : 0.0 … 1.0, uniform grid (from resample_lapdist)
- Brake        : 0.0 … 1.0 (fraction)
- Throttle     : 0.0 … 1.0 (fraction)
- SteeringWheelAngle : radians
- Speed        : m/s
- YawRate      : rad/s
- Gear         : integer gear number
- VertAccel    : m/s² (iRacing: ~9.81 at rest; thresholds in config are
                 absolute – adjust config to match your data convention)
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

_DEFAULT_CONFIG_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "config" / "coaching" / "event_config_v1.json"
)

_CORNER_EVENTS_ENGINE_VERSION = 2


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_lap_events(
    *,
    parquet_path: Path | str,
    output_path: Path | str | None = None,
    config_path: Path | str | None = None,
    corner_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract driving events from a resampled lap parquet.

    Parameters
    ----------
    parquet_path:
        Path to the resampled lap parquet (output of Story 2.1.1).
    output_path:
        Where to write ``lap_events.json``.  No file is written when *None*.
    config_path:
        Event config JSON.  Defaults to ``config/coaching/event_config_v1.json``.
    corner_map:
        Optional corner map dict (Story 2.2.1) for understeer detection.
        When *None*, ``understeer_event`` is ``null`` in the output.

    Returns
    -------
    dict
        The lap events dict (also written to *output_path* when provided).
    """
    parquet_path = Path(parquet_path)
    cfg_path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
    cfg = _load_config(cfg_path)

    data = _read_parquet_as_dict(parquet_path)
    n = len(data.get("LapDistPct", []))

    ldp = _get_channel(data, "LapDistPct", n)
    st = _get_channel(data, "SessionTime", n)

    missing: dict[str, str] = {}  # event_name → absent channel name

    def _singular(name: str, fn) -> Any:
        ev, miss = fn(data, n, ldp, st, cfg)
        if miss:
            missing[name] = miss
        return ev

    def _array(name: str, fn) -> Any:
        evs, miss = fn(data, n, ldp, st, cfg)
        if miss:
            missing[name] = miss
        return evs

    events: dict[str, Any] = {
        "turn_in":             _singular("turn_in",             _event_turn_in),
        "brake_start":         _singular("brake_start",         _event_brake_start),
        "peak_brake":          _singular("peak_brake",          _event_peak_brake),
        "brake_release_start": _singular("brake_release_start", _event_brake_release_start),
        "brake_release_end":   _singular("brake_release_end",   _event_brake_release_end),
        "min_speed":           _singular("min_speed",           _event_min_speed),
        "throttle_on":         _singular("throttle_on",         _event_throttle_on),
        "throttle_full":       _singular("throttle_full",       _event_throttle_full),
        "gear_change":         _array("gear_change",            _events_gear_change),
        "oversteer_event":     _array("oversteer_event",        _events_oversteer),
        "crest":               _array("crest",                  _events_crest),
        "compression":         _array("compression",            _events_compression),
    }

    # Understeer requires corner_map; null when map unavailable
    us_evs, us_miss = _events_understeer(data, n, ldp, st, cfg, corner_map)
    if us_miss:
        missing["understeer_event"] = us_miss
    events["understeer_event"] = us_evs

    result: dict[str, Any] = {
        "meta": {
            "config_version": int(cfg["version"]),
            "n_grid_points": n,
            "missing_channels": missing,
        },
        "events": events,
    }

    if output_path is not None:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    return result


def extract_corner_events(
    *,
    parquet_path: Path | str,
    corners: list[dict],
    output_path: Path | str | None = None,
    config_path: Path | str | None = None,
    track_length_m: float | None = None,
) -> dict[str, Any]:
    """Extract per-corner driving events from a resampled lap parquet.

    Parameters
    ----------
    parquet_path:
        Path to the resampled lap parquet (output of resample_lapdist).
    corners:
        Corner map entries, each with corner_id, start_lapdist_pct,
        end_lapdist_pct.
    output_path:
        Where to write ``corner_events.json``.  No file is written when *None*.
        If the file already exists and engine_version + config_version match,
        the cached file is returned without recomputation.
    config_path:
        Event config JSON.  Defaults to ``config/coaching/event_config_v1.json``.
    track_length_m:
        Track length in metres (from session_meta.json).  Used to convert the
        entry/exit padding from metres to LapDistPct fractions.  When *None*,
        fractional fallbacks are used (entry=0.015, exit=0.008).

    Returns
    -------
    dict
        The corner events dict (also written to *output_path* when provided).
    """
    parquet_path = Path(parquet_path)
    out_path = Path(output_path) if output_path is not None else None
    cfg_path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
    cfg = _load_config(cfg_path)
    cfg_version = int(cfg.get("version", 1))

    # Cache: skip recomputation when file exists and versions match
    if out_path is not None and out_path.exists():
        try:
            existing: dict[str, Any] = json.loads(out_path.read_text(encoding="utf-8"))
            meta = existing.get("meta", {})
            if (
                int(meta.get("engine_version", -1)) == _CORNER_EVENTS_ENGINE_VERSION
                and int(meta.get("config_version", -1)) == cfg_version
            ):
                return existing
        except Exception:
            pass  # recompute on any parse error

    entry_padding_m = float(cfg.get("corner_entry_padding_m", 80))
    exit_padding_m = float(cfg.get("corner_exit_padding_m", 40))

    if track_length_m and track_length_m > 0.0:
        entry_pct = entry_padding_m / track_length_m
        exit_pct = exit_padding_m / track_length_m
    else:
        entry_pct = 0.015
        exit_pct = 0.008

    data = _read_parquet_as_dict(parquet_path)
    n = len(data.get("LapDistPct", []))
    ldp = _get_channel(data, "LapDistPct", n)
    st = _get_channel(data, "SessionTime", n)

    corners_out: dict[str, list[dict]] = {}
    for corner in corners:
        corner_id = corner.get("corner_id")
        start_pct = float(corner.get("start_lapdist_pct", 0.0))
        end_pct = float(corner.get("end_lapdist_pct", 1.0))
        lo = max(0.0, start_pct - entry_pct)
        hi = min(1.0, end_pct + exit_pct)
        curvature_mean = float(corner.get("curvature_mean", 0.0))
        events = _extract_corner_window_events(data, n, ldp, st, cfg, lo, hi, curvature_mean)
        corners_out[str(corner_id)] = events

    result: dict[str, Any] = {
        "meta": {
            "engine_version": _CORNER_EVENTS_ENGINE_VERSION,
            "config_version": cfg_version,
            "entry_padding_m": entry_padding_m,
            "exit_padding_m": exit_padding_m,
            "track_length_m": track_length_m,
        },
        "corners": corners_out,
    }

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    return result


# ---------------------------------------------------------------------------
# Singular event extractors
# Each returns (event_dict | None, missing_channel_name | None)
# ---------------------------------------------------------------------------


def _event_turn_in(
    data: dict, n: int, ldp: np.ndarray | None, st: np.ndarray | None, cfg: dict
) -> tuple[dict | None, str | None]:
    ch = _get_channel(data, "SteeringWheelAngle", n)
    if ch is None or ldp is None:
        return None, "SteeringWheelAngle"
    thr = float(cfg.get("threshold_steering_turn_in", 0.05))
    for i in range(n):
        if math.isfinite(ch[i]) and abs(ch[i]) > thr:
            return _make_event("turn_in", i, ldp, st, round(float(ch[i]), 6)), None
    return None, None


def _event_brake_start(
    data: dict, n: int, ldp: np.ndarray | None, st: np.ndarray | None, cfg: dict
) -> tuple[dict | None, str | None]:
    ch = _get_channel(data, "Brake", n)
    if ch is None or ldp is None:
        return None, "Brake"
    thr = float(cfg["threshold_brake_start"])
    for i in range(n):
        if math.isfinite(ch[i]) and ch[i] > thr:
            return _make_event("brake_start", i, ldp, st, None), None
    return None, None


def _event_peak_brake(
    data: dict, n: int, ldp: np.ndarray | None, st: np.ndarray | None, cfg: dict
) -> tuple[dict | None, str | None]:
    ch = _get_channel(data, "Brake", n)
    if ch is None or ldp is None:
        return None, "Brake"
    thr = float(cfg["threshold_brake_start"])
    active = np.where(ch > thr, ch, np.nan)
    if not np.any(np.isfinite(active)):
        return None, None
    idx = int(np.nanargmax(active))
    return _make_event("peak_brake", idx, ldp, st, round(float(ch[idx]), 6)), None


def _event_brake_release_start(
    data: dict, n: int, ldp: np.ndarray | None, st: np.ndarray | None, cfg: dict
) -> tuple[dict | None, str | None]:
    ch = _get_channel(data, "Brake", n)
    if ch is None or ldp is None:
        return None, "Brake"
    thr = float(cfg["threshold_brake_start"])
    active = np.where(ch > thr, ch, np.nan)
    if not np.any(np.isfinite(active)):
        return None, None
    peak_idx = int(np.nanargmax(active))
    peak_val = float(ch[peak_idx])
    # First sample after peak where Brake drops below peak value
    for i in range(peak_idx + 1, n):
        if math.isfinite(ch[i]) and ch[i] < peak_val:
            return _make_event("brake_release_start", i, ldp, st, round(float(ch[i]), 6)), None
    return None, None


def _event_brake_release_end(
    data: dict, n: int, ldp: np.ndarray | None, st: np.ndarray | None, cfg: dict
) -> tuple[dict | None, str | None]:
    ch = _get_channel(data, "Brake", n)
    if ch is None or ldp is None:
        return None, "Brake"
    thr_start = float(cfg["threshold_brake_start"])
    thr_off = float(cfg.get("threshold_brake_off", thr_start))
    active = np.where(ch > thr_start, ch, np.nan)
    if not np.any(np.isfinite(active)):
        return None, None
    peak_idx = int(np.nanargmax(active))
    # First sample after peak where Brake drops below thr_off
    for i in range(peak_idx + 1, n):
        if math.isfinite(ch[i]) and ch[i] < thr_off:
            return _make_event("brake_release_end", i, ldp, st, None), None
    return None, None


def _event_min_speed(
    data: dict, n: int, ldp: np.ndarray | None, st: np.ndarray | None, cfg: dict
) -> tuple[dict | None, str | None]:
    ch = _get_channel(data, "Speed", n)
    if ch is None or ldp is None:
        return None, "Speed"
    if not np.any(np.isfinite(ch)):
        return None, None
    idx = int(np.nanargmin(ch))
    return _make_event("min_speed", idx, ldp, st, round(float(ch[idx]), 6)), None


def _event_throttle_on(
    data: dict, n: int, ldp: np.ndarray | None, st: np.ndarray | None, cfg: dict
) -> tuple[dict | None, str | None]:
    ch = _get_channel(data, "Throttle", n)
    if ch is None or ldp is None:
        return None, "Throttle"
    thr = float(cfg["threshold_throttle_on"])
    for i in range(n):
        if math.isfinite(ch[i]) and ch[i] > thr:
            return _make_event("throttle_on", i, ldp, st, None), None
    return None, None


def _event_throttle_full(
    data: dict, n: int, ldp: np.ndarray | None, st: np.ndarray | None, cfg: dict
) -> tuple[dict | None, str | None]:
    ch = _get_channel(data, "Throttle", n)
    if ch is None or ldp is None:
        return None, "Throttle"
    thr = float(cfg["threshold_throttle_full"])
    for i in range(n):
        if math.isfinite(ch[i]) and ch[i] > thr:
            return _make_event("throttle_full", i, ldp, st, None), None
    return None, None


# ---------------------------------------------------------------------------
# Array event extractors
# Each returns (list | None, missing_channel_name | None)
# None  → channel absent (event is null in JSON)
# []    → channel present but no events detected
# ---------------------------------------------------------------------------


def _events_gear_change(
    data: dict, n: int, ldp: np.ndarray | None, st: np.ndarray | None, cfg: dict
) -> tuple[list | None, str | None]:
    ch = _get_channel(data, "Gear", n)
    if ch is None or ldp is None:
        return None, "Gear"
    events: list[dict] = []
    prev: int | None = None
    for i in range(n):
        if not math.isfinite(ch[i]):
            continue
        curr = int(ch[i])
        if prev is not None and curr != prev:
            ev = _make_event("gear_change", i, ldp, st, {"from": prev, "to": curr})
            events.append(ev)
        prev = curr
    return events, None


def _events_oversteer(
    data: dict, n: int, ldp: np.ndarray | None, st: np.ndarray | None, cfg: dict
) -> tuple[list | None, str | None]:
    ch = _get_channel(data, "YawRate", n)
    if ch is None or ldp is None:
        return None, "YawRate"
    thr = float(cfg["threshold_oversteer_yawrate"])
    min_samp = int(cfg.get("min_oversteer_samples", 3))

    above = np.abs(ch) > thr
    above[~np.isfinite(ch)] = False

    events: list[dict] = []
    for s, e in _find_runs(above):
        if (e - s + 1) < min_samp:
            continue
        sl = ch[s : e + 1]
        peak_local = int(np.nanargmax(np.abs(sl)))
        peak_global = s + peak_local
        ev = _make_event(
            "oversteer_event",
            peak_global,
            ldp,
            st,
            {
                "start_lapdist_pct": round(float(ldp[s]), 6),
                "end_lapdist_pct": round(float(ldp[e]), 6),
                "peak_yawrate": round(float(ch[peak_global]), 6),
            },
        )
        events.append(ev)
    return events, None


def _events_understeer(
    data: dict,
    n: int,
    ldp: np.ndarray | None,
    st: np.ndarray | None,
    cfg: dict,
    corner_map: dict[str, Any] | None,
) -> tuple[list | None, str | None]:
    """Understeer detection requires a corner map; returns null when absent."""
    if corner_map is None:
        return None, None  # conditional feature, not a missing channel error

    speed = _get_channel(data, "Speed", n)
    yaw = _get_channel(data, "YawRate", n)

    if speed is None:
        return None, "Speed"
    if yaw is None:
        return None, "YawRate"
    if ldp is None:
        return None, "LapDistPct"

    thr = float(cfg.get("threshold_understeer_yawrate", cfg.get("threshold_oversteer_yawrate", 0.3)))
    min_samp = int(cfg.get("min_understeer_samples", 3))

    corners: list[dict] = corner_map.get("corners", [])
    events: list[dict] = []

    for corner in corners:
        c_start = float(corner["start_lapdist_pct"])
        c_end = float(corner["end_lapdist_pct"])
        curv_mean = float(corner.get("curvature_mean", 0.0))
        if curv_mean <= 0.0:
            continue

        mask = (ldp >= c_start) & (ldp <= c_end)
        if not np.any(mask):
            continue

        idxs = np.where(mask)[0]
        ideal_yaw = speed[idxs] * curv_mean
        delta = ideal_yaw - np.abs(yaw[idxs])
        under = delta > thr
        under[~np.isfinite(delta)] = False

        for s_r, e_r in _find_runs(under):
            if (e_r - s_r + 1) < min_samp:
                continue
            s_g = int(idxs[s_r])
            e_g = int(idxs[e_r])
            peak_local = s_r + int(np.nanargmax(delta[s_r : e_r + 1]))
            peak_g = int(idxs[peak_local])
            ev = _make_event(
                "understeer_event",
                peak_g,
                ldp,
                st,
                {
                    "start_lapdist_pct": round(float(ldp[s_g]), 6),
                    "end_lapdist_pct": round(float(ldp[e_g]), 6),
                    "corner_id": corner.get("corner_id"),
                    "peak_delta_yawrate": round(float(delta[peak_local]), 6),
                },
            )
            events.append(ev)

    return events, None


def _events_crest(
    data: dict, n: int, ldp: np.ndarray | None, st: np.ndarray | None, cfg: dict
) -> tuple[list | None, str | None]:
    ch = _get_channel(data, "VertAccel", n)
    if ch is None or ldp is None:
        return None, "VertAccel"
    thr = float(cfg["threshold_crest"])
    min_samp = int(cfg.get("min_crest_samples", 3))

    below = ch < thr
    below[~np.isfinite(ch)] = False

    events: list[dict] = []
    for s, e in _find_runs(below):
        if (e - s + 1) < min_samp:
            continue
        sl = ch[s : e + 1]
        min_local = int(np.nanargmin(sl))
        min_g = s + min_local
        ev = _make_event(
            "crest",
            min_g,
            ldp,
            st,
            {
                "start_lapdist_pct": round(float(ldp[s]), 6),
                "end_lapdist_pct": round(float(ldp[e]), 6),
                "min_vert_accel": round(float(ch[min_g]), 6),
            },
        )
        events.append(ev)
    return events, None


def _events_compression(
    data: dict, n: int, ldp: np.ndarray | None, st: np.ndarray | None, cfg: dict
) -> tuple[list | None, str | None]:
    ch = _get_channel(data, "VertAccel", n)
    if ch is None or ldp is None:
        return None, "VertAccel"
    thr = float(cfg["threshold_compression"])
    min_samp = int(cfg.get("min_compression_samples", 3))

    above = ch > thr
    above[~np.isfinite(ch)] = False

    events: list[dict] = []
    for s, e in _find_runs(above):
        if (e - s + 1) < min_samp:
            continue
        sl = ch[s : e + 1]
        max_local = int(np.nanargmax(sl))
        max_g = s + max_local
        ev = _make_event(
            "compression",
            max_g,
            ldp,
            st,
            {
                "start_lapdist_pct": round(float(ldp[s]), 6),
                "end_lapdist_pct": round(float(ldp[e]), 6),
                "max_vert_accel": round(float(ch[max_g]), 6),
            },
        )
        events.append(ev)
    return events, None


# ---------------------------------------------------------------------------
# Corner-window event extractor
# ---------------------------------------------------------------------------


def _extract_corner_window_events(
    data: dict,
    n: int,
    ldp: np.ndarray | None,
    st: np.ndarray | None,
    cfg: dict,
    lo: float,
    hi: float,
    curvature_mean: float = 0.0,
) -> list[dict]:
    """Return a sorted list of events within the LapDistPct window [lo, hi]."""
    if ldp is None or n == 0:
        return []

    mask = (ldp >= lo) & (ldp <= hi)
    if not np.any(mask):
        return []

    w_idxs = np.where(mask)[0]
    w_start = int(w_idxs[0])
    w_end = int(w_idxs[-1])

    events: list[dict] = []
    thr_brake = float(cfg["threshold_brake_start"])
    min_speed_idx: int | None = None

    # --- Brake channels ---
    brake_ch = _get_channel(data, "Brake", n)
    if brake_ch is not None:
        # brake_start: first sample in window above threshold
        for i in range(w_start, w_end + 1):
            if math.isfinite(brake_ch[i]) and brake_ch[i] > thr_brake:
                events.append(_make_event("brake_start", i, ldp, st, None))
                break

        # peak_brake: global max in window
        w_brake = np.where(mask, brake_ch, np.nan)
        if np.any(np.isfinite(w_brake)):
            peak_val = float(np.nanmax(w_brake))
            if peak_val > thr_brake:
                peak_idx = int(np.nanargmax(w_brake))
                events.append(
                    _make_event("peak_brake", peak_idx, ldp, st, round(peak_val, 6))
                )

    # --- Turn-in ---
    steer_ch = _get_channel(data, "SteeringWheelAngle", n)
    thr_turn_in = float(cfg.get("threshold_turn_in", 0.1))
    if steer_ch is not None:
        for i in range(w_start, w_end + 1):
            if math.isfinite(steer_ch[i]) and abs(steer_ch[i]) > thr_turn_in:
                events.append(
                    _make_event("turn_in", i, ldp, st, round(float(steer_ch[i]), 6))
                )
                break

    # --- Min speed ---
    speed_ch = _get_channel(data, "Speed", n)
    if speed_ch is not None:
        w_speed = np.where(mask, speed_ch, np.nan)
        if np.any(np.isfinite(w_speed)):
            min_speed_idx = int(np.nanargmin(w_speed))
            min_val = float(speed_ch[min_speed_idx])
            events.append(
                _make_event("min_speed", min_speed_idx, ldp, st, round(min_val, 6))
            )

    # --- Throttle (search from min_speed onwards) ---
    throttle_ch = _get_channel(data, "Throttle", n)
    if throttle_ch is not None:
        thr_on = float(cfg["threshold_throttle_on"])
        thr_full = float(cfg["threshold_throttle_full"])
        search_from = min_speed_idx if min_speed_idx is not None else w_start
        found_on = False
        found_full = False
        for i in range(search_from, w_end + 1):
            if not math.isfinite(throttle_ch[i]):
                continue
            if not found_on and throttle_ch[i] > thr_on:
                events.append(_make_event("throttle_on", i, ldp, st, None))
                found_on = True
            if not found_full and throttle_ch[i] > thr_full:
                events.append(_make_event("throttle_full", i, ldp, st, None))
                found_full = True
            if found_on and found_full:
                break

    # --- Gear changes ---
    gear_ch = _get_channel(data, "Gear", n)
    if gear_ch is not None:
        prev_gear: int | None = None
        for i in range(w_start, w_end + 1):
            if not math.isfinite(gear_ch[i]):
                continue
            curr_gear = int(gear_ch[i])
            if prev_gear is not None and curr_gear != prev_gear:
                events.append(
                    _make_event(
                        "gear_change", i, ldp, st, {"from": prev_gear, "to": curr_gear}
                    )
                )
            prev_gear = curr_gear

    # --- Oversteer events ---
    yaw_ch = _get_channel(data, "YawRate", n)
    if yaw_ch is not None:
        thr_yaw = float(cfg["threshold_oversteer_yawrate"])
        min_samp = int(cfg.get("min_oversteer_samples", cfg.get("n_min_oversteer_samples", 3)))
        w_yaw_abs = np.abs(yaw_ch[w_start : w_end + 1])
        above_local = w_yaw_abs > thr_yaw
        above_local[~np.isfinite(yaw_ch[w_start : w_end + 1])] = False
        for s_l, e_l in _find_runs(above_local):
            if (e_l - s_l + 1) < min_samp:
                continue
            s_g = w_start + s_l
            e_g = w_start + e_l
            sl = yaw_ch[s_g : e_g + 1]
            peak_local = int(np.nanargmax(np.abs(sl)))
            peak_g = s_g + peak_local
            events.append(
                _make_event(
                    "oversteer_event",
                    peak_g,
                    ldp,
                    st,
                    {
                        "start_lapdist_pct": round(float(ldp[s_g]), 6),
                        "end_lapdist_pct": round(float(ldp[e_g]), 6),
                        "peak_yawrate": round(float(yaw_ch[peak_g]), 6),
                    },
                )
            )

    # --- Understeer events ---
    speed_ch = _get_channel(data, "Speed", n)
    if yaw_ch is not None and speed_ch is not None and curvature_mean > 0.0:
        thr_under = float(cfg.get("threshold_understeer_yawrate", cfg.get("threshold_oversteer_yawrate", 0.3)))
        min_samp_under = int(cfg.get("min_understeer_samples", 3))
        w_speed = speed_ch[w_start : w_end + 1]
        w_yaw = yaw_ch[w_start : w_end + 1]
        ideal_yaw = w_speed * curvature_mean
        delta = ideal_yaw - np.abs(w_yaw)
        under = delta > thr_under
        under[~np.isfinite(delta)] = False
        for s_l, e_l in _find_runs(under):
            if (e_l - s_l + 1) < min_samp_under:
                continue
            s_g = w_start + s_l
            e_g = w_start + e_l
            peak_local = s_l + int(np.nanargmax(delta[s_l : e_l + 1]))
            peak_g = w_start + peak_local
            events.append(
                _make_event(
                    "understeer_event",
                    peak_g,
                    ldp,
                    st,
                    {
                        "start_lapdist_pct": round(float(ldp[s_g]), 6),
                        "end_lapdist_pct": round(float(ldp[e_g]), 6),
                        "peak_delta_yawrate": round(float(delta[peak_local]), 6),
                    },
                )
            )

    events.sort(key=lambda e: e["lapdist_pct"])
    return events


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_event(
    name: str,
    idx: int,
    ldp: np.ndarray,
    st: np.ndarray | None,
    value: Any,
) -> dict[str, Any]:
    st_val: float | None = None
    if st is not None and math.isfinite(st[idx]):
        st_val = round(float(st[idx]), 6)
    return {
        "name": name,
        "lapdist_pct": round(float(ldp[idx]), 6),
        "session_time": st_val,
        "value": value,
    }


def _find_runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Return ``(start_idx, end_idx)`` for each contiguous ``True`` run."""
    runs: list[tuple[int, int]] = []
    in_run = False
    start = 0
    for i, v in enumerate(mask):
        if bool(v) and not in_run:
            start = i
            in_run = True
        elif not bool(v) and in_run:
            runs.append((start, i - 1))
            in_run = False
    if in_run:
        runs.append((start, len(mask) - 1))
    return runs


def _get_channel(data: dict, name: str, n: int) -> np.ndarray | None:
    """Return channel as float64 array, or ``None`` if absent or all-NaN."""
    if name not in data:
        return None
    vals = data[name]
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


def _load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_bytes())


def _read_parquet_as_dict(path: Path) -> dict[str, Any]:
    table = pq.read_table(str(path))
    return table.to_pydict()
