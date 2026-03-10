"""track_geometry.py – XY-Rekonstruktion und TrackMap-Rendering (Story 3.1).

Öffentliche API
---------------
reconstruct_xy(resampled_df)
    Integriert VelocityX/Y (oder liest X/Y direkt) aus einem Resampled-DataFrame
    und gibt ein auf [0, 1] normiertes (N, 2) numpy-Array zurück.

render_trackmap(canvas, xy, corners, selected_corner_id, width, height, ...)
    Zeichnet die TrackMap auf einem tk.Canvas:
      - Streckenlinie (grau #555)
      - Corner-Segmente (farbig, stipple-halbtransparent)
      - Fahrlinie (Primärfarbe, on top)
      - Corner-Label am Apex
      - click-bindings via canvas.tag_bind
"""

from __future__ import annotations

import logging
import math
import tkinter as tk
from typing import TYPE_CHECKING, Callable, List, NamedTuple, Optional, TypedDict

import numpy as np
from PIL import Image, ImageDraw
from PIL.ImageTk import PhotoImage as PILPhotoImage
from core import persistence

if TYPE_CHECKING:
    import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LOG = logging.getLogger(__name__)

_PADDING = 0.06          # fraction of canvas dimension reserved per edge
_TRACK_COLOR = "#555555"
_TRACK_WIDTH = 1
_LAP_WIDTH = 2
_SEG_WIDTH = 7           # corner-segment highlight width (px)
_ROAD_FILL_COLOR = "#2D2D2D"
_ROAD_EDGE_COLOR = "#8A8A8A"
_ROAD_EDGE_WIDTH = 1
_CORNER_COLOR = "#FFD700"   # default corner colour
_SELECTED_COLOR = "#FFFFFF" # selected corner colour
_LABEL_FONT = ("Arial", 12, "bold")
_LABEL_OFFSET = 13       # px above midpoint

# Corner-Zoom constants
_ZOOM_PADDING = 0.10
_ZOOM_LINE_COLOR = "#E53935"
_ZOOM_LINE_WIDTH = 3
_ZOOM_MARKER_FONT = ("Arial", 17)
_ZOOM_LEGEND_FONT = ("Arial", 11)
_ZOOM_TOOLTIP_FONT = ("Arial", 8)
EVENT_SYMBOL_SIZE = 20   # px; event symbol size used for collision detection
LEGEND_SYMBOL_SIZE = EVENT_SYMBOL_SIZE  # px; legend symbol size

_FIT_LEGEND_WIDTH = 160
_FIT_PADDING_LEFT = 10
_FIT_PADDING_RIGHT = 10
_FIT_PADDING_TOP = 40
_FIT_PADDING_BOTTOM = 10

_EVENT_STYLE: dict = {
    "brake_start":     ("▼", "#CC2222"),
    "peak_brake":      ("●", "#770000"),
    "turn_in_rate_based":  ("◀", "#FF8800"),
    "turn_in_angle_based": ("◂", "#FFAA44"),
    "min_speed":       ("★", "#FFDD00"),
    "throttle_on":     ("▲", "#88FF44"),
    # Default ▲ to match the current requirement; switch to ▽ if a distinct lift marker is preferred.
    "throttle_off":    ("▲", "#88FF88"),
    "throttle_full":   ("▲", "#00CC00"),
    "gear_change":     ("⬡", "#4488FF"),
    "oversteer_event":  ("⚠", "#FF44FF"),
    "understeer_event": ("⚠", "#FF8000"),
    "crest":            ("⌒", "#00DDFF"),
    # Incident events (PlayerCarMyIncidentCount jumps)
    "offtrack_incident":  ("✕", "#CC0000"),
    "loose_control":      ("↻", "#CC0000"),
    "crash":              ("⚡", "#CC0000"),
    # Chicane / compound-corner transition
    "steering_crossover": ("↔", "#4FC3F7"),
}

# Symbol types that need a contrast background (hollow / low-contrast glyphs only)
_BG_SYMBOL_TYPES = {
    "gear_change", "oversteer_event", "understeer_event", "crest", "peak_brake",
    "offtrack_incident", "loose_control", "crash",
}

# Short display labels for legend and tooltip (override default replace("_"," "))
_EVENT_LABELS: dict = {
    "oversteer_event":   "oversteer",
    "understeer_event":  "understeer",
    "offtrack_incident": "off-track",
    "loose_control":     "loose ctrl",
    "crash":             "crash",
}

_TOOLTIP_TITLES: dict[str, str] = {
    "peak_brake": "brake peak",
}

_TOOLTIP_TRACK_LENGTH_DEBUG_PRINTED = False


def _fmt_lapdist_m(lapdist_pct: float, track_length_m: float | None) -> str:
    """LapDistPct -> metres, fallback to raw pct if track length is unavailable."""
    try:
        lapdist = float(lapdist_pct)
        track_length = float(track_length_m) if track_length_m is not None else math.nan
    except Exception:
        return str(lapdist_pct)
    if math.isfinite(track_length) and track_length > 0.0:
        return f"{lapdist * track_length:.2f}m"
    return f"{lapdist:.4f}"


def _log_tooltip_track_length_once(track_length_m: float | None) -> None:
    global _TOOLTIP_TRACK_LENGTH_DEBUG_PRINTED
    if _TOOLTIP_TRACK_LENGTH_DEBUG_PRINTED:
        return
    print(f"[TOOLTIP-DEBUG] track_length_m={track_length_m!r}")
    _TOOLTIP_TRACK_LENGTH_DEBUG_PRINTED = True


def _fmt_yawrate(rad_per_s: float) -> str:
    """rad/s -> deg/s, 2 decimals."""
    return f"{math.degrees(rad_per_s):.2f}°/s"


def _fmt_steering(rad: float) -> str:
    """Radians -> degrees, 2 decimals."""
    return f"{math.degrees(rad):.2f}°"


def _fmt_speed(m_per_s: float, speed_units: str) -> str:
    """m/s -> km/h or mph depending on speed_units."""
    units = str(speed_units or "km/h").strip().lower()
    if units in ("mph", "imperial"):
        return f"{m_per_s * 2.23694:.1f} mph"
    return f"{m_per_s * 3.6:.1f} km/h"


def _fmt_pct(value: float) -> str:
    """0.0-1.0 -> percent, 2 decimals."""
    return f"{value * 100:.2f}%"


def _coerce_float(value: object) -> float | None:
    try:
        number = float(value)
    except Exception:
        return None
    return number if math.isfinite(number) else None


def _read_speed_units() -> str:
    for section in ("units", "display", "video_compare"):
        raw = str(persistence.cfg_get(section, "speed_units", "")).strip().lower()
        if raw:
            if raw in ("mph", "imperial"):
                return "mph"
            if raw in ("kmh", "km/h", "metric"):
                return "km/h"
            return raw
    return "km/h"


def _brake_start_speed_tooltip(
    lapdist_pct: float,
    speed_data: "tuple[np.ndarray, np.ndarray] | None",
    speed_units: str,
) -> str:
    """Return formatted speed string for a brake_start hover tooltip.

    Uses the Speed channel (m/s) from *speed_data* = (speed_arr, ldp_arr),
    looks up the nearest sample to *lapdist_pct*, converts to the requested
    unit and returns e.g. ``"187 km/h"`` or ``"116 mph"``.
    Returns an empty string when data is unavailable so the caller can
    suppress the tooltip.
    """
    if speed_data is None:
        return ""
    speed_arr, ldp_arr = speed_data
    if len(speed_arr) == 0 or len(ldp_arr) == 0:
        return ""
    try:
        idx = int(np.argmin(np.abs(ldp_arr - float(lapdist_pct))))
        speed_ms = float(speed_arr[idx])
        if not math.isfinite(speed_ms):
            return ""
        units = str(speed_units or "km/h").strip().lower()
        if units in ("mph", "imperial"):
            return f"brake start\n{int(speed_ms * 2.23694)} mph"
        return f"brake start\n{int(speed_ms * 3.6)} km/h"
    except Exception:
        return ""


