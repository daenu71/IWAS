"""Extract persistent track geometry from IBT telemetry or fallback parquet data."""

from __future__ import annotations

import json
import logging
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

try:
    from core.irsdk.channels import REQUESTED_CHANNEL_ALIASES
    from core.irsdk.sessioninfo_parser import extract_session_meta
except ImportError:
    from ..irsdk.channels import REQUESTED_CHANNEL_ALIASES  # type: ignore[no-redef]
    from ..irsdk.sessioninfo_parser import extract_session_meta  # type: ignore[no-redef]


_EDGE_OFFSET_M: float = 5.0
_IBT_SAMPLE_DT: float = 1.0 / 60.0
_MAX_FRAMES: int = 200_000
_REFERENCE_LAP_START_MAX_PCT: float = 0.02
_REFERENCE_LAP_END_MIN_PCT: float = 0.98
_REFERENCE_LAP_MIN_CLOSURE_GAP_M: float = 20.0
_REFERENCE_LAP_CLOSURE_MEDIAN_STEP_FACTOR: float = 3.0
_EXPORT_ORIENTATION_RULE: str = "start_finish_tangent"
_EXPORT_START_TANGENT_LOOKAHEAD_PCT: float = 0.01
_IBT_INVENTORY_CANDIDATES: tuple[str, ...] = (
    "Lat",
    "Lon",
    "Alt",
    "LapDistPct",
    "LapDist",
    "SessionTime",
    "Yaw",
    "Speed",
    "VelocityX",
    "VelocityY",
    "VelocityZ",
)
_IBT_GEO_REQUIRED_CHANNELS: tuple[str, ...] = ("Lat", "Lon", "LapDistPct")
_IBT_GEO_OPTIONAL_CHANNELS: tuple[str, ...] = ("SessionTime", "Alt")

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrackSessionMetadata:
    track_key: str
    track_display_name: str
    track_config_name: str
    track_name: str
    track_id: int | None
    iracing_build: str


@dataclass
class _GeoPipelineResult:
    status: str
    reason: str
    required_channels: list[str]
    missing_channels: list[str]
    similar_channels: dict[str, list[dict[str, Any]]]
    source_channels: dict[str, str]
    sample_count: int
    joint_valid_sample_count: int
    joint_in_range_sample_count: int
    joint_valid_share: float | None
    lap_dist_pct_in_range_share: float | None
    lat_range: float | None
    lon_range: float | None
    lap_dist_pct_min: float | None
    lap_dist_pct_max: float | None
    unique_lap_dist_pct_samples: int
    min_joint_samples_required: int | None
    min_unique_lap_dist_required: int | None
    enough_samples_for_centerline: bool
    pipeline_counts: dict[str, int]
    debug_valid_geo_samples_head: list[dict[str, float | None]]
    debug_valid_geo_samples_tail: list[dict[str, float | None]]
    center_line: list[dict[str, float]]
    selected_reference_lap: int | None
    selected_reference_min_lap_dist_pct: float | None
    selected_reference_max_lap_dist_pct: float | None
    selected_reference_point_count: int
    selected_reference_closure_gap_m: float | None
    selected_reference_is_valid: bool


@dataclass(frozen=True)
class _GeoLapSelection:
    lap_index: int
    sorted_samples: list[dict[str, float | None]]
    deduped_samples: list[dict[str, float | None]]
    min_lap_dist_pct: float
    max_lap_dist_pct: float
    point_count: int
    closure_gap_m: float | None
    coverage: float
    is_valid_reference_lap: bool


@dataclass(frozen=True)
class _OrientationNormalisationResult:
    center_line: list[dict[str, float]]
    rule: str
    transform: str
    applied: bool
    start_anchor_lap_dist_pct: float | None
    start_anchor_xy_before: tuple[float, float] | None
    start_tangent_before: tuple[float, float] | None
    rotation_deg: float | None
    mirrored: bool
    before_first: tuple[float, float] | None
    before_last: tuple[float, float] | None
    after_first: tuple[float, float] | None
    after_last: tuple[float, float] | None


