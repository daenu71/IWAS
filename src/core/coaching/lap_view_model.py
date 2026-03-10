"""LapViewModel – Datenzugriffs-Layer für Visualisierungs-UI (Story 3.0).

Einzige Schnittstelle zwischen Analyse-Backend und Visualisierungs-UI.
Sprint 4/5/6 erweitern dieses Modell ohne die UI anfassen zu müssen.

Artefakt-Pfade (flat Sprint-1 Struktur):
    <session_dir>/laps/lap_XXXX/analysis/lap_resampled.parquet
    <session_dir>/laps/lap_XXXX/analysis/lap_events.json
    <session_dir>/laps/lap_XXXX/analysis/corner_features.parquet
    <coaching_root>/corner_maps/<track_key>/corner_map_v1.json
    <session_dir>/session_meta.json
    <session_dir>/run_XXXX_lap_YYYY_meta.json
"""

from __future__ import annotations

import configparser
import json
import logging
import math
from dataclasses import dataclass, field, replace as _dataclass_replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

_PROJECT_ROOT = Path(__file__).parent.parent.parent

import numpy as np
import pyarrow.parquet as pq

from core.irsdk.sessioninfo_parser import resolve_session_environment

from .corner_map import load_corner_map
from .track_key import build_track_key
from .track_orientation import TrackDisplayOrientationResult, orient_track_display_frame

if TYPE_CHECKING:
    from .track_geometry import TrackRoadGeometry


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class LapMeta:
    """Lap-level metadata (track, car, timing, validity)."""

    track: str
    car: str
    lap_no: int
    lap_time: float | None
    valid: bool
    environment: dict[str, Any] | None = None
    track_usage: str | None = None


@dataclass
class CornerInfo:
    """Minimal corner descriptor from corner_map_v1.json.

    ``padded_start_lapdist_pct`` / ``padded_end_lapdist_pct`` are set at
    load time by :func:`apply_corner_padding`.  They extend the original
    corner bounds for event-grouping purposes; the original bounds are kept
    for geometry / visual rendering.
    """

    corner_id: int
    start_lapdist_pct: float
    end_lapdist_pct: float
    corner_type: str
    padded_start_lapdist_pct: float | None = field(default=None)
    padded_end_lapdist_pct: float | None = field(default=None)


@dataclass
class Event:
    """Single driving event extracted by the event engine."""

    event_type: str
    lapdist_pct: float
    value: Any = field(default=None)
    session_time: float | None = field(default=None)


@dataclass(frozen=True)
class TrackRoadGeometryLoadResult:
    geometry: "TrackRoadGeometry | None"
    requested_track_key: str
    geometry_path: Path
    payload_track_key: str | None
    source_type: str
    center_line_points: int
    left_edge_points: int
    right_edge_points: int
    center_line_format: str
    reason: str


@dataclass(frozen=True)
class TrackGeometryData:
    """Track geometry plus minimal metadata for rendering decisions."""

    lap_dist_pct: np.ndarray
    track_xy: np.ndarray
    source: str
    is_closed: bool


# ---------------------------------------------------------------------------
# LapViewModel
# ---------------------------------------------------------------------------


class LapViewModel:
    """Consolidated data access layer for one lap's analysis artefacts.

    Usage::

        vm = LapViewModel.load(session_dir, run_id=1, lap_no=3)
        speed = vm.get_resampled_channel("Speed")
    """

    def __init__(self) -> None:
        self.meta: LapMeta | None = None
        self.track_xy: np.ndarray = np.empty((0, 2), dtype=np.float64)
        self.track_xy_source: str = "unavailable"
        self.track_xy_is_closed: bool = False
        self.lap_dist_pct: np.ndarray = np.empty(0, dtype=np.float64)
        self.corners: list[CornerInfo] = []
        self.track_road_geometry: TrackRoadGeometry | None = None
        self.events: dict[int, list[Event]] = {}
        self.corner_events: dict[int, list[Event]] = {}
        self.features: dict[int, dict[str, Any]] = {}
        self.track_length_m: float | None = None
        self._resampled_path: Path | None = None
        self._loaded: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def load(
        session_dir: Path | str,
        run_id: int,
        lap_no: int,
    ) -> "LapViewModel":
        """Load all analysis artefacts for the given lap.

        Null-safe: missing optional artefacts yield empty containers.
        Raises ``FileNotFoundError`` only if the lap directory does not exist.
        """
        vm = LapViewModel()
        session_dir = Path(session_dir)

        lap_dir = _resolve_lap_dir(session_dir, run_id, lap_no)
        analysis_dir = lap_dir / "analysis"

        vm._resampled_path = analysis_dir / "lap_resampled.parquet"
        vm.meta = _load_meta(session_dir, run_id, lap_no)
        geometry = _load_resampled_geometry(vm._resampled_path)
        vm.lap_dist_pct = geometry.lap_dist_pct
        vm.track_xy = geometry.track_xy
        vm.track_xy_source = geometry.source
        vm.track_xy_is_closed = geometry.is_closed
        vm.track_length_m = _load_track_length_m(session_dir)
        print(f"[LVM-DEBUG] track_length_m resolved: {vm.track_length_m!r}")
        vm.corners = _load_corners(session_dir, vm.track_length_m)
        road_geometry_result = _load_track_road_geometry(session_dir)
        vm.track_road_geometry = road_geometry_result.geometry
        _apply_saved_trackmap_geometry(vm, road_geometry_result)
        _log_track_geometry_debug(vm)
        vm.events = _load_events(analysis_dir / "lap_events.json", vm.corners)
        vm.corner_events = _load_corner_events(analysis_dir / "corner_events.json")
        vm.features = _load_features(analysis_dir / "corner_features.parquet")

        vm._loaded = True
        return vm

    def get_resampled_channel(self, channel: str) -> np.ndarray:
        """Return a channel from lap_resampled.parquet as float64 array (lazy).

        Returns an empty array if the channel is absent or the parquet missing.
        """
        if self._resampled_path is None or not self._resampled_path.exists():
            return np.empty(0, dtype=np.float64)
        try:
            table = pq.read_table(str(self._resampled_path), columns=[channel])
            return _col_to_float64(table.column(0).to_pylist())
        except Exception:
            return np.empty(0, dtype=np.float64)

    def is_loaded(self) -> bool:
        """True if ``load()`` completed successfully."""
        return self._loaded


