"""Extract persistent track geometry from IBT telemetry or fallback parquet data."""

from __future__ import annotations

import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

_SRC_DIR = str(Path(__file__).resolve().parent.parent.parent)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

try:
    from .track_key import build_track_key
except ImportError:
    from core.coaching.track_key import build_track_key  # type: ignore[no-redef]


_EDGE_OFFSET_M: float = 5.0
_IBT_SAMPLE_DT: float = 1.0 / 60.0
_MAX_FRAMES: int = 200_000


@dataclass(frozen=True)
class TrackSessionMetadata:
    track_key: str
    track_display_name: str
    track_config_name: str
    track_name: str
    track_id: int | None
    iracing_build: str


def extract_track_geometry(ibt_path: str | Path, storage_root: str | Path) -> Path:
    """Extract track geometry from *ibt_path* and persist it below *storage_root*."""
    ibt_path = Path(ibt_path)
    storage_root = Path(storage_root)

    ir = _open_ibt(ibt_path)
    try:
        metadata = _read_track_metadata(ir)
        center_m = _extract_centerline_m(ir)
        left_m, right_m = _compute_edges_m(center_m, ir)
    finally:
        _close_ibt(ir)

    center_norm, left_norm, right_norm = _normalise_road_geometry(center_m, left_m, right_m)
    payload = _build_track_geometry_payload(
        track_key=metadata.track_key,
        center_line=center_norm,
        left_edge=left_norm,
        right_edge=right_norm,
        source_type="ibt",
        source_name="ibt_telemetry",
        source_path=str(ibt_path),
        iracing_build=metadata.iracing_build,
        track_display_name=metadata.track_display_name,
        track_config_name=metadata.track_config_name,
        track_name=metadata.track_name,
        track_id=metadata.track_id,
    )

    out_path = _output_path(storage_root, metadata.track_key)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out_path


def extract_track_geometry_from_parquet(
    parquet_path: str | Path,
    track_key: str,
    storage_root: str | Path,
) -> Path:
    """Extract fallback geometry from a recorded parquet file."""
    parquet_path = Path(parquet_path)
    storage_root = Path(storage_root)

    center_m = _integrate_velocity_from_parquet(parquet_path)
    if len(center_m) < 2:
        raise RuntimeError(f"No usable velocity data in Parquet: {parquet_path}")

    normals = _compute_normals(center_m)
    left_m = _close_edge_loop(center_m + normals * _EDGE_OFFSET_M)
    right_m = _close_edge_loop(center_m - normals * _EDGE_OFFSET_M)
    center_norm, left_norm, right_norm = _normalise_road_geometry(center_m, left_m, right_m)

    payload = _build_track_geometry_payload(
        track_key=track_key,
        center_line=center_norm,
        left_edge=left_norm,
        right_edge=right_norm,
        source_type="fallback",
        source_name="parquet_velocity_integration",
        source_path=str(parquet_path),
        iracing_build="",
        fallback_reason="dead_reckoning",
    )

    out_path = _output_path(storage_root, track_key)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out_path


def read_ibt_session_metadata(ibt_path: str | Path) -> TrackSessionMetadata:
    """Return track-identifying metadata for an IBT file."""
    ibt_path = Path(ibt_path)
    ir = _open_ibt(ibt_path)
    try:
        return _read_track_metadata(ir)
    finally:
        _close_ibt(ir)


def _build_track_geometry_payload(
    *,
    track_key: str,
    center_line: list[list[float]],
    left_edge: list[list[float]],
    right_edge: list[list[float]],
    source_type: str,
    source_name: str,
    source_path: str,
    iracing_build: str,
    track_display_name: str = "",
    track_config_name: str = "",
    track_name: str = "",
    track_id: int | None = None,
    fallback_reason: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "track_key": track_key,
        "source_type": source_type,
        "source": source_name,
        "source_name": source_name,
        "source_path": source_path,
        "iracing_build": iracing_build,
        "extracted_at": _utc_now_iso(),
        "center_line": center_line,
        "left_edge": left_edge,
        "right_edge": right_edge,
    }
    if track_display_name:
        payload["track_display_name"] = track_display_name
    if track_config_name:
        payload["track_config_name"] = track_config_name
    if track_name:
        payload["track_name"] = track_name
    if track_id is not None:
        payload["track_id"] = track_id
    if fallback_reason:
        payload["fallback_reason"] = fallback_reason
    return payload


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _open_ibt(ibt_path: Path) -> Any:
    import irsdk  # type: ignore[import]

    ir = irsdk.IRSDK()
    result = ir.startup(test_file=str(ibt_path))
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