def extract_track_geometry(ibt_path: str | Path, storage_root: str | Path) -> Path:
    """Extract track geometry from *ibt_path* and persist it below *storage_root*."""
    ibt_path = Path(ibt_path)
    storage_root = Path(storage_root)

    ibt = _open_ibt_history(ibt_path)
    try:
        metadata = _read_track_metadata(ibt)
        available_channels = _describe_available_channels(ibt)
        pipeline = _build_latlon_centerline_pipeline(ibt, available_channels)
    finally:
        _close_ibt(ibt)

    _log_latlon_centerline_pipeline(ibt_path, metadata.track_key, pipeline)
    center_line = pipeline.center_line

    if len(center_line) < 2:
        message = f"missing valid Lat/Lon + LapDistPct centerline in IBT: {ibt_path} ({pipeline.reason})"
        _LOG.warning("[ibt_track_extract] %s", message)
        raise RuntimeError(message)

    orientation_result = _normalise_exported_center_line_orientation(center_line)
    _log_export_orientation_normalisation(metadata.track_key, orientation_result)

    payload = _build_track_geometry_payload(
        track_key=metadata.track_key,
        center_line=orientation_result.center_line,
        left_edge=[],
        right_edge=[],
        source_type="ibt",
        source_name="ibt_telemetry",
        source_path=str(ibt_path),
        iracing_build=metadata.iracing_build,
        track_display_name=metadata.track_display_name,
        track_config_name=metadata.track_config_name,
        track_name=metadata.track_name,
        track_id=metadata.track_id,
        geometry_kind="centerline_only",
        position_source="latlon",
        distance_source="LapDistPct",
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


def build_ibt_inventory_report(ibt_path: str | Path) -> dict[str, Any]:
    """Inspect one IBT file and return a diagnostic inventory without deriving geometry."""
    ibt_path = Path(ibt_path)
    stat = ibt_path.stat()
    ibt = _open_ibt_history(ibt_path)
    try:
        yaml_text = _get_session_yaml(ibt)
        metadata = _read_inventory_metadata(ibt, yaml_text)
        available_channels = _describe_available_channels(ibt)
        channel_stats = _collect_ibt_channel_stats(ibt, available_channels)
        candidate_report = _build_candidate_channel_report(available_channels, channel_stats)
        viability = _geo_pipeline_result_to_viability_report(_build_latlon_centerline_pipeline(ibt, available_channels))
        recommendation = _build_ibt_recommendation(candidate_report, viability)
    finally:
        _close_ibt(ibt)

    return {
        "generated_at": _utc_now_iso(),
        "file_summary": {
            "path": str(ibt_path.resolve()),
            "file_name": ibt_path.name,
            "size_bytes": int(stat.st_size),
            "modified_at": _timestamp_to_iso(stat.st_mtime),
            "session_record_count": _read_ibt_record_count(available_channels, channel_stats),
            "session_lap_count": _read_ibt_disk_header_int(ibt_path, "session_lap_count"),
            "track_display_name": metadata.get("track_display_name"),
            "track_config_name": metadata.get("track_config_name"),
            "track_name": metadata.get("track_name"),
            "track_id": metadata.get("track_id"),
            "car_path": metadata.get("car_path"),
            "car_screen_name": metadata.get("car_screen_name"),
            "car_class_short_name": metadata.get("car_class_short_name"),
            "session_type": metadata.get("session_type"),
            "session_type_raw": metadata.get("session_type_raw"),
            "iracing_build": metadata.get("iracing_build"),
            "track_key": metadata.get("track_key"),
        },
        "session_metadata": metadata,
        "available_channels": sorted(available_channels.keys(), key=str.lower),
        "all_channel_stats": {name: channel_stats[name] for name in sorted(channel_stats.keys(), key=str.lower)},
        "candidate_channel_report": candidate_report,
        "lat_lon_centerline_viability": viability,
        "recommendation": recommendation,
    }


def write_ibt_inventory_report(
    ibt_path: str | Path,
    *,
    output_dir: str | Path,
    report: dict[str, Any] | None = None,
) -> Path:
    """Write an IBT inventory report as JSON below *output_dir*."""
    ibt_path = Path(ibt_path)
    output_dir = Path(output_dir)
    report = report if report is not None else build_ibt_inventory_report(ibt_path)
    safe_stem = _slugify_filename(ibt_path.stem) or "ibt"
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{safe_stem}__inventory.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return out_path


def _build_track_geometry_payload(
    *,
    track_key: str,
    center_line: list[Any],
    left_edge: list[Any],
    right_edge: list[Any],
    source_type: str,
    source_name: str,
    source_path: str,
    iracing_build: str,
    track_display_name: str = "",
    track_config_name: str = "",
    track_name: str = "",
    track_id: int | None = None,
    fallback_reason: str = "",
    geometry_kind: str = "",
    position_source: str = "",
    distance_source: str = "",
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
    if geometry_kind:
        payload["geometry_kind"] = geometry_kind
    if position_source:
        payload["position_source"] = position_source
    if distance_source:
        payload["distance_source"] = distance_source
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
        close = getattr(ir, "close", None)
        if callable(close):
            close()
            return
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
    text = _read_session_yaml_from_shared_mem(ir)
    if text:
        return text
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


def _open_ibt_history(ibt_path: Path) -> Any:
    import irsdk  # type: ignore[import]

    ibt_ctor = getattr(irsdk, "IBT", None)
    if not callable(ibt_ctor):
        raise RuntimeError("irsdk.IBT unavailable; full IBT inventory requires pyirsdk offline reader support")
    ibt = ibt_ctor()
    open_file = getattr(ibt, "open", None)
    if not callable(open_file):
        raise RuntimeError("irsdk.IBT.open missing")
    open_file(str(ibt_path))
    return ibt


def _describe_available_channels(ir: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    headers = getattr(ir, "_var_headers", None)
    if headers is None:
        headers = getattr(ir, "var_headers", None)
    for header in _iter_ir_headers(headers):
        name = _header_field(header, "name", "Name", "var_name", "varName")
        if not name:
            continue
        result[str(name)] = _extract_header_info(header)
    return result


def _collect_ibt_channel_stats(
    ir: Any,
    available_channels: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for name, info in available_channels.items():
        values = _read_ibt_series(ir, name)
        stats[name] = _summarize_channel_values(name=name, values=values, header_info=info)
    return stats


def _summarize_channel_values(
    *,
    name: str,
    values: list[Any] | None,
    header_info: dict[str, Any],
) -> dict[str, Any]:
    sample_count = len(values) if isinstance(values, list) else 0
    non_null_count = 0
    flattened_values: list[float] = []
    for value in values or []:
        if value is None:
            continue
        non_null_count += 1
        items = value if isinstance(value, (list, tuple)) else [value]
        for item in items:
            numeric = _safe_float(item)
            if numeric is not None:
                flattened_values.append(numeric)

    finite_count = 0
    zero_count = 0
    nonzero_count = 0
    min_value: float | None = None
    max_value: float | None = None
    mean_value: float | None = None
    value_count = len(flattened_values)
    if flattened_values:
        arr = np.asarray(flattened_values, dtype=np.float64)
        finite_mask = np.isfinite(arr)
        finite_count = int(finite_mask.sum())
        if finite_count > 0:
            finite_values = arr[finite_mask]
            zero_mask = np.isclose(finite_values, 0.0, atol=0.0, rtol=0.0)
            zero_count = int(zero_mask.sum())
            nonzero_count = int((~zero_mask).sum())
            min_value = float(np.min(finite_values))
            max_value = float(np.max(finite_values))
            mean_value = float(np.mean(finite_values))

    valid_share = float(finite_count / value_count) if value_count > 0 else None
    nonzero_share = float(nonzero_count / finite_count) if finite_count > 0 else None
    non_null_share = float(non_null_count / sample_count) if sample_count > 0 else None

    return {
        "name": name,
        "type": header_info.get("type"),
        "count": header_info.get("count"),
        "unit": header_info.get("unit"),
        "desc": header_info.get("desc"),
        "sample_count": sample_count,
        "non_null_count": non_null_count,
        "non_null_share": non_null_share,
        "value_count": value_count,
        "finite_count": finite_count,
        "zero_count": zero_count,
        "nonzero_count": nonzero_count,
        "valid_share": valid_share,
        "nonzero_share": nonzero_share,
        "min": min_value,
        "max": max_value,
        "mean": mean_value,
    }


def _build_candidate_channel_report(
    available_channels: dict[str, dict[str, Any]],
    channel_stats: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    report: dict[str, dict[str, Any]] = {}
    for name in _IBT_INVENTORY_CANDIDATES:
        entry: dict[str, Any] = {
            "present": name in available_channels,
            "configured_aliases": list(REQUESTED_CHANNEL_ALIASES.get(name, ())),
            "similar_names": _find_similar_available_names(available_channels, (name, *REQUESTED_CHANNEL_ALIASES.get(name, ()))),
        }
        if name in channel_stats:
            entry.update(channel_stats[name])
        else:
            entry.update(
                {
                    "sample_count": 0,
                    "non_null_count": 0,
                    "non_null_share": None,
                    "value_count": 0,
                    "finite_count": 0,
                    "zero_count": 0,
                    "nonzero_count": 0,
                    "valid_share": None,
                    "nonzero_share": None,
                    "min": None,
                    "max": None,
                    "mean": None,
                }
            )
        report[name] = entry
    return report


def _assess_latlon_centerline_viability(
    ir: Any,
    available_channels: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return _geo_pipeline_result_to_viability_report(_build_latlon_centerline_pipeline(ir, available_channels))


def _geo_pipeline_result_to_viability_report(result: _GeoPipelineResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "reason": result.reason,
        "required_channels": list(result.required_channels),
        "missing_channels": list(result.missing_channels),
        "similar_channels": dict(result.similar_channels),
        "source_channels": dict(result.source_channels),
        "joint_valid_sample_count": result.joint_valid_sample_count,
        "joint_in_range_sample_count": result.joint_in_range_sample_count,
        "joint_valid_share": result.joint_valid_share,
        "lap_dist_pct_in_range_share": result.lap_dist_pct_in_range_share,
        "lat_range": result.lat_range,
        "lon_range": result.lon_range,
        "lap_dist_pct_min": result.lap_dist_pct_min,
        "lap_dist_pct_max": result.lap_dist_pct_max,
        "unique_lap_dist_pct_samples": result.unique_lap_dist_pct_samples,
        "min_joint_samples_required": result.min_joint_samples_required,
        "min_unique_lap_dist_required": result.min_unique_lap_dist_required,
        "enough_samples_for_centerline": result.enough_samples_for_centerline,
        "pipeline_counts": dict(result.pipeline_counts),
        "debug_valid_geo_samples_head": list(result.debug_valid_geo_samples_head),
        "debug_valid_geo_samples_tail": list(result.debug_valid_geo_samples_tail),
        "center_line_point_count": len(result.center_line),
        "selected_reference_lap": result.selected_reference_lap,
        "selected_reference_min_lap_dist_pct": result.selected_reference_min_lap_dist_pct,
        "selected_reference_max_lap_dist_pct": result.selected_reference_max_lap_dist_pct,
        "selected_reference_point_count": result.selected_reference_point_count,
        "selected_reference_closure_gap_m": result.selected_reference_closure_gap_m,
        "selected_reference_is_valid": result.selected_reference_is_valid,
    }


def _build_latlon_centerline_pipeline(
    ir: Any,
    available_channels: dict[str, dict[str, Any]] | None = None,
) -> _GeoPipelineResult:
    available_channels = available_channels if available_channels is not None else _describe_available_channels(ir)
    raw_series, source_channels = _read_geo_channel_series(ir, available_channels)
    required = list(_IBT_GEO_REQUIRED_CHANNELS)
    missing = [name for name in required if name not in source_channels]
    similar = {
        name: _find_similar_available_names(available_channels, (name, *REQUESTED_CHANNEL_ALIASES.get(name, ())))
        for name in missing
    }
    sample_count = max((len(values) for values in raw_series.values()), default=0)
    pipeline_counts = {
        "raw_sample_count": int(sample_count),
        "joint_valid_geo_sample_count": 0,
        "after_unwrap_count": 0,
        "after_sort_count": 0,
        "after_dedupe_count": 0,
        "after_resample_count": 0,
        "before_json_write_count": 0,
    }
    if missing:
        return _GeoPipelineResult(
            status="missing",
            reason=f"missing required channels: {', '.join(missing)}",
            required_channels=required,
            missing_channels=missing,
            similar_channels=similar,
            source_channels=source_channels,
            sample_count=int(sample_count),
            joint_valid_sample_count=0,
            joint_in_range_sample_count=0,
            joint_valid_share=None,
            lap_dist_pct_in_range_share=None,
            lat_range=None,
            lon_range=None,
            lap_dist_pct_min=None,
            lap_dist_pct_max=None,
            unique_lap_dist_pct_samples=0,
            min_joint_samples_required=None,
            min_unique_lap_dist_required=None,
            enough_samples_for_centerline=False,
            pipeline_counts=pipeline_counts,
            debug_valid_geo_samples_head=[],
            debug_valid_geo_samples_tail=[],
            center_line=[],
            selected_reference_lap=None,
            selected_reference_min_lap_dist_pct=None,
            selected_reference_max_lap_dist_pct=None,
            selected_reference_point_count=0,
            selected_reference_closure_gap_m=None,
            selected_reference_is_valid=False,
        )

    arrays = {name: _align_float_array(raw_series.get(name), sample_count) for name in (*_IBT_GEO_REQUIRED_CHANNELS, *_IBT_GEO_OPTIONAL_CHANNELS)}
    lat = arrays["Lat"]
    lon = arrays["Lon"]
    lap_dist_pct = arrays["LapDistPct"]
    if sample_count == 0:
        return _GeoPipelineResult(
            status="insufficient",
            reason="required channels present but contain no samples",
            required_channels=required,
            missing_channels=[],
            similar_channels={},
            source_channels=source_channels,
            sample_count=0,
            joint_valid_sample_count=0,
            joint_in_range_sample_count=0,
            joint_valid_share=None,
            lap_dist_pct_in_range_share=None,
            lat_range=None,
            lon_range=None,
            lap_dist_pct_min=None,
            lap_dist_pct_max=None,
            unique_lap_dist_pct_samples=0,
            min_joint_samples_required=None,
            min_unique_lap_dist_required=None,
            enough_samples_for_centerline=False,
            pipeline_counts=pipeline_counts,
            debug_valid_geo_samples_head=[],
            debug_valid_geo_samples_tail=[],
            center_line=[],
            selected_reference_lap=None,
            selected_reference_min_lap_dist_pct=None,
            selected_reference_max_lap_dist_pct=None,
            selected_reference_point_count=0,
            selected_reference_closure_gap_m=None,
            selected_reference_is_valid=False,
        )

    nonzero_geo_mask = ~(np.isclose(lat, 0.0, atol=0.0, rtol=0.0) & np.isclose(lon, 0.0, atol=0.0, rtol=0.0))
    valid_mask = np.isfinite(lat) & np.isfinite(lon) & np.isfinite(lap_dist_pct) & nonzero_geo_mask
    joint_valid_count = int(valid_mask.sum())
    pipeline_counts["joint_valid_geo_sample_count"] = joint_valid_count
    in_range_mask = valid_mask & (lap_dist_pct >= 0.0) & (lap_dist_pct <= 1.0)
    joint_in_range_count = int(in_range_mask.sum())
    joint_valid_share = float(joint_valid_count / sample_count) if sample_count > 0 else None
    in_range_share = float(joint_in_range_count / joint_valid_count) if joint_valid_count > 0 else None

    lat_range = _finite_range(lat[valid_mask])
    lon_range = _finite_range(lon[valid_mask])
    lap_dist_pct_min = _finite_min(lap_dist_pct[valid_mask])
    lap_dist_pct_max = _finite_max(lap_dist_pct[valid_mask])
    unique_lap_dist_pct_samples = _count_unique_finite(lap_dist_pct[in_range_mask], decimals=6)
    min_joint_samples = 4 if sample_count <= 10 else 20 if sample_count <= 100 else 100
    min_unique_lap_dist = 4 if sample_count <= 10 else 20
    enough_samples = joint_in_range_count >= min_joint_samples and unique_lap_dist_pct_samples >= min_unique_lap_dist
    varies_meaningfully = lat_range is not None and lon_range is not None and (lat_range > 1e-6 or lon_range > 1e-6)
    plausible_lap_dist_pct = in_range_share is not None and in_range_share >= 0.95
    debug_head, debug_tail = _build_geo_sample_debug_views(arrays, valid_mask)

    status = "ok"
    reason = "Lat/Lon + LapDistPct appear populated and varying enough for later geometry work"
    selected_reference: _GeoLapSelection | None = None
    if joint_valid_count == 0:
        status = "insufficient"
        reason = "no samples contain valid Lat + Lon + LapDistPct together"
    elif not plausible_lap_dist_pct:
        status = "insufficient"
        reason = "LapDistPct is not plausibly bounded to 0..1 for most joint-valid samples"
    elif not varies_meaningfully:
        status = "insufficient"
        reason = "Lat/Lon do not vary meaningfully across joint-valid samples"
    elif not enough_samples:
        status = "insufficient"
        reason = "too few joint-valid samples for a centerline"

    center_line: list[dict[str, float]] = []
    if status == "ok":
        joint_samples = _collect_joint_valid_geo_samples(arrays, valid_mask)
        unwrapped_samples = _unwrap_joint_valid_geo_samples(joint_samples)
        pipeline_counts["after_unwrap_count"] = len(unwrapped_samples)
        selected_reference = _select_best_geo_lap(
            unwrapped_samples,
            min_point_count=min_unique_lap_dist,
        )
        sorted_samples = [] if selected_reference is None else selected_reference.sorted_samples
        pipeline_counts["after_sort_count"] = len(sorted_samples)
        deduped_samples = [] if selected_reference is None else selected_reference.deduped_samples
        pipeline_counts["after_dedupe_count"] = len(deduped_samples)
        resampled_samples = _resample_geo_samples(
            deduped_samples,
            full_lap=bool(selected_reference and selected_reference.is_valid_reference_lap),
        )
        pipeline_counts["after_resample_count"] = len(resampled_samples)
        center_line = _build_centerline_from_geo_samples(
            resampled_samples,
            close_loop=bool(selected_reference and selected_reference.is_valid_reference_lap),
        )
        pipeline_counts["before_json_write_count"] = len(center_line)
        if len(center_line) < 2:
            status = "insufficient"
            reason = "valid geo samples collapsed before JSON write"

    return _GeoPipelineResult(
        status=status,
        reason=reason,
        required_channels=required,
        missing_channels=[],
        similar_channels={},
        source_channels=source_channels,
        sample_count=int(sample_count),
        joint_valid_sample_count=joint_valid_count,
        joint_in_range_sample_count=joint_in_range_count,
        joint_valid_share=joint_valid_share,
        lap_dist_pct_in_range_share=in_range_share,
        lat_range=lat_range,
        lon_range=lon_range,
        lap_dist_pct_min=lap_dist_pct_min,
        lap_dist_pct_max=lap_dist_pct_max,
        unique_lap_dist_pct_samples=unique_lap_dist_pct_samples,
        min_joint_samples_required=min_joint_samples,
        min_unique_lap_dist_required=min_unique_lap_dist,
        enough_samples_for_centerline=status == "ok" and len(center_line) >= 2,
        pipeline_counts=pipeline_counts,
        debug_valid_geo_samples_head=debug_head,
        debug_valid_geo_samples_tail=debug_tail,
        center_line=center_line,
        selected_reference_lap=None if selected_reference is None else selected_reference.lap_index,
        selected_reference_min_lap_dist_pct=None if selected_reference is None else selected_reference.min_lap_dist_pct,
        selected_reference_max_lap_dist_pct=None if selected_reference is None else selected_reference.max_lap_dist_pct,
        selected_reference_point_count=0 if selected_reference is None else selected_reference.point_count,
        selected_reference_closure_gap_m=None if selected_reference is None else selected_reference.closure_gap_m,
        selected_reference_is_valid=False if selected_reference is None else selected_reference.is_valid_reference_lap,
    )


def _build_ibt_recommendation(
    candidate_report: dict[str, dict[str, Any]],
    viability: dict[str, Any],
) -> dict[str, Any]:
    usable = viability.get("status") == "ok"
    alternatives: list[dict[str, Any]] = []
    if not usable:
        velocity_channels = [
            name
            for name in ("SessionTime", "Yaw", "Speed", "VelocityX", "VelocityY", "VelocityZ", "LapDistPct", "LapDist")
            if candidate_report.get(name, {}).get("present")
        ]
        if "VelocityX" in velocity_channels and "VelocityY" in velocity_channels:
            alternatives.append(
                {
                    "channel_set": velocity_channels,
                    "kind": "relative_path_candidate",
                    "note": "Velocity + time/yaw can support later dead-reckoning, but not proven absolute geo position.",
                }
            )
        elif any(name in velocity_channels for name in ("LapDistPct", "LapDist")):
            alternatives.append(
                {
                    "channel_set": velocity_channels,
                    "kind": "distance_axis_only",
                    "note": "Distance channels are available, but no reliable absolute position source is proven here.",
                }
            )

    return {
        "status": "usable" if usable else "not_usable",
        "summary": "Lat/Lon + LapDistPct nutzbar" if usable else "Lat/Lon + LapDistPct nicht nutzbar",
        "basis": viability.get("reason"),
        "alternatives": alternatives,
    }


def _read_inventory_metadata(ir: Any, yaml_text: str) -> dict[str, Any]:
    parsed = _parse_session_yaml(yaml_text)
    weekend_info = _read_weekend_info(ir)
    parsed_weekend = parsed.get("WeekendInfo")
    if isinstance(parsed_weekend, dict):
        merged_weekend = dict(parsed_weekend)
        merged_weekend.update(weekend_info)
        weekend_info = merged_weekend
    driver_info = parsed.get("DriverInfo") if isinstance(parsed, dict) else {}
    driver = _select_driver_info(driver_info if isinstance(driver_info, dict) else {})
    extracted_meta = extract_session_meta(yaml_text) if yaml_text.strip() else {}

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
        or _coerce_optional_str(weekend_info.get("TrackVersion"))
        or _coerce_optional_str(weekend_info.get("SimMode"))
        or _read_scalar_from_yaml(yaml_text, "SimMode")
        or ""
    )
    return {
        "track_key": build_track_key(track_display_name, track_config_name),
        "track_display_name": track_display_name,
        "track_config_name": track_config_name,
        "track_name": track_name,
        "track_id": track_id,
        "track_version": _coerce_optional_str(weekend_info.get("TrackVersion")),
        "car_path": _coerce_optional_str(driver.get("CarPath")),
        "car_screen_name": _coerce_optional_str(driver.get("CarScreenName"))
        or _coerce_optional_str(extracted_meta.get("CarScreenName")),
        "car_class_short_name": _coerce_optional_str(driver.get("CarClassShortName"))
        or _coerce_optional_str(extracted_meta.get("CarClassShortName")),
        "driver_name": _coerce_optional_str(driver.get("UserName"))
        or _coerce_optional_str(extracted_meta.get("DriverName")),
        "session_type": _coerce_optional_str(extracted_meta.get("SessionType"))
        or _coerce_optional_str(_read_scalar_from_yaml(yaml_text, "SessionType"))
        or _coerce_optional_str(_read_scalar_from_yaml(yaml_text, "EventType")),
        "session_type_raw": _coerce_optional_str(extracted_meta.get("session_type_raw"))
        or _coerce_optional_str(_read_scalar_from_yaml(yaml_text, "SessionType"))
        or _coerce_optional_str(_read_scalar_from_yaml(yaml_text, "EventType")),
        "iracing_build": iracing_build,
    }


def _select_driver_info(driver_info: dict[str, Any]) -> dict[str, Any]:
    drivers = driver_info.get("Drivers")
    if not isinstance(drivers, list):
        return {}
    driver_car_idx = _coerce_optional_int(driver_info.get("DriverCarIdx"))
    if driver_car_idx is not None:
        for item in drivers:
            if not isinstance(item, dict):
                continue
            if _coerce_optional_int(item.get("CarIdx")) == driver_car_idx:
                return item
    for item in drivers:
        if isinstance(item, dict) and _coerce_optional_int(item.get("IsSpectator")) in (None, 0):
            return item
    return drivers[0] if drivers and isinstance(drivers[0], dict) else {}


def _read_session_yaml_from_shared_mem(ir: Any) -> str:
    header = getattr(ir, "_header", None)
    shared_mem = getattr(ir, "_shared_mem", None)
    if header is None or shared_mem is None:
        return ""
    try:
        offset = int(getattr(header, "session_info_offset"))
        length = int(getattr(header, "session_info_len"))
    except Exception:
        return ""
    if offset < 0 or length <= 0:
        return ""
    try:
        chunk = shared_mem[offset : offset + length]
    except Exception:
        return ""
    try:
        data = bytes(chunk)
    except Exception:
        return ""
    data = data.split(b"\x00", 1)[0]
    return _decode_text_best_effort(data)


def _decode_text_best_effort(data: bytes) -> str:
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def _read_ibt_series(ir: Any, name: str) -> list[Any] | None:
    getter = getattr(ir, "get_all", None)
    if not callable(getter):
        return None
    try:
        values = getter(name)
    except Exception:
        return None
    return values if isinstance(values, list) else None


def _series_to_float_array(values: list[Any] | None) -> np.ndarray:
    if not values:
        return np.empty(0, dtype=np.float64)
    arr = np.full(len(values), np.nan, dtype=np.float64)
    for idx, value in enumerate(values):
        numeric = _safe_float(value)
        if numeric is not None:
            arr[idx] = numeric
    return arr


def _align_float_array(values: list[Any] | None, sample_count: int) -> np.ndarray:
    arr = _series_to_float_array(values)
    if sample_count <= 0 or len(arr) == sample_count:
        return arr
    aligned = np.full(sample_count, np.nan, dtype=np.float64)
    aligned[: min(sample_count, len(arr))] = arr[:sample_count]
    return aligned


def _read_geo_channel_series(
    ir: Any,
    available_channels: dict[str, dict[str, Any]],
) -> tuple[dict[str, list[Any]], dict[str, str]]:
    raw_series: dict[str, list[Any]] = {}
    source_channels: dict[str, str] = {}
    for name in (*_IBT_GEO_REQUIRED_CHANNELS, *_IBT_GEO_OPTIONAL_CHANNELS):
        source_name, values = _read_resolved_ibt_series(ir, available_channels, name)
        if source_name is None or values is None:
            continue
        raw_series[name] = values
        source_channels[name] = source_name
    return raw_series, source_channels


def _read_resolved_ibt_series(
    ir: Any,
    available_channels: dict[str, dict[str, Any]],
    requested_name: str,
) -> tuple[str | None, list[Any] | None]:
    candidates = (requested_name, *REQUESTED_CHANNEL_ALIASES.get(requested_name, ()))
    for name in candidates:
        if name not in available_channels:
            continue
        values = _read_ibt_series(ir, name)
        if values is not None:
            return name, values
    for name in candidates:
        values = _read_ibt_series(ir, name)
        if values is not None:
            return name, values
    return None, None


def _build_geo_sample_debug_views(
    arrays: dict[str, np.ndarray],
    valid_mask: np.ndarray,
) -> tuple[list[dict[str, float | None]], list[dict[str, float | None]]]:
    indices = np.flatnonzero(valid_mask)
    if indices.size == 0:
        return [], []
    head_idx = indices[:5]
    tail_idx = indices[-5:]
    return _format_geo_sample_debug_rows(arrays, head_idx), _format_geo_sample_debug_rows(arrays, tail_idx)


def _format_geo_sample_debug_rows(
    arrays: dict[str, np.ndarray],
    indices: np.ndarray,
) -> list[dict[str, float | None]]:
    rows: list[dict[str, float | None]] = []
    for idx in indices.tolist():
        rows.append(
            {
                "SessionTime": _finite_float_or_none(arrays["SessionTime"][idx]),
                "LapDistPct": _finite_float_or_none(arrays["LapDistPct"][idx]),
                "Lat": _finite_float_or_none(arrays["Lat"][idx]),
                "Lon": _finite_float_or_none(arrays["Lon"][idx]),
                "Alt": _finite_float_or_none(arrays["Alt"][idx]),
            }
        )
    return rows


def _finite_float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except Exception:
        return None
    return number if math.isfinite(number) else None


def _collect_joint_valid_geo_samples(
    arrays: dict[str, np.ndarray],
    valid_mask: np.ndarray,
) -> list[dict[str, float | None]]:
    samples: list[dict[str, float | None]] = []
    for idx in np.flatnonzero(valid_mask).tolist():
        samples.append(
            {
                "session_time": _finite_float_or_none(arrays["SessionTime"][idx]),
                "lap_dist_pct": float(arrays["LapDistPct"][idx]),
                "lat": float(arrays["Lat"][idx]),
                "lon": float(arrays["Lon"][idx]),
                "alt": _finite_float_or_none(arrays["Alt"][idx]),
            }
        )
    return samples


def _unwrap_joint_valid_geo_samples(samples: list[dict[str, float | None]]) -> list[dict[str, float | None]]:
    if not samples:
        return []
    wrapped = np.asarray([float(sample["lap_dist_pct"]) for sample in samples], dtype=np.float64)
    unwrapped = _unwrap_lap_dist_pct(wrapped)
    out: list[dict[str, float | None]] = []
    for sample, lap_dist_unwrapped in zip(samples, unwrapped):
        row = dict(sample)
        row["lap_dist_unwrapped"] = float(lap_dist_unwrapped)
        out.append(row)
    return out


def _unwrap_lap_dist_pct(values: np.ndarray) -> np.ndarray:
    if values.size == 0:
        return np.empty(0, dtype=np.float64)
    out = np.asarray(values, dtype=np.float64).copy()
    offset = 0.0
    prev = out[0]
    for idx in range(1, len(out)):
        cur = out[idx]
        if math.isfinite(prev) and math.isfinite(cur) and cur < prev - 0.5:
            offset += 1.0
        out[idx] = cur + offset
        prev = cur
    return out


def _select_best_geo_lap(
    samples: list[dict[str, float | None]],
    *,
    min_point_count: int,
) -> _GeoLapSelection | None:
    if not samples:
        return None
    buckets: dict[int, list[dict[str, float | None]]] = {}
    for sample in samples:
        lap_dist_unwrapped = _finite_float_or_none(sample.get("lap_dist_unwrapped"))
        if lap_dist_unwrapped is None:
            continue
        lap_index = int(math.floor(lap_dist_unwrapped + 1e-9))
        lap_dist_pct = lap_dist_unwrapped - float(lap_index)
        if lap_dist_pct < 0.0:
            lap_dist_pct += 1.0
        elif lap_dist_pct >= 1.0:
            lap_dist_pct -= 1.0
        row = dict(sample)
        row["lap_dist_pct"] = float(lap_dist_pct)
        buckets.setdefault(lap_index, []).append(row)
    if not buckets:
        return None

    selections: list[_GeoLapSelection] = []
    for lap_index, bucket in buckets.items():
        sorted_bucket = sorted(
            bucket,
            key=lambda item: (
                float(item["lap_dist_pct"]),
                _finite_float_or_none(item.get("session_time")) if item.get("session_time") is not None else -1.0,
            ),
        )
        deduped_bucket = _dedupe_sorted_geo_samples(sorted_bucket)
        if len(deduped_bucket) < 2:
            continue
        lap_dist = np.asarray([float(item["lap_dist_pct"]) for item in deduped_bucket], dtype=np.float64)
        min_lap_dist_pct = float(lap_dist[0])
        max_lap_dist_pct = float(lap_dist[-1])
        coverage = max_lap_dist_pct - min_lap_dist_pct
        closure_gap_m = _geo_sample_closure_gap_m(deduped_bucket)
        selections.append(
            _GeoLapSelection(
                lap_index=lap_index,
                sorted_samples=sorted_bucket,
                deduped_samples=deduped_bucket,
                min_lap_dist_pct=min_lap_dist_pct,
                max_lap_dist_pct=max_lap_dist_pct,
                point_count=len(deduped_bucket),
                closure_gap_m=closure_gap_m,
                coverage=coverage,
                is_valid_reference_lap=_is_valid_reference_lap(
                    min_lap_dist_pct=min_lap_dist_pct,
                    max_lap_dist_pct=max_lap_dist_pct,
                    point_count=len(deduped_bucket),
                    min_point_count=min_point_count,
                    closure_gap_m=closure_gap_m,
                    samples=deduped_bucket,
                ),
            )
        )
    if not selections:
        return None

    def _selection_score(selection: _GeoLapSelection) -> tuple[int, int, int, int, float, int, float, int]:
        return (
            1 if selection.is_valid_reference_lap else 0,
            1 if selection.min_lap_dist_pct <= _REFERENCE_LAP_START_MAX_PCT else 0,
            1 if selection.max_lap_dist_pct >= _REFERENCE_LAP_END_MIN_PCT else 0,
            1 if selection.closure_gap_m is not None else 0,
            selection.coverage,
            selection.point_count,
            -selection.closure_gap_m if selection.closure_gap_m is not None else float("-inf"),
            selection.lap_index,
        )

    return max(selections, key=_selection_score)


def _dedupe_sorted_geo_samples(samples: list[dict[str, float | None]]) -> list[dict[str, float | None]]:
    deduped: list[dict[str, float | None]] = []
    seen_keys: set[float] = set()
    for sample in samples:
        lap_dist_pct = _finite_float_or_none(sample.get("lap_dist_pct"))
        if lap_dist_pct is None:
            continue
        key = round(lap_dist_pct, 6)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(sample)
    return deduped


def _resample_geo_samples(
    samples: list[dict[str, float | None]],
    *,
    full_lap: bool = False,
) -> list[dict[str, float | None]]:
    if len(samples) < 2:
        return []
    lap_dist = np.asarray([float(sample["lap_dist_pct"]) for sample in samples], dtype=np.float64)
    if len(np.unique(np.round(lap_dist, 6))) < 2:
        return []
    target_count = len(samples)
    if full_lap:
        grid = np.linspace(0.0, 1.0, num=target_count, endpoint=True, dtype=np.float64)
        lat = _interp_cyclic_geo_field(
            lap_dist,
            np.asarray([float(sample["lat"]) for sample in samples], dtype=np.float64),
            grid,
        )
        lon = _interp_cyclic_geo_field(
            lap_dist,
            np.asarray([float(sample["lon"]) for sample in samples], dtype=np.float64),
            grid,
        )
    else:
        grid = np.linspace(float(lap_dist[0]), float(lap_dist[-1]), num=target_count, endpoint=True, dtype=np.float64)
        lat = np.interp(grid, lap_dist, np.asarray([float(sample["lat"]) for sample in samples], dtype=np.float64))
        lon = np.interp(grid, lap_dist, np.asarray([float(sample["lon"]) for sample in samples], dtype=np.float64))
    session_time = _interp_optional_sample_field(samples, lap_dist, grid, "session_time", full_lap=full_lap)
    alt = _interp_optional_sample_field(samples, lap_dist, grid, "alt", full_lap=full_lap)
    resampled: list[dict[str, float | None]] = []
    for idx, lap_dist_pct in enumerate(grid.tolist()):
        resampled.append(
            {
                "session_time": session_time[idx],
                "lap_dist_pct": float(lap_dist_pct),
                "lat": float(lat[idx]),
                "lon": float(lon[idx]),
                "alt": alt[idx],
            }
        )
    return resampled


def _interp_optional_sample_field(
    samples: list[dict[str, float | None]],
    lap_dist: np.ndarray,
    grid: np.ndarray,
    field_name: str,
    *,
    full_lap: bool = False,
) -> list[float | None]:
    values = np.asarray([np.nan if sample.get(field_name) is None else float(sample[field_name]) for sample in samples], dtype=np.float64)
    finite_mask = np.isfinite(values)
    if int(finite_mask.sum()) >= 2:
        x = lap_dist[finite_mask]
        y = values[finite_mask]
        interpolated = _interp_cyclic_geo_field(x, y, grid) if full_lap else np.interp(grid, x, y)
        return [float(value) for value in interpolated.tolist()]
    if int(finite_mask.sum()) == 1:
        only_value = float(values[finite_mask][0])
        return [only_value] * len(grid)
    return [None] * len(grid)


def _interp_cyclic_geo_field(lap_dist: np.ndarray, values: np.ndarray, grid: np.ndarray) -> np.ndarray:
    if len(lap_dist) < 2 or len(values) < 2 or len(lap_dist) != len(values):
        return np.empty(0, dtype=np.float64)
    extended_lap_dist = np.concatenate(([lap_dist[-1] - 1.0], lap_dist, [lap_dist[0] + 1.0]))
    extended_values = np.concatenate(([values[-1]], values, [values[0]]))
    return np.interp(grid, extended_lap_dist, extended_values)


def _geo_sample_closure_gap_m(samples: list[dict[str, float | None]]) -> float | None:
    if len(samples) < 2:
        return None
    lat = np.asarray([float(sample["lat"]) for sample in samples], dtype=np.float64)
    lon = np.asarray([float(sample["lon"]) for sample in samples], dtype=np.float64)
    xy = _project_latlon_to_xy(lat, lon)
    if xy is None or len(xy) < 2:
        return None
    return float(np.linalg.norm(xy[-1] - xy[0]))


def _is_valid_reference_lap(
    *,
    min_lap_dist_pct: float,
    max_lap_dist_pct: float,
    point_count: int,
    min_point_count: int,
    closure_gap_m: float | None,
    samples: list[dict[str, float | None]],
) -> bool:
    if point_count < max(2, int(min_point_count)):
        return False
    if min_lap_dist_pct > _REFERENCE_LAP_START_MAX_PCT:
        return False
    if max_lap_dist_pct < _REFERENCE_LAP_END_MIN_PCT:
        return False
    if closure_gap_m is None:
        return False
    return closure_gap_m <= _reference_lap_closure_gap_limit(samples)


def _reference_lap_closure_gap_limit(samples: list[dict[str, float | None]]) -> float:
    if len(samples) < 2:
        return _REFERENCE_LAP_MIN_CLOSURE_GAP_M
    lat = np.asarray([float(sample["lat"]) for sample in samples], dtype=np.float64)
    lon = np.asarray([float(sample["lon"]) for sample in samples], dtype=np.float64)
    xy = _project_latlon_to_xy(lat, lon)
    if xy is None or len(xy) < 2:
        return _REFERENCE_LAP_MIN_CLOSURE_GAP_M
    segment_lengths = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    finite_lengths = segment_lengths[np.isfinite(segment_lengths) & (segment_lengths > 0.0)]
    median_step_m = float(np.median(finite_lengths)) if len(finite_lengths) > 0 else 0.0
    return max(
        _REFERENCE_LAP_MIN_CLOSURE_GAP_M,
        median_step_m * _REFERENCE_LAP_CLOSURE_MEDIAN_STEP_FACTOR,
    )


def _finite_range(values: np.ndarray) -> float | None:
    if values.size == 0:
        return None
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return None
    return float(np.max(finite) - np.min(finite))


def _finite_min(values: np.ndarray) -> float | None:
    if values.size == 0:
        return None
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return None
    return float(np.min(finite))


def _finite_max(values: np.ndarray) -> float | None:
    if values.size == 0:
        return None
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return None
    return float(np.max(finite))


def _count_unique_finite(values: np.ndarray, *, decimals: int) -> int:
    if values.size == 0:
        return 0
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return 0
    rounded = np.round(finite, decimals=decimals)
    return int(np.unique(rounded).size)


def _find_similar_available_names(
    available_channels: dict[str, dict[str, Any]],
    names: tuple[str, ...],
) -> list[dict[str, Any]]:
    terms: list[str] = []
    seen_terms: set[str] = set()
    for name in names:
        normalized = _normalize_var_key(name)
        if len(normalized) < 3 or normalized in seen_terms:
            continue
        seen_terms.add(normalized)
        terms.append(normalized)

    matches: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for candidate_name, info in available_channels.items():
        norm_name = _normalize_var_key(candidate_name)
        if not norm_name or not any(_similar_name_matches(norm_name, term) for term in terms):
            continue
        if candidate_name in seen_names:
            continue
        seen_names.add(candidate_name)
        item = {"name": candidate_name}
        for key in ("type", "count", "unit", "desc"):
            if key in info:
                item[key] = info.get(key)
        matches.append(item)
    matches.sort(key=lambda item: str(item.get("name") or "").lower())
    return matches


def _normalize_var_key(value: Any) -> str:
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())


def _similar_name_matches(norm_name: str, term: str) -> bool:
    if not norm_name or not term:
        return False
    if norm_name.startswith(term) or term.startswith(norm_name):
        return True
    if len(term) >= 4 and (term in norm_name or norm_name in term):
        return True
    return False


def _iter_ir_headers(headers: Any) -> list[Any]:
    try:
        return list(headers)
    except Exception:
        return []


def _header_field(header: Any, *names: str) -> Any:
    for name in names:
        if isinstance(header, dict) and name in header:
            return header.get(name)
        if hasattr(header, name):
            try:
                value = getattr(header, name)
                return value() if callable(value) else value
            except Exception:
                continue
    return None


def _extract_header_info(header: Any) -> dict[str, Any]:
    info: dict[str, Any] = {}
    raw_type = _header_field(header, "type", "Type", "var_type", "varType")
    if raw_type is not None:
        pretty = _pretty_var_type(raw_type)
        info["type"] = pretty if pretty is not None else str(raw_type)
    count = _coerce_optional_int(_header_field(header, "count", "Count"))
    if count is not None:
        info["count"] = count
    unit = _header_field(header, "unit", "Unit")
    if unit not in (None, ""):
        info["unit"] = str(unit)
    desc = _header_field(header, "desc", "Desc", "description", "Description")
    if desc not in (None, ""):
        info["desc"] = str(desc)
    return info


def _pretty_var_type(raw_type: Any) -> str | None:
    key = str(raw_type).strip().lower()
    if key in {"irsdk_char", "irsdk_bool", "irsdk_int", "irsdk_bitfield", "irsdk_float", "irsdk_double"}:
        return key.replace("irsdk_", "")
    idx = _coerce_optional_int(raw_type)
    return {
        0: "char",
        1: "bool",
        2: "int",
        3: "bitfield",
        4: "float",
        5: "double",
    }.get(idx)


def _timestamp_to_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _slugify_filename(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(value or "").strip())
    return slug.strip("._")


def _read_ibt_record_count(
    available_channels: dict[str, dict[str, Any]],
    channel_stats: dict[str, dict[str, Any]],
) -> int | None:
    session_time = channel_stats.get("SessionTime")
    if isinstance(session_time, dict):
        sample_count = _coerce_optional_int(session_time.get("sample_count"))
        if sample_count is not None:
            return sample_count
    if not available_channels:
        return None
    first_name = sorted(available_channels.keys(), key=str.lower)[0]
    return _coerce_optional_int(channel_stats.get(first_name, {}).get("sample_count"))


def _read_ibt_disk_header_int(ibt_path: Path, field_name: str) -> int | None:
    ibt = _open_ibt_history(ibt_path)
    try:
        disk_header = getattr(ibt, "_disk_header", None)
        if disk_header is None:
            return None
        return _coerce_optional_int(getattr(disk_header, field_name, None))
    finally:
        _close_ibt(ibt)


def _read_iracing_build(ir: Any) -> str:
    return _read_track_metadata(ir).iracing_build


def _log_latlon_centerline_pipeline(
    ibt_path: Path,
    track_key: str,
    result: _GeoPipelineResult,
) -> None:
    counts = result.pipeline_counts
    _LOG.info(
        "[ibt_track_extract] track_key=%s ibt=%s status=%s reason=%s raw_sample_count=%d joint_valid_geo_sample_count=%d after_unwrap_count=%d after_sort_count=%d after_dedupe_count=%d after_resample_count=%d before_json_write_count=%d selected_reference_lap=%s min_lap_dist_pct=%s max_lap_dist_pct=%s point_count=%d closure_gap_m=%s reference_lap_valid=%s source_channels=%s",
        track_key,
        ibt_path,
        result.status,
        result.reason,
        counts.get("raw_sample_count", 0),
        counts.get("joint_valid_geo_sample_count", 0),
        counts.get("after_unwrap_count", 0),
        counts.get("after_sort_count", 0),
        counts.get("after_dedupe_count", 0),
        counts.get("after_resample_count", 0),
        counts.get("before_json_write_count", 0),
        result.selected_reference_lap,
        "nan" if result.selected_reference_min_lap_dist_pct is None else f"{result.selected_reference_min_lap_dist_pct:.6f}",
        "nan" if result.selected_reference_max_lap_dist_pct is None else f"{result.selected_reference_max_lap_dist_pct:.6f}",
        result.selected_reference_point_count,
        "nan" if result.selected_reference_closure_gap_m is None else f"{result.selected_reference_closure_gap_m:.3f}",
        result.selected_reference_is_valid,
        result.source_channels,
    )
    if result.debug_valid_geo_samples_head:
        _LOG.debug(
            "[ibt_track_extract] track_key=%s ibt=%s valid_geo_samples_head=%s",
            track_key,
            ibt_path,
            result.debug_valid_geo_samples_head,
        )
        _LOG.debug(
            "[ibt_track_extract] track_key=%s ibt=%s valid_geo_samples_tail=%s",
            track_key,
            ibt_path,
            result.debug_valid_geo_samples_tail,
        )


def _build_centerline_from_geo_samples(
    samples: list[dict[str, float | None]],
    *,
    close_loop: bool = False,
) -> list[dict[str, float]]:
    if len(samples) < 2:
        return []
    lat = np.asarray([float(sample["lat"]) for sample in samples], dtype=np.float64)
    lon = np.asarray([float(sample["lon"]) for sample in samples], dtype=np.float64)
    xy_m = _project_latlon_to_xy(lat, lon)
    if xy_m is None or len(xy_m) != len(samples) or len(samples) < 2:
        return []

    center_line: list[dict[str, float]] = []
    for sample, xy in zip(samples, xy_m):
        lap_dist = _finite_float_or_none(sample.get("lap_dist_pct"))
        lat_deg = _finite_float_or_none(sample.get("lat"))
        lon_deg = _finite_float_or_none(sample.get("lon"))
        x_m = _safe_float(xy[0])
        y_m = _safe_float(xy[1])
        if lap_dist is None or lat_deg is None or lon_deg is None or x_m is None or y_m is None:
            continue
        center_line.append(
            {
                "lap_dist_pct": lap_dist,
                "lat": lat_deg,
                "lon": lon_deg,
                "x_m": x_m,
                "y_m": y_m,
            }
        )
    if close_loop and len(center_line) >= 2:
        center_line[-1] = {
            **center_line[-1],
            "lap_dist_pct": 1.0,
            "lat": float(center_line[0]["lat"]),
            "lon": float(center_line[0]["lon"]),
            "x_m": float(center_line[0]["x_m"]),
            "y_m": float(center_line[0]["y_m"]),
        }
    return center_line if len(center_line) >= 2 else []


def _normalise_exported_center_line_orientation(
    center_line: list[dict[str, float]],
) -> _OrientationNormalisationResult:
    if len(center_line) < 2:
        return _OrientationNormalisationResult(
            center_line=list(center_line),
            rule=_EXPORT_ORIENTATION_RULE,
            transform="identity(insufficient_points)",
            applied=False,
            start_anchor_lap_dist_pct=None,
            start_anchor_xy_before=None,
            start_tangent_before=None,
            rotation_deg=None,
            mirrored=False,
            before_first=None,
            before_last=None,
            after_first=None,
            after_last=None,
        )

    start_anchor_index = _find_start_anchor_index(center_line)
    xy_before = np.asarray(
        [[float(point["x_m"]), float(point["y_m"])] for point in center_line],
        dtype=np.float64,
    )
    start_tangent = _find_start_tangent_vector(center_line, xy_before, start_anchor_index)
    xy_after, rotation_rad, mirrored = _normalise_trackmap_orientation_impl(
        xy_before,
        start_anchor_index=start_anchor_index,
        start_tangent=start_tangent,
    )
    applied = not np.allclose(xy_before, xy_after, rtol=0.0, atol=1e-9)

    normalised_center_line: list[dict[str, float]] = []
    for point, xy in zip(center_line, xy_after):
        row = dict(point)
        row["x_m"] = float(xy[0])
        row["y_m"] = float(xy[1])
        normalised_center_line.append(row)

    return _OrientationNormalisationResult(
        center_line=normalised_center_line,
        rule=_EXPORT_ORIENTATION_RULE,
        transform=_build_orientation_transform_string(rotation_rad, mirrored),
        applied=applied,
        start_anchor_lap_dist_pct=float(center_line[start_anchor_index]["lap_dist_pct"]),
        start_anchor_xy_before=(float(xy_before[start_anchor_index, 0]), float(xy_before[start_anchor_index, 1])),
        start_tangent_before=None if start_tangent is None else (float(start_tangent[0]), float(start_tangent[1])),
        rotation_deg=None if rotation_rad is None else math.degrees(rotation_rad),
        mirrored=mirrored,
        before_first=(float(xy_before[0, 0]), float(xy_before[0, 1])),
        before_last=(float(xy_before[-1, 0]), float(xy_before[-1, 1])),
        after_first=(float(xy_after[0, 0]), float(xy_after[0, 1])),
        after_last=(float(xy_after[-1, 0]), float(xy_after[-1, 1])),
    )


def _log_export_orientation_normalisation(
    track_key: str,
    result: _OrientationNormalisationResult,
) -> None:
    _LOG.info(
        "[ibt_track_export_orientation] track_key=%s point_count=%d normalisation_rule=%s normalisation_applied=%s start_anchor_lap_dist_pct=%s start_anchor_xy_before=%s start_tangent_before=%s applied_rotation_deg=%s applied_mirror_x=%s orientation_transform=%s",
        track_key,
        len(result.center_line),
        result.rule,
        "yes" if result.applied else "no",
        "nan" if result.start_anchor_lap_dist_pct is None else f"{result.start_anchor_lap_dist_pct:.6f}",
        result.start_anchor_xy_before,
        result.start_tangent_before,
        "nan" if result.rotation_deg is None else f"{result.rotation_deg:.6f}",
        "yes" if result.mirrored else "no",
        result.transform,
    )
    _LOG.debug(
        "[ibt_track_export_orientation_debug] track_key=%s before_first=%s before_last=%s after_first=%s after_last=%s",
        track_key,
        result.before_first,
        result.before_last,
        result.after_first,
        result.after_last,
    )


def _project_latlon_to_xy(lat: np.ndarray, lon: np.ndarray) -> np.ndarray | None:
    if len(lat) < 2 or len(lon) < 2 or len(lat) != len(lon):
        return None

    finite_mask = np.isfinite(lat) & np.isfinite(lon)
    if int(finite_mask.sum()) < 2:
        return None

    first_idx = int(np.flatnonzero(finite_mask)[0])
    lat0_rad = math.radians(float(lat[first_idx]))
    lon0_rad = math.radians(float(lon[first_idx]))
    cos_lat0 = math.cos(lat0_rad)
    if abs(cos_lat0) < 1e-6:
        cos_lat0 = 1e-6 if cos_lat0 >= 0.0 else -1e-6

    r = 6_371_000.0
    east_m = np.full(len(lat), np.nan, dtype=np.float64)
    north_m = np.full(len(lat), np.nan, dtype=np.float64)
    lat_rad = np.radians(lat[finite_mask])
    lon_rad = np.radians(lon[finite_mask])
    east_m[finite_mask] = (lon_rad - lon0_rad) * cos_lat0 * r
    north_m[finite_mask] = (lat_rad - lat0_rad) * r

    if not np.all(np.isfinite(east_m[finite_mask])) or not np.all(np.isfinite(north_m[finite_mask])):
        return None
    return np.column_stack([east_m, north_m])


def _find_start_anchor_index(center_line: list[dict[str, float]]) -> int:
    best_idx = 0
    best_key = (math.inf, math.inf, math.inf)
    for idx, point in enumerate(center_line):
        lap_dist_pct = float(point["lap_dist_pct"])
        key = (abs(lap_dist_pct), lap_dist_pct, idx)
        if key < best_key:
            best_key = key
            best_idx = idx
    return best_idx


def _find_start_tangent_vector(
    center_line: list[dict[str, float]],
    xy: np.ndarray,
    start_anchor_index: int,
) -> np.ndarray | None:
    if len(center_line) < 2 or len(xy) != len(center_line):
        return None

    start_lap_dist_pct = float(center_line[start_anchor_index]["lap_dist_pct"])
    candidates: list[tuple[float, np.ndarray]] = []
    for idx, point in enumerate(center_line):
        if idx == start_anchor_index:
            continue
        lap_dist_pct = float(point["lap_dist_pct"])
        delta = lap_dist_pct - start_lap_dist_pct
        if delta < 0.0:
            delta += 1.0
        if delta <= 1.0e-9 or delta >= 1.0 - 1.0e-9:
            continue
        tangent = np.asarray(xy[idx] - xy[start_anchor_index], dtype=np.float64)
        if not np.all(np.isfinite(tangent)) or float(np.linalg.norm(tangent)) <= 1.0e-9:
            continue
        candidates.append((delta, tangent))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    for delta, tangent in candidates:
        if delta >= _EXPORT_START_TANGENT_LOOKAHEAD_PCT - 1.0e-9:
            return tangent
    return candidates[0][1]


def _build_orientation_transform_string(rotation_rad: float | None, mirrored: bool) -> str:
    if rotation_rad is None:
        rotation_text = "rotate=none"
    else:
        rotation_text = f"rotate={math.degrees(rotation_rad):.6f}deg"
    mirror_text = "mirror_x=yes" if mirrored else "mirror_x=no"
    return f"translate=start_anchor_to_origin,{rotation_text},{mirror_text}"


def _normalise_trackmap_orientation_impl(
    xy: np.ndarray,
    *,
    start_anchor_index: int = 0,
    start_tangent: np.ndarray | None = None,
) -> tuple[np.ndarray, float | None, bool]:
    if xy is None or len(xy) == 0:
        return np.empty((0, 2), dtype=np.float64), None, False

    xy_arr = np.asarray(xy, dtype=np.float64)
    if xy_arr.ndim != 2 or xy_arr.shape[1] != 2:
        raise ValueError("xy must have shape (n, 2)")

    start_anchor_index = min(max(int(start_anchor_index), 0), len(xy_arr) - 1)
    translated = xy_arr - xy_arr[start_anchor_index]

    tangent = start_tangent
    if tangent is None:
        for idx in range(len(translated)):
            if idx == start_anchor_index:
                continue
            candidate = np.asarray(xy_arr[idx] - xy_arr[start_anchor_index], dtype=np.float64)
            if np.all(np.isfinite(candidate)) and float(np.linalg.norm(candidate)) > 1.0e-9:
                tangent = candidate
                break

    if tangent is None or not np.all(np.isfinite(tangent)) or float(np.linalg.norm(tangent)) <= 1.0e-9:
        return translated, None, False

    tangent_x = float(tangent[0])
    tangent_y = float(tangent[1])
    current_angle = math.atan2(tangent_y, tangent_x)
    target_angle = -math.pi / 2.0
    rotation_rad = target_angle - current_angle
    cos_a = math.cos(rotation_rad)
    sin_a = math.sin(rotation_rad)
    rotation_matrix = np.asarray([[cos_a, -sin_a], [sin_a, cos_a]], dtype=np.float64)
    rotated = translated @ rotation_matrix.T

    max_x = float(np.max(rotated[:, 0]))
    min_x = float(np.min(rotated[:, 0]))
    mirrored = max_x > abs(min_x) + 1.0e-9
    if mirrored:
        rotated = rotated.copy()
        rotated[:, 0] *= -1.0
    rotated[np.abs(rotated) <= 1.0e-12] = 0.0
    return rotated, rotation_rad, mirrored


def _normalise_trackmap_orientation(xy: np.ndarray) -> np.ndarray:
    oriented, _rotation_rad, _mirrored = _normalise_trackmap_orientation_impl(xy)
    return oriented


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
        "ldp": [],
        "lat": [],
        "lon": [],
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
        lat = _safe_float(ir["Lat"])
        lon = _safe_float(ir["Lon"])

        if lap_dist_pct is not None and seen_high and lap_dist_pct < 0.15 and n > 0:
            break

        result["t"].append(t)
        result["vx"].append(vx if vx is not None else 0.0)
        result["vy"].append(vy if vy is not None else 0.0)
        result["speed"].append(speed if speed is not None else 0.0)
        result["yaw"].append(yaw if yaw is not None else 0.0)
        result["ldp"].append(lap_dist_pct)
        result["lat"].append(lat)
        result["lon"].append(lon)
        n += 1

        if lap_dist_pct is not None and lap_dist_pct > 0.85:
            seen_high = True

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