# ---------------------------------------------------------------------------
# Internals – directory resolution
# ---------------------------------------------------------------------------


def _resolve_lap_dir(session_dir: Path, run_id: int, lap_no: int) -> Path:
    """Return the lap directory, supporting flat and nested layouts.

    Flat  (Sprint-1):  <session_dir>/laps/lap_XXXX/
    Nested (tests):    <session_dir>/run_XXXX/laps/lap_XXXX/
    """
    flat = session_dir / "laps" / f"lap_{lap_no:04d}"
    if flat.exists():
        return flat
    nested = session_dir / f"run_{run_id:04d}" / "laps" / f"lap_{lap_no:04d}"
    if nested.exists():
        return nested
    # Default to flat even if it does not exist yet (compute creates it)
    return flat


# ---------------------------------------------------------------------------
# Internals – meta loading
# ---------------------------------------------------------------------------


def _load_meta(session_dir: Path, run_id: int, lap_no: int) -> LapMeta:
    """Build LapMeta from session_meta.json and lap-level meta files."""
    session_meta = _read_json(session_dir / "session_meta.json")
    environment = resolve_session_environment(
        session_meta,
        session_info_yaml=_read_text(session_dir / "session_info.yaml"),
    )

    track = (
        _str_or(session_meta.get("TrackDisplayName"))
        or _str_or(session_meta.get("TrackName"))
        or "unknown_track"
    )
    car = (
        _str_or(session_meta.get("CarScreenName"))
        or _str_or(session_meta.get("DriverCarName"))
        or _str_or(session_meta.get("CarClassShortName"))
        or "unknown_car"
    )
    track_usage = _str_or(session_meta.get("TrackUsage"))
    if track_usage is None and isinstance(environment, dict):
        track_usage = _str_or(environment.get("track_usage"))

    lap_meta = _read_lap_meta(session_dir, run_id, lap_no)
    lap_time = _float_or(
        lap_meta.get("lap_time")
        or lap_meta.get("LapLastLapTime")
        or lap_meta.get("laptime")
    )
    # Fallback: derive from session timestamps present in Sprint-1 meta
    if lap_time is None:
        start_ts = _float_or(lap_meta.get("lap_start_ts"))
        end_ts = _float_or(lap_meta.get("lap_end_ts"))
        if start_ts is not None and end_ts is not None and end_ts > start_ts:
            lap_time = end_ts - start_ts
    valid = not (
        lap_meta.get("incomplete", False)
        or lap_meta.get("lap_incomplete", False)
        or not lap_meta.get("lap_complete", True)
        or lap_meta.get("offtrack", False)
        or lap_meta.get("offtrack_surface", False)
    )

    return LapMeta(
        track=track,
        car=car,
        lap_no=lap_no,
        lap_time=lap_time,
        valid=valid,
        environment=environment,
        track_usage=track_usage,
    )


def _read_lap_meta(session_dir: Path, run_id: int, lap_no: int) -> dict[str, Any]:
    """Try multiple meta file candidates; return first non-empty dict."""
    # Primary lookup: resolve the correct flat meta file via run_meta.json.
    # The flat files are named by a 1-based sequential counter, not iRacing lap_no,
    # so we must look up the right file by matching lap_no in lap_segments.
    run_meta = _read_json(session_dir / f"run_{run_id:04d}_meta.json")
    meta_files: list = run_meta.get("lap_meta_files", [])
    segments: list = run_meta.get("lap_segments", [])
    for idx, seg in enumerate(segments):
        if isinstance(seg, dict) and seg.get("lap_no") == lap_no and idx < len(meta_files):
            data = _read_json(session_dir / str(meta_files[idx]))
            if data:
                return data
            break

    candidates = [
        # Flat Sprint-1 per-lap meta (direct name — may be off-by-one vs iRacing lap_no)
        session_dir / f"run_{run_id:04d}_lap_{lap_no:04d}_meta.json",
        # Nested run-level lap meta
        session_dir / f"run_{run_id:04d}" / "laps" / f"lap_{lap_no:04d}" / "lap_meta.json",
        session_dir / "laps" / f"lap_{lap_no:04d}" / "lap_meta.json",
        session_dir / "laps" / f"lap_{lap_no:04d}" / "meta.json",
    ]
    for path in candidates:
        data = _read_json(path)
        if data:
            return data
    return {}


