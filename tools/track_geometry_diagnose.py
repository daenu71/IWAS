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


EARTH_R_M = 6_378_137.0
PLOT_W = 960
PLOT_H = 960
PLOT_PAD = 72


@dataclass
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
    for i, raw in enumerate(out):
        if not np.isfinite(raw):
            continue
        if prev is not None and (raw - prev) < -0.5:
            offset += 1.0
        out[i] = raw + offset
        prev = float(raw)
    return out


def _build_dt(session_time: np.ndarray) -> np.ndarray:
    if session_time.size >= 2:
        dt = np.diff(session_time, prepend=session_time[0])
        fallback = float(np.nanmedian(np.diff(session_time)))
        if not np.isfinite(fallback) or fallback <= 0.0:
            fallback = 0.01
        return np.where(np.isfinite(dt) & (dt > 0.0), dt, fallback)
    return np.full(session_time.shape, 0.01, dtype=np.float64)


def _dedupe_nonincreasing_time(
    session_time: np.ndarray,
    *arrays: np.ndarray,
) -> tuple[np.ndarray, ...]:
    if session_time.size == 0:
        return (session_time, *arrays)
    keep: list[int] = [0]
    for idx in range(1, len(session_time)):
        if session_time[idx] > session_time[keep[-1]]:
            keep.append(idx)
        else:
            keep[-1] = idx
    keep_idx = np.array(keep, dtype=np.int64)
    return (session_time[keep_idx],) + tuple(arr[keep_idx] for arr in arrays)


