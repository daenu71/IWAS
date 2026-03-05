"""LapViewModel – Datenzugriffs-Layer für Visualisierungs-UI (Story 3.0).

Einzige Schnittstelle zwischen Analyse-Backend und Visualisierungs-UI.
Sprint 4/5/6 erweitern dieses Modell ohne die UI anfassen zu müssen.

Artefakt-Pfade (flat Sprint-1 Struktur):
    <session_dir>/laps/lap_XXXX/analysis/lap_resampled.parquet
    <session_dir>/laps/lap_XXXX/analysis/lap_events.json
    <session_dir>/laps/lap_XXXX/analysis/corner_features.parquet
    <session_dir>/corner_maps/<track_key>/corner_map_v1.json
    <session_dir>/session_meta.json
    <session_dir>/run_XXXX_lap_YYYY_meta.json
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    """Minimal corner descriptor from corner_map_v1.json."""

    corner_id: int
    start_lapdist_pct: float
    end_lapdist_pct: float
    corner_type: str


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
        self.features: dict[int, dict[str, Any]] = {}
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
        vm.corners = _load_corners(session_dir)
        vm.events = _load_events(analysis_dir / "lap_events.json", vm.corners)
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
    candidates = [
        # Flat Sprint-1 per-lap meta
        session_dir / f"run_{run_id:04d}_lap_{lap_no:04d}_meta.json",
        # Nested run-level lap meta
        session_dir / f"run_{run_id:04d}" / "laps" / f"lap_{lap_no:04d}" / "lap_meta.json",
        session_dir / "laps" / f"lap_{lap_no:04d}" / "lap_meta.json",
        session_dir / "laps" / f"lap_{lap_no:04d}" / "meta.json",
        # Run-level meta
        session_dir / f"run_{run_id:04d}_meta.json",
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

    track_xy is (N, 2) normalised to [0, 1] via VelocityX/Y integration.
    Falls back to zeros if channels are absent.
    """
    if not parquet_path.exists():
        return np.empty(0, dtype=np.float64), np.empty((0, 2), dtype=np.float64)

    try:
        wanted = ["LapDistPct", "SessionTime", "VelocityX", "VelocityY", "X", "Y"]
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
    """Reconstruct normalised (N, 2) XY from velocity integration or XY columns."""
    # Prefer direct XY if available
    x_raw = _get_float_array(data, "X", n)
    y_raw = _get_float_array(data, "Y", n)
    if x_raw is not None and y_raw is not None:
        return _normalise_xy(x_raw, y_raw)

    # Integrate VelocityX/Y with dt from SessionTime
    vx = _get_float_array(data, "VelocityX", n)
    vy = _get_float_array(data, "VelocityY", n)
    st = _get_float_array(data, "SessionTime", n)
    if vx is None or vy is None:
        return np.zeros((n, 2), dtype=np.float64)

    if st is not None and st.size >= 2:
        dt = np.diff(st, prepend=st[0])
        dt = np.where(np.isfinite(dt) & (dt > 0), dt, np.nanmedian(np.diff(st)))
    else:
        dt = np.full(n, 0.01, dtype=np.float64)  # assume 100 Hz

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


def _load_corners(session_dir: Path) -> list[CornerInfo]:
    """Load corners from corner_map_v1.json."""
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
    track_key = f"{sanitize_name(track_name)}__{sanitize_name(config_name)}"

    corner_map = load_corner_map(storage_root=session_dir, track_key=track_key)
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
    return corners


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
    """Return the corner_id whose range contains lapdist_pct, else 0."""
    for corner in corners:
        if corner.start_lapdist_pct <= lapdist_pct <= corner.end_lapdist_pct:
            return corner.corner_id
    return 0


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
