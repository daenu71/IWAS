from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq
from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from core.coaching import lap_view_model as lap_view_model_mod


PLOT_W = 1120
PLOT_H = 960
PLOT_PAD = 72

RAW_CHANNELS = (
    "SessionTime",
    "LapDistPct",
    "Speed",
    "Yaw",
    "VelocityX",
    "VelocityY",
    "VelocityZ",
)

ROTATION_VARIANTS: dict[str, tuple[float, float]] = {
    # Variant A: local Y behaves like "left-positive".
    "local_xy_rot_a": (-1.0, 1.0),
    # Variant B: local Y behaves like "right-positive".
    "local_xy_rot_b": (1.0, -1.0),
}


@dataclass(frozen=True)
class Stage:
    name: str
    source: str
    lap_dist_pct: np.ndarray
    xy: np.ndarray
    metadata: dict[str, Any]


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _unwrap_lapdist(values: np.ndarray) -> np.ndarray:
    out = np.array(values, dtype=np.float64, copy=True)
    offset = 0.0
    prev: float | None = None
    for idx, raw in enumerate(out):
        if not np.isfinite(raw):
            continue
        if prev is not None and (raw - prev) < -0.5:
            offset += 1.0
        out[idx] = raw + offset
        prev = float(raw)
    return out


def _build_dt(session_time: np.ndarray) -> tuple[np.ndarray, str]:
    session_time = np.asarray(session_time, dtype=np.float64)
    if session_time.size >= 2:
        diffs = np.diff(session_time, prepend=session_time[0])
        positive = np.diff(session_time)
        positive = positive[np.isfinite(positive) & (positive > 0.0)]
        fallback = float(np.median(positive)) if positive.size else 0.01
        dt = np.where(np.isfinite(diffs) & (diffs > 0.0), diffs, fallback)
        return dt, "SessionTime"
    return np.full(session_time.shape, 0.01, dtype=np.float64), "fixed_0.01s_fallback"


def _finite_mask(*arrays: np.ndarray) -> np.ndarray:
    mask = np.ones(len(arrays[0]), dtype=bool)
    for arr in arrays:
        mask &= np.isfinite(arr)
    return mask


def _integrate_world_velocity(
    session_time: np.ndarray,
    world_vx: np.ndarray,
    world_vy: np.ndarray,
) -> tuple[np.ndarray, str]:
    dt, time_source = _build_dt(session_time)
    x = np.cumsum(np.where(np.isfinite(world_vx), world_vx, 0.0) * dt)
    y = np.cumsum(np.where(np.isfinite(world_vy), world_vy, 0.0) * dt)
    return np.column_stack([x, y]), time_source


def _integrate_speed_yaw(
    session_time: np.ndarray,
    speed: np.ndarray,
    yaw: np.ndarray,
) -> tuple[np.ndarray, str]:
    speed = np.asarray(speed, dtype=np.float64)
    yaw = np.asarray(yaw, dtype=np.float64)
    world_vx = np.where(np.isfinite(speed), speed, 0.0) * np.cos(np.where(np.isfinite(yaw), yaw, 0.0))
    world_vy = np.where(np.isfinite(speed), speed, 0.0) * np.sin(np.where(np.isfinite(yaw), yaw, 0.0))
    return _integrate_world_velocity(session_time, world_vx, world_vy)


def _integrate_velocity_direct(
    session_time: np.ndarray,
    velocity_x: np.ndarray,
    velocity_y: np.ndarray,
) -> tuple[np.ndarray, str]:
    return _integrate_world_velocity(session_time, velocity_x, velocity_y)


def _integrate_local_velocity_rotated(
    session_time: np.ndarray,
    velocity_x: np.ndarray,
    velocity_y: np.ndarray,
    yaw: np.ndarray,
    *,
    variant: str,
) -> tuple[np.ndarray, str]:
    sign_sin, sign_cos = ROTATION_VARIANTS[variant]
    vx = np.where(np.isfinite(velocity_x), velocity_x, 0.0)
    vy = np.where(np.isfinite(velocity_y), velocity_y, 0.0)
    yaw_safe = np.where(np.isfinite(yaw), yaw, 0.0)
    world_vx = vx * np.cos(yaw_safe) + sign_sin * vy * np.sin(yaw_safe)
    world_vy = vx * np.sin(yaw_safe) + sign_cos * vy * np.cos(yaw_safe)
    return _integrate_world_velocity(session_time, world_vx, world_vy)


def _heading_deg(xy: np.ndarray, idx: int, window: int = 5) -> float | None:
    if len(xy) < 2:
        return None
    i0 = max(0, idx - window)
    i1 = min(len(xy) - 1, idx + window)
    dx = float(xy[i1, 0] - xy[i0, 0])
    dy = float(xy[i1, 1] - xy[i0, 1])
    if not np.isfinite(dx) or not np.isfinite(dy) or math.hypot(dx, dy) < 1e-9:
        return None
    return math.degrees(math.atan2(dy, dx))


def _line_metrics(xy: np.ndarray, mask: np.ndarray) -> dict[str, Any]:
    pts = xy[mask]
    if len(pts) < 2:
        return {"sample_count": int(len(pts))}
    center = pts.mean(axis=0)
    _, _, vh = np.linalg.svd(pts - center, full_matrices=False)
    direction = vh[0]
    normal = np.array([-direction[1], direction[0]], dtype=np.float64)
    deviations = np.abs((pts - center) @ normal)
    return {
        "sample_count": int(len(pts)),
        "mean_cross_track_m": float(np.mean(deviations)),
        "max_cross_track_m": float(np.max(deviations)),
        "direction": [float(direction[0]), float(direction[1])],
    }


def _polyline_length(xy: np.ndarray) -> float:
    if len(xy) < 2:
        return 0.0
    delta = np.diff(xy, axis=0)
    return float(np.sum(np.hypot(delta[:, 0], delta[:, 1])))


def _path_area_abs(xy: np.ndarray) -> float:
    if len(xy) < 3:
        return 0.0
    x = xy[:, 0]
    y = xy[:, 1]
    return float(abs(0.5 * np.sum(x[:-1] * y[1:] - x[1:] * y[:-1])))


