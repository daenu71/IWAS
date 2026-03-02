"""Feature Engine v1 – Story 2.4.1.

Computes per-corner scalar features from a resampled lap parquet,
lap events, and a corner map.  Writes results as corner_features.parquet.

Storage paths:
    <session>/<run>/laps/<lap_id>/analysis/corner_features.parquet
    <session>/<run>/laps/<lap_id>/analysis/snapshots/<corner_id>_<snapshot_id>.json
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .analysis_contract import AnalysisContract
from .feature_schema import FeatureSchema

_ENGINE_VERSION = "1.0.0"
_G = 9.81          # m/s² standard gravity
_SNAPSHOT_HALF = 10  # window half-width → 21 samples total

# VertAccel thresholds (same convention as corner_map.py)
_COMPRESSION_THRESHOLD = 9.81
_CREST_THRESHOLD = 8.5
_YAWRATE_SPIKE_THRESHOLD = 0.5  # rad/s


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_corner_features(
    *,
    parquet_path: Path | str,
    events: dict[str, Any] | None = None,
    corner_map: dict[str, Any],
    run_id: str,
    lap_id: str,
    track_key: str,
    car_key: str,
    engine_version: str = _ENGINE_VERSION,
    schema_path: Path | str | None = None,
    contract_path: Path | str | None = None,
    lap_validity_flag: bool = True,
    output_path: Path | str | None = None,
    snapshots_dir: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Extract per-corner scalar features from a resampled lap parquet.

    Parameters
    ----------
    parquet_path:
        Resampled lap parquet (output of Story 2.1.1).
    events:
        Lap events dict (output of Story 2.3.1).  May be ``None``.
    corner_map:
        Corner map dict (output of Story 2.2.1).
    run_id, lap_id, track_key, car_key:
        Identifiers written into every output row.
    engine_version:
        Version string for this feature engine build.
    schema_path:
        Path to feature_schema_v1.json.  Defaults to config/coaching/feature_schema_v1.json.
    contract_path:
        Path to analysis_contract.json.  Defaults to config/coaching/analysis_contract.json.
    lap_validity_flag:
        True when the lap passed Sprint-1 validity checks.
    output_path:
        Destination for corner_features.parquet.  No file is written when ``None``.
    snapshots_dir:
        Directory for snapshot JSONs.  No snapshots are written when ``None``.

    Returns
    -------
    list[dict]
        One row dict per corner (also written to *output_path* when given).
    """
    parquet_path = Path(parquet_path)
    schema = FeatureSchema.load(schema_path)
    contract = AnalysisContract(contract_path)
    schema_feature_defaults = _schema_feature_defaults(schema)

    data = _read_parquet_as_dict(parquet_path)
    n = len(data.get("LapDistPct", []))

    ch = _ChannelSet(data, n)
    corner_map_version = int(corner_map.get("corner_map_version", 1))
    corners: list[dict[str, Any]] = corner_map.get("corners", [])

    rows: list[dict[str, Any]] = []
    for corner in corners:
        row = _process_corner(
            corner=corner,
            ch=ch,
            events=events,
            run_id=run_id,
            lap_id=lap_id,
            track_key=track_key,
            car_key=car_key,
            engine_version=engine_version,
            schema_hash=schema.schema_hash,
            contract_hash=contract.contract_hash,
            corner_map_version=corner_map_version,
            lap_validity_flag=lap_validity_flag,
            schema_feature_defaults=schema_feature_defaults,
            snapshots_dir=Path(snapshots_dir) if snapshots_dir else None,
        )
        rows.append(row)

    if output_path is not None:
        _write_parquet(rows, Path(output_path))

    return rows


# ---------------------------------------------------------------------------
# Per-corner processing
# ---------------------------------------------------------------------------


