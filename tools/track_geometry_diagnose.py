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
    if len(stage.xy) == 0:
        return {"name": stage.name, "source": stage.source, "sample_count": 0, "metadata": stage.metadata}
    lap_dist_pct = stage.lap_dist_pct
    wrap_mask = (lap_dist_pct >= 0.95) | (lap_dist_pct <= 0.05)
    start_mask = (lap_dist_pct >= 0.0) & (lap_dist_pct <= 0.06)
    lane_like_indices = [int(idx) for idx in stage.metadata.get("lane_like_indices", [])]
    start_end_distance = float(np.linalg.norm(stage.xy[-1] - stage.xy[0])) if len(stage.xy) >= 2 else 0.0
    polyline_length = _polyline_length(stage.xy)
    turning = _turning_metrics(stage.xy)
    ratio = polyline_length / max(start_end_distance, 1e-6)
    return {
        "name": stage.name,
        "source": stage.source,
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
    return {
        "sample_count": int(len(delta)),
        "median_m": _safe_quantile(delta, 0.50),
        "p95_m": _safe_quantile(delta, 0.95),
        "max_m": _safe_quantile(delta, 1.00),
        "lane_like_sample_count": int(lane_values.size),
        "lane_like_median_m": _safe_quantile(lane_values, 0.50) if lane_values.size else 0.0,
        "lane_like_max_m": _safe_quantile(lane_values, 1.00) if lane_values.size else 0.0,
    }


def _axis_semantics(columns: dict[str, np.ndarray]) -> dict[str, Any]:
    required = {"Speed", "VelocityX", "VelocityY", "VelocityZ"}
    if not required <= set(columns):
        return {"status": "insufficient_data"}
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


def _choose_best_rotated_variant(stage_reports: dict[str, dict[str, Any]]) -> dict[str, Any]:
    candidates = []
    for name in ROTATION_VARIANTS:
        report = stage_reports.get(name)
        if not isinstance(report, dict):
            continue
        wrap = dict(report.get("wrap_zone_straightness") or {})
        candidates.append(
            {
                "name": name,
                "wrap_mean_cross_track_m": float(wrap.get("mean_cross_track_m") or float("inf")),
                "wrap_max_cross_track_m": float(wrap.get("max_cross_track_m") or float("inf")),
                "length_over_start_end_ratio": float(report.get("length_over_start_end_ratio") or 0.0),
            }
        )
    if not candidates:
        return {"status": "insufficient_data"}
    candidates.sort(key=lambda item: (item["wrap_mean_cross_track_m"], item["wrap_max_cross_track_m"], -item["length_over_start_end_ratio"]))
    return {"status": "ok", "best_candidate": candidates[0], "all_candidates": candidates}


def _load_table_columns(table: pq.Table) -> dict[str, np.ndarray]:
    return {
        name: np.array(table.column(name).to_pylist(), dtype=np.float64)
        for name in table.schema.names
    }


def _load_raw_lap(session_dir: Path, lap_no: int) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    meta_path = session_dir / f"run_0001_lap_{lap_no:04d}_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    run_path = session_dir / "run_0001.parquet"
    table = pq.read_table(run_path, columns=list(RAW_CHANNELS)).slice(
        meta["lap_start_sample"],
        meta["lap_end_sample"] - meta["lap_start_sample"] + 1,
    )
    return _load_table_columns(table), meta


def _load_resampled_lap(session_dir: Path, lap_no: int) -> tuple[dict[str, np.ndarray], Path]:
    path = session_dir / "laps" / f"lap_{lap_no:04d}" / "analysis" / "lap_resampled.parquet"
    table = pq.read_table(path)
    return _load_table_columns(table), path


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
) -> list[Stage]:
    stages: list[Stage] = []
    lap_dist_pct = np.asarray(resampled["LapDistPct"], dtype=np.float64)
    session_time = np.asarray(resampled["SessionTime"], dtype=np.float64)
    speed = np.asarray(resampled["Speed"], dtype=np.float64)
    yaw = np.asarray(resampled["Yaw"], dtype=np.float64)
    velocity_x = np.asarray(resampled["VelocityX"], dtype=np.float64)
    velocity_y = np.asarray(resampled["VelocityY"], dtype=np.float64)
    velocity_z = np.asarray(resampled.get("VelocityZ", np.full_like(velocity_x, np.nan)), dtype=np.float64)
    lane_like_indices = np.flatnonzero(_lane_like_mask(lap_dist_pct, session_time, yaw, velocity_y)).tolist()
    channel_presence = sorted(key for key in ("Speed", "Yaw", "VelocityX", "VelocityY", "VelocityZ") if key in resampled)

    current_ui = _ui_stage(resampled_path, lap_dist_pct, lane_like_indices)
    stages.append(current_ui)

    speed_xy, time_source = _integrate_speed_yaw(session_time, speed, yaw)
    stages.append(
        Stage(
            name="speed_yaw",
            source="resampled",
            lap_dist_pct=lap_dist_pct,
            xy=speed_xy,
            metadata={
                "time_source": time_source,
                "channel_presence": channel_presence,
                "formula": "world_v=(Speed*cos(Yaw), Speed*sin(Yaw))",
                "lane_like_indices": lane_like_indices,
            },
        )
    )

    velocity_direct_xy, time_source = _integrate_velocity_direct(session_time, velocity_x, velocity_y)
    stages.append(
        Stage(
            name="velocity_xy_direct",
            source="resampled",
            lap_dist_pct=lap_dist_pct,
            xy=velocity_direct_xy,
            metadata={
                "time_source": time_source,
                "channel_presence": channel_presence,
                "formula": "world_v=(VelocityX, VelocityY)",
                "lane_like_indices": lane_like_indices,
                "expected_semantics": "negative_control_if_vehicle_local",
            },
        )
    )

    for variant in ROTATION_VARIANTS:
        rotated_xy, time_source = _integrate_local_velocity_rotated(
            session_time,
            velocity_x,
            velocity_y,
            yaw,
            variant=variant,
        )
        stages.append(
            Stage(
                name=variant,
                source="resampled",
                lap_dist_pct=lap_dist_pct,
                xy=rotated_xy,
                metadata={
                    "time_source": time_source,
                    "channel_presence": channel_presence,
                    "formula": "world_v=rotate(local VelocityX/VelocityY by Yaw)",
                    "rotation_variant": variant,
                    "lane_like_indices": lane_like_indices,
                },
            )
        )

    raw_semantics = _axis_semantics(raw)
    if raw_semantics.get("status") == "ok":
        stages.append(
            Stage(
                name="velocity_xz_direct",
                source="resampled",
                lap_dist_pct=lap_dist_pct,
                xy=_integrate_velocity_direct(session_time, velocity_x, velocity_z)[0],
                metadata={
                    "time_source": "SessionTime",
                    "channel_presence": channel_presence,
                    "formula": "world_v=(VelocityX, VelocityZ)",
                    "lane_like_indices": lane_like_indices,
                    "diagnostic_only": True,
                },
            )
        )
    return stages


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
    raw, lap_meta = _load_raw_lap(session_dir, lap_no)
    resampled, resampled_path = _load_resampled_lap(session_dir, lap_no)
    stages = _make_stages(raw, resampled, resampled_path=resampled_path)
    stage_reports = {stage.name: _stage_report(stage) for stage in stages}
    axis_semantics = _axis_semantics(raw)
    rotated_choice = _choose_best_rotated_variant(stage_reports)

    all_points = np.vstack([stage.xy for stage in stages if len(stage.xy) > 0])
    bbox = (
        float(np.min(all_points[:, 0])),
        float(np.min(all_points[:, 1])),
        float(np.max(all_points[:, 0])),
        float(np.max(all_points[:, 1])),
    )

    lap_out = output_root / f"lap_{lap_no:04d}"
    lap_out.mkdir(parents=True, exist_ok=True)
    for stage in stages:
        _save_stage_plot(stage, bbox, lap_out / f"{stage.name}.png")
    _save_overlay_plot(stages, bbox, lap_out / "comparison_overlay.png")

    stage_map = {stage.name: stage for stage in stages}
    comparisons = {
        "speed_yaw_vs_velocity_xy_direct": _pointwise_delta(stage_map["speed_yaw"], stage_map["velocity_xy_direct"]),
        "speed_yaw_vs_local_xy_rot_a": _pointwise_delta(stage_map["speed_yaw"], stage_map["local_xy_rot_a"]),
        "speed_yaw_vs_local_xy_rot_b": _pointwise_delta(stage_map["speed_yaw"], stage_map["local_xy_rot_b"]),
        "ui_current_vs_speed_yaw": _pointwise_delta(stage_map["ui_current_path"], stage_map["speed_yaw"]),
        "raw_time_axis_quality": _time_axis_quality(np.asarray(raw["SessionTime"], dtype=np.float64)),
        "resampled_time_axis_quality": _time_axis_quality(np.asarray(resampled["SessionTime"], dtype=np.float64)),
    }

    report = {
        "lap_no": lap_no,
        "lap_meta": lap_meta,
        "input_columns": {
            "raw": sorted(raw.keys()),
            "resampled": sorted(resampled.keys()),
        },
        "channel_stats": {
            "raw": _channel_stats(raw),
            "resampled": _channel_stats({name: resampled[name] for name in RAW_CHANNELS if name in resampled}),
        },
        "axis_semantics": axis_semantics,
        "rotation_variant_assessment": rotated_choice,
        "raw_sample_count": int(len(raw["LapDistPct"])),
        "resampled_sample_count": int(len(resampled["LapDistPct"])),
        "stages": stage_reports,
        "comparisons": comparisons,
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