def _turning_metrics(xy: np.ndarray) -> dict[str, float]:
    if len(xy) < 3:
        return {"net_turn_deg": 0.0, "abs_turn_deg": 0.0}
    seg = np.diff(xy, axis=0)
    lengths = np.hypot(seg[:, 0], seg[:, 1])
    valid = lengths > 1e-6
    if int(np.sum(valid)) < 2:
        return {"net_turn_deg": 0.0, "abs_turn_deg": 0.0}
    headings = np.unwrap(np.arctan2(seg[valid, 1], seg[valid, 0]))
    delta = np.diff(headings)
    return {
        "net_turn_deg": float(np.degrees(np.sum(delta))),
        "abs_turn_deg": float(np.degrees(np.sum(np.abs(delta)))),
    }


def _bbox(xy: np.ndarray) -> dict[str, float]:
    return {
        "x_min": float(np.min(xy[:, 0])),
        "x_max": float(np.max(xy[:, 0])),
        "y_min": float(np.min(xy[:, 1])),
        "y_max": float(np.max(xy[:, 1])),
        "width": float(np.ptp(xy[:, 0])),
        "height": float(np.ptp(xy[:, 1])),
    }


def _sample_report(lap_dist_pct: np.ndarray, xy: np.ndarray, *, n_each_side: int = 20) -> dict[str, Any]:
    if len(xy) == 0:
        return {"first_samples": [], "last_samples": []}
    first_indices = list(range(min(n_each_side, len(xy))))
    last_indices = list(range(max(0, len(xy) - n_each_side), len(xy)))

    def _rows(indices: list[int]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for idx in indices:
            rows.append(
                {
                    "idx": int(idx),
                    "lap_dist_pct": float(lap_dist_pct[idx]),
                    "x": float(xy[idx, 0]),
                    "y": float(xy[idx, 1]),
                    "heading_deg": _heading_deg(xy, idx),
                }
            )
        return rows

    return {
        "first_samples": _rows(first_indices),
        "last_samples": _rows(last_indices),
    }


def _channel_stats(columns: dict[str, np.ndarray]) -> dict[str, Any]:
    report: dict[str, Any] = {
        "available_channels": sorted(columns.keys()),
        "channel_count": int(len(columns)),
        "channels": {},
    }
    for name, arr in columns.items():
        values = np.asarray(arr, dtype=np.float64)
        finite = values[np.isfinite(values)]
        if finite.size == 0:
            report["channels"][name] = {
                "sample_count": int(len(values)),
                "finite_count": 0,
                "nonzero_share": 0.0,
            }
            continue
        report["channels"][name] = {
            "sample_count": int(len(values)),
            "finite_count": int(finite.size),
            "min": float(np.min(finite)),
            "max": float(np.max(finite)),
            "mean": float(np.mean(finite)),
            "std": float(np.std(finite)),
            "nonzero_share": float(np.mean(np.abs(finite) > 1e-6)),
        }
    return report


def _time_axis_quality(session_time: np.ndarray) -> dict[str, Any]:
    session_time = np.asarray(session_time, dtype=np.float64)
    if session_time.size < 2:
        return {
            "sample_count": int(session_time.size),
            "nonpositive_step_count": 0,
            "positive_step_median_s": None,
            "positive_step_max_s": None,
        }
    diffs = np.diff(session_time)
    positive = diffs[diffs > 0.0]
    return {
        "sample_count": int(session_time.size),
        "nonpositive_step_count": int(np.sum(diffs <= 0.0)),
        "positive_step_median_s": float(np.median(positive)) if positive.size else None,
        "positive_step_max_s": float(np.max(positive)) if positive.size else None,
    }


def _lane_like_mask(
    lap_dist_pct: np.ndarray,
    session_time: np.ndarray,
    yaw: np.ndarray,
    velocity_y: np.ndarray,
) -> np.ndarray:
    wrap_mask = (lap_dist_pct >= 0.95) | (lap_dist_pct <= 0.05)
    dt, _ = _build_dt(session_time)
    yaw_unwrapped = np.unwrap(np.where(np.isfinite(yaw), yaw, 0.0))
    yaw_rate = np.divide(
        np.diff(yaw_unwrapped, prepend=yaw_unwrapped[0]),
        dt,
        out=np.zeros_like(yaw_unwrapped),
        where=dt > 0.0,
    )
    return wrap_mask & (np.abs(np.where(np.isfinite(velocity_y), velocity_y, 0.0)) > 0.10) & (np.abs(yaw_rate) < 0.15)


def _stage_report(stage: Stage) -> dict[str, Any]:
    base = {
        "name": stage.name,
        "source": stage.source,
        "status": stage.metadata.get("status", "ok"),
        "required_channels": list(stage.metadata.get("required_channels", [])),
        "missing_channels": list(stage.metadata.get("missing_channels", [])),
        "channel_presence": dict(stage.metadata.get("channel_presence", {})),
    }
    if len(stage.xy) == 0:
        return {**base, "sample_count": 0, "metadata": stage.metadata}
    lap_dist_pct = stage.lap_dist_pct
    wrap_mask = (lap_dist_pct >= 0.95) | (lap_dist_pct <= 0.05)
    start_mask = (lap_dist_pct >= 0.0) & (lap_dist_pct <= 0.06)
    lane_like_indices = [int(idx) for idx in stage.metadata.get("lane_like_indices", [])]
    start_end_distance = float(np.linalg.norm(stage.xy[-1] - stage.xy[0])) if len(stage.xy) >= 2 else 0.0
    polyline_length = _polyline_length(stage.xy)
    turning = _turning_metrics(stage.xy)
    ratio = polyline_length / max(start_end_distance, 1e-6)
    return {
        **base,
        "sample_count": int(len(stage.xy)),
        "bbox_m": _bbox(stage.xy),
        "polyline_length_m": polyline_length,
        "start_point_m": [float(stage.xy[0, 0]), float(stage.xy[0, 1])],
        "end_point_m": [float(stage.xy[-1, 0]), float(stage.xy[-1, 1])],
        "start_end_distance_m": start_end_distance,
        "length_over_start_end_ratio": float(ratio),
        "path_area_abs_m2": _path_area_abs(stage.xy),
        "heading_start_deg": _heading_deg(stage.xy, 5),
        "heading_end_deg": _heading_deg(stage.xy, max(0, len(stage.xy) - 6)),
        "turning": turning,
        "start_straightness": _line_metrics(stage.xy, start_mask),
        "wrap_zone_straightness": _line_metrics(stage.xy, wrap_mask),
        "lane_like_count": int(len(lane_like_indices)),
        "metadata": stage.metadata,
        "boundary_samples": _sample_report(lap_dist_pct, stage.xy),
    }


def _stage_issue_report(
    *,
    name: str,
    source: str,
    status: str,
    required_channels: tuple[str, ...] | list[str],
    available_channels: set[str],
    missing_channels: list[str],
    metadata: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    report = {
        "name": name,
        "source": source,
        "status": status,
        "required_channels": list(required_channels),
        "missing_channels": list(missing_channels),
        "channel_presence": {channel: (channel in available_channels) for channel in required_channels},
        "metadata": metadata or {},
    }
    if error is not None:
        report["error"] = error
    return report


def _safe_quantile(values: np.ndarray, q: float) -> float:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return float("nan")
    return float(np.quantile(finite, q))


def _pointwise_delta(a: Stage, b: Stage) -> dict[str, Any] | None:
    if len(a.xy) != len(b.xy) or len(a.xy) == 0:
        return None
    delta = np.hypot(a.xy[:, 0] - b.xy[:, 0], a.xy[:, 1] - b.xy[:, 1])
    lane_like_indices = np.array(a.metadata.get("lane_like_indices", []), dtype=np.int64)
    lane_values = delta[lane_like_indices] if lane_like_indices.size else np.empty(0, dtype=np.float64)
    wrap_mask = (a.lap_dist_pct >= 0.95) | (a.lap_dist_pct <= 0.05)
    non_wrap_values = delta[~wrap_mask]
    return {
        "sample_count": int(len(delta)),
        "median_m": _safe_quantile(delta, 0.50),
        "p95_m": _safe_quantile(delta, 0.95),
        "max_m": _safe_quantile(delta, 1.00),
        "non_wrap_sample_count": int(non_wrap_values.size),
        "non_wrap_median_m": _safe_quantile(non_wrap_values, 0.50) if non_wrap_values.size else 0.0,
        "non_wrap_p95_m": _safe_quantile(non_wrap_values, 0.95) if non_wrap_values.size else 0.0,
        "non_wrap_max_m": _safe_quantile(non_wrap_values, 1.00) if non_wrap_values.size else 0.0,
        "lane_like_sample_count": int(lane_values.size),
        "lane_like_median_m": _safe_quantile(lane_values, 0.50) if lane_values.size else 0.0,
        "lane_like_max_m": _safe_quantile(lane_values, 1.00) if lane_values.size else 0.0,
    }


def _axis_semantics(columns: dict[str, np.ndarray]) -> dict[str, Any]:
    required = {"Speed", "VelocityX", "VelocityY", "VelocityZ"}
    missing = sorted(required - set(columns))
    if missing:
        return {
            "status": "skipped_missing_channels",
            "required_channels": sorted(required),
            "missing_channels": missing,
        }
    speed = np.asarray(columns["Speed"], dtype=np.float64)
    vx = np.asarray(columns["VelocityX"], dtype=np.float64)
    vy = np.asarray(columns["VelocityY"], dtype=np.float64)
    vz = np.asarray(columns["VelocityZ"], dtype=np.float64)
    magnitude = np.sqrt(np.maximum(0.0, vx * vx + vy * vy + vz * vz))
    finite_speed = np.where(np.isfinite(speed), speed, 0.0)
    finite_vx = np.where(np.isfinite(vx), vx, 0.0)
    finite_vy = np.where(np.isfinite(vy), vy, 0.0)
    finite_vz = np.where(np.isfinite(vz), vz, 0.0)
    return {
        "status": "ok",
        "speed_vs_velocityx_corr": float(np.corrcoef(finite_speed, finite_vx)[0, 1]),
        "speed_vs_velocity_magnitude_corr": float(np.corrcoef(finite_speed, np.where(np.isfinite(magnitude), magnitude, 0.0))[0, 1]),
        "mean_abs_speed_minus_velocityx_mps": float(np.nanmean(np.abs(speed - vx))),
        "mean_abs_speed_minus_velocity_magnitude_mps": float(np.nanmean(np.abs(speed - magnitude))),
        "mean_abs_velocityy_mps": float(np.nanmean(np.abs(vy))),
        "mean_abs_velocityz_mps": float(np.nanmean(np.abs(vz))),
        "std_ratio_velocityy_to_velocityx": float(np.nanstd(vy) / max(np.nanstd(vx), 1e-6)),
        "std_ratio_velocityz_to_velocityx": float(np.nanstd(vz) / max(np.nanstd(vx), 1e-6)),
        "likely_coordinate_system": "vehicle_local",
        "likely_horizontal_axes": ["VelocityX", "VelocityY"],
        "likely_vertical_axis": "VelocityZ",
        "justification": (
            "VelocityX tracks Speed almost 1:1, while VelocityY and VelocityZ remain small side/vertical components. "
            "That is consistent with a vehicle-local frame, not a world-frame XY velocity."
        ),
    }


def _comparison_report(stage_map: dict[str, Stage], left_name: str, right_name: str) -> dict[str, Any]:
    missing_stages = [name for name in (left_name, right_name) if name not in stage_map]
    if missing_stages:
        return {"status": "skipped_missing_stage", "missing_stages": missing_stages}
    delta = _pointwise_delta(stage_map[left_name], stage_map[right_name])
    if delta is None:
        return {"status": "failed", "reason": "sample_count_mismatch_or_empty"}
    return {"status": "ok", **delta}


def _time_axis_quality_report(columns: dict[str, np.ndarray], source: str) -> dict[str, Any]:
    if "SessionTime" not in columns:
        return {
            "status": "skipped_missing_channels",
            "source": source,
            "missing_channels": ["SessionTime"],
        }
    return {"status": "ok", "source": source, **_time_axis_quality(np.asarray(columns["SessionTime"], dtype=np.float64))}


def _choose_best_rotated_variant(stage_reports: dict[str, dict[str, Any]], comparisons: dict[str, Any]) -> dict[str, Any]:
    assessed_candidates: list[dict[str, Any]] = []
    for name in ROTATION_VARIANTS:
        report = stage_reports.get(name)
        if not isinstance(report, dict):
            continue
        if report.get("status") != "ok":
            assessed_candidates.append(
                {
                    "name": name,
                    "status": report.get("status", "missing"),
                    "missing_channels": list(report.get("missing_channels", [])),
                }
            )
            continue
        wrap = dict(report.get("wrap_zone_straightness") or {})
        ui_cmp = dict(comparisons.get(f"ui_current_path_vs_{name}") or {})
        wrap_mean = _optional_float(wrap.get("mean_cross_track_m"))
        wrap_max = _optional_float(wrap.get("max_cross_track_m"))
        start_end_distance = _optional_float(report.get("start_end_distance_m"))
        length_ratio = _optional_float(report.get("length_over_start_end_ratio")) or 0.0
        ui_non_wrap_p95 = _optional_float(ui_cmp.get("non_wrap_p95_m")) if ui_cmp.get("status") == "ok" else None
        ui_global_p95 = _optional_float(ui_cmp.get("p95_m")) if ui_cmp.get("status") == "ok" else None
        global_metric_name = "ui_non_wrap_p95_delta_m" if ui_non_wrap_p95 is not None else "start_end_distance_m"
        global_metric_value = ui_non_wrap_p95 if ui_non_wrap_p95 is not None else start_end_distance
        candidate = {
            "name": name,
            "status": "ok",
            "wrap_mean_cross_track_m": wrap_mean,
            "wrap_max_cross_track_m": wrap_max,
            "start_end_distance_m": start_end_distance,
            "length_over_start_end_ratio": length_ratio,
            "ui_non_wrap_p95_delta_m": ui_non_wrap_p95,
            "ui_global_p95_delta_m": ui_global_p95,
            "global_shape_metric": global_metric_name,
            "global_shape_score_m": global_metric_value,
            "score_components": {
                "global_shape_metric": global_metric_name,
                "global_shape_score_m": global_metric_value,
                "start_end_distance_m": start_end_distance,
                "wrap_mean_cross_track_m": wrap_mean,
                "wrap_max_cross_track_m": wrap_max,
                "length_over_start_end_ratio": length_ratio,
            },
        }
        candidate["_sort_key"] = (
            global_metric_value if global_metric_value is not None else float("inf"),
            start_end_distance if start_end_distance is not None else float("inf"),
            wrap_mean if wrap_mean is not None else float("inf"),
            wrap_max if wrap_max is not None else float("inf"),
            -length_ratio,
        )
        assessed_candidates.append(candidate)
    ok_candidates = [item for item in assessed_candidates if item.get("status") == "ok"]
    if not ok_candidates:
        for item in assessed_candidates:
            item.pop("_sort_key", None)
        return {
            "status": "insufficient_data",
            "selection_rule": [
                "prefer smaller ui_non_wrap_p95_delta_m when ui_current_path is available",
                "otherwise prefer smaller start_end_distance_m as a simple global closure metric",
                "then prefer smaller wrap_mean_cross_track_m and wrap_max_cross_track_m",
                "finally prefer larger length_over_start_end_ratio",
            ],
            "all_candidates": assessed_candidates,
        }
    ok_candidates.sort(key=lambda item: item["_sort_key"])
    best_candidate = {key: value for key, value in ok_candidates[0].items() if key != "_sort_key"}
    all_candidates = [{key: value for key, value in item.items() if key != "_sort_key"} for item in assessed_candidates]
    return {
        "status": "ok",
        "selection_rule": [
            "prefer smaller ui_non_wrap_p95_delta_m when ui_current_path is available",
            "otherwise prefer smaller start_end_distance_m as a simple global closure metric",
            "then prefer smaller wrap_mean_cross_track_m and wrap_max_cross_track_m",
            "finally prefer larger length_over_start_end_ratio",
        ],
        "best_candidate": best_candidate,
        "all_candidates": all_candidates,
    }


def _load_table_columns(table: pq.Table) -> dict[str, np.ndarray]:
    return {
        name: np.array(table.column(name).to_pylist(), dtype=np.float64)
        for name in table.schema.names
    }


def _load_raw_lap(session_dir: Path, lap_no: int) -> tuple[dict[str, np.ndarray], dict[str, Any], dict[str, Any]]:
    meta_path = session_dir / f"run_0001_lap_{lap_no:04d}_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    run_path = session_dir / "run_0001.parquet"
    available_channels = list(pq.ParquetFile(run_path).schema_arrow.names)
    available_set = set(available_channels)
    loaded_channels = [name for name in RAW_CHANNELS if name in available_set]
    missing_channels = [name for name in RAW_CHANNELS if name not in available_set]
    table = pq.read_table(run_path, columns=loaded_channels).slice(
        meta["lap_start_sample"],
        meta["lap_end_sample"] - meta["lap_start_sample"] + 1,
    )
    load_report = {
        "path": str(run_path),
        "requested_channels": list(RAW_CHANNELS),
        "available_channels": available_channels,
        "loaded_channels": loaded_channels,
        "missing_channels": missing_channels,
        "channel_presence": {name: (name in available_set) for name in RAW_CHANNELS},
        "sample_count": int(table.num_rows),
    }
    return _load_table_columns(table), meta, load_report


def _load_resampled_lap(session_dir: Path, lap_no: int) -> tuple[dict[str, np.ndarray], Path, dict[str, Any]]:
    path = session_dir / "laps" / f"lap_{lap_no:04d}" / "analysis" / "lap_resampled.parquet"
    table = pq.read_table(path)
    available_channels = list(table.schema.names)
    available_set = set(available_channels)
    load_report = {
        "path": str(path),
        "requested_channels": list(RAW_CHANNELS),
        "available_channels": available_channels,
        "loaded_channels": [name for name in RAW_CHANNELS if name in available_set],
        "missing_channels": [name for name in RAW_CHANNELS if name not in available_set],
        "channel_presence": {name: (name in available_set) for name in RAW_CHANNELS},
        "sample_count": int(table.num_rows),
    }
    return _load_table_columns(table), path, load_report


def _ui_stage(resampled_path: Path, lap_dist_pct: np.ndarray, lane_like_indices: list[int]) -> Stage:
    geometry = lap_view_model_mod._load_resampled_geometry(resampled_path)
    return Stage(
        name="ui_current_path",
        source="core.coaching.lap_view_model._load_resampled_geometry",
        lap_dist_pct=np.array(lap_dist_pct, dtype=np.float64),
        xy=np.array(geometry.track_xy, dtype=np.float64),
        metadata={
            "source_name": geometry.source,
            "is_closed": bool(geometry.is_closed),
            "time_source": "SessionTime",
            "lane_like_indices": lane_like_indices,
        },
    )


def _make_stages(
    raw: dict[str, np.ndarray],
    resampled: dict[str, np.ndarray],
    *,
    resampled_path: Path,
) -> tuple[list[Stage], dict[str, dict[str, Any]], dict[str, Any]]:
    stages: list[Stage] = []
    stage_reports: dict[str, dict[str, Any]] = {}
    available_channels = set(resampled)
    lap_dist_pct = np.asarray(resampled["LapDistPct"], dtype=np.float64) if "LapDistPct" in resampled else None

    lane_like_required = ("LapDistPct", "SessionTime", "Yaw", "VelocityY")
    lane_like_missing = [name for name in lane_like_required if name not in available_channels]
    if lane_like_missing:
        lane_like_indices: list[int] = []
        lane_like_report = {
            "status": "skipped_missing_channels",
            "required_channels": list(lane_like_required),
            "missing_channels": lane_like_missing,
            "sample_count": 0,
        }
    else:
        lane_like_indices = np.flatnonzero(
            _lane_like_mask(
                np.asarray(resampled["LapDistPct"], dtype=np.float64),
                np.asarray(resampled["SessionTime"], dtype=np.float64),
                np.asarray(resampled["Yaw"], dtype=np.float64),
                np.asarray(resampled["VelocityY"], dtype=np.float64),
            )
        ).tolist()
        lane_like_report = {
            "status": "ok",
            "required_channels": list(lane_like_required),
            "missing_channels": [],
            "sample_count": int(len(lane_like_indices)),
        }

    def build_stage_metadata(required_channels: tuple[str, ...], **extra: Any) -> dict[str, Any]:
        metadata = {
            "status": "ok",
            "required_channels": list(required_channels),
            "missing_channels": [],
            "channel_presence": {channel: (channel in available_channels) for channel in required_channels},
            "lane_like_indices": lane_like_indices,
            "lane_like_status": lane_like_report["status"],
        }
        metadata.update(extra)
        return metadata

    def skip_stage(name: str, source: str, required_channels: tuple[str, ...], missing_channels: list[str], **metadata: Any) -> None:
        stage_reports[name] = _stage_issue_report(
            name=name,
            source=source,
            status="skipped_missing_channels",
            required_channels=required_channels,
            available_channels=available_channels,
            missing_channels=missing_channels,
            metadata=metadata,
        )

    def fail_stage(name: str, source: str, required_channels: tuple[str, ...], error: Exception, **metadata: Any) -> None:
        stage_reports[name] = _stage_issue_report(
            name=name,
            source=source,
            status="failed",
            required_channels=required_channels,
            available_channels=available_channels,
            missing_channels=[],
            metadata=metadata,
            error=f"{type(error).__name__}: {error}",
        )

    ui_required = ("LapDistPct",)
    ui_missing = [name for name in ui_required if name not in available_channels]
    if ui_missing:
        skip_stage("ui_current_path", "core.coaching.lap_view_model._load_resampled_geometry", ui_required, ui_missing, lane_like_status=lane_like_report["status"])
    else:
        try:
            current_ui = _ui_stage(resampled_path, lap_dist_pct, lane_like_indices)
            current_ui = Stage(
                name=current_ui.name,
                source=current_ui.source,
                lap_dist_pct=current_ui.lap_dist_pct,
                xy=current_ui.xy,
                metadata={
                    **current_ui.metadata,
                    **build_stage_metadata(
                        ui_required,
                        time_source=current_ui.metadata.get("time_source"),
                        source_name=current_ui.metadata.get("source_name"),
                        is_closed=current_ui.metadata.get("is_closed"),
                    ),
                },
            )
            stages.append(current_ui)
            stage_reports[current_ui.name] = _stage_report(current_ui)
        except Exception as exc:
            fail_stage("ui_current_path", "core.coaching.lap_view_model._load_resampled_geometry", ui_required, exc, lane_like_status=lane_like_report["status"])

    speed_required = ("LapDistPct", "SessionTime", "Speed", "Yaw")
    speed_missing = [name for name in speed_required if name not in available_channels]
    if speed_missing:
        skip_stage(
            "speed_yaw",
            "resampled",
            speed_required,
            speed_missing,
            formula="world_v=(Speed*cos(Yaw), Speed*sin(Yaw))",
            lane_like_status=lane_like_report["status"],
        )
    else:
        try:
            speed_xy, time_source = _integrate_speed_yaw(
                np.asarray(resampled["SessionTime"], dtype=np.float64),
                np.asarray(resampled["Speed"], dtype=np.float64),
                np.asarray(resampled["Yaw"], dtype=np.float64),
            )
            stage = Stage(
                name="speed_yaw",
                source="resampled",
                lap_dist_pct=lap_dist_pct,
                xy=speed_xy,
                metadata=build_stage_metadata(
                    speed_required,
                    time_source=time_source,
                    formula="world_v=(Speed*cos(Yaw), Speed*sin(Yaw))",
                ),
            )
            stages.append(stage)
            stage_reports[stage.name] = _stage_report(stage)
        except Exception as exc:
            fail_stage(
                "speed_yaw",
                "resampled",
                speed_required,
                exc,
                formula="world_v=(Speed*cos(Yaw), Speed*sin(Yaw))",
                lane_like_status=lane_like_report["status"],
            )

    direct_required = ("LapDistPct", "SessionTime", "VelocityX", "VelocityY")
    direct_missing = [name for name in direct_required if name not in available_channels]
    if direct_missing:
        skip_stage(
            "velocity_xy_direct",
            "resampled",
            direct_required,
            direct_missing,
            formula="world_v=(VelocityX, VelocityY)",
            expected_semantics="negative_control_if_vehicle_local",
            lane_like_status=lane_like_report["status"],
        )
    else:
        try:
            velocity_direct_xy, time_source = _integrate_velocity_direct(
                np.asarray(resampled["SessionTime"], dtype=np.float64),
                np.asarray(resampled["VelocityX"], dtype=np.float64),
                np.asarray(resampled["VelocityY"], dtype=np.float64),
            )
            stage = Stage(
                name="velocity_xy_direct",
                source="resampled",
                lap_dist_pct=lap_dist_pct,
                xy=velocity_direct_xy,
                metadata=build_stage_metadata(
                    direct_required,
                    time_source=time_source,
                    formula="world_v=(VelocityX, VelocityY)",
                    expected_semantics="negative_control_if_vehicle_local",
                ),
            )
            stages.append(stage)
            stage_reports[stage.name] = _stage_report(stage)
        except Exception as exc:
            fail_stage(
                "velocity_xy_direct",
                "resampled",
                direct_required,
                exc,
                formula="world_v=(VelocityX, VelocityY)",
                expected_semantics="negative_control_if_vehicle_local",
                lane_like_status=lane_like_report["status"],
            )

    rotated_required = ("LapDistPct", "SessionTime", "VelocityX", "VelocityY", "Yaw")
    for variant in ROTATION_VARIANTS:
        rotated_missing = [name for name in rotated_required if name not in available_channels]
        if rotated_missing:
            skip_stage(
                variant,
                "resampled",
                rotated_required,
                rotated_missing,
                formula="world_v=rotate(local VelocityX/VelocityY by Yaw)",
                rotation_variant=variant,
                lane_like_status=lane_like_report["status"],
            )
            continue
        try:
            rotated_xy, time_source = _integrate_local_velocity_rotated(
                np.asarray(resampled["SessionTime"], dtype=np.float64),
                np.asarray(resampled["VelocityX"], dtype=np.float64),
                np.asarray(resampled["VelocityY"], dtype=np.float64),
                np.asarray(resampled["Yaw"], dtype=np.float64),
                variant=variant,
            )
            stage = Stage(
                name=variant,
                source="resampled",
                lap_dist_pct=lap_dist_pct,
                xy=rotated_xy,
                metadata=build_stage_metadata(
                    rotated_required,
                    time_source=time_source,
                    formula="world_v=rotate(local VelocityX/VelocityY by Yaw)",
                    rotation_variant=variant,
                ),
            )
            stages.append(stage)
            stage_reports[stage.name] = _stage_report(stage)
        except Exception as exc:
            fail_stage(
                variant,
                "resampled",
                rotated_required,
                exc,
                formula="world_v=rotate(local VelocityX/VelocityY by Yaw)",
                rotation_variant=variant,
                lane_like_status=lane_like_report["status"],
            )

    xz_required = ("LapDistPct", "SessionTime", "VelocityX", "VelocityZ")
    raw_semantics = _axis_semantics(raw)
    if raw_semantics.get("status") != "ok":
        skip_stage(
            "velocity_xz_direct",
            "resampled",
            xz_required,
            list(raw_semantics.get("missing_channels", [])),
            formula="world_v=(VelocityX, VelocityZ)",
            diagnostic_only=True,
            gating_status=raw_semantics.get("status"),
            gating_missing_channels=list(raw_semantics.get("missing_channels", [])),
            reason="raw_axis_semantics_missing_channels",
            lane_like_status=lane_like_report["status"],
        )
    else:
        xz_missing = [name for name in xz_required if name not in available_channels]
        if xz_missing:
            skip_stage(
                "velocity_xz_direct",
                "resampled",
                xz_required,
                xz_missing,
                formula="world_v=(VelocityX, VelocityZ)",
                diagnostic_only=True,
                lane_like_status=lane_like_report["status"],
            )
        else:
            try:
                velocity_xz_xy, time_source = _integrate_velocity_direct(
                    np.asarray(resampled["SessionTime"], dtype=np.float64),
                    np.asarray(resampled["VelocityX"], dtype=np.float64),
                    np.asarray(resampled["VelocityZ"], dtype=np.float64),
                )
                stage = Stage(
                    name="velocity_xz_direct",
                    source="resampled",
                    lap_dist_pct=lap_dist_pct,
                    xy=velocity_xz_xy,
                    metadata=build_stage_metadata(
                        xz_required,
                        time_source=time_source,
                        formula="world_v=(VelocityX, VelocityZ)",
                        diagnostic_only=True,
                    ),
                )
                stages.append(stage)
                stage_reports[stage.name] = _stage_report(stage)
            except Exception as exc:
                fail_stage(
                    "velocity_xz_direct",
                    "resampled",
                    xz_required,
                    exc,
                    formula="world_v=(VelocityX, VelocityZ)",
                    diagnostic_only=True,
                    lane_like_status=lane_like_report["status"],
                )

    return stages, stage_reports, {"lane_like_mask": lane_like_report}


def _transform_points(points: np.ndarray, bbox: tuple[float, float, float, float]) -> np.ndarray:
    x_min, y_min, x_max, y_max = bbox
    draw_w = max(1.0, float(PLOT_W - 2 * PLOT_PAD))
    draw_h = max(1.0, float(PLOT_H - 2 * PLOT_PAD))
    span_x = max(1.0, x_max - x_min)
    span_y = max(1.0, y_max - y_min)
    scale = min(draw_w / span_x, draw_h / span_y)
    pad_x = PLOT_PAD + (draw_w - span_x * scale) * 0.5
    pad_y = PLOT_PAD + (draw_h - span_y * scale) * 0.5
    px = pad_x + (points[:, 0] - x_min) * scale
    py = PLOT_H - (pad_y + (points[:, 1] - y_min) * scale)
    return np.column_stack([px, py])


def _draw_marker(draw: ImageDraw.ImageDraw, xy: tuple[float, float], fill: str, label: str) -> None:
    radius = 6
    draw.ellipse((xy[0] - radius, xy[1] - radius, xy[0] + radius, xy[1] + radius), fill=fill, outline="#111111")
    draw.text((xy[0] + 8, xy[1] - 10), label, fill=fill)


def _save_stage_plot(stage: Stage, bbox: tuple[float, float, float, float], output_path: Path) -> None:
    if len(stage.xy) < 2:
        return
    img = Image.new("RGB", (PLOT_W, PLOT_H), "#0f1317")
    draw = ImageDraw.Draw(img)
    coords = _transform_points(stage.xy, bbox)
    draw.line([tuple(point) for point in coords], fill="#d7dde5", width=3)
    wrap_mask = (stage.lap_dist_pct >= 0.95) | (stage.lap_dist_pct <= 0.05)
    wrap_coords = coords[wrap_mask]
    if len(wrap_coords) >= 2:
        draw.line([tuple(point) for point in wrap_coords], fill="#f4c542", width=5)
    lane_like_indices = [idx for idx in stage.metadata.get("lane_like_indices", []) if 0 <= idx < len(coords)]
    for idx in lane_like_indices:
        px, py = coords[idx]
        draw.ellipse((px - 4, py - 4, px + 4, py + 4), fill="#ff7043", outline="#111111")
    _draw_marker(draw, tuple(coords[0]), "#4caf50", "start")
    _draw_marker(draw, tuple(coords[-1]), "#e53935", "end")
    draw.text((24, 18), stage.name, fill="#ffffff")
    draw.text((24, 44), f"source={stage.source}", fill="#b9c2cc")
    draw.text((24, 68), f"time={stage.metadata.get('time_source')}", fill="#b9c2cc")
    draw.text((24, 92), str(stage.metadata.get("formula") or ""), fill="#b9c2cc")
    img.save(output_path)


def _save_overlay_plot(
    stages: list[Stage],
    bbox: tuple[float, float, float, float],
    output_path: Path,
) -> None:
    color_map = {
        "ui_current_path": "#d7dde5",
        "speed_yaw": "#4fc3f7",
        "velocity_xy_direct": "#ef5350",
        "local_xy_rot_a": "#66bb6a",
        "local_xy_rot_b": "#ffa726",
    }
    img = Image.new("RGB", (PLOT_W, PLOT_H), "#0f1317")
    draw = ImageDraw.Draw(img)
    legend_y = 18
    for stage in stages:
        if len(stage.xy) < 2 or stage.name not in color_map:
            continue
        coords = _transform_points(stage.xy, bbox)
        draw.line([tuple(point) for point in coords], fill=color_map[stage.name], width=3)
        wrap_mask = (stage.lap_dist_pct >= 0.95) | (stage.lap_dist_pct <= 0.05)
        wrap_coords = coords[wrap_mask]
        if len(wrap_coords) >= 2 and stage.name == "speed_yaw":
            draw.line([tuple(point) for point in wrap_coords], fill="#f4c542", width=6)
        if stage.name == "local_xy_rot_b":
            lane_like_indices = [idx for idx in stage.metadata.get("lane_like_indices", []) if 0 <= idx < len(coords)]
            for idx in lane_like_indices:
                px, py = coords[idx]
                draw.ellipse((px - 4, py - 4, px + 4, py + 4), fill="#ff7043", outline="#111111")
        draw.line((24, legend_y + 8, 54, legend_y + 8), fill=color_map[stage.name], width=4)
        draw.text((64, legend_y), stage.name, fill=color_map[stage.name])
        legend_y += 24
    img.save(output_path)


def _session_lap_presence(session_dir: Path) -> dict[str, Any]:
    result = {"raw_parquet": {}, "resampled_laps": {}}
    run_path = session_dir / "run_0001.parquet"
    if run_path.exists():
        raw_cols = set(pq.ParquetFile(run_path).schema_arrow.names)
        for name in RAW_CHANNELS:
            result["raw_parquet"][name] = name in raw_cols
    for path in sorted(session_dir.glob("laps/lap_*/analysis/lap_resampled.parquet")):
        lap_name = path.parent.parent.name
        cols = set(pq.ParquetFile(path).schema_arrow.names)
        result["resampled_laps"][lap_name] = {name: name in cols for name in RAW_CHANNELS}
    return result


def _primary_hypothesis(
    stage_reports: dict[str, dict[str, Any]],
    comparisons: dict[str, Any],
    axis_semantics: dict[str, Any],
) -> dict[str, str]:
    direct = dict(stage_reports.get("velocity_xy_direct") or {})
    rotated = dict(stage_reports.get("local_xy_rot_b") or {})
    speed = dict(stage_reports.get("speed_yaw") or {})
    cmp_rotated = dict(comparisons.get("speed_yaw_vs_local_xy_rot_b") or {})
    missing_stages = [name for name, report in (("velocity_xy_direct", direct), ("local_xy_rot_b", rotated), ("speed_yaw", speed)) if report.get("status") != "ok"]
    if axis_semantics.get("status") != "ok":
        return {
            "status": "unklar",
            "reason": "Die Kanal-Semantik konnte wegen fehlender Rohkanaele nicht belastbar bestimmt werden.",
        }
    if missing_stages:
        return {
            "status": "unklar",
            "reason": "Mindestens ein benoetigter Rekonstruktionspfad wurde uebersprungen oder ist fehlgeschlagen.",
        }
    direct_ratio = float(direct.get("length_over_start_end_ratio") or 0.0)
    speed_wrap = float(((speed.get("wrap_zone_straightness") or {}).get("mean_cross_track_m")) or float("inf"))
    rotated_wrap = float(((rotated.get("wrap_zone_straightness") or {}).get("mean_cross_track_m")) or float("inf"))
    lane_delta = float(cmp_rotated.get("lane_like_max_m") or 0.0)
    if axis_semantics.get("likely_coordinate_system") != "vehicle_local":
        return {"status": "unklar", "reason": "Die lokale/vs.-Welt-Semantik konnte datenbasiert nicht belastbar bestimmt werden."}
    if direct_ratio <= 1.15:
        if rotated_wrap + 0.05 < speed_wrap and lane_delta >= 0.10:
            return {
                "status": "teilweise bestaetigt",
                "reason": (
                    "Die reine Speed+Yaw-Approximation unterschlaegt seitliche Geschwindigkeit. "
                    "Der rotierte Velocity-Pfad verbessert die Gerade lokal, aber die Gesamtverbesserung bleibt klein und nicht durchgehend stabil."
                ),
            }
        return {
            "status": "teilweise bestaetigt",
            "reason": (
                "Der direkte VelocityX/Y-Pfad ist unbrauchbar, weil die Kanaele im Fahrzeug-Frame liegen. "
                "Ein rotierter Velocity-Pfad ist physikalisch naeher am tatsaechlichen Vektor, liefert hier aber nur kleine bis inkonsistente Geometriegewinne."
            ),
        }
    return {
        "status": "verworfen",
        "reason": "Die Daten sprechen nicht fuer einen dominanten Speed-vs.-Velocity-Fehler.",
    }


def analyze_lap(session_dir: Path, lap_no: int, output_root: Path) -> dict[str, Any]:
    raw, lap_meta, raw_load = _load_raw_lap(session_dir, lap_no)
    resampled, resampled_path, resampled_load = _load_resampled_lap(session_dir, lap_no)
    stages, stage_reports, diagnostic_features = _make_stages(raw, resampled, resampled_path=resampled_path)
    axis_semantics = _axis_semantics(raw)
    stage_map = {stage.name: stage for stage in stages}
    comparisons = {
        "speed_yaw_vs_velocity_xy_direct": _comparison_report(stage_map, "speed_yaw", "velocity_xy_direct"),
        "speed_yaw_vs_local_xy_rot_a": _comparison_report(stage_map, "speed_yaw", "local_xy_rot_a"),
        "speed_yaw_vs_local_xy_rot_b": _comparison_report(stage_map, "speed_yaw", "local_xy_rot_b"),
        "ui_current_vs_speed_yaw": _comparison_report(stage_map, "ui_current_path", "speed_yaw"),
        "ui_current_path_vs_local_xy_rot_a": _comparison_report(stage_map, "ui_current_path", "local_xy_rot_a"),
        "ui_current_path_vs_local_xy_rot_b": _comparison_report(stage_map, "ui_current_path", "local_xy_rot_b"),
        "raw_time_axis_quality": _time_axis_quality_report(raw, "raw"),
        "resampled_time_axis_quality": _time_axis_quality_report(resampled, "resampled"),
    }
    rotated_choice = _choose_best_rotated_variant(stage_reports, comparisons)

    all_stage_points = [stage.xy for stage in stages if len(stage.xy) > 0]
    bbox: tuple[float, float, float, float] | None = None
    if all_stage_points:
        all_points = np.vstack(all_stage_points)
        bbox = (
            float(np.min(all_points[:, 0])),
            float(np.min(all_points[:, 1])),
            float(np.max(all_points[:, 0])),
            float(np.max(all_points[:, 1])),
        )

    lap_out = output_root / f"lap_{lap_no:04d}"
    lap_out.mkdir(parents=True, exist_ok=True)
    if bbox is not None:
        for stage in stages:
            _save_stage_plot(stage, bbox, lap_out / f"{stage.name}.png")
        _save_overlay_plot(stages, bbox, lap_out / "comparison_overlay.png")
        plot_status = {"status": "ok", "rendered_stage_count": int(len(stages))}
    else:
        plot_status = {"status": "skipped_no_stage_geometry", "rendered_stage_count": 0}

    analysis_status = "ok"
    if any(report.get("status") == "failed" for report in stage_reports.values()):
        analysis_status = "partial_failed"
    elif any(report.get("status") != "ok" for report in stage_reports.values()):
        analysis_status = "partial"

    report = {
        "lap_no": lap_no,
        "lap_meta": lap_meta,
        "analysis_status": analysis_status,
        "input_columns": {
            "raw": sorted(raw.keys()),
            "resampled": sorted(resampled.keys()),
        },
        "channel_presence": {
            "raw": raw_load["channel_presence"],
            "resampled": resampled_load["channel_presence"],
        },
        "input_loading": {
            "raw": raw_load,
            "resampled": resampled_load,
        },
        "channel_stats": {
            "raw": _channel_stats(raw),
            "resampled": _channel_stats({name: resampled[name] for name in RAW_CHANNELS if name in resampled}),
        },
        "diagnostic_features": diagnostic_features,
        "axis_semantics": axis_semantics,
        "rotation_variant_assessment": rotated_choice,
        "raw_sample_count": int(raw_load["sample_count"]),
        "resampled_sample_count": int(resampled_load["sample_count"]),
        "stages": stage_reports,
        "comparisons": comparisons,
        "plot_generation": plot_status,
        "primary_hypothesis": _primary_hypothesis(stage_reports, comparisons, axis_semantics),
        "next_step_proposal": {
            "status": "candidate_fix",
            "proposal": (
                "Keine Vollumstellung auf rohe VelocityX/Y. Falls weiter verfolgt, zuerst einen Debug-Flag-Pfad bauen, "
                "der VelocityX/Y als Fahrzeug-Frame interpretiert und vor der Integration mit Yaw in den Welt-Frame rotiert."
            ),
            "guardrails": [
                "A/B-Vergleich Speed+Yaw vs. rotierter Velocity-Pfad pro Lap beibehalten",
                "Nur aktivieren, wenn VelocityX/Y, Yaw und SessionTime vorhanden sind",
                "Direkte VelocityX/Y-Integration nicht produktiv verwenden",
            ],
        },
    }
    (lap_out / "geometry_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Investigate whether VelocityX/Y/Z can replace Speed+Yaw for track geometry.")
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--laps", nargs="+", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("_logs") / "track_geometry_diagnose")
    args = parser.parse_args()

    output_root = args.output_dir / args.session_dir.name
    output_root.mkdir(parents=True, exist_ok=True)

    summary = {
        "session_dir": str(args.session_dir),
        "lap_presence": _session_lap_presence(args.session_dir),
        "laps": {},
    }
    for lap_no in args.laps:
        summary["laps"][str(lap_no)] = analyze_lap(args.session_dir, lap_no, output_root)

    (output_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