def _process_corner(
    *,
    corner: dict[str, Any],
    ch: "_ChannelSet",
    events: dict[str, Any] | None,
    run_id: str,
    lap_id: str,
    track_key: str,
    car_key: str,
    engine_version: str,
    schema_hash: str,
    contract_hash: str,
    corner_map_version: int,
    lap_validity_flag: bool,
    schema_feature_defaults: dict[str, Any],
    snapshots_dir: Path | None,
) -> dict[str, Any]:
    corner_id = int(corner["corner_id"])
    corner_type = str(corner.get("corner_type", "unknown"))
    start_pct = float(corner["start_lapdist_pct"])
    end_pct = float(corner["end_lapdist_pct"])

    sl = ch.slice(start_pct, end_pct)
    grip_usage = _compute_grip_usage(sl)
    corner_events = _filter_events(events, start_pct, end_pct)

    row: dict[str, Any] = {
        # Identifier columns
        "run_id": run_id,
        "lap_id": lap_id,
        "corner_id": corner_id,
        "track_key": track_key,
        "car_key": car_key,
        # Version / hash columns
        "engine_version": engine_version,
        "schema_hash": schema_hash,
        "contract_hash": contract_hash,
        "corner_map_version": corner_map_version,
        # Corner meta
        "corner_type": corner_type,
        "corner_radius_est": corner.get("radius_est"),
        "crest_present_map": corner.get("crest_present"),
        "compression_present_map": corner.get("compression_present"),
        "lap_validity_flag": lap_validity_flag,
    }
    row.update(schema_feature_defaults)

    row.update(_group_rotation_via_load(sl))
    row.update(_group_friction_ellipse(sl, grip_usage))
    row.update(_group_trail_braking(sl))
    row.update(_group_downshift_stability(sl))
    row.update(_group_light_hands(sl))
    row.update(_group_vertical_dynamics(sl))
    row.update(_group_limit_consistency(sl, grip_usage))
    row.update(_group_exit_timing(sl, corner_events))
    row.update(_group_compound_strategy(sl, grip_usage, corner_type))
    row.update(_group_dynamic_balance(sl))

    if snapshots_dir is not None:
        _write_snapshots(
            corner_id=corner_id,
            sl=sl,
            grip_usage=grip_usage,
            snapshots_dir=snapshots_dir,
        )

    return row


def _schema_feature_defaults(schema: FeatureSchema) -> dict[str, Any]:
    """Return per-feature defaults for schema conformance and confidence meta."""
    defaults: dict[str, Any] = {}
    for features in schema._groups.values():
        for feature in features:
            defaults.setdefault(feature.id, None)
            if feature.low_confidence:
                defaults.setdefault(f"{feature.id}_confidence", "low")
    return defaults


# ---------------------------------------------------------------------------
# Feature groups
# ---------------------------------------------------------------------------


def _group_rotation_via_load(sl: "_CornerSlice") -> dict[str, Any]:
    """rotation_via_load group."""
    rot_efficiency_load: float | None = None
    rot_dependency_steer: float | None = None
    yawrate_rise_before_steer: float | None = None

    if sl.yaw_rate is not None and sl.lat_accel is not None:
        mean_lat = float(np.nanmean(np.abs(sl.lat_accel)))
        if mean_lat > 1e-6:
            rot_efficiency_load = round(
                float(np.nanmean(np.abs(sl.yaw_rate))) / mean_lat, 6
            )

    if sl.yaw_rate is not None and sl.steering is not None:
        mask = np.isfinite(sl.yaw_rate) & np.isfinite(sl.steering)
        if np.sum(mask) > 2:
            c = float(
                np.corrcoef(np.abs(sl.yaw_rate[mask]), np.abs(sl.steering[mask]))[0, 1]
            )
            rot_dependency_steer = round(c, 6) if math.isfinite(c) else None

    if sl.yaw_rate is not None and sl.steering is not None and sl.ldp is not None:
        yaw_rise = _first_exceed_idx(np.abs(sl.yaw_rate), 0.05)
        steer_rise = _first_exceed_idx(np.abs(sl.steering), 0.05)
        if yaw_rise is not None and steer_rise is not None:
            delta = float(sl.ldp[steer_rise]) - float(sl.ldp[yaw_rise])
            yawrate_rise_before_steer = round(delta, 6)

    return {
        "rot_efficiency_load": rot_efficiency_load,
        "rot_dependency_steer": rot_dependency_steer,
        "yawrate_rise_before_steer": yawrate_rise_before_steer,
    }


