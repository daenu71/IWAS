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

import tkinter as tk
from typing import TYPE_CHECKING, Callable, List, Optional

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
_LABEL_FONT = ("Arial", 7, "bold")
_LABEL_OFFSET = 9        # px above midpoint


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

    coords = _to_canvas(xy, width, height)  # (N, 2) pixel coords
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