# ---------------------------------------------------------------------------
# Internals – geometry
# ---------------------------------------------------------------------------


def _load_resampled_geometry(
    parquet_path: Path,
) -> TrackGeometryData:
    """Return track geometry plus metadata from lap_resampled.parquet.

    track_xy is returned in the shared final display frame. Reconstruction priority:
      1. Direct X/Y world-frame columns.
      2. Lat/Lon projected to local meters.
      3. Dead-reckoning via Speed × cos/sin(Yaw) × dt.
         (iRacing VelocityX is forward velocity in vehicle frame.)
      4. Raw VelocityX/VelocityY integration as last resort.
    """
    if not parquet_path.exists():
        return _empty_track_geometry_data()

    try:
        wanted = [
            "LapDistPct",
            "SessionTime",
            "Speed",
            "Yaw",
            "VelocityX",
            "VelocityY",
            "X",
            "Y",
            "Lat",
            "Lon",
        ]
        schema_names = pq.ParquetFile(str(parquet_path)).schema_arrow.names
        present = [c for c in wanted if c in schema_names]
        table = pq.read_table(str(parquet_path), columns=present)
        data = {col: table.column(col).to_pylist() for col in present}
    except Exception:
        return _empty_track_geometry_data()

    n = len(data.get("LapDistPct", []))
    if n == 0:
        return _empty_track_geometry_data()

    ldp = _col_to_float64(data.get("LapDistPct", []))
    geometry = _reconstruct_track_geometry(data, n)
    geometry, orientation = _orient_track_geometry_for_display(geometry, ldp)
    _log_track_xy_orientation(geometry.source, orientation)
    return TrackGeometryData(
        lap_dist_pct=ldp,
        track_xy=geometry.track_xy,
        source=geometry.source,
        is_closed=geometry.is_closed,
    )


def _reconstruct_track_geometry(data: dict[str, Any], n: int) -> TrackGeometryData:
    """Reconstruct (N, 2) XY plus source metadata.

    Priority:
      1. Direct X/Y world-frame columns.
      2. Lat/Lon projected to local meters.
      3. Dead-reckoning: Speed × cos/sin(Yaw) × dt.
         (iRacing VelocityX is the car's forward velocity in vehicle frame,
         not a world-frame East component.)
      4. Raw VelocityX/VelocityY integration as last resort.
    """
    # Build dt from SessionTime
    st = _get_float_array(data, "SessionTime", n)
    if st is not None and st.size >= 2:
        dt = np.diff(st, prepend=st[0])
        dt = np.where(np.isfinite(dt) & (dt > 0), dt, np.nanmedian(np.diff(st)))
    else:
        dt = np.full(n, 0.01, dtype=np.float64)

    # 1 – Prefer direct XY
    x_raw = _get_float_array(data, "X", n)
    y_raw = _get_float_array(data, "Y", n)
    if x_raw is not None and y_raw is not None:
        return TrackGeometryData(
            lap_dist_pct=np.empty(0, dtype=np.float64),
            track_xy=np.column_stack([x_raw, y_raw]),
            source="xy",
            is_closed=_geometry_is_closed(x_raw, y_raw),
        )

    # 2 – Prefer Lat/Lon over reconstructed geometry
    lat = _get_float_array(data, "Lat", n)
    lon = _get_float_array(data, "Lon", n)
    latlon_xy = _project_latlon_to_xy(lat, lon)
    if latlon_xy is not None:
        return TrackGeometryData(
            lap_dist_pct=np.empty(0, dtype=np.float64),
            track_xy=latlon_xy,
            source="latlon",
            is_closed=_geometry_is_closed(latlon_xy[:, 0], latlon_xy[:, 1]),
        )

    # 3 – Dead-reckoning: Speed × cos/sin(Yaw)
    sp = _get_float_array(data, "Speed", n)
    yaw = _get_float_array(data, "Yaw", n)
    if sp is not None and yaw is not None:
        sp_safe = np.where(np.isfinite(sp), sp, 0.0)
        yaw_safe = np.where(np.isfinite(yaw), yaw, 0.0)
        x = np.cumsum(sp_safe * np.cos(yaw_safe) * dt)
        y = np.cumsum(-sp_safe * np.sin(yaw_safe) * dt)
        x, y = _close_loop(x, y)
        if np.ptp(x) > 1.0 or np.ptp(y) > 1.0:
            return TrackGeometryData(
                lap_dist_pct=np.empty(0, dtype=np.float64),
                track_xy=np.column_stack([x, y]),
                source="dead_reckoning",
                is_closed=_geometry_is_closed(x, y),
            )

    # 4 – Fallback: raw VelocityX/VelocityY
    vx = _get_float_array(data, "VelocityX", n)
    vy = _get_float_array(data, "VelocityY", n)
    if vx is None or vy is None:
        return TrackGeometryData(
            lap_dist_pct=np.empty(0, dtype=np.float64),
            track_xy=np.zeros((n, 2), dtype=np.float64),
            source="unavailable",
            is_closed=False,
        )
    x = np.cumsum(np.where(np.isfinite(vx), vx, 0.0) * dt)
    y = np.cumsum(-np.where(np.isfinite(vy), vy, 0.0) * dt)
    x, y = _close_loop(x, y)
    return TrackGeometryData(
        lap_dist_pct=np.empty(0, dtype=np.float64),
        track_xy=np.column_stack([x, y]),
        source="dead_reckoning",
        is_closed=_geometry_is_closed(x, y),
    )


