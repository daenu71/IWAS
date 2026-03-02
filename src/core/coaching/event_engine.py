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
