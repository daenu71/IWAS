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
import logging
import math
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq

_log = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "config" / "coaching" / "event_config_v1.json"
)

_CORNER_EVENTS_ENGINE_VERSION = 7


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

    throttle_full_event, throttle_off_events, throttle_miss = _throttle_full_and_off_events(
        data, n, ldp, st, cfg
    )
    if throttle_miss:
        missing["throttle_full"] = throttle_miss
        missing["throttle_off"] = throttle_miss

    # Turn-in: both definitions computed in one pass so confidence can be paired per zone
    ti_rate_evs, ti_angle_evs, ti_miss = _events_turn_in_both(data, n, ldp, st, cfg)
    if ti_miss:
        missing["turn_in_rate_based"] = ti_miss
        missing["turn_in_angle_based"] = ti_miss

    events: dict[str, Any] = {
        "turn_in_rate_based":  ti_rate_evs,
        "turn_in_angle_based": ti_angle_evs,
        "brake_start":         _array("brake_start",            _events_brake_start),
        "peak_brake":          _array("peak_brake",             _events_peak_brake),
        "brake_release_start": _singular("brake_release_start", _event_brake_release_start),
        "brake_release_end":   _singular("brake_release_end",   _event_brake_release_end),
        "min_speed":           _array("min_speed",              _events_min_speed),
        "throttle_on":         _singular("throttle_on",         _event_throttle_on),
        "throttle_full":       throttle_full_event,
        "throttle_off":        throttle_off_events,
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
    throttle_full_event, _, miss = _throttle_full_and_off_events(data, n, ldp, st, cfg)
    return throttle_full_event, miss


# ---------------------------------------------------------------------------
# Array event extractors
# Each returns (list | None, missing_channel_name | None)
# None  → channel absent (event is null in JSON)
# []    → channel present but no events detected
# ---------------------------------------------------------------------------


def _events_brake_start(
    data: dict, n: int, ldp: np.ndarray | None, st: np.ndarray | None, cfg: dict
) -> tuple[list | None, str | None]:
    """Detect each rising edge of the Brake channel (one per brake zone).

    Runs with peak_value < threshold_brake_peak_min_value or
    sample_count < threshold_brake_peak_min_samples are skipped so that
    noise / micro-brake events do not produce brake_start events.
    """
    ch = _get_channel(data, "Brake", n)
    if ch is None or ldp is None:
        return None, "Brake"
    thr = float(cfg["threshold_brake_start"])
    min_val_thr = float(cfg.get("threshold_brake_peak_min_value", 0.15))
    min_samples_thr = int(cfg.get("threshold_brake_peak_min_samples", 5))
    above = ch > thr
    above[~np.isfinite(ch)] = False
    runs = _find_runs(above)
    events: list[dict] = []
    for run_idx, (s, e) in enumerate(runs):
        sl = ch[s : e + 1]
        peak_val = round(float(np.nanmax(sl)), 6)
        sample_count = e - s + 1
        if peak_val < min_val_thr or sample_count < min_samples_thr:
            _log.debug(
                "[event_engine] brake_start run %d: FILTERED (peak=%.3f samples=%d)",
                run_idx, peak_val, sample_count,
            )
            continue
        events.append(_make_event("brake_start", s, ldp, st, None))
    return events, None


def _events_peak_brake(
    data: dict, n: int, ldp: np.ndarray | None, st: np.ndarray | None, cfg: dict
) -> tuple[list | None, str | None]:
    """Find the Brake peak within each contiguous brake run."""
    ch = _get_channel(data, "Brake", n)
    if ch is None or ldp is None:
        return None, "Brake"
    thr = float(cfg["threshold_brake_start"])
    above = ch > thr
    above[~np.isfinite(ch)] = False
    runs = _find_runs(above)
    _log.debug("[event_engine] peak_brake: %d brake run(s) detected (thr=%.3f)", len(runs), thr)
    min_val_thr = float(cfg.get("threshold_brake_peak_min_value", 0.15))
    min_samples_thr = int(cfg.get("threshold_brake_peak_min_samples", 5))
    events: list[dict] = []
    for run_idx, (s, e) in enumerate(runs):
        sl = ch[s : e + 1]
        peak_local = int(np.nanargmax(sl))
        peak_g = s + peak_local
        peak_val = round(float(ch[peak_g]), 6)
        sample_count = e - s + 1
        _log.debug(
            "[event_engine] peak_brake run %d: lapdist_pct_start=%.6f "
            "lapdist_pct_peak=%.6f peak_value=%.6f sample_count=%d",
            run_idx,
            float(ldp[s]),
            float(ldp[peak_g]),
            peak_val,
            sample_count,
        )
        if peak_val < min_val_thr or sample_count < min_samples_thr:
            _log.debug(
                "[event_engine] peak_brake run %d: FILTERED (peak=%.3f samples=%d)",
                run_idx, peak_val, sample_count,
            )
            continue
        events.append(_make_event("peak_brake", peak_g, ldp, st, peak_val))
    return events, None


def _events_turn_in_both(
    data: dict, n: int, ldp: np.ndarray | None, st: np.ndarray | None, cfg: dict
) -> tuple[list | None, list | None, str | None]:
    """Compute rate-based and angle-based turn-in events with per-zone confidence pairing.

    For each brake zone the search window is [brake_peak, next_min_speed].

    Rate-based  – argmax(|SteeringRate|) in the window.  SteeringRate is computed
                  as d(SteeringWheelAngle)/d(SessionTime) [rad/s]; falls back to
                  d/d(LapDistPct) when SessionTime is absent.

    Angle-based – first sample where |SteeringWheelAngle| exceeds
                  threshold_turn_in_angle_fraction × SteeringPeak, where
                  SteeringPeak = max(|SteeringWheelAngle|) over the full corner window
                  [brake_start → throttle_full].

    Returns (rate_events, angle_events, missing_channel_name).
    Both lists are None only when SteeringWheelAngle is absent.
    """
    steer_ch = _get_channel(data, "SteeringWheelAngle", n)
    if steer_ch is None or ldp is None:
        return None, None, "SteeringWheelAngle"

    brake_ch = _get_channel(data, "Brake", n)
    throttle_ch = _get_channel(data, "Throttle", n)
    speed_ch = _get_channel(data, "Speed", n)

    thr_brake = float(cfg.get("threshold_brake_start", 0.05))
    thr_throttle_full = float(cfg.get("threshold_throttle_full", 0.95))
    thr_angle_fraction = float(cfg.get("threshold_turn_in_angle_fraction", 0.10))
    thr_rate_min = float(cfg.get("threshold_turn_in_rate_min_rads", 0.05))
    thr_confidence = float(cfg.get("threshold_turn_in_confidence_dist", 0.005))
    lookahead = int(cfg.get("min_speed_lookahead_samples", 100))

    # SteeringRate = d(steer)/d(time); fall back to d(steer)/d(lapdist) when no time
    ref_arr = st if st is not None else ldp
    steer_rate = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        dr = float(ref_arr[i]) - float(ref_arr[i - 1])
        if dr > 1e-9 and math.isfinite(steer_ch[i]) and math.isfinite(steer_ch[i - 1]):
            steer_rate[i] = (float(steer_ch[i]) - float(steer_ch[i - 1])) / dr
    if n > 1:
        steer_rate[0] = steer_rate[1]

    # Without a brake channel we cannot define the search window
    if brake_ch is None:
        return [], [], None

    above = brake_ch > thr_brake
    above[~np.isfinite(brake_ch)] = False
    runs = _find_runs(above)

    # Collect (rate_ev, angle_ev) pairs per brake zone for confidence pairing
    pairs: list[tuple[dict | None, dict | None]] = []

    for s, e in runs:
        # Brake peak in this run
        sl_brake = brake_ch[s : e + 1]
        brake_peak_idx = s + int(np.nanargmax(sl_brake))

        # Min-speed window: [brake_peak, brake_peak + lookahead]
        min_speed_idx: int | None = None
        if speed_ch is not None:
            search_end = min(brake_peak_idx + lookahead, n - 1)
            sl_speed = speed_ch[brake_peak_idx : search_end + 1].copy()
            sl_speed[~np.isfinite(sl_speed)] = np.inf
            if np.any(np.isfinite(speed_ch[brake_peak_idx : search_end + 1])):
                min_speed_idx = brake_peak_idx + int(np.argmin(sl_speed))

        ti_end = min_speed_idx if min_speed_idx is not None else min(brake_peak_idx + lookahead, n - 1)
        if ti_end <= brake_peak_idx:
            pairs.append((None, None))
            continue

        # --- Rate-based turn-in: argmax(|SteeringRate|) in [brake_peak, ti_end] ---
        rate_ev: dict | None = None
        w_rate_abs = np.abs(steer_rate[brake_peak_idx : ti_end + 1])
        if len(w_rate_abs) > 0:
            rate_peak_local = int(np.argmax(w_rate_abs))
            rate_peak_abs = float(w_rate_abs[rate_peak_local])
            if rate_peak_abs >= thr_rate_min:
                rate_peak_g = brake_peak_idx + rate_peak_local
                rate_ev = _make_event(
                    "turn_in_rate_based", rate_peak_g, ldp, st, round(rate_peak_abs, 6)
                )

        # --- Angle-based turn-in ---
        # SteeringPeak = max(|steer|) over full corner window [brake_start, throttle_full]
        angle_ev: dict | None = None
        corner_end = ti_end  # fallback when throttle channel absent
        if throttle_ch is not None:
            for i in range(brake_peak_idx, n):
                if math.isfinite(throttle_ch[i]) and throttle_ch[i] >= thr_throttle_full:
                    corner_end = i
                    break

        steer_slice = np.abs(steer_ch[s : corner_end + 1])
        if len(steer_slice) > 0 and np.any(np.isfinite(steer_slice)):
            steer_peak = float(np.nanmax(steer_slice))
            if steer_peak > 0.0:
                thr_angle = thr_angle_fraction * steer_peak
                for i in range(brake_peak_idx, ti_end + 1):
                    if math.isfinite(steer_ch[i]) and abs(steer_ch[i]) > thr_angle:
                        angle_ev = _make_event(
                            "turn_in_angle_based", i, ldp, st, round(float(steer_ch[i]), 6)
                        )
                        break

        pairs.append((rate_ev, angle_ev))

    # Assign confidence flags and flatten into output lists
    final_rate: list[dict] = []
    final_angle: list[dict] = []

    for rate_ev, angle_ev in pairs:
        if rate_ev is not None and angle_ev is not None:
            dist = abs(rate_ev["lapdist_pct"] - angle_ev["lapdist_pct"])
            confidence = "high" if dist < thr_confidence else "low"
            rate_ev["confidence"] = confidence
            angle_ev["confidence"] = confidence
            final_rate.append(rate_ev)
            final_angle.append(angle_ev)
        elif rate_ev is not None:
            rate_ev["confidence"] = "low"
            final_rate.append(rate_ev)
        elif angle_ev is not None:
            angle_ev["confidence"] = "low"
            final_angle.append(angle_ev)

    return final_rate, final_angle, None


def _events_min_speed(
    data: dict, n: int, ldp: np.ndarray | None, st: np.ndarray | None, cfg: dict
) -> tuple[list | None, str | None]:
    """Find the minimum Speed within a window anchored to each brake run.

    For each brake run [s, e], searches for the speed minimum in
    [s, e + lookahead] to capture the apex that follows braking.
    Falls back to the global minimum when no brake channel or no brake runs.
    """
    speed_ch = _get_channel(data, "Speed", n)
    if speed_ch is None or ldp is None:
        return None, "Speed"

    brake_ch = _get_channel(data, "Brake", n)
    lookahead = int(cfg.get("min_speed_lookahead_samples", 100))

    def _global_min() -> list[dict]:
        if not np.any(np.isfinite(speed_ch)):
            return []
        idx = int(np.nanargmin(speed_ch))
        return [_make_event("min_speed", idx, ldp, st, round(float(speed_ch[idx]), 6))]

    if brake_ch is None:
        return _global_min(), None

    thr_brake = float(cfg["threshold_brake_start"])
    above = brake_ch > thr_brake
    above[~np.isfinite(brake_ch)] = False
    runs = _find_runs(above)

    if not runs:
        return _global_min(), None

    events: list[dict] = []
    for s, e in runs:
        search_end = min(e + lookahead, n - 1)
        sl = speed_ch[s : search_end + 1].copy()
        sl[~np.isfinite(sl)] = np.inf
        if not np.any(np.isfinite(speed_ch[s : search_end + 1])):
            continue
        local_min = int(np.argmin(sl))
        global_idx = s + local_min
        events.append(
            _make_event("min_speed", global_idx, ldp, st, round(float(speed_ch[global_idx]), 6))
        )
    return events, None


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


def _events_throttle_off(
    data: dict, n: int, ldp: np.ndarray | None, st: np.ndarray | None, cfg: dict
) -> tuple[list | None, str | None]:
    _, throttle_off_events, miss = _throttle_full_and_off_events(data, n, ldp, st, cfg)
    return throttle_off_events, miss


def _throttle_full_and_off_events(
    data: dict, n: int, ldp: np.ndarray | None, st: np.ndarray | None, cfg: dict
) -> tuple[dict | None, list | None, str | None]:
    ch = _get_channel(data, "Throttle", n)
    if ch is None or ldp is None:
        return None, None, "Throttle"
    thr = float(cfg["threshold_throttle_full"])
    throttle_full_event: dict | None = None
    events: list[dict] = []
    throttle_was_full = False
    for i in range(n):
        if not math.isfinite(ch[i]):
            continue
        throttle = float(ch[i])
        if not throttle_was_full and throttle >= thr:
            throttle_was_full = True
            if throttle_full_event is None:
                throttle_full_event = _make_event("throttle_full", i, ldp, st, None)
        elif throttle_was_full and throttle < thr:
            events.append(_make_event("throttle_off", i, ldp, st, None))
            throttle_was_full = False
    return throttle_full_event, events, None


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
    corner_brake_peak_idx: int | None = None
    brake_peak_indices: list[int] = []

    # --- Brake channels ---
    brake_ch = _get_channel(data, "Brake", n)
    if brake_ch is not None:
        # Use _find_runs so that each distinct brake phase in the window gets its
        # own brake_start and peak_brake (fixes single-peak issue for multi-curve
        # corners where the driver brakes twice).
        w_brake_slice = brake_ch[w_start : w_end + 1].copy()
        brake_above_w = w_brake_slice > thr_brake
        brake_above_w[~np.isfinite(w_brake_slice)] = False
        brake_runs = _find_runs(brake_above_w)
        _log.debug(
            "[event_engine] corner window [%.4f, %.4f]: %d brake run(s)",
            float(ldp[w_start]), float(ldp[w_end]), len(brake_runs),
        )
        min_val_thr = float(cfg.get("threshold_brake_peak_min_value", 0.15))
        min_samples_thr = int(cfg.get("threshold_brake_peak_min_samples", 5))
        for run_idx, (s_l, e_l) in enumerate(brake_runs):
            s_g = w_start + s_l
            e_g = w_start + e_l
            # Compute peak and sample count first so we can filter noise runs.
            run_slice = brake_ch[s_g : e_g + 1]
            peak_local = int(np.nanargmax(run_slice))
            peak_g = s_g + peak_local
            peak_val = round(float(brake_ch[peak_g]), 6)
            sample_count = e_g - s_g + 1
            _log.debug(
                "[event_engine] corner brake run %d: lapdist_pct_start=%.6f "
                "lapdist_pct_peak=%.6f peak_value=%.6f sample_count=%d",
                run_idx,
                float(ldp[s_g]),
                float(ldp[peak_g]),
                peak_val,
                sample_count,
            )
            if peak_val < min_val_thr or sample_count < min_samples_thr:
                _log.debug(
                    "[event_engine] corner brake run %d: FILTERED (peak=%.3f samples=%d)",
                    run_idx, peak_val, sample_count,
                )
                continue
            # brake_start: first sample of this run
            events.append(_make_event("brake_start", s_g, ldp, st, None))
            # peak_brake: maximum within this run
            events.append(_make_event("peak_brake", peak_g, ldp, st, peak_val))
            brake_peak_indices.append(peak_g)
            # corner_brake_peak_idx kept for backward compat; set to first valid peak.
            if corner_brake_peak_idx is None:
                corner_brake_peak_idx = peak_g

    # --- Min speed (computed before turn-in so the window [brake_peak→min_speed] is known) ---
    speed_ch = _get_channel(data, "Speed", n)
    if speed_ch is not None:
        w_speed = np.where(mask, speed_ch, np.nan)
        if np.any(np.isfinite(w_speed)):
            min_speed_idx = int(np.nanargmin(w_speed))
            min_val = float(speed_ch[min_speed_idx])
            events.append(
                _make_event("min_speed", min_speed_idx, ldp, st, round(min_val, 6))
            )

    # Pre-load throttle channel here so it's available for the angle-based corner-end detection
    throttle_ch = _get_channel(data, "Throttle", n)

    # --- Turn-in (rate-based and angle-based) – one event per brake peak ---
    steer_ch = _get_channel(data, "SteeringWheelAngle", n)
    if steer_ch is not None:
        thr_angle_fraction = float(cfg.get("threshold_turn_in_angle_fraction", 0.10))
        thr_rate_min = float(cfg.get("threshold_turn_in_rate_min_rads", 0.05))
        thr_confidence = float(cfg.get("threshold_turn_in_confidence_dist", 0.005))
        thr_throttle_full_w = float(cfg.get("threshold_throttle_full", 0.95))
        thr_crossover_confirm = float(cfg.get("threshold_crossover_confirm_window", 0.02))
        thr_crossover_min_angle = float(cfg.get("threshold_crossover_min_angle_rad", 0.05))

        ref_arr = st if st is not None else ldp

        # SteeringPeak for angle-based: max(|steer|) over [w_start, throttle_full or w_end]
        corner_end_w = w_end
        if throttle_ch is not None:
            for i in range(w_start, w_end + 1):
                if math.isfinite(throttle_ch[i]) and throttle_ch[i] >= thr_throttle_full_w:
                    corner_end_w = i
                    break
        steer_window_abs = np.abs(steer_ch[w_start : corner_end_w + 1])
        steer_peak_global = (
            float(np.nanmax(steer_window_abs))
            if len(steer_window_abs) > 0 and np.any(np.isfinite(steer_window_abs))
            else 0.0
        )

        turn_in_indices: list[int] = []  # rate-based indices for crossover detection

        for bp_i, bp_idx in enumerate(brake_peak_indices):
            # Window end: next brake peak (exclusive) or corner end
            next_boundary = (
                brake_peak_indices[bp_i + 1]
                if bp_i + 1 < len(brake_peak_indices)
                else w_end
            )

            # Find local min_speed in [bp_idx, next_boundary] for this brake phase
            ti_end_i: int = next_boundary
            if speed_ch is not None:
                sl_sp = speed_ch[bp_idx : next_boundary + 1].copy()
                sl_sp[~np.isfinite(sl_sp)] = np.inf
                if np.any(np.isfinite(speed_ch[bp_idx : next_boundary + 1])):
                    ti_end_i = bp_idx + int(np.argmin(sl_sp))

            if ti_end_i <= bp_idx:
                continue

            # Rate-based: argmax(|SteeringRate|) in [bp_idx, ti_end_i]
            rate_ev: dict | None = None
            seg_len = ti_end_i - bp_idx + 1
            rates = np.zeros(seg_len, dtype=np.float64)
            for j in range(1, seg_len):
                gi = bp_idx + j
                dr = float(ref_arr[gi]) - float(ref_arr[gi - 1])
                if dr > 1e-9 and math.isfinite(steer_ch[gi]) and math.isfinite(steer_ch[gi - 1]):
                    rates[j] = (float(steer_ch[gi]) - float(steer_ch[gi - 1])) / dr
            rate_peak_local = int(np.argmax(np.abs(rates)))
            rate_peak_abs = float(abs(rates[rate_peak_local]))
            if rate_peak_abs >= thr_rate_min:
                rate_peak_g = bp_idx + rate_peak_local
                rate_ev = _make_event(
                    "turn_in_rate_based", rate_peak_g, ldp, st, round(rate_peak_abs, 6)
                )
                turn_in_indices.append(rate_peak_g)

            # Angle-based: first sample in [bp_idx, ti_end_i] where |steer| > fraction * peak
            angle_ev: dict | None = None
            if steer_peak_global > 0.0:
                thr_angle = thr_angle_fraction * steer_peak_global
                for i in range(bp_idx, ti_end_i + 1):
                    if math.isfinite(steer_ch[i]) and abs(steer_ch[i]) > thr_angle:
                        angle_ev = _make_event(
                            "turn_in_angle_based", i, ldp, st, round(float(steer_ch[i]), 6)
                        )
                        break

            # Confidence and emit
            if rate_ev is not None and angle_ev is not None:
                dist = abs(rate_ev["lapdist_pct"] - angle_ev["lapdist_pct"])
                confidence = "high" if dist < thr_confidence else "low"
                rate_ev["confidence"] = confidence
                angle_ev["confidence"] = confidence
                events.append(rate_ev)
                events.append(angle_ev)
            elif rate_ev is not None:
                rate_ev["confidence"] = "low"
                events.append(rate_ev)
            elif angle_ev is not None:
                angle_ev["confidence"] = "low"
                events.append(angle_ev)

        # --- Steering crossover: zero-crossing between consecutive turn-in events ---
        turn_in_indices.sort()
        for xi in range(len(turn_in_indices) - 1):
            ti1 = turn_in_indices[xi]
            ti2 = turn_in_indices[xi + 1]
            if ti2 <= ti1 + 1:
                continue

            # Require significant steering in the lead-up window
            steer_before = np.abs(steer_ch[ti1 : ti2])
            if not np.any(np.isfinite(steer_before)):
                continue
            if float(np.nanmax(steer_before)) < thr_crossover_min_angle:
                continue

            # Find local minimum of |steer| between the two turn-ins
            cross_window = np.abs(steer_ch[ti1 : ti2 + 1]).copy()
            cross_window[~np.isfinite(steer_ch[ti1 : ti2 + 1])] = np.inf
            if not np.any(np.isfinite(steer_ch[ti1 : ti2 + 1])):
                continue
            cross_local = int(np.argmin(cross_window))
            cross_idx = ti1 + cross_local
            cross_val = float(abs(steer_ch[cross_idx]))

            # Confirmation: |steer| must rise above fraction*peak within confirm window
            confirm_threshold = thr_angle_fraction * steer_peak_global
            cross_ldp = float(ldp[cross_idx])
            confirmed = False
            for j in range(cross_idx + 1, n):
                if float(ldp[j]) - cross_ldp > thr_crossover_confirm:
                    break
                if math.isfinite(steer_ch[j]) and abs(steer_ch[j]) > confirm_threshold:
                    confirmed = True
                    break

            if confirmed:
                events.append(
                    _make_event("steering_crossover", cross_idx, ldp, st, round(cross_val, 6))
                )

    # --- Throttle ---
    if throttle_ch is not None:
        thr_on = float(cfg["threshold_throttle_on"])
        thr_full = float(cfg["threshold_throttle_full"])
        search_from = min_speed_idx if min_speed_idx is not None else w_start
        found_on = False

        # Preserve existing throttle_on behaviour by searching from min_speed.
        for i in range(search_from, w_end + 1):
            if not math.isfinite(throttle_ch[i]):
                continue
            throttle = float(throttle_ch[i])
            if not found_on and throttle > thr_on:
                events.append(_make_event("throttle_on", i, ldp, st, None))
                found_on = True

        # Detect full-throttle edges over the entire corner window so a lift
        # around brake_start is not missed before min_speed.
        throttle_was_full: bool | None = None
        for i in range(w_start, w_end + 1):
            if not math.isfinite(throttle_ch[i]):
                continue
            throttle = float(throttle_ch[i])
            throttle_is_full = throttle >= thr_full
            if throttle_was_full is None:
                throttle_was_full = throttle_is_full
                continue
            if not throttle_was_full and throttle_is_full:
                events.append(_make_event("throttle_full", i, ldp, st, None))
            elif throttle_was_full and not throttle_is_full:
                events.append(_make_event("throttle_off", i, ldp, st, None))
            throttle_was_full = throttle_is_full

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

    # --- Incident events (PlayerCarMyIncidentCount jumps) ---
    # Delta == 1 → offtrack_incident, Delta == 2 → loose_control, Delta >= 4 → crash.
    # Defensively skipped when the channel is absent or contains no valid data.
    incident_ch = _get_channel(data, "PlayerCarMyIncidentCount", n)
    if incident_ch is not None:
        for i in range(w_start + 1, w_end + 1):
            if not math.isfinite(incident_ch[i]) or not math.isfinite(incident_ch[i - 1]):
                continue
            delta = int(round(incident_ch[i] - incident_ch[i - 1]))
            if delta <= 0:
                continue
            if delta == 1:
                events.append(_make_event("offtrack_incident", i, ldp, st, {"delta": delta}))
            elif delta == 2:
                events.append(_make_event("loose_control", i, ldp, st, {"delta": delta}))
            elif delta >= 4:
                events.append(_make_event("crash", i, ldp, st, {"delta": delta}))

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