def _read_track_metadata(ir: Any) -> TrackSessionMetadata:
    yaml_text = _get_session_yaml(ir)
    parsed = _parse_session_yaml(yaml_text)
    weekend_info = _read_weekend_info(ir)
    parsed_weekend = parsed.get("WeekendInfo")
    if isinstance(parsed_weekend, dict):
        merged_weekend = dict(parsed_weekend)
        merged_weekend.update(weekend_info)
        weekend_info = merged_weekend

    track_display_name = (
        _coerce_optional_str(weekend_info.get("TrackDisplayName"))
        or _read_scalar_from_yaml(yaml_text, "TrackDisplayName")
        or _coerce_optional_str(weekend_info.get("TrackName"))
        or _read_scalar_from_yaml(yaml_text, "TrackName")
        or "unknown"
    )
    track_config_name = (
        _coerce_optional_str(weekend_info.get("TrackConfigName"))
        or _read_scalar_from_yaml(yaml_text, "TrackConfigName")
        or ""
    )
    track_name = (
        _coerce_optional_str(weekend_info.get("TrackName"))
        or _read_scalar_from_yaml(yaml_text, "TrackName")
        or track_display_name
    )
    track_id = _coerce_optional_int(weekend_info.get("TrackID"))
    if track_id is None:
        track_id = _coerce_optional_int(_read_scalar_from_yaml(yaml_text, "TrackID"))

    iracing_build = (
        _coerce_optional_str(weekend_info.get("BuildVersion"))
        or _read_scalar_from_yaml(yaml_text, "BuildVersion")
        or _coerce_optional_str(weekend_info.get("SimMode"))
        or _read_scalar_from_yaml(yaml_text, "SimMode")
        or ""
    )
    return TrackSessionMetadata(
        track_key=build_track_key(track_display_name, track_config_name),
        track_display_name=track_display_name,
        track_config_name=track_config_name,
        track_name=track_name,
        track_id=track_id,
        iracing_build=iracing_build,
    )


def _read_track_key(ir: Any) -> str:
    return _read_track_metadata(ir).track_key


def _get_session_yaml(ir: Any) -> str:
    for attr in ("session_info", "sessionInfo", "session_info_yaml", "sessionInfoYaml"):
        value = getattr(ir, attr, None)
        if value is None:
            continue
        text = value() if callable(value) else value
        if isinstance(text, str) and text.strip():
            return text
    try:
        raw = ir["SessionInfo"]
        if isinstance(raw, str):
            return raw
    except Exception:
        pass
    return ""