def _group_friction_ellipse(
    sl: "_CornerSlice", grip_usage: np.ndarray | None
) -> dict[str, Any]:
    """friction_ellipse group."""
    grip_usage_p95: float | None = None
    grip_usage_max: float | None = None
    grip_usage_std: float | None = None
    yawrate_spike_count: int | None = None
    abs_active_ratio: float | None = None

    if grip_usage is not None:
        finite_gu = grip_usage[np.isfinite(grip_usage)]
        if len(finite_gu) > 0:
            grip_usage_p95 = round(float(np.percentile(finite_gu, 95)), 6)
            grip_usage_max = round(float(np.max(finite_gu)), 6)
            grip_usage_std = round(float(np.std(finite_gu)), 6)

    if sl.yaw_rate is not None:
        finite_yaw = sl.yaw_rate[np.isfinite(sl.yaw_rate)]
        yawrate_spike_count = int(np.sum(np.abs(finite_yaw) > _YAWRATE_SPIKE_THRESHOLD))

    if sl.abs_active is not None:
        n = len(sl.abs_active)
        if n > 0:
            active = int(np.sum(sl.abs_active[np.isfinite(sl.abs_active)] > 0.5))
            abs_active_ratio = round(active / n, 6)

    return {
        "grip_usage_p95": grip_usage_p95,
        "grip_usage_max": grip_usage_max,
        "grip_usage_std": grip_usage_std,
        "yawrate_spike_count": yawrate_spike_count,
        "abs_active_ratio": abs_active_ratio,
    }


