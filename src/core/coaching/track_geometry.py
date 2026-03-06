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

import math
import tkinter as tk
from typing import TYPE_CHECKING, Callable, List, NamedTuple, Optional

import numpy as np

if TYPE_CHECKING:
    import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PADDING = 0.06          # fraction of canvas dimension reserved per edge
_TRACK_COLOR = "#555555"
_TRACK_WIDTH = 1
_LAP_WIDTH = 2
_SEG_WIDTH = 7           # corner-segment highlight width (px)
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
EVENT_SYMBOL_SIZE = 18   # px; event symbol size used for collision detection

_EVENT_STYLE: dict = {
    "brake_start":     ("▼", "#CC2222"),
    "peak_brake":      ("●", "#770000"),
    "turn_in":         ("◀", "#FF8800"),
    "min_speed":       ("★", "#FFDD00"),
    "throttle_on":     ("▲", "#88FF44"),
    "throttle_full":   ("▲", "#00CC00"),
    "gear_change":     ("⬡", "#4488FF"),
    "oversteer_event": ("⚠", "#FF44FF"),
    "crest":           ("⌒", "#00DDFF"),
}

# Symbol types that need a contrast background (hollow / low-contrast glyphs only)
_BG_SYMBOL_TYPES = {"gear_change", "oversteer_event", "crest", "peak_brake"}


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
        y = np.cumsum(sp * np.sin(yaw) * dt)
        if np.ptp(x) > 1.0 or np.ptp(y) > 1.0:
            return _normalise_xy(x, y)

    # --- Fallback: raw VelocityX / VelocityY ---
    if "VelocityX" not in cols or "VelocityY" not in cols:
        return np.empty((0, 2), dtype=np.float64)

    vx = _to_f64(resampled_df["VelocityX"].to_numpy())
    vy = _to_f64(resampled_df["VelocityY"].to_numpy())
    x = np.cumsum(np.where(np.isfinite(vx), vx, 0.0) * dt)
    y = np.cumsum(np.where(np.isfinite(vy), vy, 0.0) * dt)
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
    lap_color: str = "#E53935",
    lap_dist_pct: Optional[np.ndarray] = None,
    on_corner_selected: Optional[Callable[[int], None]] = None,
    zoom: float = 1.0,
    offset: tuple = (0.0, 0.0),
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
    lap_color:
        Colour of the lap line drawn on top of the grey track line.
    lap_dist_pct:
        (N,) array of ``LapDistPct`` values corresponding to *xy*.  Used to
        map corner segments to indices.  If ``None`` a uniform mapping is
        assumed.
    on_corner_selected:
        Callback ``on_corner_selected(corner_id: int)`` fired when the user
        clicks a corner segment.
    """
    canvas.delete("all")

    if xy is None or len(xy) < 2:
        return

    coords = _transform_zoom(xy, width, height, zoom, offset)  # (N, 2) pixel coords
    closed_flat = _closed_flat(coords)       # flat list, first == last

    # 1 – Base track line (grey)
    canvas.create_line(closed_flat, fill=_TRACK_COLOR, width=_TRACK_WIDTH,
                       smooth=False, tags=("track",))

    # 2 – Corner segments (underneath the lap line)
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

    # 3 – Lap line (primary colour, on top)
    canvas.create_line(closed_flat, fill=lap_color, width=_LAP_WIDTH,
                       smooth=False, tags=("lapline",))


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
        Reserved for future ``TrackRoadGeometry``; ignored when ``None``.
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
    if xy is None or len(xy) < 2:
        return

    # Resolve effective bounds
    lo_eff = lo if lo is not None else corner.start_lapdist_pct
    hi_eff = hi if hi is not None else corner.end_lapdist_pct

    n = len(xy)
    if lap_dist_pct is not None and len(lap_dist_pct) == n:
        mask = (lap_dist_pct >= lo_eff) & (lap_dist_pct <= hi_eff)
        indices = np.where(mask)[0]
    else:
        indices = _corner_indices(corner, lap_dist_pct, n)
    if len(indices) < 2:
        indices = np.arange(n)

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
    seg_canvas = _transform_zoom(seg_norm, width, height, zoom, offset)

    # -- Road band (future: road_geometry support) --------------------------
    # road_geometry rendering intentionally omitted until type is defined.

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
    drawn_types: set = set()
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

    _bg_col = _symbol_bg_color(canvas)
    _main_r = EVENT_SYMBOL_SIZE / 2 * 1.2
    for rev in resolved:
        style = _EVENT_STYLE.get(rev.event.event_type)
        if style is None:
            continue
        symbol, color = style

        tag = f"zev_{id(rev.event)}"
        if rev.event.event_type in _BG_SYMBOL_TYPES:
            _draw_symbol_bg(
                canvas, rev.canvas_x, rev.canvas_y,
                rev.event.event_type, _bg_col, _main_r,
                ("zoom_event_bg",),
            )
        canvas.create_text(
            rev.canvas_x, rev.canvas_y, text=symbol, fill=color,
            font=_ZOOM_MARKER_FONT,
            tags=("zoom_event", tag),
        )

        tip = rev.event.event_type.replace("_", " ")
        if rev.event.value is not None:
            tip += f"\n{rev.event.value}"

        event_map[tag] = tip
        drawn_types.add(rev.event.event_type)

    # Single motion handler instead of per-item tag_bind (avoids tooltip-loop freeze)
    canvas.bind("<Motion>", lambda e, em=event_map: _zoom_on_motion(canvas, e, em))
    canvas.bind("<Leave>", lambda _e: _zoom_hide_tooltip(canvas))

    # -- Legend -------------------------------------------------------------
    _zoom_draw_legend(canvas, drawn_types, width, height)


# ---------------------------------------------------------------------------
# Corner-Zoom private helpers
# ---------------------------------------------------------------------------


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
                _zoom_show_tooltip(canvas, event_map[tag], event.x, event.y)
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

    line_h = 18
    box_w = 120
    box_h = len(items) * line_h + 10
    x0 = width - box_w - 10
    y0 = height - box_h - 10

    canvas.create_rectangle(
        x0, y0, width - 10, height - 10,
        fill="#1a1a1a", outline="#555555", tags=("zoom_legend",),
    )
    _bg_col = _symbol_bg_color(canvas)
    _leg_r = EVENT_SYMBOL_SIZE * 0.8 / 2
    for i, (ev_type, symbol, color) in enumerate(items):
        ly = y0 + 5 + i * line_h
        if ev_type in _BG_SYMBOL_TYPES:
            scx = x0 + 8 + _leg_r
            scy = ly + _leg_r
            _draw_symbol_bg(
                canvas, scx, scy,
                ev_type, _bg_col, _leg_r,
                ("zoom_legend",),
            )
        canvas.create_text(
            x0 + 8, ly, text=symbol, fill=color,
            font=_ZOOM_LEGEND_FONT, anchor="nw", tags=("zoom_legend",),
        )
        canvas.create_text(
            x0 + 26, ly, text=ev_type.replace("_", " "), fill="#AAAAAA",
            font=_ZOOM_LEGEND_FONT, anchor="nw", tags=("zoom_legend",),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _symbol_bg_color(canvas: tk.Canvas) -> str:
    """Return #FFFFFF for dark canvas backgrounds, #000000 for light ones.

    Uses ``winfo_rgb`` so that named colours ('black', 'gray10', …) are
    resolved correctly to their true RGB values.
    """
    try:
        r16, g16, b16 = canvas.winfo_rgb(canvas.cget("background"))
        # winfo_rgb returns values in [0, 65535]
        lum = (0.299 * r16 + 0.587 * g16 + 0.114 * b16) / 65535.0
        return "#FFFFFF" if lum < 0.5 else "#000000"
    except Exception:
        return "#FFFFFF"


def _draw_symbol_bg(
    canvas: tk.Canvas,
    cx: float,
    cy: float,
    ev_type: str,
    bg_col: str,
    r: float,
    tags: tuple,
) -> None:
    """Draw a shape-appropriate contrast background for a hollow symbol.

    The background is drawn 1.2× larger than the nominal glyph radius *r*
    (caller is expected to pass ``nominal_half_size * 1.2`` already).

    Shapes:
      peak_brake      → filled circle
      gear_change     → filled flat-top regular hexagon
      oversteer_event → filled equilateral triangle (apex up, ⚠ style)
      crest           → thick horizontal line (width=4), matches ⌒ stroke
    """
    if ev_type == "peak_brake":
        canvas.create_oval(
            cx - r, cy - r, cx + r, cy + r,
            fill=bg_col, outline="", tags=tags,
        )
    elif ev_type == "gear_change":
        pts: list = []
        for k in range(6):
            angle = math.radians(30 + k * 60)   # flat-top hexagon, start at 30°
            pts.extend([cx + r * math.cos(angle), cy + r * math.sin(angle)])
        canvas.create_polygon(pts, fill=bg_col, outline="", tags=tags)
    elif ev_type == "oversteer_event":
        pts = []
        for k in range(3):
            angle = -math.pi / 2 + k * 2 * math.pi / 3   # apex at top
            pts.extend([cx + r * math.cos(angle), cy + r * math.sin(angle)])
        canvas.create_polygon(pts, fill=bg_col, outline="", tags=tags)
    elif ev_type == "crest":
        canvas.create_line(
            cx - r, cy, cx + r, cy,
            fill=bg_col, width=4, tags=tags,
        )


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


def _normalise_xy(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Normalise *x* and *y* to [0, 1] preserving aspect ratio."""
    x = np.where(np.isfinite(x), x, 0.0)
    y = np.where(np.isfinite(y), y, 0.0)
    x_min, x_max = float(np.min(x)), float(np.max(x))
    y_min, y_max = float(np.min(y)), float(np.max(y))
    x_range = x_max - x_min or 1.0
    y_range = y_max - y_min or 1.0
    scale = max(x_range, y_range)
    xn = (x - x_min) / scale
    yn = (y - y_min) / scale
    return np.column_stack([xn, yn])


def _transform_zoom(
    xy: np.ndarray, width: int, height: int,
    zoom: float, offset: tuple,
) -> np.ndarray:
    """Map normalised [0, 1] coords to canvas pixels with zoom and offset.

    At zoom=1.0 and offset=(0.0, 0.0) the result matches the same 20 px
    padding used for the interactive TrackMap.  Y-axis is flipped so that
    mathematical positive-Y maps upward on screen.
    """
    pad = 20
    B_w = (width - 2 * pad) * zoom
    B_h = (height - 2 * pad) * zoom
    origin_x = pad + offset[0] * width + (width - 2 * pad) * (1 - zoom) / 2
    origin_y = pad + offset[1] * height + (height - 2 * pad) * (1 - zoom) / 2
    px = origin_x + xy[:, 0] * B_w
    py = origin_y + (1.0 - xy[:, 1]) * B_h   # flip Y
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


def _closed_flat(coords: np.ndarray) -> list:
    """Return a flat [x0, y0, x1, y1, …, x0, y0] list closing the path."""
    pts = coords.tolist()
    pts.append(pts[0])  # close
    flat: list = []
    for x, y in pts:
        flat.append(x)
        flat.append(y)
    return flat


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