def _tooltip_title(event_type: str) -> str:
    base_title = _EVENT_LABELS.get(event_type, event_type.replace("_", " "))
    return _TOOLTIP_TITLES.get(event_type, base_title)


def _tooltip_lines(event, track_length_m: float | None, speed_units: str) -> list[str]:
    event_type = str(getattr(event, "event_type", "") or "")
    value = getattr(event, "value", None)
    if event_type in ("oversteer_event", "understeer_event"):
        if not isinstance(value, dict):
            return []
        lines: list[str] = []
        start_pct = _coerce_float(value.get("start_lapdist_pct"))
        end_pct = _coerce_float(value.get("end_lapdist_pct"))
        yawrate_peak = _coerce_float(
            value.get("yawrate_delta_peak", value.get("peak_yawrate", value.get("peak_delta_yawrate")))
        )
        if start_pct is not None or end_pct is not None:
            _log_tooltip_track_length_once(track_length_m)
        if start_pct is not None:
            lines.append(f"Start: {_fmt_lapdist_m(start_pct, track_length_m)}")
        if end_pct is not None:
            lines.append(f"End:   {_fmt_lapdist_m(end_pct, track_length_m)}")
        if yawrate_peak is not None:
            lines.append(f"Yawrate peak: {_fmt_yawrate(yawrate_peak)}")
        return lines
    if event_type == "gear_change":
        if not isinstance(value, dict):
            return []
        from_gear = value.get("from")
        to_gear = value.get("to")
        if from_gear is None or to_gear is None:
            return []
        return [f"{from_gear} -> {to_gear}"]
    scalar = _coerce_float(value)
    if event_type in ("turn_in_rate_based", "turn_in_angle_based"):
        return [f"Steering: {_fmt_steering(scalar)}"] if scalar is not None else []
    if event_type == "peak_brake":
        return [f"Brake: {_fmt_pct(scalar)}"] if scalar is not None else []
    if event_type == "min_speed":
        return [f"Speed: {_fmt_speed(scalar, speed_units)}"] if scalar is not None else []
    if scalar is not None:
        return [f"{scalar:.4f}"]
    if value is None:
        return []
    return [str(value)]


def _build_event_tooltip(event, track_length_m: float | None, speed_units: str) -> str:
    title = _tooltip_title(str(getattr(event, "event_type", "") or ""))
    lines = _tooltip_lines(event, track_length_m, speed_units)
    return title if not lines else "\n".join([title, *lines])


def _build_event_marker_tooltip(
    event,
    speed_data: "tuple[np.ndarray, np.ndarray] | None",
    track_length_m: float | None,
    speed_units: str,
) -> str:
    """Shared tooltip builder for TrackMap and Corner-Zoom event markers."""
    if getattr(event, "event_type", None) == "brake_start":
        return _brake_start_speed_tooltip(event.lapdist_pct, speed_data, speed_units)
    return _build_event_tooltip(event, track_length_m, speed_units)


class _FitContext(NamedTuple):
    x_min: float
    y_min: float
    x_max: float
    y_max: float
    x_range: float
    y_range: float
    usable_width: float
    usable_height: float
    scale: float
    offset_x: float
    offset_y: float
    draw_width: float
    draw_height: float


class TrackRoadGeometry(TypedDict):
    track_key: str
    source_type: str
    center_line: list[list[float]]
    left_edge: list[list[float]]
    right_edge: list[list[float]]


# ---------------------------------------------------------------------------
# PIL sprite helpers
# ---------------------------------------------------------------------------


def _hex_to_rgba(hex_color: str, alpha: int = 255) -> tuple:
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)


def _contrast_color(canvas: tk.Canvas) -> str:
    """#FFFFFF for dark canvas backgrounds, #000000 for light ones."""
    try:
        rgb = canvas.winfo_rgb(canvas.cget("background"))
        luminance = (rgb[0] / 65535 * 0.299 +
                     rgb[1] / 65535 * 0.587 +
                     rgb[2] / 65535 * 0.114)
        return "#FFFFFF" if luminance < 0.5 else "#000000"
    except Exception:
        return "#FFFFFF"