def _empty_track_geometry_data() -> TrackGeometryData:
    return TrackGeometryData(
        lap_dist_pct=np.empty(0, dtype=np.float64),
        track_xy=np.empty((0, 2), dtype=np.float64),
        source="unavailable",
        is_closed=False,
    )


def _project_latlon_to_xy(
    lat: np.ndarray | None,
    lon: np.ndarray | None,
) -> np.ndarray | None:
    if lat is None or lon is None or len(lat) < 2 or len(lon) < 2:
        return None

    finite_mask = np.isfinite(lat) & np.isfinite(lon)
    if not np.any(finite_mask):
        return None

    first_idx = int(np.flatnonzero(finite_mask)[0])
    lat0_rad = math.radians(float(lat[first_idx]))
    lon0_rad = math.radians(float(lon[first_idx]))
    cos_lat0 = math.cos(lat0_rad)
    if abs(cos_lat0) < 1e-6:
        cos_lat0 = 1e-6 if cos_lat0 >= 0.0 else -1e-6

    r = 6378137.0
    x = np.full(len(lat), np.nan, dtype=np.float64)
    y = np.full(len(lat), np.nan, dtype=np.float64)
    lat_rad = np.radians(lat[finite_mask])
    lon_rad = np.radians(lon[finite_mask])
    x[finite_mask] = (lon_rad - lon0_rad) * cos_lat0 * r
    y[finite_mask] = (lat_rad - lat0_rad) * r
    return np.column_stack([x, y])


def _geometry_is_closed(x: np.ndarray, y: np.ndarray) -> bool:
    if len(x) < 2 or len(y) < 2:
        return False
    distance = _start_end_distance(x, y)
    if not math.isfinite(distance):
        return False
    try:
        span = math.hypot(
            float(np.nanmax(x) - np.nanmin(x)),
            float(np.nanmax(y) - np.nanmin(y)),
        )
    except ValueError:
        return False
    tolerance = min(5.0, max(0.5, 0.01 * span))
    return distance <= tolerance