def _parse_session_yaml(yaml_text: str) -> dict[str, Any]:
    if not yaml_text.strip():
        return {}
    try:
        import yaml  # type: ignore[import]

        parsed = yaml.safe_load(yaml_text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _read_scalar_from_yaml(yaml_text: str, field_name: str) -> str:
    match = re.search(rf"(?m)^\s*{re.escape(field_name)}\s*:\s*(.+?)\s*$", yaml_text)
    if not match:
        return ""
    value = match.group(1).strip()
    if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
        value = value[1:-1]
    return value.strip()


def _read_weekend_info(ir: Any) -> dict[str, Any]:
    try:
        weekend = ir["WeekendInfo"]
        if isinstance(weekend, dict):
            return dict(weekend)
    except Exception:
        pass
    return {}


def _read_iracing_build(ir: Any) -> str:
    return _read_track_metadata(ir).iracing_build


def _extract_centerline_m(ir: Any) -> np.ndarray:
    pts = _try_read_header_geometry(ir)
    if pts is not None and len(pts) >= 2:
        return pts
    return _integrate_velocity(ir)


def _try_read_header_geometry(ir: Any) -> np.ndarray | None:
    weekend = _read_weekend_info(ir)
    for key in ("TrackCenterLine", "CenterLine", "Geometry", "TrackGeometry"):
        pts = _coerce_xy_array(weekend.get(key))
        if pts is not None and len(pts) >= 2:
            return pts
    return None


def _coerce_xy_array(raw: Any) -> np.ndarray | None:
    try:
        arr = np.asarray(raw, dtype=np.float64)
    except Exception:
        return None
    if arr.ndim != 2 or arr.shape[1] != 2 or len(arr) < 2:
        return None
    return arr


def _integrate_velocity(ir: Any) -> np.ndarray:
    raw = _read_first_lap_frames(ir)
    if len(raw["vx"]) == 0:
        return np.empty((0, 2), dtype=np.float64)
    return _integrate_world_frame(raw)


def _integrate_world_frame(frames: dict[str, list[Any]]) -> np.ndarray:
    n = len(frames.get("t", []))
    if n == 0:
        return np.empty((0, 2), dtype=np.float64)

    t_arr = np.asarray(frames["t"], dtype=np.float64)
    if n > 1:
        median_dt = float(np.nanmedian(np.diff(t_arr)))
        if not math.isfinite(median_dt) or median_dt <= 0.0:
            median_dt = _IBT_SAMPLE_DT
    else:
        median_dt = _IBT_SAMPLE_DT
    dt = np.diff(t_arr, prepend=t_arr[0])
    dt = np.where(np.isfinite(dt) & (dt > 0.0), dt, median_dt)

    speed_list = frames.get("speed") or []
    yaw_list = frames.get("yaw") or []
    if len(speed_list) == n and len(yaw_list) == n:
        speed = np.asarray(speed_list, dtype=np.float64)
        yaw = np.asarray(yaw_list, dtype=np.float64)
        speed = np.where(np.isfinite(speed), speed, 0.0)
        yaw = np.where(np.isfinite(yaw), yaw, 0.0)
        x = np.cumsum(speed * np.cos(yaw) * dt)
        y = np.cumsum(speed * np.sin(yaw) * dt)
        if np.ptp(x) > 1.0 or np.ptp(y) > 1.0:
            return np.column_stack([x, y])

    vx = np.asarray(frames.get("vx", []), dtype=np.float64)
    vy = np.asarray(frames.get("vy", []), dtype=np.float64)
    vx = np.where(np.isfinite(vx), vx, 0.0)
    vy = np.where(np.isfinite(vy), vy, 0.0)
    return np.column_stack([np.cumsum(vx * dt), np.cumsum(vy * dt)])


def _read_first_lap_frames(ir: Any) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {
        "vx": [],
        "vy": [],
        "speed": [],
        "yaw": [],
        "t": [],
    }
    try:
        _fill_frames_via_parse_to(ir, result)
    except Exception:
        pass
    return result


def _fill_frames_via_parse_to(ir: Any, result: dict[str, list[Any]]) -> None:
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
        speed = _safe_float(ir["Speed"])
        yaw = _safe_float(ir["Yaw"])
        lap_dist_pct = _safe_float(ir["LapDistPct"])

        result["t"].append(t)
        result["vx"].append(vx if vx is not None else 0.0)
        result["vy"].append(vy if vy is not None else 0.0)
        result["speed"].append(speed if speed is not None else 0.0)
        result["yaw"].append(yaw if yaw is not None else 0.0)
        n += 1

        if lap_dist_pct is not None:
            if lap_dist_pct > 0.85:
                seen_high = True
            if seen_high and lap_dist_pct < 0.15:
                break

        try:
            ok = parse_to(t + _IBT_SAMPLE_DT)
        except Exception:
            break
        if not ok:
            break


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except Exception:
        return None
    return number if math.isfinite(number) else None


def _compute_edges_m(center_m: np.ndarray, ir: Any) -> tuple[np.ndarray, np.ndarray]:
    if len(center_m) < 2:
        empty = np.empty((0, 2), dtype=np.float64)
        return empty, empty

    normals = _compute_normals(center_m)
    offset = _read_track_half_width(ir)
    if offset is None:
        offset = _EDGE_OFFSET_M

    left_m = _close_edge_loop(center_m + normals * offset)
    right_m = _close_edge_loop(center_m - normals * offset)
    return left_m, right_m


def _read_track_half_width(ir: Any) -> float | None:
    weekend = _read_weekend_info(ir)
    for key in ("TrackWidth", "TrackWidthM", "TrackWidthMeters"):
        width = _safe_float(weekend.get(key))
        if width is not None and width > 0.5:
            return width / 2.0

    yaml_text = _get_session_yaml(ir)
    match = re.search(r"(?m)^\s*TrackWidth\s*:\s*([\d.]+)\s*$", yaml_text)
    if not match:
        return None
    try:
        width = float(match.group(1))
    except Exception:
        return None
    return width / 2.0 if width > 0.5 else None


def _close_edge_loop(pts: np.ndarray) -> np.ndarray:
    n = len(pts)
    if n < 2:
        return pts
    drift = pts[-1] - pts[0]
    correction = np.outer(np.linspace(0.0, 1.0, n), drift)
    return pts - correction


def _compute_normals(pts: np.ndarray, smooth_window: int = 21) -> np.ndarray:
    n = len(pts)
    tangents = np.zeros_like(pts)
    tangents[1:-1] = pts[2:] - pts[:-2]
    tangents[0] = pts[1] - pts[0]
    tangents[-1] = pts[-1] - pts[-2]

    if smooth_window > 1 and n > smooth_window:
        kernel = np.ones(smooth_window) / smooth_window
        tangents[:, 0] = np.convolve(tangents[:, 0], kernel, mode="same")
        tangents[:, 1] = np.convolve(tangents[:, 1], kernel, mode="same")

    lengths = np.hypot(tangents[:, 0], tangents[:, 1])
    lengths = np.where(lengths > 1e-9, lengths, 1.0)
    tangents /= lengths[:, np.newaxis]
    return np.column_stack([-tangents[:, 1], tangents[:, 0]])


def _normalise_road_geometry(
    center_m: np.ndarray,
    left_m: np.ndarray,
    right_m: np.ndarray,
) -> tuple[list[list[float]], list[list[float]], list[list[float]]]:
    if len(center_m) < 2:
        return [], [], []
    return center_m.tolist(), left_m.tolist(), right_m.tolist()


def _integrate_velocity_from_parquet(parquet_path: Path) -> np.ndarray:
    try:
        import pyarrow.parquet as pq

        wanted = ["VelocityX", "VelocityY", "Speed", "Yaw", "SessionTime", "LapDistPct", "Lap"]
        schema_names = pq.ParquetFile(str(parquet_path)).schema_arrow.names
        present = [column for column in wanted if column in schema_names]
        table = pq.read_table(parquet_path, columns=present)
    except Exception as exc:
        raise RuntimeError(f"Cannot read Parquet: {exc}") from exc

    def _column(name: str) -> list[Any]:
        return table.column(name).to_pylist() if name in present else []

    best_lap_frames = _select_best_lap_frames(
        vx_all=_column("VelocityX"),
        vy_all=_column("VelocityY"),
        speed_all=_column("Speed"),
        yaw_all=_column("Yaw"),
        t_all=_column("SessionTime"),
        ldp_all=_column("LapDistPct"),
        lap_all=_column("Lap"),
    )
    if len(best_lap_frames["t"]) < 10:
        return np.empty((0, 2), dtype=np.float64)
    return _integrate_world_frame(best_lap_frames)


def _select_best_lap_frames(
    vx_all: list[Any],
    vy_all: list[Any],
    speed_all: list[Any],
    yaw_all: list[Any],
    t_all: list[Any],
    ldp_all: list[Any],
    lap_all: list[Any],
) -> dict[str, list[float]]:
    from collections import defaultdict

    buckets: dict[int, dict[str, list[float]]] = defaultdict(
        lambda: {"vx": [], "vy": [], "speed": [], "yaw": [], "t": [], "ldp": []}
    )
    speed_iter = speed_all or [None] * len(vx_all)
    yaw_iter = yaw_all or [None] * len(vx_all)

    for vx, vy, speed, yaw, t, ldp, lap in zip(vx_all, vy_all, speed_iter, yaw_iter, t_all, ldp_all, lap_all):
        lap_no = _coerce_optional_int(lap)
        if lap_no is None or lap_no < 1:
            continue
        bucket = buckets[lap_no]
        bucket["vx"].append(float(vx) if vx is not None else 0.0)
        bucket["vy"].append(float(vy) if vy is not None else 0.0)
        bucket["speed"].append(float(speed) if speed is not None else 0.0)
        bucket["yaw"].append(float(yaw) if yaw is not None else 0.0)
        bucket["t"].append(float(t) if t is not None else 0.0)
        bucket["ldp"].append(float(ldp) if ldp is not None else 0.0)

    if not buckets:
        return {"vx": [], "vy": [], "speed": [], "yaw": [], "t": []}

    best_lap = max(buckets, key=lambda lap_no: len(buckets[lap_no]["vx"]))
    bucket = buckets[best_lap]
    return {
        "vx": bucket["vx"],
        "vy": bucket["vy"],
        "speed": bucket["speed"],
        "yaw": bucket["yaw"],
        "t": bucket["t"],
    }


def _coerce_optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _coerce_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _output_path(storage_root: Path, track_key: str) -> Path:
    return storage_root / "track_geometries" / track_key / "track_road_geometry.json"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Extract TrackRoadGeometry from an IBT file.")
    parser.add_argument("--ibt", required=True, help="Path to the IBT file")
    parser.add_argument("--storage", required=True, help="Storage root directory")
    args = parser.parse_args()

    written = extract_track_geometry(args.ibt, args.storage)
    print(f"Written: {written}")