class EventSpriteCache:
    """Generates and caches PIL-based event symbols as tk.PhotoImage.

    Key: (event_type, size, fg_color_hex, bg_color_hex)
    bg_color_hex=None means no background (symbol is filled).
    """

    _cache: dict = {}

    @classmethod
    def get(cls, event_type: str, size: int,
            fg_color: str, bg_color: "str | None",
            canvas: tk.Canvas) -> PILPhotoImage:
        key = (event_type, size, fg_color, bg_color)
        if key not in cls._cache:
            cls._cache[key] = cls._make(event_type, size, fg_color, bg_color, canvas)
        return cls._cache[key]

    @classmethod
    def clear(cls) -> None:
        cls._cache.clear()

    @classmethod
    def _make(cls, event_type: str, size: int,
              fg_color: str, bg_color: "str | None",
              canvas: tk.Canvas) -> PILPhotoImage:
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        cx = size // 2
        cy = size // 2
        r = size // 2 - 2  # inner radius with 2px margin
        r_bg = r + 2

        fg = _hex_to_rgba(fg_color)
        bg = _hex_to_rgba(bg_color) if bg_color else None

        if event_type == "brake_start":
            pts = [(cx, cy - r), (cx - r, cy + r), (cx + r, cy + r)]
            draw.polygon(pts, fill=fg)

        elif event_type in ("turn_in_rate_based", "turn_in_angle_based"):
            pts = [(cx + r, cy - r), (cx + r, cy + r), (cx - r, cy)]
            draw.polygon(pts, fill=fg)

        elif event_type == "min_speed":
            outer_r = r
            inner_r = int(r * 0.45)
            pts = []
            for k in range(10):
                angle = math.radians(-90 + k * 36)
                rad = outer_r if k % 2 == 0 else inner_r
                pts.append((cx + rad * math.cos(angle),
                             cy + rad * math.sin(angle)))
            draw.polygon(pts, fill=fg)

        elif event_type == "throttle_on":
            # ▷  flat side left, apex right (90° CW from ▽)
            pts = [(cx - r, cy - r), (cx - r, cy + r), (cx + r, cy)]
            draw.polygon(pts, fill=fg)

        elif event_type == "throttle_full":
            # △  flat side bottom, apex up (180° from ▽)
            pts = [(cx, cy - r), (cx - r, cy + r), (cx + r, cy + r)]
            draw.polygon(pts, fill=fg)

        elif event_type == "throttle_off":
            # ▽  flat side top, apex down (unchanged)
            pts = [(cx, cy + r), (cx - r, cy - r), (cx + r, cy - r)]
            draw.polygon(pts, fill=fg)

        elif event_type == "peak_brake":
            # Background: filled circle r+2
            if bg:
                draw.ellipse([cx - r_bg, cy - r_bg, cx + r_bg, cy + r_bg],
                             fill=bg)
            # Symbol: small filled circle r//2
            half = r // 2
            draw.ellipse([cx - half, cy - half, cx + half, cy + half],
                         fill=fg)

        elif event_type == "gear_change":
            # Background: filled hexagon r+2
            if bg:
                bg_pts = [(cx + r_bg * math.cos(math.radians(30 + k * 60)),
                           cy + r_bg * math.sin(math.radians(30 + k * 60)))
                          for k in range(6)]
                draw.polygon(bg_pts, fill=bg)
            # Symbol: hexagon outline r
            fg_pts = [(cx + r * math.cos(math.radians(30 + k * 60)),
                       cy + r * math.sin(math.radians(30 + k * 60)))
                      for k in range(6)]
            draw.polygon(fg_pts, outline=fg, fill=None, width=2)

        elif event_type == "oversteer_event":
            # Background: filled triangle apex up r+2
            if bg:
                bg_pts = [(cx + r_bg * math.cos(math.radians(-90 + k * 120)),
                           cy + r_bg * math.sin(math.radians(-90 + k * 120)))
                          for k in range(3)]
                draw.polygon(bg_pts, fill=bg)
            # Symbol: triangle outline r, apex up
            fg_pts = [(cx + r * math.cos(math.radians(-90 + k * 120)),
                       cy + r * math.sin(math.radians(-90 + k * 120)))
                      for k in range(3)]
            draw.polygon(fg_pts, outline=fg, fill=None, width=2)

        elif event_type == "understeer_event":
            # Background: filled triangle apex down r+2
            if bg:
                bg_pts = [
                    (cx - r_bg, cy - r_bg),
                    (cx + r_bg, cy - r_bg),
                    (cx,        cy + r_bg),
                ]
                draw.polygon(bg_pts, fill=bg)
            # Symbol: triangle outline r, apex down (▽)
            fg_pts = [
                (cx - r, cy - r),
                (cx + r, cy - r),
                (cx,     cy + r),
            ]
            draw.polygon(fg_pts, outline=fg, fill=None, width=2)

        elif event_type == "crest":
            # Background: filled circle (consistent with other bg-sprites)
            if bg:
                draw.ellipse([cx - r_bg, cy - r_bg, cx + r_bg, cy + r_bg],
                             fill=bg)
            # Symbol: upper half-circle arc (⌒)
            draw.arc([cx - r, cy - r, cx + r, cy + r],
                     start=180, end=0, fill=fg, width=2)

        elif event_type == "offtrack_incident":
            # Background: white square
            draw.rectangle([0, 0, size - 1, size - 1], fill=(255, 255, 255, 255))
            # Symbol: red X (two diagonal lines)
            margin = size // 4
            draw.line([margin, margin, size - 1 - margin, size - 1 - margin],
                      fill=fg, width=3)
            draw.line([size - 1 - margin, margin, margin, size - 1 - margin],
                      fill=fg, width=3)

        elif event_type == "loose_control":
            # Background: white square
            draw.rectangle([0, 0, size - 1, size - 1], fill=(255, 255, 255, 255))
            # Symbol: red circular arrow (arc + arrowhead)
            pad = size // 5
            bbox = [pad, pad, size - 1 - pad, size - 1 - pad]
            draw.arc(bbox, start=40, end=320, fill=fg, width=3)
            # Arrowhead at the end of the arc (at 320°)
            end_rad = math.radians(320)
            arc_cx = (bbox[0] + bbox[2]) / 2.0
            arc_cy = (bbox[1] + bbox[3]) / 2.0
            arc_r2 = (bbox[2] - bbox[0]) / 2.0
            tip_x = arc_cx + arc_r2 * math.cos(end_rad)
            tip_y = arc_cy + arc_r2 * math.sin(end_rad)
            tan_rad = end_rad + math.pi / 2
            ah = size // 5
            ah_pts = [
                (tip_x, tip_y),
                (tip_x - ah * math.cos(tan_rad - 0.5),
                 tip_y - ah * math.sin(tan_rad - 0.5)),
                (tip_x - ah * math.cos(tan_rad + 0.5),
                 tip_y - ah * math.sin(tan_rad + 0.5)),
            ]
            draw.polygon(ah_pts, fill=fg)

        elif event_type == "crash":
            # Background: white square
            draw.rectangle([0, 0, size - 1, size - 1], fill=(255, 255, 255, 255))
            # Symbol: red lightning bolt polygon
            pts = [
                (cx + r // 2, cy - r),
                (cx - r // 5, cy - 1),
                (cx + r // 4, cy - 1),
                (cx - r // 2, cy + r),
                (cx + r // 5, cy + 1),
                (cx - r // 4, cy + 1),
            ]
            draw.polygon(pts, fill=fg)

        elif event_type == "steering_crossover":
            # Symbol: horizontal double-headed arrow ↔
            # Central horizontal line
            draw.line([cx - r, cy, cx + r, cy], fill=fg, width=2)
            # Left arrowhead ◁
            ah = max(r // 2, 3)
            draw.polygon(
                [(cx - r, cy), (cx - r + ah, cy - ah // 2), (cx - r + ah, cy + ah // 2)],
                fill=fg,
            )
            # Right arrowhead ▷
            draw.polygon(
                [(cx + r, cy), (cx + r - ah, cy - ah // 2), (cx + r - ah, cy + ah // 2)],
                fill=fg,
            )

        return PILPhotoImage(img)


class ResolvedEvent(NamedTuple):
    """An event with its resolved canvas position and connector anchor."""
    event: object
    canvas_x: float
    canvas_y: float
    connector_start_x: float   # projection on lap line; equals canvas_x when no offset
    connector_start_y: float


# ---------------------------------------------------------------------------
# Public: XY reconstruction
# ---------------------------------------------------------------------------


def reconstruct_xy(resampled_df: "pd.DataFrame") -> np.ndarray:
    """Reconstruct normalised (N, 2) XY from *resampled_df*.

    Priority:
      1. Direct ``X`` / ``Y`` columns if both present and non-trivial.
      2. Dead-reckoning via ``Speed`` × ``cos/sin(Yaw)`` × ``dt``.
         iRacing ``VelocityX`` is the car's forward velocity (vehicle frame),
         not a world-frame East component, so Yaw is required to reconstruct
         world-frame positions.
      3. Raw ``VelocityX`` / ``VelocityY`` integration as last resort.

    Returns an (N, 2) float64 array normalised to [0, 1] with the aspect
    ratio preserved.  On failure returns an (0, 2) empty array.
    """
    try:
        import pandas as _pd  # local import – optional dependency in core
        if not isinstance(resampled_df, _pd.DataFrame) or resampled_df.empty:
            return np.empty((0, 2), dtype=np.float64)
    except ImportError:
        pass

    cols = set(resampled_df.columns)
    n = len(resampled_df)

    dt = _build_dt(resampled_df, cols, n)

    # --- Prefer direct XY ---
    if "X" in cols and "Y" in cols:
        x = _to_f64(resampled_df["X"].to_numpy())
        y = _to_f64(resampled_df["Y"].to_numpy())
        if np.any(np.isfinite(x)) and np.any(np.isfinite(y)):
            return _normalise_xy(x, y)

    # --- Dead-reckoning: Speed × cos/sin(Yaw) ---
    if "Speed" in cols and "Yaw" in cols:
        sp = _to_f64(resampled_df["Speed"].to_numpy())
        yaw = _to_f64(resampled_df["Yaw"].to_numpy())
        sp = np.where(np.isfinite(sp), sp, 0.0)
        yaw = np.where(np.isfinite(yaw), yaw, 0.0)
        x = np.cumsum(sp * np.cos(yaw) * dt)
        # Bug 2 fix: iRacing Yaw is CW-positive (positive = right turn = South direction),
        # so sin(Yaw) gives the South component.  Negate to get the North component so
        # that the Y-axis points North and _transform_zoom's (y_max − y) inversion
        # correctly places North at the top of the canvas.
        y = np.cumsum(-sp * np.sin(yaw) * dt)
        # Bug 1 fix: linear drift correction – close the integrated loop to eliminate
        # the wrap-around gap caused by accumulated integration error over one lap.
        x, y = _close_loop(x, y)
        if np.ptp(x) > 1.0 or np.ptp(y) > 1.0:
            return _normalise_xy(x, y)

    # --- Fallback: raw VelocityX / VelocityY ---
    if "VelocityX" not in cols or "VelocityY" not in cols:
        return np.empty((0, 2), dtype=np.float64)

    vx = _to_f64(resampled_df["VelocityX"].to_numpy())
    vy = _to_f64(resampled_df["VelocityY"].to_numpy())
    x = np.cumsum(np.where(np.isfinite(vx), vx, 0.0) * dt)
    # Bug 2 fix: negate vy – same South→North convention correction as Speed×Yaw path.
    y = np.cumsum(-np.where(np.isfinite(vy), vy, 0.0) * dt)
    # Bug 1 fix: linear drift correction.
    x, y = _close_loop(x, y)
    return _normalise_xy(x, y)


# ---------------------------------------------------------------------------
# Public: TrackMap rendering
# ---------------------------------------------------------------------------


def render_trackmap(
    canvas: tk.Canvas,
    xy: np.ndarray,
    corners: list,                           # list[CornerInfo]
    selected_corner_id: Optional[int],
    width: int,
    height: int,
    road_geometry: Optional[TrackRoadGeometry] = None,
    lap_color: str = "#E53935",
    lap_dist_pct: Optional[np.ndarray] = None,
    on_corner_selected: Optional[Callable[[int], None]] = None,
    zoom: float = 1.0,
    offset: tuple = (0.0, 0.0),
    is_closed: bool = True,
    events: Optional[list] = None,
    visible_event_types: Optional[set] = None,
    speed_data: "tuple[np.ndarray, np.ndarray] | None" = None,
    track_length_m: float | None = None,
) -> None:
    """Render a complete TrackMap onto *canvas*.

    Parameters
    ----------
    canvas:
        The ``tk.Canvas`` to draw on.  Will be fully cleared first.
    xy:
        (N, 2) normalised track coordinates in [0, 1].
    corners:
        List of ``CornerInfo`` objects (``corner_id``, ``start_lapdist_pct``,
        ``end_lapdist_pct``).
    selected_corner_id:
        If not ``None``, that corner is drawn with a highlighted colour.
    width, height:
        Current pixel dimensions of *canvas*.
    road_geometry:
        Optional road-band geometry for the same track. When present, a dark
        road surface is rendered underneath the existing lap overlays.
    lap_color:
        Colour of the lap line drawn on top of the grey track line.
    lap_dist_pct:
        (N,) array of ``LapDistPct`` values corresponding to *xy*.  Used to
        map corner segments to indices.  If ``None`` a uniform mapping is
        assumed.
    on_corner_selected:
        Callback ``on_corner_selected(corner_id: int)`` fired when the user
        clicks a corner segment.
    is_closed:
        Whether the lap geometry should be rendered as a closed loop. Dead-
        reckoning fallback geometry should pass ``False`` here.
    """
    canvas.delete("all")
    canvas._fit_context = None
    canvas._sprite_refs = []

    if xy is None or len(xy) < 2:
        return

    # Compute road arrays first so they can be included in the shared fit_context.
    # Both lap line and road geometry are in raw world coordinates (meters),
    # so combining them gives a correct common bounding box.
    road_arrays = _road_geometry_arrays(road_geometry)
    if road_arrays is not None:
        cl, le, re = road_arrays
        bbox_parts = [xy, cl]
        if le is not None and len(le) >= 2:
            bbox_parts.append(le)
        if re is not None and len(re) >= 2:
            bbox_parts.append(re)
        combined_xy = np.vstack(bbox_parts)
    else:
        combined_xy = xy

    fit_context = _legend_aware_fit_context(combined_xy, width, height)
    canvas._fit_context = fit_context
    _log_canvas_geometry_debug(
        "trackmap_render",
        combined_xy,
        fit_context,
        width=width,
        height=height,
    )
    coords = _transform_zoom(xy, width, height, zoom, offset, fit_context=fit_context)  # (N, 2) pixel coords
    line_flat = _line_flat(coords, is_closed=is_closed)

    # 1 – Optional road band
    if road_arrays is not None:
        _, left_edge, right_edge = road_arrays
        if left_edge is not None and right_edge is not None:
            left_canvas = _transform_zoom(left_edge, width, height, zoom, offset, fit_context=fit_context)
            right_canvas = _transform_zoom(right_edge, width, height, zoom, offset, fit_context=fit_context)
            _draw_road_band(canvas, left_canvas, right_canvas, smooth=False, prefix="trackmap")

    # 2 – Base track line (grey)
    canvas.create_line(line_flat, fill=_TRACK_COLOR, width=_TRACK_WIDTH,
                       smooth=False, tags=("track",))

    # 3 – Corner segments (underneath the lap line)
    for corner in corners:
        indices = _corner_indices(corner, lap_dist_pct, len(xy))
        if len(indices) < 2:
            continue

        seg_coords = coords[indices]
        seg_flat = seg_coords.flatten().tolist()

        is_selected = (corner.corner_id == selected_corner_id)
        fill = _SELECTED_COLOR if is_selected else _CORNER_COLOR
        tag = f"corner_{corner.corner_id}"

        canvas.create_line(
            seg_flat, fill=fill, width=_SEG_WIDTH, smooth=False,
            stipple="gray50", capstyle=tk.ROUND,
            tags=("corner_seg", tag),
        )

        if is_selected:
            # Extra bright outline for selected corner
            canvas.create_line(
                seg_flat, fill="#FFFFFF", width=_SEG_WIDTH + 4, smooth=False,
                stipple="gray25", capstyle=tk.ROUND,
                tags=("corner_sel", tag + "_sel"),
            )

        if on_corner_selected is not None:
            cid = corner.corner_id
            canvas.tag_bind(tag, "<Button-1>",
                            lambda _e, c=cid: on_corner_selected(c))

        # 2b – Corner label at apex (midpoint of segment)
        mid_i = indices[len(indices) // 2]
        lx = float(coords[mid_i, 0])
        ly = float(coords[mid_i, 1]) - _LABEL_OFFSET
        canvas.create_text(
            lx, ly, text=str(corner.corner_id),
            fill="#FFFFFF", font=_LABEL_FONT,
            tags=(f"label_{corner.corner_id}",),
        )

    # 4 – Lap line (primary colour, on top)
    canvas.create_line(line_flat, fill=lap_color, width=_LAP_WIDTH,
                       smooth=False, tags=("lapline",))

    # 5 – Event markers (above lap line)
    if events and visible_event_types:
        _render_trackmap_events(
            canvas, events, visible_event_types, coords, lap_dist_pct,
            speed_data=speed_data,
            track_length_m=track_length_m,
            speed_units=_read_speed_units(),
        )


# ---------------------------------------------------------------------------
# Public: Corner-Zoom rendering
# ---------------------------------------------------------------------------


def render_corner_zoom(
    canvas: tk.Canvas,
    xy: np.ndarray,
    corner,                              # CornerInfo
    events: list,                        # list[Event]
    width: int,
    height: int,
    road_geometry=None,                  # TrackRoadGeometry | None (future)
    lap_dist_pct: Optional[np.ndarray] = None,
    lo: Optional[float] = None,
    hi: Optional[float] = None,
    zoom: float = 1.0,
    offset: tuple = (0.0, 0.0),
    track_length_m: float | None = None,
    speed_data: "tuple[np.ndarray, np.ndarray] | None" = None,
) -> None:
    """Render a zoomed view of *corner* onto *canvas*.

    Parameters
    ----------
    canvas:
        Target ``tk.Canvas``; cleared first.
    xy:
        Full (N, 2) normalised track coordinates.
    corner:
        ``CornerInfo`` with ``start_lapdist_pct`` / ``end_lapdist_pct``.
    events:
        Events belonging to this corner (``Event.lapdist_pct`` used for
        projection).  Empty list → only the lap line is drawn.
    width, height:
        Current pixel dimensions of *canvas*.
    road_geometry:
        Optional road-band geometry for the same track. When present, the
        zoomed segment is rendered beneath the lap line using the same canvas
        transform as the race line.
    lap_dist_pct:
        (N,) array paired with *xy*.  Required for accurate event
        projection; falls back to proportional mapping when ``None``.
    lo, hi:
        Padded LapDistPct bounds for segment slicing.  When provided the
        canvas segment spans ``[lo, hi]`` instead of the bare corner
        bounds; events outside ``[lo, hi]`` are skipped.  Falls back to
        ``corner.start_lapdist_pct`` / ``end_lapdist_pct`` when ``None``.
    """
    canvas.delete("all")
    canvas._fit_context = None
    if xy is None or len(xy) < 2:
        return

    # Resolve effective bounds
    lo_eff = lo if lo is not None else corner.start_lapdist_pct
    hi_eff = hi if hi is not None else corner.end_lapdist_pct

    n = len(xy)
    if lap_dist_pct is not None and len(lap_dist_pct) == n:
        if lo_eff <= hi_eff:
            # Normalfall
            mask = (lap_dist_pct >= lo_eff) & (lap_dist_pct <= hi_eff)
        else:
            # Wrap-around: Corner überspannt LapDistPct 0/1
            mask = (lap_dist_pct >= lo_eff) | (lap_dist_pct <= hi_eff)
        indices = np.where(mask)[0]
        if len(indices) < 2:
            # Echter Fallback: Corner-Segment nicht gefunden
            # Zeige nur die nächsten 10% der Strecke um den Apex
            apex_pct = (lo_eff + hi_eff) / 2 % 1.0
            fallback_lo = (apex_pct - 0.05) % 1.0
            fallback_hi = (apex_pct + 0.05) % 1.0
            if fallback_lo <= fallback_hi:
                mask = (lap_dist_pct >= fallback_lo) & (lap_dist_pct <= fallback_hi)
            else:
                mask = (lap_dist_pct >= fallback_lo) | (lap_dist_pct <= fallback_hi)
            indices = np.where(mask)[0]
    else:
        indices = _corner_indices(corner, lap_dist_pct, n)
    if len(indices) < 2:
        return

    seg_xy = xy[indices]
    seg_ldp = (
        lap_dist_pct[indices]
        if lap_dist_pct is not None and len(lap_dist_pct) == n
        else None
    )

    # Bounding box of the segment (for event projection and normalisation)
    x_min = float(np.min(seg_xy[:, 0]))
    y_min = float(np.min(seg_xy[:, 1]))
    x_max = float(np.max(seg_xy[:, 0]))
    y_max = float(np.max(seg_xy[:, 1]))
    seg_scale = max(x_max - x_min, y_max - y_min) or 1.0

    # Normalise segment to [0, 1] in its local bounding box
    seg_norm = np.column_stack([
        (seg_xy[:, 0] - x_min) / seg_scale,
        (seg_xy[:, 1] - y_min) / seg_scale,
    ])

    # Canvas mapping with zoom/offset (Y flipped, shared transform with TrackMap)
    fit_context = _legend_aware_fit_context(seg_norm, width, height, legend_width=0)
    canvas._fit_context = fit_context
    _log_canvas_geometry_debug(
        "corner_zoom_render",
        seg_xy,
        fit_context,
        width=width,
        height=height,
    )
    seg_canvas = _transform_zoom(seg_norm, width, height, zoom, offset, fit_context=fit_context)
    speed_units = _read_speed_units()

    # -- Road band ----------------------------------------------------------
    road_segment = _road_geometry_segment(road_geometry, lo_eff, hi_eff)
    if road_segment is not None:
        _, left_seg, right_seg = road_segment
        if left_seg is not None and right_seg is not None:
            left_norm = _normalise_segment_points(left_seg, x_min, y_min, seg_scale)
            right_norm = _normalise_segment_points(right_seg, x_min, y_min, seg_scale)
            left_canvas = _transform_zoom(left_norm, width, height, zoom, offset, fit_context=fit_context)
            right_canvas = _transform_zoom(right_norm, width, height, zoom, offset, fit_context=fit_context)
            _draw_road_band(canvas, left_canvas, right_canvas, smooth=True, prefix="zoom")

    # -- Lap line -----------------------------------------------------------
    flat = seg_canvas.flatten().tolist()
    if len(flat) >= 4:
        canvas.create_line(
            flat, fill=_ZOOM_LINE_COLOR, width=_ZOOM_LINE_WIDTH,
            smooth=True, tags=("zoom_lapline",),
        )

    # Start / end dots
    for pt, tag in [(seg_canvas[0], "zoom_start"), (seg_canvas[-1], "zoom_end")]:
        cx, cy = float(pt[0]), float(pt[1])
        canvas.create_oval(cx - 3, cy - 3, cx + 3, cy + 3,
                           fill="#888888", outline="", tags=(tag,))

    # -- Event markers ------------------------------------------------------
    event_map: dict = {}  # tag -> tooltip text

    resolved = _resolve_event_collisions(events, seg_ldp, seg_canvas, lo_eff, hi_eff)

    # Connector lines drawn first so they appear behind symbols
    for rev in resolved:
        if rev.connector_start_x != rev.canvas_x or rev.connector_start_y != rev.canvas_y:
            style = _EVENT_STYLE.get(rev.event.event_type)
            if style is not None:
                _, color = style
                canvas.create_line(
                    rev.connector_start_x, rev.connector_start_y,
                    rev.canvas_x, rev.canvas_y,
                    fill=color, width=1,
                    tags=("zoom_connector",),
                )

    bg = _contrast_color(canvas)
    canvas._sprite_refs = []
    for rev in resolved:
        style = _EVENT_STYLE.get(rev.event.event_type)
        if style is None:
            continue
        _, color = style

        tag = f"zev_{id(rev.event)}"
        needs_bg = rev.event.event_type in _BG_SYMBOL_TYPES
        sprite = EventSpriteCache.get(
            rev.event.event_type, EVENT_SYMBOL_SIZE,
            color, bg if needs_bg else None, canvas,
        )
        canvas._sprite_refs.append(sprite)
        canvas.create_image(
            rev.canvas_x, rev.canvas_y, image=sprite,
            anchor=tk.CENTER, tags=("zoom_event", tag),
        )
        if rev.event.event_type == "gear_change":
            canvas.create_text(
                rev.canvas_x,
                rev.canvas_y,
                text=_gear_label(rev.event.value),
                fill="#4488FF",
                font=("Arial", 7, "bold"),
                tags=("zoom_event", tag),
            )

        event_map[tag] = _build_event_marker_tooltip(
            rev.event, speed_data, track_length_m, speed_units
        )

    # Single motion handler instead of per-item tag_bind (avoids tooltip-loop freeze)
    canvas.bind("<Motion>", lambda e, em=event_map: _zoom_on_motion(canvas, e, em))
    canvas.bind("<Leave>", lambda _e: _zoom_hide_tooltip(canvas))

# ---------------------------------------------------------------------------
# TrackMap event-marker helpers
# ---------------------------------------------------------------------------


def _trackmap_project_event(
    lapdist_pct: float,
    full_ldp: Optional[np.ndarray],
    coords: np.ndarray,
) -> tuple:
    """Return (canvas_x, canvas_y) for an event at *lapdist_pct* on the full track."""
    n = len(coords)
    if full_ldp is not None and len(full_ldp) == n:
        idx = int(np.argmin(np.abs(full_ldp - lapdist_pct)))
    else:
        idx = int(np.clip(lapdist_pct * n, 0, n - 1))
    return float(coords[idx, 0]), float(coords[idx, 1])


def _render_trackmap_events(
    canvas: tk.Canvas,
    events: list,
    visible_event_types: set,
    coords: np.ndarray,
    lap_dist_pct: Optional[np.ndarray],
    speed_data: "tuple[np.ndarray, np.ndarray] | None" = None,
    track_length_m: float | None = None,
    speed_units: str = "km/h",
) -> None:
    """Draw event sprites on the full TrackMap canvas with collision resolution."""
    # Project all visible events into canvas coordinates first
    valid: list = []
    for ev in (events or []):
        try:
            event_type = str(getattr(ev, "event_type", "") or "")
        except Exception:
            continue
        if event_type not in visible_event_types:
            continue
        if _EVENT_STYLE.get(event_type) is None:
            continue
        try:
            ldp = float(getattr(ev, "lapdist_pct", None))
        except (TypeError, ValueError):
            continue
        if not (0.0 <= ldp <= 1.0):
            continue
        try:
            ex, ey = _trackmap_project_event(ldp, lap_dist_pct, coords)
        except Exception:
            continue
        valid.append((ev, ex, ey))

    if not valid:
        return

    # Sort by lapdist_pct and resolve collisions using the shared helper
    valid.sort(key=lambda t: t[0].lapdist_pct)
    resolved = _apply_collision_offsets(valid, coords)

    bg = _contrast_color(canvas)

    # Connector lines drawn first so they appear behind symbols
    for rev in resolved:
        if rev.connector_start_x != rev.canvas_x or rev.connector_start_y != rev.canvas_y:
            style = _EVENT_STYLE.get(rev.event.event_type)
            if style is not None:
                _, color = style
                canvas.create_line(
                    rev.connector_start_x, rev.connector_start_y,
                    rev.canvas_x, rev.canvas_y,
                    fill=color, width=1,
                    tags=("tm_connector",),
                )

    event_map: dict = {}
    for rev in resolved:
        style = _EVENT_STYLE.get(rev.event.event_type)
        if style is None:
            continue
        _, color = style
        needs_bg = rev.event.event_type in _BG_SYMBOL_TYPES
        sprite = EventSpriteCache.get(
            rev.event.event_type, EVENT_SYMBOL_SIZE,
            color, bg if needs_bg else None, canvas,
        )
        canvas._sprite_refs.append(sprite)
        tag = f"tmev_{id(rev.event)}"
        canvas.create_image(rev.canvas_x, rev.canvas_y, image=sprite,
                            anchor=tk.CENTER, tags=("tm_event", tag))
        event_map[tag] = _build_event_marker_tooltip(
            rev.event, speed_data, track_length_m, speed_units
        )

    canvas.bind("<Motion>", lambda e, em=event_map: _zoom_on_motion(canvas, e, em))
    canvas.bind("<Leave>", lambda _e: _zoom_hide_tooltip(canvas))


# ---------------------------------------------------------------------------
# Corner-Zoom private helpers
# ---------------------------------------------------------------------------


def _gear_label(event: dict) -> str:
    """Return the non-neutral gear number for a gear_change event payload."""
    if not isinstance(event, dict):
        return ""
    from_gear = event.get("from", 0)
    to_gear = event.get("to", 0)
    relevant = from_gear if to_gear == 0 else to_gear
    return str(relevant)


def _apply_collision_offsets(valid: list, seg_canvas: np.ndarray) -> list:
    """Given pre-projected events, return ``ResolvedEvent`` list with collision offsets.

    *valid* is a list of ``(event, canvas_x, canvas_y)`` tuples, already
    sorted by lapdist_pct.  *seg_canvas* is the track segment coordinate array
    used to compute tangent vectors for orthogonal offsets.

    Events whose canvas positions are within ``EVENT_SYMBOL_SIZE`` pixels of
    the first event in a group are placed in that group.  Within each group
    index-0 stays on the track line; subsequent events are offset orthogonally
    (alternating right/left, growing magnitude) with a connector anchor stored
    in ``connector_start_*``.
    """
    if not valid:
        return []

    # Greedy grouping: events within EVENT_SYMBOL_SIZE canvas distance of the
    # first event in a group belong to that collision group.
    groups: list = []
    for ev, ex, ey in valid:
        placed = False
        for group in groups:
            gx, gy = group[0][1], group[0][2]
            if ((ex - gx) ** 2 + (ey - gy) ** 2) ** 0.5 < EVENT_SYMBOL_SIZE:
                group.append((ev, ex, ey))
                placed = True
                break
        if not placed:
            groups.append([(ev, ex, ey)])

    n_seg = len(seg_canvas)
    resolved: list = []

    for group in groups:
        for idx, (ev, ex, ey) in enumerate(group):
            if idx == 0:
                resolved.append(ResolvedEvent(ev, ex, ey, ex, ey))
                continue

            # Tangent vector at the event's projection point on seg_canvas
            dists = np.hypot(seg_canvas[:, 0] - ex, seg_canvas[:, 1] - ey)
            seg_idx = int(np.argmin(dists))
            prev_i = max(0, seg_idx - 1)
            next_i = min(n_seg - 1, seg_idx + 1)
            tx = float(seg_canvas[next_i, 0] - seg_canvas[prev_i, 0])
            ty = float(seg_canvas[next_i, 1] - seg_canvas[prev_i, 1])
            length = (tx ** 2 + ty ** 2) ** 0.5 or 1.0
            tx /= length
            ty /= length

            # Alternating sides with growing magnitude:
            # idx=1 → right 2×, idx=2 → left 2×, idx=3 → right 4×, …
            magnitude = ((idx + 1) // 2) * 2 * EVENT_SYMBOL_SIZE
            if idx % 2 == 1:   # right: orthogonal(-ty, tx)
                ox, oy = -ty, tx
            else:              # left:  orthogonal(+ty, -tx)
                ox, oy = ty, -tx

            new_x = ex + ox * magnitude
            new_y = ey + oy * magnitude
            resolved.append(ResolvedEvent(ev, new_x, new_y, ex, ey))

    return resolved


def _resolve_event_collisions(
    events,
    seg_ldp: Optional[np.ndarray],
    seg_canvas: np.ndarray,
    lo_eff: float,
    hi_eff: float,
) -> list:
    """Return a list of ``ResolvedEvent`` with collision-resolved canvas positions.

    Events whose projected positions are within ``EVENT_SYMBOL_SIZE`` pixels of
    each other are grouped.  Within each group index-0 stays on the lap line;
    subsequent events are offset orthogonally (alternating right/left, growing
    magnitude) with a connector line anchor stored in ``connector_start_*``.
    """
    # Collect valid events with their projected canvas positions
    valid: list = []
    for ev in (events or []):
        if _EVENT_STYLE.get(ev.event_type) is None:
            continue
        if not (lo_eff <= ev.lapdist_pct <= hi_eff):
            continue
        try:
            ex, ey = _zoom_project_event(ev.lapdist_pct, seg_ldp, seg_canvas)
        except Exception:
            continue
        valid.append((ev, ex, ey))

    if not valid:
        return []

    # Sort by lapdist_pct so nearby positions cluster together
    valid.sort(key=lambda t: t[0].lapdist_pct)

    return _apply_collision_offsets(valid, seg_canvas)


def _zoom_project_event(
    lapdist_pct: float,
    seg_ldp: Optional[np.ndarray],
    seg_canvas: np.ndarray,
) -> tuple:
    """Return (canvas_x, canvas_y) for an event at *lapdist_pct*.

    Uses nearest-neighbour lookup on *seg_ldp* when available; falls back to
    proportional mapping over the segment's own LapDistPct span.
    """
    n_seg = len(seg_canvas)
    if seg_ldp is not None and len(seg_ldp) == n_seg:
        seg_local = int(np.argmin(np.abs(seg_ldp - lapdist_pct)))
    else:
        # Proportional fallback within the segment's own span
        span = float(seg_ldp[-1] - seg_ldp[0]) if seg_ldp is not None and len(seg_ldp) >= 2 else 1.0
        lo_seg = float(seg_ldp[0]) if seg_ldp is not None and len(seg_ldp) >= 1 else 0.0
        pct = (lapdist_pct - lo_seg) / max(span, 1e-6)
        seg_local = int(np.clip(pct * n_seg, 0, n_seg - 1))
    return float(seg_canvas[seg_local, 0]), float(seg_canvas[seg_local, 1])


def _zoom_on_motion(canvas: tk.Canvas, event, event_map: dict) -> None:
    """Canvas <Motion> handler: show tooltip when hovering over an event marker."""
    items = canvas.find_overlapping(event.x - 4, event.y - 4, event.x + 4, event.y + 4)
    for item in items:
        for tag in canvas.gettags(item):
            if tag in event_map:
                text = event_map[tag]
                if text:
                    _zoom_show_tooltip(canvas, text, event.x, event.y)
                else:
                    _zoom_hide_tooltip(canvas)
                return
    _zoom_hide_tooltip(canvas)


def _zoom_show_tooltip(canvas: tk.Canvas, text: str, x: float, y: float) -> None:
    """Draw a small tooltip box on *canvas* near the current mouse position."""
    canvas.delete("zoom_tooltip")
    w = canvas.winfo_width()
    tx = x + 12
    ty = y - 20
    # Clamp so tooltip stays inside canvas
    tid = canvas.create_text(
        tx, ty, text=text, anchor="nw",
        fill="#FFFFFF", font=_ZOOM_TOOLTIP_FONT,
        tags=("zoom_tooltip",),
    )
    bbox = canvas.bbox(tid)
    if bbox:
        # Shift left if it overflows the right edge
        if bbox[2] + 2 > w:
            dx = bbox[2] + 2 - w
            canvas.move(tid, -dx, 0)
            bbox = canvas.bbox(tid)
        canvas.create_rectangle(
            bbox[0] - 2, bbox[1] - 2, bbox[2] + 2, bbox[3] + 2,
            fill="#333333", outline="#666666",
            tags=("zoom_tooltip",),
        )
        canvas.tag_raise(tid)


def _zoom_hide_tooltip(canvas: tk.Canvas) -> None:
    canvas.delete("zoom_tooltip")


def _zoom_draw_legend(canvas: tk.Canvas, event_types: set, width: int, height: int) -> None:
    """Draw a compact legend for *event_types* in the bottom-right corner."""
    items = [(t, s, c) for t, (s, c) in _EVENT_STYLE.items() if t in event_types]
    if not items:
        return

    row_height = max(LEGEND_SYMBOL_SIZE + 4, 20)
    padding_y = 6
    labels = [_EVENT_LABELS.get(ev_type, ev_type.replace("_", " ")) for ev_type, _, _ in items]
    try:
        import tkinter.font as tkFont
        _font = tkFont.Font(family="Arial", size=11)
        max_text_width = max(_font.measure(label) for label in labels)
    except Exception:
        max_text_width = max(len(label) for label in labels) * 7

    padding_x = 8
    symbol_col_width = LEGEND_SYMBOL_SIZE + 6
    text_col_width = max_text_width
    legend_width = padding_x + symbol_col_width + text_col_width + padding_x
    legend_height = len(items) * row_height + padding_y * 2

    x0 = width - legend_width - 10
    y0 = height - legend_height - 10

    canvas.create_rectangle(
        x0, y0, x0 + legend_width, y0 + legend_height,
        fill="#1a1a1a", outline="#444444", tags=("zoom_legend",),
    )
    bg = _contrast_color(canvas)
    if not hasattr(canvas, '_sprite_refs'):
        canvas._sprite_refs = []
    text_x = x0 + padding_x + symbol_col_width
    for i, (ev_type, _, color) in enumerate(items):
        ly = y0 + padding_y + i * row_height
        scx = x0 + padding_x + LEGEND_SYMBOL_SIZE // 2
        scy = ly + row_height // 2
        needs_bg = ev_type in _BG_SYMBOL_TYPES
        sprite = EventSpriteCache.get(
            ev_type, LEGEND_SYMBOL_SIZE,
            color, bg if needs_bg else None, canvas,
        )
        canvas._sprite_refs.append(sprite)
        canvas.create_image(
            scx, scy, image=sprite,
            anchor=tk.CENTER, tags=("zoom_legend",),
        )
        canvas.create_text(
            text_x, ly,
            text=_EVENT_LABELS.get(ev_type, ev_type.replace("_", " ")), fill="#AAAAAA",
            font=_ZOOM_LEGEND_FONT, anchor="nw", tags=("zoom_legend",),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_dt(resampled_df, cols: set, n: int) -> np.ndarray:
    """Build a dt array from SessionTime, or assume 100 Hz grid."""
    if "SessionTime" in cols:
        st = _to_f64(resampled_df["SessionTime"].to_numpy())
        if st.size >= 2:
            median_dt = float(np.nanmedian(np.diff(st)))
            dt = np.diff(st, prepend=st[0])
            return np.where(np.isfinite(dt) & (dt > 0), dt, median_dt)
    return np.full(n, 0.01, dtype=np.float64)


def _to_f64(arr: np.ndarray) -> np.ndarray:
    out = np.asarray(arr, dtype=np.float64)
    return out


def _close_loop(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Apply linear drift correction so the integrated path forms a closed loop.

    Distributes the wrap-around gap (xy[-1] − xy[0]) evenly over all N points:
        xy_corrected[i] = xy[i] − (i / N) × drift
    so that the last point equals the first point after correction.
    """
    n = len(x)
    if n < 2:
        return x, y
    t = np.arange(n, dtype=np.float64) / n
    x = x - t * (x[-1] - x[0])
    y = y - t * (y[-1] - y[0])
    return x, y


def _fix_nonfinite(arr: np.ndarray) -> np.ndarray:
    finite_mask = np.isfinite(arr)
    if finite_mask.all():
        return arr
    if not finite_mask.any():
        return np.zeros_like(arr)
    indices = np.arange(len(arr))
    out = arr.copy()
    out[~finite_mask] = np.interp(
        indices[~finite_mask],
        indices[finite_mask],
        out[finite_mask],
    )
    return out


def _normalise_xy(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Normalise *x* and *y* to [0, 1] preserving aspect ratio."""
    x = _fix_nonfinite(x)
    y = _fix_nonfinite(y)
    if len(x) < 2:
        return np.empty((0, 2), dtype=np.float64)
    x_min, x_max = float(np.min(x)), float(np.max(x))
    y_min, y_max = float(np.min(y)), float(np.max(y))
    x_range = x_max - x_min or 1.0
    y_range = y_max - y_min or 1.0
    scale = max(x_range, y_range)
    xn = (x - x_min) / scale
    yn = (y - y_min) / scale
    return np.column_stack([xn, yn])


def _legend_aware_fit_context(
    xy: np.ndarray,
    width: int,
    height: int,
    legend_width: int = _FIT_LEGEND_WIDTH,
    padding_left: int = _FIT_PADDING_LEFT,
    padding_right: int = _FIT_PADDING_RIGHT,
    padding_top: int = _FIT_PADDING_TOP,
    padding_bottom: int = _FIT_PADDING_BOTTOM,
) -> _FitContext:
    """Return the shared fit context for TrackMap / Corner-Zoom rendering."""
    if xy is None or len(xy) == 0:
        x_min = y_min = x_max = y_max = 0.0
        x_range = y_range = 1.0
    else:
        x_min = float(np.min(xy[:, 0]))
        y_min = float(np.min(xy[:, 1]))
        x_max = float(np.max(xy[:, 0]))
        y_max = float(np.max(xy[:, 1]))
        x_range = x_max - x_min or 1.0
        y_range = y_max - y_min or 1.0

    usable_width = max(width - padding_left - legend_width - padding_right, 1.0)
    usable_height = max(height - padding_top - padding_bottom, 1.0)
    scale = min(usable_width / (x_range or 1.0), usable_height / (y_range or 1.0))
    offset_x = padding_left + (usable_width - x_range * scale) / 2.0
    offset_y = padding_top + (usable_height - y_range * scale) / 2.0
    draw_width = x_range * scale
    draw_height = y_range * scale
    return _FitContext(
        x_min=x_min,
        y_min=y_min,
        x_max=x_max,
        y_max=y_max,
        x_range=x_range,
        y_range=y_range,
        usable_width=usable_width,
        usable_height=usable_height,
        scale=scale,
        offset_x=offset_x,
        offset_y=offset_y,
        draw_width=draw_width,
        draw_height=draw_height,
    )


def _transform_zoom(
    xy: np.ndarray, width: int, height: int,
    zoom: float, offset: tuple,
    fit_context: Optional[_FitContext] = None,
) -> np.ndarray:
    """Map coords to canvas pixels using the shared legend-aware fit context."""
    if xy is None or len(xy) == 0:
        return np.empty((0, 2), dtype=np.float64)

    fit = fit_context or _legend_aware_fit_context(xy, width, height)
    origin_x = fit.offset_x + offset[0] * width + fit.draw_width * (1.0 - zoom) / 2.0
    origin_y = fit.offset_y + offset[1] * height + fit.draw_height * (1.0 - zoom) / 2.0
    px = origin_x + (xy[:, 0] - fit.x_min) * fit.scale * zoom
    py = origin_y + (fit.y_max - xy[:, 1]) * fit.scale * zoom
    return np.column_stack([px, py])


def _to_canvas(xy: np.ndarray, width: int, height: int) -> np.ndarray:
    """Map normalised [0, 1] coords to canvas pixel coords with padding.

    Y-axis is flipped so that the mathematical positive-Y direction maps
    upward on screen.
    """
    pad_x = width * _PADDING
    pad_y = height * _PADDING
    draw_w = width - 2.0 * pad_x
    draw_h = height - 2.0 * pad_y
    cx = xy[:, 0] * draw_w + pad_x
    cy = (1.0 - xy[:, 1]) * draw_h + pad_y   # flip Y
    return np.column_stack([cx, cy])


def _line_flat(coords: np.ndarray, is_closed: bool = True) -> list:
    """Return a flat line coordinate list, optionally closing the path."""
    pts = coords.tolist()
    if is_closed and pts:
        pts.append(pts[0])
    flat: list = []
    for x, y in pts:
        flat.append(x)
        flat.append(y)
    return flat


def _road_geometry_arrays(
    road_geometry: Optional[TrackRoadGeometry],
) -> tuple[np.ndarray, np.ndarray | None, np.ndarray | None] | None:
    if not isinstance(road_geometry, dict):
        return None

    center_line = _coerce_xy_array(road_geometry.get("center_line"))
    left_edge = _coerce_xy_array(road_geometry.get("left_edge"), allow_empty=True)
    right_edge = _coerce_xy_array(road_geometry.get("right_edge"), allow_empty=True)
    if center_line is None or left_edge is None or right_edge is None:
        return None

    if len(left_edge) >= 2 and len(right_edge) >= 2:
        n = min(len(center_line), len(left_edge), len(right_edge))
        if n < 2:
            return None
        return center_line[:n], left_edge[:n], right_edge[:n]
    return center_line, None, None


def _coerce_xy_array(value: object, *, allow_empty: bool = False) -> np.ndarray | None:
    if value == [] and allow_empty:
        return np.empty((0, 2), dtype=np.float64)
    if not isinstance(value, list) or len(value) < 2:
        return None
    try:
        arr = np.asarray(value, dtype=np.float64)
    except Exception:
        return None
    if arr.ndim != 2 or arr.shape[1] != 2 or len(arr) < 2:
        return None
    if not np.all(np.isfinite(arr)):
        return None
    return arr


def _draw_road_band(
    canvas: tk.Canvas,
    left_edge: np.ndarray,
    right_edge: np.ndarray,
    *,
    smooth: bool,
    prefix: str,
) -> None:
    if len(left_edge) < 2 or len(right_edge) < 2:
        return

    polygon = np.vstack([left_edge, right_edge[::-1]])
    canvas.create_polygon(
        polygon.flatten().tolist(),
        fill=_ROAD_FILL_COLOR,
        outline="",
        tags=(f"{prefix}_road_band",),
    )
    canvas.create_line(
        left_edge.flatten().tolist(),
        fill=_ROAD_EDGE_COLOR,
        width=_ROAD_EDGE_WIDTH,
        smooth=smooth,
        tags=(f"{prefix}_road_edge", f"{prefix}_road_edge_left"),
    )
    canvas.create_line(
        right_edge.flatten().tolist(),
        fill=_ROAD_EDGE_COLOR,
        width=_ROAD_EDGE_WIDTH,
        smooth=smooth,
        tags=(f"{prefix}_road_edge", f"{prefix}_road_edge_right"),
    )


def _road_geometry_segment(
    road_geometry: Optional[TrackRoadGeometry],
    lo_eff: float,
    hi_eff: float,
) -> tuple[np.ndarray, np.ndarray | None, np.ndarray | None] | None:
    road_arrays = _road_geometry_arrays(road_geometry)
    if road_arrays is None:
        return None

    center_line, left_edge, right_edge = road_arrays
    indices = _pct_slice_indices(lo_eff, hi_eff, len(center_line))
    if len(indices) < 2:
        return None
    left_segment = left_edge[indices] if left_edge is not None else None
    right_segment = right_edge[indices] if right_edge is not None else None
    return center_line[indices], left_segment, right_segment


def _log_canvas_geometry_debug(
    log_name: str,
    points: np.ndarray,
    fit_context: _FitContext,
    *,
    width: int,
    height: int,
) -> None:
    if points is None or len(points) < 2:
        return
    try:
        x_min = float(np.nanmin(points[:, 0]))
        x_max = float(np.nanmax(points[:, 0]))
        y_min = float(np.nanmin(points[:, 1]))
        y_max = float(np.nanmax(points[:, 1]))
    except ValueError:
        return
    _LOG.debug(
        "[%s] min_x=%.3f max_x=%.3f min_y=%.3f max_y=%.3f bbox_width=%.3f bbox_height=%.3f canvas_target_rect=x=%.3f,y=%.3f,w=%.3f,h=%.3f canvas_size=%dx%d",
        log_name,
        x_min,
        x_max,
        y_min,
        y_max,
        x_max - x_min,
        y_max - y_min,
        fit_context.offset_x,
        fit_context.offset_y,
        fit_context.draw_width,
        fit_context.draw_height,
        width,
        height,
    )


def _pct_slice_indices(lo_eff: float, hi_eff: float, n: int) -> np.ndarray:
    if n < 2:
        return np.array([], dtype=int)

    lap_pct = np.linspace(0.0, 1.0, n)
    if lo_eff <= hi_eff:
        mask = (lap_pct >= lo_eff) & (lap_pct <= hi_eff)
    else:
        mask = (lap_pct >= lo_eff) | (lap_pct <= hi_eff)
    indices = np.where(mask)[0]
    if len(indices) >= 2:
        return indices

    start_i = max(0, min(n - 1, int(lo_eff * (n - 1))))
    end_i = max(0, min(n - 1, int(hi_eff * (n - 1))))
    if lo_eff <= hi_eff:
        if end_i < start_i:
            return np.array([], dtype=int)
        return np.arange(start_i, end_i + 1)
    return np.concatenate([np.arange(start_i, n), np.arange(0, end_i + 1)])


def _normalise_segment_points(
    points: np.ndarray,
    x_min: float,
    y_min: float,
    seg_scale: float,
) -> np.ndarray:
    if len(points) == 0:
        return np.empty((0, 2), dtype=np.float64)
    return np.column_stack([
        (points[:, 0] - x_min) / seg_scale,
        (points[:, 1] - y_min) / seg_scale,
    ])


def _corner_indices(corner, lap_dist_pct: Optional[np.ndarray],
                    n: int) -> np.ndarray:
    """Return the array indices belonging to *corner*.

    Uses *lap_dist_pct* when available; falls back to a proportional slice.
    """
    if lap_dist_pct is not None and len(lap_dist_pct) == n:
        mask = (
            (lap_dist_pct >= corner.start_lapdist_pct) &
            (lap_dist_pct <= corner.end_lapdist_pct)
        )
        return np.where(mask)[0]

    # Uniform fallback
    start_i = max(0, int(corner.start_lapdist_pct * n))
    end_i = min(n - 1, int(corner.end_lapdist_pct * n))
    if end_i < start_i:
        return np.array([], dtype=int)
    return np.arange(start_i, end_i + 1)
