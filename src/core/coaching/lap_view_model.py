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
import math
from dataclasses import dataclass, field, replace as _dataclass_replace
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).parent.parent.parent

import numpy as np
import pyarrow.parquet as pq

from .corner_map import load_corner_map
from .storage import sanitize_name


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
        self.lap_dist_pct: np.ndarray = np.empty(0, dtype=np.float64)
        self.corners: list[CornerInfo] = []
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
        vm.lap_dist_pct, vm.track_xy = _load_resampled_geometry(vm._resampled_path)
        vm.track_length_m = _load_track_length_m(session_dir)
        vm.corners = _load_corners(session_dir, vm.track_length_m)
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

    return LapMeta(track=track, car=car, lap_no=lap_no, lap_time=lap_time, valid=valid)


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
) -> tuple[np.ndarray, np.ndarray]:
    """Return (lap_dist_pct, track_xy) from lap_resampled.parquet.

    track_xy is (N, 2) normalised to [0, 1].  XY reconstruction priority:
      1. Direct X/Y world-frame columns.
      2. Dead-reckoning via Speed × cos/sin(Yaw) × dt.
         (iRacing VelocityX is forward velocity in vehicle frame.)
      3. Raw VelocityX/VelocityY integration as last resort.
    """
    if not parquet_path.exists():
        return np.empty(0, dtype=np.float64), np.empty((0, 2), dtype=np.float64)

    try:
        wanted = ["LapDistPct", "SessionTime", "Speed", "Yaw", "VelocityX", "VelocityY", "X", "Y"]
        schema_names = pq.ParquetFile(str(parquet_path)).schema_arrow.names
        present = [c for c in wanted if c in schema_names]
        table = pq.read_table(str(parquet_path), columns=present)
        data = {col: table.column(col).to_pylist() for col in present}
    except Exception:
        return np.empty(0, dtype=np.float64), np.empty((0, 2), dtype=np.float64)

    n = len(data.get("LapDistPct", []))
    if n == 0:
        return np.empty(0, dtype=np.float64), np.empty((0, 2), dtype=np.float64)

    ldp = _col_to_float64(data.get("LapDistPct", []))
    track_xy = _reconstruct_xy(data, n)
    return ldp, track_xy


def _reconstruct_xy(data: dict[str, Any], n: int) -> np.ndarray:
    """Reconstruct normalised (N, 2) XY.

    Priority:
      1. Direct X/Y world-frame columns.
      2. Dead-reckoning: Speed × cos/sin(Yaw) × dt.
         (iRacing VelocityX is the car's forward velocity in vehicle frame,
         not a world-frame East component.)
      3. Raw VelocityX/VelocityY integration as last resort.
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
        return _normalise_xy(x_raw, y_raw)

    # 2 – Dead-reckoning: Speed × cos/sin(Yaw)
    sp = _get_float_array(data, "Speed", n)
    yaw = _get_float_array(data, "Yaw", n)
    if sp is not None and yaw is not None:
        sp_safe = np.where(np.isfinite(sp), sp, 0.0)
        yaw_safe = np.where(np.isfinite(yaw), yaw, 0.0)
        x = np.cumsum(sp_safe * np.cos(yaw_safe) * dt)
        y = np.cumsum(sp_safe * np.sin(yaw_safe) * dt)
        if np.ptp(x) > 1.0 or np.ptp(y) > 1.0:
            return _normalise_xy(x, y)

    # 3 – Fallback: raw VelocityX/VelocityY
    vx = _get_float_array(data, "VelocityX", n)
    vy = _get_float_array(data, "VelocityY", n)
    if vx is None or vy is None:
        return np.zeros((n, 2), dtype=np.float64)
    x = np.cumsum(vx * dt)
    y = np.cumsum(vy * dt)
    return _normalise_xy(x, y)


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


# ---------------------------------------------------------------------------
# Internals – corners
# ---------------------------------------------------------------------------


def _load_corners(session_dir: Path, track_length_m: float | None) -> list[CornerInfo]:
    """Load corners from corner_map_v1.json and apply runtime padding."""
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
        or "unknown_config"
    )
    car_class = _str_or(session_meta.get("CarClassShortName")) or "unknown_class"
    track_key = (
        f"{sanitize_name(track_name)}"
        f"__{sanitize_name(config_name)}"
        f"__{sanitize_name(car_class)}"
    )

    coaching_root = session_dir.parent
    corner_map = load_corner_map(storage_root=coaching_root, track_key=track_key)
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
    """Return track length in metres from session_meta.json, or None.

    iRacing stores TrackLength either as a string with unit (for example
    "4.062 km") or as a numeric metre value, depending on the version.
    """
    session_meta = _read_json(session_dir / "session_meta.json")
    return _parse_track_length_m(session_meta)


def _parse_track_length_m(session_meta: dict[str, Any]) -> float | None:
    raw = session_meta.get("TrackLength") or session_meta.get("track_length")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if value > 0.0 else None

    s = str(raw).strip().lower()
    if not s:
        return None
    try:
        if "km" in s:
            value = float(s.replace("km", "").strip()) * 1000.0
        elif "miles" in s:
            value = float(s.replace("miles", "").strip()) * 1609.344
        elif "mile" in s:
            value = float(s.replace("mile", "").strip()) * 1609.344
        elif "mi" in s:
            value = float(s.replace("mi", "").strip()) * 1609.344
        elif s.endswith("m"):
            value = float(s[:-1].strip())
        else:
            value = float(s)
    except ValueError:
        return None
    return value if value > 0.0 else None


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