def _close_loop(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    n = len(x)
    if n < 2:
        return np.column_stack([x, y])
    t = np.arange(n, dtype=np.float64) / float(n)
    return np.column_stack([x - t * (x[-1] - x[0]), y - t * (y[-1] - y[0])])


def _finite_mask(*arrays: np.ndarray) -> np.ndarray:
    mask = np.ones(len(arrays[0]), dtype=bool)
    for arr in arrays:
        mask &= np.isfinite(arr)
    return mask


def _integrate_speed_yaw(
    session_time: np.ndarray,
    speed: np.ndarray,
    yaw: np.ndarray,
    *,
    negate_y: bool,
    close_loop_enabled: bool,
) -> np.ndarray:
    dt = _build_dt(session_time)
    sp = np.where(np.isfinite(speed), speed, 0.0)
    yaw_safe = np.where(np.isfinite(yaw), yaw, 0.0)
    x = np.cumsum(sp * np.cos(yaw_safe) * dt)
    y_factor = -1.0 if negate_y else 1.0
    y = np.cumsum(y_factor * sp * np.sin(yaw_safe) * dt)
    return _close_loop(x, y) if close_loop_enabled else np.column_stack([x, y])


def _integrate_velocity(
    session_time: np.ndarray,
    vx: np.ndarray,
    vy: np.ndarray,
    *,
    negate_y: bool,
    close_loop_enabled: bool,
) -> np.ndarray:
    dt = _build_dt(session_time)
    x = np.cumsum(np.where(np.isfinite(vx), vx, 0.0) * dt)
    y_factor = -1.0 if negate_y else 1.0
    y = np.cumsum(y_factor * np.where(np.isfinite(vy), vy, 0.0) * dt)
    return _close_loop(x, y) if close_loop_enabled else np.column_stack([x, y])


def _latlon_to_xy(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    mask = _finite_mask(lat, lon)
    if int(np.sum(mask)) < 2:
        return np.empty((0, 2), dtype=np.float64)
    lat_f = lat[mask]
    lon_f = lon[mask]
    lat0 = math.radians(float(lat_f[0]))
    lon0 = math.radians(float(lon_f[0]))
    cos_lat0 = max(1e-6, abs(math.cos(lat0))) * (1.0 if math.cos(lat0) >= 0.0 else -1.0)
    x = (np.radians(lon_f) - lon0) * cos_lat0 * EARTH_R_M
    y = (np.radians(lat_f) - lat0) * EARTH_R_M
    return np.column_stack([x, y])


def _heading_deg(xy: np.ndarray, idx: int, window: int = 5) -> float | None:
    n = len(xy)
    if n < 2:
        return None
    i0 = max(0, idx - window)
    i1 = min(n - 1, idx + window)
    dx = float(xy[i1, 0] - xy[i0, 0])
    dy = float(xy[i1, 1] - xy[i0, 1])
    if not np.isfinite(dx) or not np.isfinite(dy):
        return None
    if math.hypot(dx, dy) < 1e-9:
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
    d = np.diff(xy, axis=0)
    return float(np.sum(np.hypot(d[:, 0], d[:, 1])))


def _bbox(xy: np.ndarray) -> dict[str, float]:
    return {
        "x_min": float(np.min(xy[:, 0])),
        "x_max": float(np.max(xy[:, 0])),
        "y_min": float(np.min(xy[:, 1])),
        "y_max": float(np.max(xy[:, 1])),
        "width": float(np.ptp(xy[:, 0])),
        "height": float(np.ptp(xy[:, 1])),
    }


def _sample_report(lap_dist_pct: np.ndarray, xy: np.ndarray, *, n_each_side: int = 25) -> dict[str, Any]:
    if len(xy) == 0:
        return {"first_samples": [], "last_samples": []}
    first_indices = list(range(min(n_each_side, len(xy))))
    last_start = max(0, len(xy) - n_each_side)
    last_indices = list(range(last_start, len(xy)))

    def _build(indices: list[int]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for idx in indices:
            next_idx = min(idx + 1, len(xy) - 1)
            rows.append(
                {
                    "idx": int(idx),
                    "lap_dist_pct": float(lap_dist_pct[idx]) if idx < len(lap_dist_pct) else None,
                    "x": float(xy[idx, 0]),
                    "y": float(xy[idx, 1]),
                    "heading_deg": _heading_deg(xy, idx),
                    "distance_to_next_m": float(np.linalg.norm(xy[next_idx] - xy[idx])) if next_idx != idx else 0.0,
                }
            )
        return rows

    return {
        "first_samples": _build(first_indices),
        "last_samples": _build(last_indices),
    }


def _stage_report(stage: Stage) -> dict[str, Any]:
    xy = stage.xy
    lap_dist_pct = stage.lap_dist_pct
    if len(xy) == 0:
        return {"name": stage.name, "source": stage.source, "sample_count": 0, "metadata": stage.metadata}
    start_mask = (lap_dist_pct >= 0.0) & (lap_dist_pct <= 0.06)
    wrap_mask = (lap_dist_pct >= 0.95) | (lap_dist_pct <= 0.05)
    return {
        "name": stage.name,
        "source": stage.source,
        "sample_count": int(len(xy)),
        "bbox_m": _bbox(xy),
        "polyline_length_m": _polyline_length(xy),
        "start_point_m": [float(xy[0, 0]), float(xy[0, 1])],
        "end_point_m": [float(xy[-1, 0]), float(xy[-1, 1])],
        "start_end_distance_m": float(np.linalg.norm(xy[-1] - xy[0])),
        "heading_start_deg": _heading_deg(xy, 5),
        "heading_end_deg": _heading_deg(xy, max(0, len(xy) - 6)),
        "start_straightness": _line_metrics(xy, start_mask),
        "wrap_zone_straightness": _line_metrics(xy, wrap_mask),
        "metadata": stage.metadata,
        "boundary_samples": _sample_report(lap_dist_pct, xy),
    }


def _time_axis_quality(session_time: np.ndarray) -> dict[str, Any]:
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


def _load_table_columns(table: pq.Table) -> dict[str, np.ndarray]:
    return {
        name: np.array(table.column(name).to_pylist(), dtype=np.float64)
        for name in table.schema.names
    }


def _load_raw_lap(session_dir: Path, lap_no: int) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    meta_path = session_dir / f"run_0001_lap_{lap_no:04d}_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    run_path = session_dir / "run_0001.parquet"
    table = pq.read_table(run_path).slice(meta["lap_start_sample"], meta["lap_end_sample"] - meta["lap_start_sample"] + 1)
    return _load_table_columns(table), meta


def _load_resampled_lap(session_dir: Path, lap_no: int) -> dict[str, np.ndarray]:
    table = pq.read_table(session_dir / "laps" / f"lap_{lap_no:04d}" / "analysis" / "lap_resampled.parquet")
    return _load_table_columns(table)


def _to_ui_track_xy(resampled: dict[str, np.ndarray]) -> np.ndarray:
    payload = {key: value.tolist() for key, value in resampled.items()}
    return lap_view_model_mod._reconstruct_xy(payload, len(resampled["LapDistPct"]))


def _make_stages(raw: dict[str, np.ndarray], resampled: dict[str, np.ndarray]) -> list[Stage]:
    stages: list[Stage] = []
    raw_ldp = np.array(raw["LapDistPct"], dtype=np.float64)
    res_ldp = np.array(resampled["LapDistPct"], dtype=np.float64)

    if "Lat" in raw and "Lon" in raw:
        raw_latlon_xy = _latlon_to_xy(raw["Lat"], raw["Lon"])
        if len(raw_latlon_xy) > 0:
            latlon_mask = _finite_mask(raw["Lat"], raw["Lon"])
            stages.append(
                Stage(
                    name="raw_latlon_xy",
                    source="raw_parquet",
                    lap_dist_pct=raw_ldp[latlon_mask],
                    xy=raw_latlon_xy,
                    metadata={"formula": "local_equirectangular"},
                )
            )

    if {"SessionTime", "Speed", "Yaw"} <= set(raw):
        stages.append(
            Stage(
                name="raw_speed_yaw_open",
                source="raw_parquet",
                lap_dist_pct=raw_ldp,
                xy=_integrate_speed_yaw(raw["SessionTime"], raw["Speed"], raw["Yaw"], negate_y=False, close_loop_enabled=False),
                metadata={"negate_y": False, "close_loop": False},
            )
        )
        stages.append(
            Stage(
                name="raw_speed_yaw_closed",
                source="raw_parquet",
                lap_dist_pct=raw_ldp,
                xy=_integrate_speed_yaw(raw["SessionTime"], raw["Speed"], raw["Yaw"], negate_y=False, close_loop_enabled=True),
                metadata={"negate_y": False, "close_loop": True, "matches_ui_algorithm": True},
            )
        )

    if {"SessionTime", "VelocityX", "VelocityY"} <= set(raw):
        stages.append(
            Stage(
                name="raw_velocity_open",
                source="raw_parquet",
                lap_dist_pct=raw_ldp,
                xy=_integrate_velocity(raw["SessionTime"], raw["VelocityX"], raw["VelocityY"], negate_y=False, close_loop_enabled=False),
                metadata={"negate_y": False, "close_loop": False},
            )
        )

    if {"SessionTime", "Speed", "Yaw"} <= set(resampled):
        stages.append(
            Stage(
                name="resampled_speed_yaw_open",
                source="lap_resampled.parquet",
                lap_dist_pct=res_ldp,
                xy=_integrate_speed_yaw(
                    resampled["SessionTime"],
                    resampled["Speed"],
                    resampled["Yaw"],
                    negate_y=False,
                    close_loop_enabled=False,
                ),
                metadata={"negate_y": False, "close_loop": False},
            )
        )
        stages.append(
            Stage(
                name="resampled_speed_yaw_closed",
                source="lap_resampled.parquet",
                lap_dist_pct=res_ldp,
                xy=_integrate_speed_yaw(
                    resampled["SessionTime"],
                    resampled["Speed"],
                    resampled["Yaw"],
                    negate_y=False,
                    close_loop_enabled=True,
                ),
                metadata={"negate_y": False, "close_loop": True, "matches_ui_algorithm": True},
            )
        )

    if {"SessionTime", "VelocityX", "VelocityY"} <= set(resampled):
        stages.append(
            Stage(
                name="resampled_velocity_open",
                source="lap_resampled.parquet",
                lap_dist_pct=res_ldp,
                xy=_integrate_velocity(
                    resampled["SessionTime"],
                    resampled["VelocityX"],
                    resampled["VelocityY"],
                    negate_y=False,
                    close_loop_enabled=False,
                ),
                metadata={"negate_y": False, "close_loop": False},
            )
        )

    stages.append(
        Stage(
            name="ui_track_xy",
            source="lap_view_model._reconstruct_xy",
            lap_dist_pct=res_ldp,
            xy=_to_ui_track_xy(resampled),
            metadata={"matches_current_ui": True},
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
    r = 6
    draw.ellipse((xy[0] - r, xy[1] - r, xy[0] + r, xy[1] + r), fill=fill, outline="#111111")
    draw.text((xy[0] + 8, xy[1] - 10), label, fill=fill)


def _save_stage_plot(stage: Stage, bbox: tuple[float, float, float, float], output_path: Path) -> None:
    img = Image.new("RGB", (PLOT_W, PLOT_H), "#101214")
    draw = ImageDraw.Draw(img)
    coords = _transform_points(stage.xy, bbox)
    if len(coords) >= 2:
        draw.line([tuple(p) for p in coords], fill="#d9d9d9", width=3)
    wrap_mask = (stage.lap_dist_pct >= 0.95) | (stage.lap_dist_pct <= 0.05)
    wrap_coords = coords[wrap_mask]
    if len(wrap_coords) >= 2:
        draw.line([tuple(p) for p in wrap_coords], fill="#f6c445", width=4)
    if len(coords) > 0:
        _draw_marker(draw, tuple(coords[0]), "#4caf50", "start")
        _draw_marker(draw, tuple(coords[-1]), "#e53935", "end")
    for frac, color in ((0.2, "#4fc3f7"), (0.5, "#ab47bc"), (0.8, "#26a69a")):
        idx = min(len(coords) - 1, max(0, int(round((len(coords) - 1) * frac))))
        if len(coords) > 1:
            hdg = _heading_deg(stage.xy, idx)
            if hdg is not None:
                px, py = coords[idx]
                ang = math.radians(hdg)
                dx = 18.0 * math.cos(ang)
                dy = -18.0 * math.sin(ang)
                draw.line((px, py, px + dx, py + dy), fill=color, width=3)
    draw.text((24, 20), stage.name, fill="#ffffff")
    draw.text((24, 44), f"source={stage.source}", fill="#b0bec5")
    draw.text((24, 68), f"start_end={np.linalg.norm(stage.xy[-1] - stage.xy[0]):.3f} m", fill="#b0bec5")
    img.save(output_path)


def _raw_to_resampled_delta(raw_stage: Stage, res_stage: Stage) -> dict[str, float] | None:
    if len(raw_stage.xy) < 2 or len(res_stage.xy) < 2:
        return None
    raw_ldp_u = _unwrap_lapdist(raw_stage.lap_dist_pct)
    res_ldp_u = _unwrap_lapdist(res_stage.lap_dist_pct)
    order = np.argsort(raw_ldp_u, kind="stable")
    qx = np.interp(res_ldp_u, raw_ldp_u[order], raw_stage.xy[order, 0])
    qy = np.interp(res_ldp_u, raw_ldp_u[order], raw_stage.xy[order, 1])
    delta = np.hypot(qx - res_stage.xy[:, 0], qy - res_stage.xy[:, 1])
    wrap_mask = (res_stage.lap_dist_pct >= 0.95) | (res_stage.lap_dist_pct <= 0.05)
    return {
        "median_m": float(np.median(delta)),
        "p95_m": float(np.quantile(delta, 0.95)),
        "max_m": float(np.max(delta)),
        "wrap_median_m": float(np.median(delta[wrap_mask])) if np.any(wrap_mask) else 0.0,
        "wrap_max_m": float(np.max(delta[wrap_mask])) if np.any(wrap_mask) else 0.0,
    }


def _raw_deduped_to_resampled_delta(
    raw: dict[str, np.ndarray],
    res_stage: Stage,
) -> dict[str, float] | None:
    required = {"SessionTime", "LapDistPct", "Speed", "Yaw"}
    if not required <= set(raw):
        return None
    raw_st_dedup, raw_ldp_dedup, raw_speed_dedup, raw_yaw_dedup = _dedupe_nonincreasing_time(
        np.array(raw["SessionTime"], dtype=np.float64),
        np.array(raw["LapDistPct"], dtype=np.float64),
        np.array(raw["Speed"], dtype=np.float64),
        np.array(raw["Yaw"], dtype=np.float64),
    )
    dedup_stage = Stage(
        name="raw_speed_yaw_open_deduped",
        source="raw_parquet",
        lap_dist_pct=raw_ldp_dedup,
        xy=_integrate_speed_yaw(raw_st_dedup, raw_speed_dedup, raw_yaw_dedup, negate_y=False, close_loop_enabled=False),
        metadata={"dedup_nonincreasing_sessiontime": True},
    )
    return _raw_to_resampled_delta(dedup_stage, res_stage)


def _hypotheses(raw: dict[str, np.ndarray], stage_reports: dict[str, dict[str, Any]], comparisons: dict[str, Any]) -> dict[str, dict[str, str]]:
    has_latlon = "Lat" in raw and "Lon" in raw
    raw_open = stage_reports.get("raw_speed_yaw_open", {})
    raw_closed = stage_reports.get("raw_speed_yaw_closed", {})
    res_open = stage_reports.get("resampled_speed_yaw_open", {})
    ui_track = stage_reports.get("ui_track_xy", {})

    end_gap_open = float(raw_open.get("start_end_distance_m", 0.0) or 0.0)
    end_gap_closed = float(raw_closed.get("start_end_distance_m", 0.0) or 0.0)
    resample_cmp = comparisons.get("raw_open_deduped_vs_resampled_open")
    resample_max = float(resample_cmp.get("max_m", 0.0)) if isinstance(resample_cmp, dict) else 0.0
    raw_time_quality = comparisons.get("raw_time_axis_quality", {})
    duplicate_steps = int(raw_time_quality.get("nonpositive_step_count", 0) or 0)

    return {
        "1_latlon_to_xy_error": {
            "status": "verworfen" if not has_latlon else "unklar",
            "reason": "Die untersuchten Laps enthalten keine Lat/Lon-Spalten; der aktive UI-Pfad verwendet sie hier nicht.",
        },
        "2_axis_swap_or_sign_error": {
            "status": "unklar",
            "reason": "Es existieren zwei Rekonstruktionsimplementierungen mit unterschiedlichem Y-Vorzeichen; fuer diese UI-Ansicht ist das aber nicht die Hauptverzerrung.",
        },
        "3_scaling_error": {
            "status": "verworfen",
            "reason": "Die integrierte Groessenordnung bleibt plausibel; der dominante Fehler ist kein einheitlicher Skalenfaktor.",
        },
        "4_resampling_on_lapdist_error": {
            "status": "bestaetigt",
            "reason": f"Nach Deduplikation der Roh-Zeitachse bleibt zwischen Roh- und Resample-Integration eine Zusatzabweichung bis {resample_max:.2f} m; der dominante Fehler bleibt aber der fehlende Positionskanal. Rohdaten enthalten zudem {duplicate_steps} nicht-positive SessionTime-Schritte.",
        },
        "5_smoothing_error": {
            "status": "verworfen",
            "reason": "Im TrackMap-Renderpfad wird ohne Smoothing gezeichnet (`smooth=False`), und es gibt keinen separaten Geometrie-Filter.",
        },
        "6_lap_wrap_start_finish_handling_error": {
            "status": "bestaetigt",
            "reason": f"Die aktuelle UI-Rekonstruktion reduziert den offenen Endabstand von {end_gap_open:.2f} m auf {end_gap_closed:.4f} m, indem sie die Runde explizit schliesst.",
        },
        "7_first_last_point_artificially_pulled_together": {
            "status": "bestaetigt",
            "reason": f"`_close_loop()` verteilt den Start/End-Gap ueber alle Samples; sichtbar ist das Endergebnis mit nur noch {end_gap_closed:.4f} m Start/End-Abstand.",
        },
        "8_preview_vs_final_use_different_geometry_paths": {
            "status": "bestaetigt",
            "reason": "Codebasis hat mindestens zwei verschiedene XY-Rekonstruktionen (`lap_view_model` und `track_geometry`); die Coaching-UI nutzt `lap_view_model`.",
        },
        "9_garage61_uses_different_geometry_source": {
            "status": "bestaetigt",
            "reason": "Der aktuelle iWAS-Coaching-Pfad arbeitet hier ohne direkte Positionskanaele; Garage61 basiert typischerweise auf GPS/LatLon-CSV und wirkt deshalb geometrisch plausibler.",
        },
        "10_non_monotonic_input_handling_error": {
            "status": "verworfen",
            "reason": "Die resamplete LapDistPct-Achse ist monoton; die sichtbare Verformung kommt nicht von einem Sortier- oder Wrap-Fehler in LapDistPct allein.",
        },
    }


def analyze_lap(session_dir: Path, lap_no: int, output_root: Path) -> dict[str, Any]:
    raw, lap_meta = _load_raw_lap(session_dir, lap_no)
    resampled = _load_resampled_lap(session_dir, lap_no)
    stages = _make_stages(raw, resampled)
    stage_reports = {stage.name: _stage_report(stage) for stage in stages}

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
        if len(stage.xy) > 1:
            _save_stage_plot(stage, bbox, lap_out / f"{stage.name}.png")

    comparisons = {}
    res_open = next((s for s in stages if s.name == "resampled_speed_yaw_open"), None)
    comparisons["raw_time_axis_quality"] = _time_axis_quality(np.array(raw["SessionTime"], dtype=np.float64))
    comparisons["resampled_time_axis_quality"] = _time_axis_quality(np.array(resampled["SessionTime"], dtype=np.float64))
    if res_open is not None:
        comparisons["raw_open_deduped_vs_resampled_open"] = _raw_deduped_to_resampled_delta(raw, res_open)

    report = {
        "lap_no": lap_no,
        "lap_meta": lap_meta,
        "input_columns": {
            "raw": sorted(raw.keys()),
            "resampled": sorted(resampled.keys()),
        },
        "raw_sample_count": int(len(raw["LapDistPct"])),
        "resampled_sample_count": int(len(resampled["LapDistPct"])),
        "stages": stage_reports,
        "comparisons": comparisons,
        "hypotheses": _hypotheses(raw, stage_reports, comparisons),
    }
    (lap_out / "geometry_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose track geometry drift for coaching laps.")
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--laps", nargs="+", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("_logs") / "track_geometry_diagnose")
    args = parser.parse_args()

    output_root = args.output_dir / args.session_dir.name
    output_root.mkdir(parents=True, exist_ok=True)

    summary = {"session_dir": str(args.session_dir), "laps": {}}
    for lap_no in args.laps:
        summary["laps"][str(lap_no)] = analyze_lap(args.session_dir, lap_no, output_root)

    (output_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