def _group_trail_braking(sl: "_CornerSlice") -> dict[str, Any]:
    """trail_braking group – computed over the entry phase (first third)."""
    entry_end = max(1, sl.n // 3)
    corr_long_lat: float | None = None
    brake_release_slope: float | None = None
    lataccel_ramp_slope: float | None = None
    yawrate_stability_std_entry: float | None = None

    long_e = sl.long_accel[:entry_end] if sl.long_accel is not None else None
    lat_e = sl.lat_accel[:entry_end] if sl.lat_accel is not None else None
    brake_e = sl.brake[:entry_end] if sl.brake is not None else None
    yaw_e = sl.yaw_rate[:entry_end] if sl.yaw_rate is not None else None

    if long_e is not None and lat_e is not None:
        mask = np.isfinite(long_e) & np.isfinite(lat_e)
        if np.sum(mask) > 2:
            c = float(np.corrcoef(long_e[mask], lat_e[mask])[0, 1])
            corr_long_lat = round(c, 6) if math.isfinite(c) else None

    if brake_e is not None:
        finite_b = brake_e[np.isfinite(brake_e)]
        if len(finite_b) > 1:
            slope = _linear_slope(np.arange(len(finite_b), dtype=np.float64), finite_b)
            brake_release_slope = round(slope, 8) if math.isfinite(slope) else None

    if lat_e is not None:
        finite_la = lat_e[np.isfinite(lat_e)]
        if len(finite_la) > 1:
            slope = _linear_slope(np.arange(len(finite_la), dtype=np.float64), finite_la)
            lataccel_ramp_slope = round(slope, 8) if math.isfinite(slope) else None

    if yaw_e is not None:
        finite_ye = yaw_e[np.isfinite(yaw_e)]
        if len(finite_ye) > 1:
            yawrate_stability_std_entry = round(float(np.std(finite_ye)), 6)

    return {
        "corr_long_lat": corr_long_lat,
        "brake_release_slope": brake_release_slope,
        "lataccel_ramp_slope": lataccel_ramp_slope,
        "yawrate_stability_std_entry": yawrate_stability_std_entry,
    }


def _group_downshift_stability(sl: "_CornerSlice") -> dict[str, Any]:
    """downshift_stability group."""
    _null = {
        "gear_change_count_entry": None,
        "lataccel_at_gear_change_p95": None,
        "yawrate_var_post_shift": None,
        "rpm_delta_on_shift": None,
    }
    if sl.gear is None:
        return _null

    entry_end = max(1, sl.n // 3)
    shift_indices: list[int] = []
    for i in range(1, len(sl.gear)):
        if math.isfinite(sl.gear[i]) and math.isfinite(sl.gear[i - 1]):
            if int(sl.gear[i]) != int(sl.gear[i - 1]):
                shift_indices.append(i)

    gear_change_count_entry = len([i for i in shift_indices if i < entry_end])

    lataccel_at_gear_change_p95: float | None = None
    if sl.lat_accel is not None and shift_indices:
        lat_vals = [
            abs(float(sl.lat_accel[i]))
            for i in shift_indices
            if i < len(sl.lat_accel) and math.isfinite(sl.lat_accel[i])
        ]
        if lat_vals:
            lataccel_at_gear_change_p95 = round(float(np.percentile(lat_vals, 95)), 6)

    yawrate_var_post_shift: float | None = None
    if sl.yaw_rate is not None and shift_indices:
        post: list[float] = []
        for i in shift_indices:
            win = sl.yaw_rate[i: i + 10]
            post.extend(float(v) for v in win if math.isfinite(v))
        if post:
            yawrate_var_post_shift = round(float(np.var(post)), 6)

    rpm_delta_on_shift: float | None = None
    if sl.rpm is not None and shift_indices:
        deltas = [
            abs(float(sl.rpm[i]) - float(sl.rpm[i - 1]))
            for i in shift_indices
            if i > 0 and i < len(sl.rpm)
            and math.isfinite(sl.rpm[i]) and math.isfinite(sl.rpm[i - 1])
        ]
        if deltas:
            rpm_delta_on_shift = round(float(np.mean(deltas)), 4)

    return {
        "gear_change_count_entry": gear_change_count_entry,
        "lataccel_at_gear_change_p95": lataccel_at_gear_change_p95,
        "yawrate_var_post_shift": yawrate_var_post_shift,
        "rpm_delta_on_shift": rpm_delta_on_shift,
    }


def _group_light_hands(sl: "_CornerSlice") -> dict[str, Any]:
    """light_hands group – all features are optional (require SteeringWheelTorque)."""
    torque_response_latency_ms: float | None = None
    angle_correction_latency_ms: float | None = None
    torque_peak_during_correction: float | None = None

    # Sample duration in ms (for lag → ms conversion)
    sample_ms: float | None = None
    if sl.session_time is not None:
        finite_st = sl.session_time[np.isfinite(sl.session_time)]
        if len(finite_st) > 1:
            sample_ms = float(np.median(np.diff(finite_st))) * 1000.0

    torque = sl.steer_torque
    steer = sl.steering
    yaw = sl.yaw_rate

    if torque is not None and yaw is not None:
        lag = _xcorr_lag(torque, yaw)
        if lag is not None:
            ms = lag * (sample_ms if sample_ms is not None else 1.0)
            torque_response_latency_ms = round(ms, 2)

    if steer is not None and yaw is not None:
        corrections = _find_sign_reversals(steer)
        if corrections and sample_ms is not None:
            lags: list[float] = []
            for cidx in corrections:
                ws = max(0, cidx - 5)
                we = min(len(steer), cidx + 15)
                lag = _xcorr_lag(steer[ws:we], yaw[ws:we])
                if lag is not None:
                    lags.append(lag * sample_ms)
            if lags:
                angle_correction_latency_ms = round(float(np.mean(lags)), 2)

    if torque is not None and steer is not None:
        corrections = _find_sign_reversals(steer)
        if corrections:
            peaks = [
                abs(float(torque[cidx]))
                for cidx in corrections
                if cidx < len(torque) and math.isfinite(torque[cidx])
            ]
            if peaks:
                torque_peak_during_correction = round(float(np.max(peaks)), 6)

    return {
        "torque_response_latency_ms": torque_response_latency_ms,
        "angle_correction_latency_ms": angle_correction_latency_ms,
        "torque_peak_during_correction": torque_peak_during_correction,
    }


def _group_vertical_dynamics(sl: "_CornerSlice") -> dict[str, Any]:
    """vertical_dynamics group."""
    vertaccel_min: float | None = None
    vertaccel_max: float | None = None
    crest_present: bool | None = None
    compression_present: bool | None = None
    decel_efficiency_vs_vertload: float | None = None

    if sl.vert_accel is not None:
        finite_va = sl.vert_accel[np.isfinite(sl.vert_accel)]
        if len(finite_va) > 0:
            vertaccel_min = round(float(np.min(finite_va)), 6)
            vertaccel_max = round(float(np.max(finite_va)), 6)
            crest_present = bool(np.any(finite_va < _CREST_THRESHOLD))
            compression_present = bool(np.any(finite_va > _COMPRESSION_THRESHOLD))

    if sl.vert_accel is not None and sl.long_accel is not None:
        mask = np.isfinite(sl.vert_accel) & np.isfinite(sl.long_accel)
        if np.sum(mask) > 2:
            va_m = sl.vert_accel[mask]
            lo_m = sl.long_accel[mask]
            high_load = va_m > float(np.nanmean(va_m))
            if np.sum(high_load) > 0 and np.sum(~high_load) > 0:
                eff = float(np.nanmean(lo_m[~high_load])) - float(np.nanmean(lo_m[high_load]))
                if math.isfinite(eff):
                    decel_efficiency_vs_vertload = round(eff, 6)

    return {
        "vertaccel_min": vertaccel_min,
        "vertaccel_max": vertaccel_max,
        "crest_present": crest_present,
        "compression_present": compression_present,
        "decel_efficiency_vs_vertload": decel_efficiency_vs_vertload,
    }


def _group_limit_consistency(
    sl: "_CornerSlice", grip_usage: np.ndarray | None
) -> dict[str, Any]:
    """limit_consistency group."""
    grip_usage_consistency_std: float | None = None
    grip_usage_mean: float | None = None

    if grip_usage is not None:
        finite_gu = grip_usage[np.isfinite(grip_usage)]
        if len(finite_gu) > 0:
            grip_usage_mean = round(float(np.nanmean(finite_gu)), 6)
            window = 5
            maxima = [
                float(np.max(finite_gu[i: i + window]))
                for i in range(0, len(finite_gu) - window + 1, window)
            ]
            grip_usage_consistency_std = round(
                float(np.std(maxima)) if len(maxima) > 1 else 0.0, 6
            )

    return {
        "grip_usage_consistency_std": grip_usage_consistency_std,
        "grip_usage_mean": grip_usage_mean,
    }


def _group_exit_timing(
    sl: "_CornerSlice", corner_events: dict[str, Any]
) -> dict[str, Any]:
    """exit_timing group."""
    throttle_onset_vs_yawrate_peak: float | None = None
    yawrate_at_throttle_on: float | None = None
    throttle_progression_slope: float | None = None

    # Throttle-on LapDistPct: try events first, then scan channel
    throttle_on_ldp: float | None = None
    ev = corner_events.get("throttle_on")
    if isinstance(ev, dict):
        throttle_on_ldp = ev.get("lapdist_pct")
    if throttle_on_ldp is None and sl.throttle is not None and sl.ldp is not None:
        idx = _first_exceed_idx(sl.throttle, 0.05)
        if idx is not None:
            throttle_on_ldp = float(sl.ldp[idx])

    # YawRate peak LapDistPct
    yaw_peak_ldp: float | None = None
    if sl.yaw_rate is not None and sl.ldp is not None:
        abs_yaw = np.where(np.isfinite(sl.yaw_rate), np.abs(sl.yaw_rate), np.nan)
        if np.any(np.isfinite(abs_yaw)):
            peak_idx = int(np.nanargmax(abs_yaw))
            yaw_peak_ldp = float(sl.ldp[peak_idx])

    if throttle_on_ldp is not None and yaw_peak_ldp is not None:
        throttle_onset_vs_yawrate_peak = round(throttle_on_ldp - yaw_peak_ldp, 6)

    if throttle_on_ldp is not None and sl.yaw_rate is not None and sl.ldp is not None:
        idx = _nearest_idx(sl.ldp, throttle_on_ldp)
        if idx is not None and math.isfinite(sl.yaw_rate[idx]):
            yawrate_at_throttle_on = round(float(sl.yaw_rate[idx]), 6)

    if throttle_on_ldp is not None and sl.throttle is not None and sl.ldp is not None:
        on_idx = _nearest_idx(sl.ldp, throttle_on_ldp)
        if on_idx is not None:
            post = sl.throttle[on_idx:]
            finite_post = post[np.isfinite(post)]
            if len(finite_post) > 1:
                slope = _linear_slope(
                    np.arange(len(finite_post), dtype=np.float64), finite_post
                )
                throttle_progression_slope = round(slope, 8) if math.isfinite(slope) else None

    return {
        "throttle_onset_vs_yawrate_peak": throttle_onset_vs_yawrate_peak,
        "yawrate_at_throttle_on": yawrate_at_throttle_on,
        "throttle_progression_slope": throttle_progression_slope,
    }


def _group_compound_strategy(
    sl: "_CornerSlice",
    grip_usage: np.ndarray | None,
    corner_type: str,
) -> dict[str, Any]:
    """compound_strategy group – only populated for compound corners."""
    if corner_type != "compound":
        return {
            "phase1_grip_usage_mean": None,
            "phase2_exit_speed": None,
            "curvature_peaks_count": None,
        }

    mid = sl.n // 2

    phase1_grip_usage_mean: float | None = None
    if grip_usage is not None and mid > 0:
        p1 = grip_usage[:mid]
        finite_p1 = p1[np.isfinite(p1)]
        if len(finite_p1) > 0:
            phase1_grip_usage_mean = round(float(np.nanmean(finite_p1)), 6)

    phase2_exit_speed: float | None = None
    if sl.speed is not None and mid < sl.n:
        p2 = sl.speed[mid:]
        finite_p2 = p2[np.isfinite(p2)]
        if len(finite_p2) > 0:
            phase2_exit_speed = round(float(np.nanmean(finite_p2)), 6)

    curvature_peaks_count: int | None = None
    if sl.yaw_rate is not None and sl.speed is not None:
        with np.errstate(invalid="ignore", divide="ignore"):
            curv = np.where(sl.speed > 0.5, sl.yaw_rate / sl.speed, 0.0)
        curv = np.where(np.isfinite(curv), np.abs(curv), 0.0)
        curvature_peaks_count = _count_peaks(curv, min_val=0.001)

    return {
        "phase1_grip_usage_mean": phase1_grip_usage_mean,
        "phase2_exit_speed": phase2_exit_speed,
        "curvature_peaks_count": curvature_peaks_count,
    }


def _group_dynamic_balance(sl: "_CornerSlice") -> dict[str, Any]:
    """dynamic_balance group."""
    if (
        sl.throttle is None
        or sl.brake is None
        or sl.lat_accel is None
        or sl.long_accel is None
    ):
        return {
            "balance_4d_variance": None,
            "balance_4d_variance_peak_lapdist": None,
        }

    arr = np.column_stack([
        sl.throttle,
        sl.brake,
        sl.lat_accel / _G,
        sl.long_accel / _G,
    ])
    finite_rows = np.all(np.isfinite(arr), axis=1)
    if np.sum(finite_rows) < 2:
        return {
            "balance_4d_variance": None,
            "balance_4d_variance_peak_lapdist": None,
        }

    arr_f = arr[finite_rows]
    norms = np.sqrt(np.sum(arr_f ** 2, axis=1))
    balance_4d_variance = round(float(np.var(norms)), 6)

    balance_4d_variance_peak_lapdist: float | None = None
    if sl.ldp is not None:
        idxs_finite = np.where(finite_rows)[0]
        peak_global = int(idxs_finite[int(np.argmax(norms))])
        if peak_global < len(sl.ldp) and math.isfinite(sl.ldp[peak_global]):
            balance_4d_variance_peak_lapdist = round(float(sl.ldp[peak_global]), 6)

    return {
        "balance_4d_variance": balance_4d_variance,
        "balance_4d_variance_peak_lapdist": balance_4d_variance_peak_lapdist,
    }


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def _write_snapshots(
    *,
    corner_id: int,
    sl: "_CornerSlice",
    grip_usage: np.ndarray | None,
    snapshots_dir: Path,
) -> None:
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    center_entry = min(sl.n // 4, max(0, sl.n - 1))

    _write_snapshot(
        snapshots_dir / f"{corner_id}_brake_entry_window.json",
        channels={"Brake": sl.brake, "LongAccel": sl.long_accel},
        center=center_entry,
        ldp=sl.ldp,
    )
    _write_snapshot(
        snapshots_dir / f"{corner_id}_yawrate_entry_window.json",
        channels={"YawRate": sl.yaw_rate, "SteeringWheelAngle": sl.steering},
        center=center_entry,
        ldp=sl.ldp,
    )

    if grip_usage is not None:
        gu_masked = np.where(np.isfinite(grip_usage), grip_usage, -1.0)
        center_grip = int(np.argmax(gu_masked))
    else:
        center_grip = sl.n // 2

    _write_snapshot(
        snapshots_dir / f"{corner_id}_gripusage_window.json",
        channels={"grip_usage": grip_usage},
        center=center_grip,
        ldp=sl.ldp,
    )


def _write_snapshot(
    path: Path,
    *,
    channels: dict[str, np.ndarray | None],
    center: int,
    ldp: np.ndarray | None,
) -> None:
    s = max(0, center - _SNAPSHOT_HALF)
    e = s + 2 * _SNAPSHOT_HALF + 1
    out: dict[str, Any] = {}
    if ldp is not None:
        win = ldp[s:e]
        out["LapDistPct"] = [
            round(float(v), 6) if math.isfinite(v) else None for v in win
        ]
    for name, arr in channels.items():
        if arr is not None:
            win = arr[s:e]
            out[name] = [
                round(float(v), 6) if math.isfinite(v) else None for v in win
            ]
        else:
            out[name] = None
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Channel container + corner slice
# ---------------------------------------------------------------------------


class _ChannelSet:
    """Loads and holds all telemetry arrays from a parquet column dict."""

    __slots__ = (
        "n",
        "ldp", "session_time", "speed", "yaw_rate", "lat_accel", "long_accel",
        "vert_accel", "throttle", "brake", "steering", "steer_torque",
        "gear", "rpm", "abs_active",
    )

    def __init__(self, data: dict[str, Any], n: int) -> None:
        self.n = n
        self.ldp = _get_channel(data, "LapDistPct", n)
        self.session_time = _get_channel(data, "SessionTime", n)
        self.speed = _get_channel(data, "Speed", n)
        self.yaw_rate = _get_channel(data, "YawRate", n)
        self.lat_accel = _get_channel(data, "LatAccel", n)
        self.long_accel = _get_channel(data, "LongAccel", n)
        self.vert_accel = _get_channel(data, "VertAccel", n)
        self.throttle = _get_channel(data, "Throttle", n)
        self.brake = _get_channel(data, "Brake", n)
        self.steering = _get_channel(data, "SteeringWheelAngle", n)
        self.steer_torque = _get_channel(data, "SteeringWheelTorque", n)
        self.gear = _get_channel(data, "Gear", n)
        self.rpm = _get_channel(data, "RPM", n)
        self.abs_active = _get_channel(data, "ABSactive", n)

    def slice(self, start_pct: float, end_pct: float) -> "_CornerSlice":
        if self.ldp is None:
            return _CornerSlice(self, 0, 0)
        mask = (self.ldp >= start_pct) & (self.ldp <= end_pct)
        idxs = np.where(mask)[0]
        if len(idxs) == 0:
            return _CornerSlice(self, 0, 0)
        return _CornerSlice(self, int(idxs[0]), int(idxs[-1]) + 1)


class _CornerSlice:
    """A sliced view of a _ChannelSet for one corner's LapDistPct window."""

    __slots__ = (
        "n",
        "ldp", "session_time", "speed", "yaw_rate", "lat_accel", "long_accel",
        "vert_accel", "throttle", "brake", "steering", "steer_torque",
        "gear", "rpm", "abs_active",
    )

    def __init__(self, parent: _ChannelSet, s: int, e: int) -> None:
        self.n = e - s

        def _sl(arr: np.ndarray | None) -> np.ndarray | None:
            return arr[s:e] if arr is not None else None

        self.ldp = _sl(parent.ldp)
        self.session_time = _sl(parent.session_time)
        self.speed = _sl(parent.speed)
        self.yaw_rate = _sl(parent.yaw_rate)
        self.lat_accel = _sl(parent.lat_accel)
        self.long_accel = _sl(parent.long_accel)
        self.vert_accel = _sl(parent.vert_accel)
        self.throttle = _sl(parent.throttle)
        self.brake = _sl(parent.brake)
        self.steering = _sl(parent.steering)
        self.steer_torque = _sl(parent.steer_torque)
        self.gear = _sl(parent.gear)
        self.rpm = _sl(parent.rpm)
        self.abs_active = _sl(parent.abs_active)


# ---------------------------------------------------------------------------
# Signal helpers
# ---------------------------------------------------------------------------


def _compute_grip_usage(sl: "_CornerSlice") -> np.ndarray | None:
    """grip_usage = sqrt((LatAccel/g)² + (LongAccel/g)²)."""
    if sl.lat_accel is None or sl.long_accel is None:
        return None
    gu = np.sqrt((sl.lat_accel / _G) ** 2 + (sl.long_accel / _G) ** 2)
    return np.where(np.isfinite(gu), gu, np.nan)


def _first_exceed_idx(arr: np.ndarray, threshold: float) -> int | None:
    """Index of first finite value in *arr* that exceeds *threshold*."""
    for i, v in enumerate(arr):
        if math.isfinite(float(v)) and float(v) > threshold:
            return i
    return None


def _linear_slope(x: np.ndarray, y: np.ndarray) -> float:
    """OLS slope of y regressed on x."""
    n = len(x)
    if n < 2:
        return float("nan")
    xm = float(np.mean(x))
    denom = float(np.sum((x - xm) ** 2))
    if denom < 1e-12:
        return float("nan")
    return float(np.sum((x - xm) * (y - float(np.mean(y)))) / denom)


def _xcorr_lag(a: np.ndarray, b: np.ndarray) -> int | None:
    """Sample lag that maximises cross-correlation between *a* and *b*."""
    af = a[np.isfinite(a)]
    bf = b[np.isfinite(b)]
    n = min(len(af), len(bf))
    if n < 4:
        return None
    an = af[:n] - float(np.mean(af[:n]))
    bn = bf[:n] - float(np.mean(bf[:n]))
    corr = np.correlate(an, bn, mode="full")
    lags = np.arange(-(n - 1), n)
    return int(lags[int(np.argmax(corr))])


def _find_sign_reversals(arr: np.ndarray) -> list[int]:
    """Indices where the sign of *arr* changes (correction events)."""
    result: list[int] = []
    for i in range(1, len(arr)):
        if (
            math.isfinite(float(arr[i]))
            and math.isfinite(float(arr[i - 1]))
            and arr[i - 1] != 0
            and arr[i] * arr[i - 1] < 0
        ):
            result.append(i)
    return result


def _count_peaks(arr: np.ndarray, min_val: float = 0.0) -> int:
    """Count local maxima in *arr* that are at least *min_val*."""
    count = 0
    for i in range(1, len(arr) - 1):
        if arr[i] > arr[i - 1] and arr[i] > arr[i + 1] and arr[i] >= min_val:
            count += 1
    return count


def _nearest_idx(ldp: np.ndarray, target: float) -> int | None:
    if len(ldp) == 0:
        return None
    return int(np.argmin(np.abs(ldp - target)))


def _filter_events(
    events: dict[str, Any] | None,
    start_pct: float,
    end_pct: float,
) -> dict[str, Any]:
    """Return subset of *events* whose lapdist_pct falls in [start_pct, end_pct]."""
    if events is None:
        return {}
    raw = events.get("events", events)
    result: dict[str, Any] = {}
    for key, ev in raw.items():
        if ev is None:
            continue
        if isinstance(ev, dict):
            ldp = ev.get("lapdist_pct")
            if ldp is not None and start_pct <= ldp <= end_pct:
                result[key] = ev
        elif isinstance(ev, list):
            filtered = [
                e for e in ev
                if isinstance(e, dict) and start_pct <= e.get("lapdist_pct", -1) <= end_pct
            ]
            if filtered:
                result[key] = filtered
    return result


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _write_parquet(rows: list[dict[str, Any]], path: Path) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)

    # Collect ordered column names (preserving insertion order across rows)
    all_keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for k in row:
            if k not in seen:
                all_keys.append(k)
                seen.add(k)

    arrays: dict[str, pa.Array] = {}
    for key in all_keys:
        values = [row.get(key) for row in rows]
        arrays[key] = _infer_pa_array(values)

    pq.write_table(pa.table(arrays), str(path))


def _infer_pa_array(values: list[Any]) -> pa.Array:
    """Build a PyArrow array with type inferred from the first non-None value."""
    first = next((v for v in values if v is not None), None)
    if isinstance(first, bool):
        return pa.array(values, type=pa.bool_())
    if isinstance(first, int):
        return pa.array(values, type=pa.int64())
    if isinstance(first, float):
        return pa.array(values, type=pa.float64())
    if isinstance(first, str):
        return pa.array(values, type=pa.string())
    # Fallback: try float64, then string
    try:
        return pa.array(
            [float(v) if v is not None else None for v in values], type=pa.float64()
        )
    except Exception:
        return pa.array(
            [str(v) if v is not None else None for v in values], type=pa.string()
        )


def _read_parquet_as_dict(path: Path) -> dict[str, Any]:
    return pq.read_table(str(path)).to_pydict()


def _get_channel(data: dict[str, Any], name: str, n: int) -> np.ndarray | None:
    """Return a channel as a float64 numpy array, or ``None`` if absent/all-NaN."""
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