def _start_end_distance(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 2 or len(y) < 2:
        return float("nan")
    x0 = float(x[0])
    y0 = float(y[0])
    x1 = float(x[-1])
    y1 = float(y[-1])
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
        return float("nan")
    return math.hypot(x1 - x0, y1 - y0)


def _log_track_geometry_debug(vm: LapViewModel) -> None:
    start_end_distance = float("nan")
    if len(vm.track_xy) >= 2:
        start_end_distance = _start_end_distance(vm.track_xy[:, 0], vm.track_xy[:, 1])
    print(
        "[TRACKMAP-DEBUG]"
        f" source={vm.track_xy_source}"
        f" is_closed={vm.track_xy_is_closed}"
        f" points={len(vm.track_xy)}"
        f" start_end_distance_m={start_end_distance:.3f}"
    )


def _orient_track_geometry_for_display(
    geometry: TrackGeometryData,
    lap_dist_pct: np.ndarray,
) -> tuple[TrackGeometryData, TrackDisplayOrientationResult | None]:
    if len(geometry.track_xy) == 0:
        return geometry, None

    orientation = orient_track_display_frame(geometry.track_xy, lap_dist_pct)
    return (
        TrackGeometryData(
            lap_dist_pct=geometry.lap_dist_pct,
            track_xy=orientation.xy,
            source=geometry.source,
            is_closed=geometry.is_closed,
        ),
        orientation,
    )


def _log_track_xy_orientation(
    track_xy_source: str,
    orientation: TrackDisplayOrientationResult | None,
) -> None:
    if orientation is None:
        _LOG.info(
            "[track_xy_orientation] track_xy_source=%s normalisation_applied=no normalisation_rule=none applied_rotation_deg=nan applied_mirror_x=no orientation_transform=identity(empty_track_xy)",
            track_xy_source,
        )
        return

    _LOG.info(
        "[track_xy_orientation] track_xy_source=%s normalisation_applied=%s normalisation_rule=%s start_anchor_lap_dist_pct=%s applied_rotation_deg=%s applied_mirror_x=%s orientation_transform=%s",
        track_xy_source,
        "yes" if orientation.applied else "no",
        orientation.rule,
        "nan" if orientation.start_anchor_lap_dist_pct is None else f"{orientation.start_anchor_lap_dist_pct:.6f}",
        "nan" if orientation.rotation_deg is None else f"{orientation.rotation_deg:.6f}",
        "yes" if orientation.mirrored else "no",
        orientation.transform,
    )
    _LOG.debug(
        "[track_xy_orientation_debug] track_xy_source=%s before_first=%s before_last=%s after_first=%s after_last=%s start_tangent_before=%s",
        track_xy_source,
        orientation.before_first,
        orientation.before_last,
        orientation.after_first,
        orientation.after_last,
        orientation.start_tangent_before,
    )


def _normalise_xy(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Normalise x and y to [0, 1] preserving aspect ratio."""
    x_min, x_max = float(np.nanmin(x)), float(np.nanmax(x))
    y_min, y_max = float(np.nanmin(y)), float(np.nanmax(y))
    x_range = x_max - x_min or 1.0
    y_range = y_max - y_min or 1.0
    scale = max(x_range, y_range)
    xn = (x - x_min) / scale
    yn = (y - y_min) / scale
    return np.column_stack([xn, yn])


def _close_loop(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    n = len(x)
    if n < 2:
        return x, y
    t = np.arange(n, dtype=np.float64) / n
    return x - t * (x[-1] - x[0]), y - t * (y[-1] - y[0])


# ---------------------------------------------------------------------------
# Internals – corners
# ---------------------------------------------------------------------------


def _load_corners(session_dir: Path, track_length_m: float | None) -> list[CornerInfo]:
    """Load corners from corner_map_v1.json and apply runtime padding."""
    coaching_root = _coaching_storage_root(session_dir)
    corner_map = None
    for track_key in _track_key_candidates(session_dir):
        corner_map = load_corner_map(storage_root=coaching_root, track_key=track_key)
        if corner_map is not None:
            break
    if corner_map is None:
        return []

    corners: list[CornerInfo] = []
    for entry in corner_map.get("corners", []):
        try:
            corners.append(
                CornerInfo(
                    corner_id=int(entry["corner_id"]),
                    start_lapdist_pct=float(entry["start_lapdist_pct"]),
                    end_lapdist_pct=float(entry["end_lapdist_pct"]),
                    corner_type=str(entry.get("corner_type", "unknown")),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue

    entry_m, exit_m = _read_corner_map_padding_m()
    return apply_corner_padding(corners, entry_m, exit_m, track_length_m)


# ---------------------------------------------------------------------------
# Internals – events
# ---------------------------------------------------------------------------


def _load_events(
    events_path: Path,
    corners: list[CornerInfo],
) -> dict[int, list[Event]]:
    """Load lap_events.json and group events by corner_id.

    Events outside any corner are placed under key 0 (lap-global).
    """
    result: dict[int, list[Event]] = {}
    if not events_path.exists():
        return result

    raw = _read_json(events_path)
    events_section = raw.get("events", {})
    if not isinstance(events_section, dict):
        return result

    all_events: list[Event] = []
    for event_type, payload in events_section.items():
        if payload is None:
            continue
        if isinstance(payload, list):
            for item in payload:
                ev = _parse_event(event_type, item)
                if ev is not None:
                    all_events.append(ev)
        elif isinstance(payload, dict):
            ev = _parse_event(event_type, payload)
            if ev is not None:
                all_events.append(ev)

    for ev in all_events:
        cid = _find_corner_id(ev.lapdist_pct, corners)
        result.setdefault(cid, []).append(ev)

    return result


def _parse_event(event_type: str, data: dict[str, Any]) -> Event | None:
    """Parse one event dict into an Event dataclass."""
    ldp = _float_or(data.get("lapdist_pct"))
    if ldp is None:
        return None
    return Event(
        event_type=event_type,
        lapdist_pct=ldp,
        value=data.get("value"),
        session_time=_float_or(data.get("session_time")),
    )


def _find_corner_id(lapdist_pct: float, corners: list[CornerInfo]) -> int:
    """Return the corner_id whose range contains lapdist_pct, else 0.

    Uses ``padded_start/end_lapdist_pct`` when available so that events
    near the corner boundary (e.g. brake_start before entry) are grouped
    under the correct corner rather than the lap-global bucket (id 0).
    """
    for corner in corners:
        lo = (
            corner.padded_start_lapdist_pct
            if corner.padded_start_lapdist_pct is not None
            else corner.start_lapdist_pct
        )
        hi = (
            corner.padded_end_lapdist_pct
            if corner.padded_end_lapdist_pct is not None
            else corner.end_lapdist_pct
        )
        if lo <= lapdist_pct <= hi:
            return corner.corner_id
    return 0


# ---------------------------------------------------------------------------
# Internals – corner_events
# ---------------------------------------------------------------------------


def _load_corner_events(corner_events_path: Path) -> dict[int, list[Event]]:
    """Load corner_events.json and return a dict of corner_id → list[Event].

    Returns an empty dict when the file is absent or cannot be parsed.
    """
    result: dict[int, list[Event]] = {}
    if not corner_events_path.exists():
        return result

    raw = _read_json(corner_events_path)
    corners_section = raw.get("corners", {})
    if not isinstance(corners_section, dict):
        return result

    for corner_id_str, event_list in corners_section.items():
        cid = _int_or(corner_id_str)
        if cid is None or not isinstance(event_list, list):
            continue
        events: list[Event] = []
        for item in event_list:
            if not isinstance(item, dict):
                continue
            ev = _parse_event(item.get("name", ""), item)
            if ev is not None:
                events.append(ev)
        result[cid] = events

    return result


# ---------------------------------------------------------------------------
# Internals – features
# ---------------------------------------------------------------------------


def _load_features(features_path: Path) -> dict[int, dict[str, Any]]:
    """Load corner_features.parquet and index by corner_id."""
    result: dict[int, dict[str, Any]] = {}
    if not features_path.exists():
        return result

    try:
        table = pq.read_table(str(features_path))
        records = table.to_pylist()
    except Exception:
        return result

    for row in records:
        if not isinstance(row, dict):
            continue
        cid = _int_or(row.get("corner_id"))
        if cid is None:
            continue
        result[cid] = row

    return result


# ---------------------------------------------------------------------------
# Internals – corner padding
# ---------------------------------------------------------------------------


def _read_corner_map_padding_m() -> tuple[float, float]:
    """Return (entry_padding_m, exit_padding_m) from config/defaults.ini."""
    cp = configparser.ConfigParser()
    cp.read(_PROJECT_ROOT / "config" / "defaults.ini", encoding="utf-8-sig")
    try:
        entry = float(cp.get("coaching_analysis", "corner_map_entry_padding_m", fallback="80"))
        entry = max(0.0, min(10000.0, entry))
    except Exception:
        entry = 80.0
    try:
        exit_ = float(cp.get("coaching_analysis", "corner_map_exit_padding_m", fallback="40"))
        exit_ = max(0.0, min(10000.0, exit_))
    except Exception:
        exit_ = 40.0
    return entry, exit_


def apply_corner_padding(
    corners: list[CornerInfo],
    entry_m: float,
    exit_m: float,
    track_length_m: float | None,
) -> list[CornerInfo]:
    """Return *corners* with ``padded_start/end_lapdist_pct`` populated.

    The original ``start/end_lapdist_pct`` fields are never modified so that
    geometry rendering and feature extraction remain unaffected.

    When *track_length_m* is ``None`` or zero the padding falls back to
    fractional defaults (entry=0.015, exit=0.008), matching ``event_engine``.
    """
    if not track_length_m or track_length_m <= 0:
        entry_pct = 0.015
        exit_pct = 0.008
    else:
        entry_pct = entry_m / track_length_m
        exit_pct = exit_m / track_length_m

    return [
        _dataclass_replace(
            c,
            padded_start_lapdist_pct=max(0.0, c.start_lapdist_pct - entry_pct),
            padded_end_lapdist_pct=min(1.0, c.end_lapdist_pct + exit_pct),
        )
        for c in corners
    ]


# ---------------------------------------------------------------------------
# Internals – track length
# ---------------------------------------------------------------------------


def _load_track_length_m(session_dir: Path) -> float | None:
    """Liest TrackLength aus session_info.yaml (WeekendInfo.TrackLength).
    Format in iRacing: '4.062 km' oder '2.524 mi'
    Fallback: None (kein Crash)
    """
    yaml_path = session_dir / "session_info.yaml"
    if not yaml_path.exists():
        print(f"[LVM-DEBUG] session_info.yaml nicht gefunden: {yaml_path}")
        return None
    try:
        import yaml

        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        raw = ((data or {}).get("WeekendInfo", {}).get("TrackLength"))
        print(f"[LVM-DEBUG] WeekendInfo.TrackLength raw: {raw!r}")
        return _parse_track_length_m_str(raw)
    except ImportError:
        print("[LVM-DEBUG] PyYAML nicht verfügbar")
        return None
    except Exception as exc:
        print(f"[LVM-DEBUG] session_info.yaml Lesefehler: {exc}")
        return None


def _load_track_road_geometry(session_dir: Path) -> TrackRoadGeometryLoadResult:
    """Load track_road_geometry.json and report the exact selection outcome."""
    storage_root = _coaching_storage_root(session_dir)
    candidate_keys = _track_key_candidates(session_dir)
    last_invalid: TrackRoadGeometryLoadResult | None = None

    for track_key in candidate_keys:
        for path in _track_road_geometry_paths(storage_root, track_key):
            payload = _read_json(path)
            if not payload:
                continue
            result = _coerce_track_road_geometry(payload, fallback_track_key=track_key, geometry_path=path)
            _log_track_road_geometry_result(result)
            if result.geometry is not None:
                return result
            last_invalid = result

    primary_track_key = candidate_keys[0] if candidate_keys else "unknown_track"
    miss = TrackRoadGeometryLoadResult(
        geometry=None,
        requested_track_key=primary_track_key,
        geometry_path=_track_road_geometry_paths(storage_root, primary_track_key)[0],
        payload_track_key=None,
        source_type="unknown",
        center_line_points=0,
        left_edge_points=0,
        right_edge_points=0,
        center_line_format="none",
        reason="geometry_not_found",
    )
    if last_invalid is not None:
        return last_invalid
    _log_track_road_geometry_result(miss)
    return miss


def _coaching_storage_root(session_dir: Path) -> Path:
    return session_dir.parent


def _track_key_candidates(session_dir: Path) -> list[str]:
    """Return the primary coaching track key plus backward-compat legacy keys."""
    from .storage import sanitize_name

    session_meta = _read_json(session_dir / "session_meta.json")
    parts = session_dir.name.split("__")

    track_name = (
        _str_or(session_meta.get("TrackDisplayName"))
        or _str_or(session_meta.get("TrackName"))
        or (parts[2] if len(parts) >= 6 else None)
        or "unknown_track"
    )
    config_name = (
        _str_or(session_meta.get("TrackConfigName"))
        or _str_or(session_meta.get("TrackConfig"))
        or ""
    )
    car_class = (
        _str_or(session_meta.get("CarClassShortName"))
        or _str_or(session_meta.get("CarClassID"))
        or _str_or(session_meta.get("CarClass"))
        or ""
    )

    primary = build_track_key(track_name, config_name)

    candidates = [primary]

    # Altes Format für Backward-Compat (bestehende corner_maps und andere Assets)
    old_primary = (
        f"{sanitize_name(track_name)}"
        f"__{sanitize_name(config_name)}"
        f"__{sanitize_name(car_class)}"
    )
    old_legacy = f"{sanitize_name(track_name)}__{sanitize_name(config_name)}"
    for k in (old_primary, old_legacy):
        if k not in candidates:
            candidates.append(k)

    return candidates


def _track_road_geometry_paths(storage_root: Path, track_key: str) -> list[Path]:
    return [storage_root / "track_geometries" / track_key / "track_road_geometry.json"]


def _coerce_track_road_geometry(
    payload: dict[str, Any],
    *,
    fallback_track_key: str,
    geometry_path: Path,
) -> TrackRoadGeometryLoadResult:
    if not payload:
        return TrackRoadGeometryLoadResult(
            geometry=None,
            requested_track_key=fallback_track_key,
            geometry_path=geometry_path,
            payload_track_key=None,
            source_type="unknown",
            center_line_points=0,
            left_edge_points=0,
            right_edge_points=0,
            center_line_format="none",
            reason="empty_payload",
        )

    center_line_count = _count_xy_points(payload.get("center_line"))
    left_edge_count = _count_xy_points(payload.get("left_edge"))
    right_edge_count = _count_xy_points(payload.get("right_edge"))
    payload_track_key = _str_or(payload.get("track_key"))
    source_type = _track_geometry_source_type(payload)

    center_line, center_line_format = _coerce_xy_points(payload.get("center_line"))
    left_edge, _ = _coerce_xy_points(payload.get("left_edge"), allow_empty=True)
    right_edge, _ = _coerce_xy_points(payload.get("right_edge"), allow_empty=True)
    if center_line is None or left_edge is None or right_edge is None:
        return TrackRoadGeometryLoadResult(
            geometry=None,
            requested_track_key=fallback_track_key,
            geometry_path=geometry_path,
            payload_track_key=payload_track_key,
            source_type=source_type,
            center_line_points=center_line_count,
            left_edge_points=left_edge_count,
            right_edge_points=right_edge_count,
            center_line_format=center_line_format,
            reason="invalid_geometry_payload",
        )

    if payload_track_key and payload_track_key != fallback_track_key:
        return TrackRoadGeometryLoadResult(
            geometry=None,
            requested_track_key=fallback_track_key,
            geometry_path=geometry_path,
            payload_track_key=payload_track_key,
            source_type=source_type,
            center_line_points=center_line_count,
            left_edge_points=left_edge_count,
            right_edge_points=right_edge_count,
            center_line_format=center_line_format,
            reason="track_key_mismatch",
        )

    track_key = payload_track_key or fallback_track_key
    geometry: TrackRoadGeometry = {
        "track_key": track_key,
        "source_type": source_type,
        "center_line": center_line,
        "left_edge": left_edge,
        "right_edge": right_edge,
    }
    return TrackRoadGeometryLoadResult(
        geometry=geometry,
        requested_track_key=fallback_track_key,
        geometry_path=geometry_path,
        payload_track_key=payload_track_key,
        source_type=source_type,
        center_line_points=len(center_line),
        left_edge_points=len(left_edge),
        right_edge_points=len(right_edge),
        center_line_format=center_line_format,
        reason="accepted",
    )


def _coerce_xy_points(
    value: Any,
    *,
    allow_empty: bool = False,
) -> tuple[list[list[float]] | None, str]:
    if value == [] and allow_empty:
        return [], "empty"
    if not isinstance(value, list) or len(value) < 2:
        return None, "none"

    points: list[list[float]] = []
    point_formats: set[str] = set()
    for item in value:
        point, point_format = _coerce_xy_point(item)
        if point is None or point_format is None:
            return None, "invalid"
        points.append(point)
        point_formats.add(point_format)
    return points, _point_format_name(point_formats)


def _coerce_xy_point(item: Any) -> tuple[list[float] | None, str | None]:
    if isinstance(item, (list, tuple)) and len(item) == 2:
        x = _float_or(item[0])
        y = _float_or(item[1])
        if x is None or y is None:
            return None, None
        return [x, y], "xy_pairs"
    if isinstance(item, dict):
        x = _float_or(item.get("x_m"))
        y = _float_or(item.get("y_m"))
        if x is None or y is None:
            return None, None
        return [x, y], "x_m_y_m_dicts"
    return None, None


def _point_format_name(point_formats: set[str]) -> str:
    if not point_formats:
        return "none"
    if len(point_formats) == 1:
        return next(iter(point_formats))
    return "mixed"


def _count_xy_points(value: Any) -> int:
    if not isinstance(value, list):
        return 0
    return sum(1 for item in value if _coerce_xy_point(item)[0] is not None)


def _track_geometry_source_type(payload: dict[str, Any]) -> str:
    source_type = str(payload.get("source_type") or "").strip().lower()
    if source_type in {"ibt", "fallback"}:
        return source_type

    legacy_source = str(payload.get("source") or payload.get("source_name") or "").strip().lower()
    if legacy_source in {"ibt", "ibt_telemetry"}:
        return "ibt"
    if legacy_source in {"parquet_fallback", "parquet_velocity_integration", "dead_reckoning"}:
        return "fallback"
    if "fallback" in legacy_source:
        return "fallback"
    return "unknown"


def _log_track_road_geometry_result(result: TrackRoadGeometryLoadResult) -> None:
    _LOG.info(
        "[track_geometry_consumer] track_key=%s geometry_path=%s payload_track_key=%s center_line_points=%d left_edge_points=%d right_edge_points=%d center_line_format=%s ibt_geometry_accepted=%s fallback_used=%s reason=%s",
        result.requested_track_key,
        result.geometry_path,
        result.payload_track_key or "-",
        result.center_line_points,
        result.left_edge_points,
        result.right_edge_points,
        result.center_line_format,
        "yes" if result.geometry is not None and result.source_type == "ibt" else "no",
        "yes" if (result.geometry is None or result.source_type == "fallback") else "no",
        result.reason,
    )


def _trackmap_source_name(source_type: str) -> str:
    if source_type == "ibt":
        return "track_geometries_ibt"
    if source_type == "fallback":
        return "track_geometries_fallback"
    return "track_geometries_saved"


def _road_geometry_mode_from_counts(
    center_line_points: int,
    left_edge_points: int,
    right_edge_points: int,
) -> str:
    if center_line_points >= 2 and left_edge_points >= 2 and right_edge_points >= 2:
        return "band"
    if center_line_points >= 2:
        return "center_line_only"
    return "none"


def _apply_saved_trackmap_geometry(vm: LapViewModel, result: TrackRoadGeometryLoadResult) -> None:
    lap_geometry_source = vm.track_xy_source
    if result.geometry is None:
        _LOG.info(
            "[trackmap_source] track_key=%s geometry_path=%s center_line_points=%d left_edge_points=%d right_edge_points=%d center_line_format=%s ibt_geometry_accepted=no fallback_used=yes trackmap_source=dead_reckoning_live_fallback lap_geometry_source=%s road_geometry_mode=none",
            result.requested_track_key,
            result.geometry_path,
            result.center_line_points,
            result.left_edge_points,
            result.right_edge_points,
            result.center_line_format,
            lap_geometry_source,
        )
        return

    trackmap_source = _trackmap_source_name(result.source_type)
    road_geometry_mode = _road_geometry_mode_from_counts(
        result.center_line_points,
        result.left_edge_points,
        result.right_edge_points,
    )

    _LOG.info(
        "[trackmap_source] track_key=%s geometry_path=%s center_line_points=%d left_edge_points=%d right_edge_points=%d center_line_format=%s ibt_geometry_accepted=%s fallback_used=%s trackmap_source=%s lap_geometry_source=%s road_geometry_mode=%s",
        result.requested_track_key,
        result.geometry_path,
        result.center_line_points,
        result.left_edge_points,
        result.right_edge_points,
        result.center_line_format,
        "yes" if result.source_type == "ibt" else "no",
        "yes" if result.source_type == "fallback" else "no",
        trackmap_source,
        lap_geometry_source,
        road_geometry_mode,
    )


def _parse_track_length_m_str(raw) -> float | None:
    """Parst iRacing TrackLength-String in Meter."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip().lower()
    try:
        if "km" in s:
            return float(s.replace("km", "").strip()) * 1000.0
        if "mi" in s or "mile" in s:
            return (
                float(s.replace("miles", "").replace("mile", "").replace("mi", "").strip())
                * 1609.344
            )
        return float(s)
    except ValueError:
        print(f"[LVM-DEBUG] TrackLength Parse-Fehler: {raw!r}")
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON file; return empty dict on any error."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _col_to_float64(values: list[Any]) -> np.ndarray:
    """Convert a Python list to a float64 numpy array (NaN for non-finite)."""
    out = np.empty(len(values), dtype=np.float64)
    for i, v in enumerate(values):
        if v is None:
            out[i] = np.nan
            continue
        try:
            f = float(v)
            out[i] = f if math.isfinite(f) else np.nan
        except Exception:
            out[i] = np.nan
    return out


def _get_float_array(data: dict[str, Any], col: str, n: int) -> np.ndarray | None:
    """Return column as float64 array or None if absent / all-NaN."""
    if col not in data:
        return None
    arr = _col_to_float64(data[col][:n])
    return arr if np.any(np.isfinite(arr)) else None


def _str_or(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_environment(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    copied = dict(value)
    return copied or None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _float_or(value: Any) -> float | None:
    try:
        f = float(value)
        return f if math.isfinite(f) else None
    except Exception:
        return None


def _int_or(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None
