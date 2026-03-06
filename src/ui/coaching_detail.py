"""coaching_detail.py – CoachingDetailView: Layout-Gerüst für Lap-Visualisierung (Story 3.2).

Rechte Seite der Coaching-View. Stabiles Layout-Gerüst das Sprint 4–6 füllen.

Layout::

    ┌─────────────────────────────────────────────┐
    │ [Header: Track / Car / Lap / Zeit]          │
    ├──────────────────────┬──────────────────────┤
    │ TrackMap Canvas      │ Corner-Zoom Canvas   │
    │ (klickbar)           │ (bei Auswahl)        │
    ├──────────────────────┴──────────────────────┤
    │ Telemetrie-Trace (matplotlib, collapsible)  │
    ├─────────────────────────────────────────────┤
    │ Corner Scorecard (Feature-Tabelle)          │
    └─────────────────────────────────────────────┘

Interface::

    class CoachingDetailView(ttk.Frame):
        def load_lap(self, vm: LapViewModel) -> None
        def clear(self) -> None
        def set_selected_corner(self, corner_id: int) -> None
"""

from __future__ import annotations

import configparser
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Optional

from core.coaching.lap_view_model import LapViewModel
from core.coaching.track_geometry import render_corner_zoom, render_trackmap

_PROJECT_ROOT = Path(__file__).parent.parent.parent


def _read_corner_event_padding_m() -> float:
    """Read corner_event_padding_m from config/defaults.ini (default 50 m)."""
    cp = configparser.ConfigParser()
    cp.read(_PROJECT_ROOT / "config" / "defaults.ini", encoding="utf-8-sig")
    try:
        val = float(cp.get("coaching_analysis", "corner_event_padding_m", fallback="50"))
        return max(0.0, min(10000.0, val))
    except Exception:
        return 50.0


class CoachingDetailView(ttk.Frame):
    """Stable layout container for single-lap visualisation.

    All four sub-areas are independent frames so Sprint 4–6 can extend
    them individually without touching the surrounding structure.
    """

    def __init__(self, master: tk.Widget, **kw) -> None:
        super().__init__(master, **kw)
        self._vm: Optional[LapViewModel] = None
        self._selected_corner_id: Optional[int] = None
        self._map_zoom: float = 1.0
        self._map_offset: tuple = (0.0, 0.0)
        self._map_drag_start: Optional[tuple] = None
        self._map_drag_offset_start: tuple = (0.0, 0.0)
        self._corner_zoom: float = 1.0
        self._corner_offset: tuple = (0.0, 0.0)
        self._corner_drag_start: Optional[tuple] = None
        self._corner_drag_offset_start: tuple = (0.0, 0.0)
        self._build_layout()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_lap(self, vm: Optional[LapViewModel], *, is_purple: bool = False) -> None:
        """Load *vm* and refresh all sub-widgets.

        Passing ``None`` is equivalent to calling ``clear()``.
        """
        self._vm = vm
        if vm is None:
            self.clear()
            return
        self._update_header(vm, is_purple=is_purple)
        self._redraw_trackmap()
        self._clear_corner_zoom()
        self._update_scorecard(None)

    def clear(self) -> None:
        """Reset all sub-widgets to the empty / placeholder state."""
        self._vm = None
        self._selected_corner_id = None
        self._set_header_empty()
        self._trackmap_canvas.delete("all")
        self._clear_corner_zoom()
        self._update_scorecard(None)

    def set_selected_corner(self, corner_id: int) -> None:
        """Highlight *corner_id* on the TrackMap and update dependent widgets."""
        self._selected_corner_id = corner_id
        self._redraw_trackmap()
        self._show_corner_zoom_overlay()
        if self._vm is not None:
            features = self._vm.features.get(corner_id, {})
            self._update_scorecard(features)

    # ------------------------------------------------------------------
    # Layout construction
    # ------------------------------------------------------------------

    def _build_layout(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)  # maps row expands

        # Row 0 – Header
        self._header_frame = self._build_header()
        self._header_frame.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))

        # Row 1 – Maps (TrackMap left, Corner-Zoom right)
        self._maps_frame = self._build_maps()
        self._maps_frame.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)

        # Row 2 – Telemetrie-Trace (collapsible placeholder for Story 3.4)
        self._trace_frame = self._build_trace_placeholder()
        self._trace_frame.grid(row=2, column=0, sticky="ew", padx=8, pady=4)

        # Row 3 – Corner Scorecard (placeholder for Story 3.5)
        self._scorecard_frame = self._build_scorecard_placeholder()
        self._scorecard_frame.grid(row=3, column=0, sticky="ew", padx=8, pady=(4, 8))

    def _build_header(self) -> ttk.Frame:
        frame = ttk.Frame(self)
        info_row = ttk.Frame(frame)
        info_row.grid(row=0, column=0, sticky="ew")

        self._lbl_track = ttk.Label(info_row, text="—", font=("", 11, "bold"))
        self._lbl_track.grid(row=0, column=0, sticky="w", padx=(0, 16))

        self._lbl_car = ttk.Label(info_row, text="—")
        self._lbl_car.grid(row=0, column=1, sticky="w", padx=(0, 16))

        self._lbl_lap = ttk.Label(info_row, text="Lap —")
        self._lbl_lap.grid(row=0, column=2, sticky="w", padx=(0, 16))

        self._lbl_time = ttk.Label(info_row, text="—:—.—")
        self._lbl_time.grid(row=0, column=3, sticky="w")

        return frame

    def _build_maps(self) -> ttk.Frame:
        frame = ttk.Frame(self)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        # TrackMap Canvas
        trackmap_lf = ttk.LabelFrame(frame, text="Track Map")
        trackmap_lf.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        trackmap_lf.columnconfigure(0, weight=1)
        trackmap_lf.rowconfigure(0, weight=1)

        self._trackmap_canvas = tk.Canvas(trackmap_lf, bg="#1e1e1e", highlightthickness=0)
        self._trackmap_canvas.grid(row=0, column=0, sticky="nsew")
        self._trackmap_canvas.bind("<Configure>", self._on_trackmap_resize)
        self._trackmap_canvas.bind("<MouseWheel>", self._on_map_wheel)
        self._trackmap_canvas.bind("<Button-4>", self._on_map_wheel)
        self._trackmap_canvas.bind("<Button-5>", self._on_map_wheel)
        self._trackmap_canvas.bind("<ButtonPress-1>", self._on_map_drag_start)
        self._trackmap_canvas.bind("<B1-Motion>", self._on_map_drag_move)
        self._trackmap_canvas.bind("<ButtonRelease-1>", self._on_map_drag_end)
        self._trackmap_canvas.bind("<Double-Button-1>", self._on_map_reset)

        # Corner-Zoom Overlay – placed over trackmap_lf on demand (Story 3.3).
        # Uses place geometry manager (compatible with the canvas above using grid).
        self._zoom_overlay = tk.Frame(trackmap_lf, bg="#1e1e1e")
        self._zoom_overlay_visible = False

        self._zoom_back_btn = ttk.Button(
            self._zoom_overlay, text="← Zurück",
            command=self._hide_corner_zoom_overlay,
        )
        self._zoom_back_btn.pack(side="top", anchor="nw", padx=4, pady=(4, 2))

        self._zoom_canvas = tk.Canvas(
            self._zoom_overlay, bg="#1e1e1e", highlightthickness=0,
        )
        self._zoom_canvas.pack(fill="both", expand=True)
        self._zoom_canvas.bind("<Configure>", self._on_zoom_canvas_resize)
        self._zoom_canvas.bind("<MouseWheel>", self._on_corner_wheel)
        self._zoom_canvas.bind("<Button-4>", self._on_corner_wheel)
        self._zoom_canvas.bind("<Button-5>", self._on_corner_wheel)
        self._zoom_canvas.bind("<ButtonPress-1>", self._on_corner_drag_start)
        self._zoom_canvas.bind("<B1-Motion>", self._on_corner_drag_move)
        self._zoom_canvas.bind("<ButtonRelease-1>", self._on_corner_drag_end)
        self._zoom_canvas.bind("<Double-Button-1>", self._on_corner_reset)

        return frame

    def _build_trace_placeholder(self) -> ttk.LabelFrame:
        """Collapsible placeholder for Story 3.4 (Telemetrie-Trace)."""
        frame = ttk.LabelFrame(self, text="Telemetrie")
        self._trace_visible = False

        self._trace_toggle_btn = ttk.Button(frame, text="▶ Show", command=self._toggle_trace)
        self._trace_toggle_btn.grid(row=0, column=0, sticky="w", padx=4, pady=2)

        self._trace_content = ttk.Label(
            frame,
            text="Telemetrie-Trace wird in Story 3.4 implementiert.",
            foreground="#666666",
        )
        # Not gridded until expanded

        return frame

    def _build_scorecard_placeholder(self) -> ttk.LabelFrame:
        """Placeholder for Story 3.5 (Corner Scorecard)."""
        frame = ttk.LabelFrame(self, text="Corner Scorecard")
        frame.columnconfigure(0, weight=1)

        self._scorecard_label = ttk.Label(frame, text="Keinen Corner ausgewählt.", foreground="#666666")
        self._scorecard_label.grid(row=0, column=0, sticky="w", padx=6, pady=4)

        return frame

    # ------------------------------------------------------------------
    # Header helpers
    # ------------------------------------------------------------------

    def _update_header(self, vm: LapViewModel, *, is_purple: bool = False) -> None:
        meta = vm.meta
        if meta is None:
            self._set_header_empty()
            return
        self._lbl_track.configure(text=meta.track or "—")
        self._lbl_car.configure(text=meta.car or "—")
        self._lbl_lap.configure(text=f"Lap {meta.lap_no}")
        time_color = "#9C27B0" if is_purple else ""
        if meta.lap_time is not None:
            self._lbl_time.configure(text=_format_laptime(meta.lap_time), foreground=time_color)
        else:
            self._lbl_time.configure(text="—:—.—", foreground=time_color)

    def _set_header_empty(self) -> None:
        self._lbl_track.configure(text="—")
        self._lbl_car.configure(text="—")
        self._lbl_lap.configure(text="Lap —")
        self._lbl_time.configure(text="—:—.—")

    # ------------------------------------------------------------------
    # TrackMap helpers
    # ------------------------------------------------------------------

    def _redraw_trackmap(self) -> None:
        vm = self._vm
        if vm is None:
            self._trackmap_canvas.delete("all")
            return
        w = self._trackmap_canvas.winfo_width()
        h = self._trackmap_canvas.winfo_height()
        if w < 2 or h < 2:
            # Canvas not yet laid out — retry after Tk has processed geometry
            self._trackmap_canvas.after(50, self._redraw_trackmap)
            return
        render_trackmap(
            canvas=self._trackmap_canvas,
            xy=vm.track_xy,
            corners=vm.corners,
            selected_corner_id=self._selected_corner_id,
            width=w,
            height=h,
            on_corner_selected=self.set_selected_corner,
            lap_dist_pct=vm.lap_dist_pct,
            zoom=self._map_zoom,
            offset=self._map_offset,
        )

    def _on_trackmap_resize(self, _event=None) -> None:
        if self._vm is not None:
            self._redraw_trackmap()

    def _on_map_wheel(self, event) -> None:
        if event.num == 4:
            delta = 1
        elif event.num == 5:
            delta = -1
        else:
            delta = event.delta / 120

        old_zoom = self._map_zoom
        new_zoom = max(0.5, min(10.0, old_zoom * (1.1 ** delta)))
        if new_zoom == old_zoom:
            return

        canvas = self._trackmap_canvas
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        fit = getattr(canvas, "_fit_context", None)
        if fit is None:
            return

        origin_x = fit.offset_x + self._map_offset[0] * w + fit.draw_width * (1 - old_zoom) / 2
        origin_y = fit.offset_y + self._map_offset[1] * h + fit.draw_height * (1 - old_zoom) / 2

        new_origin_x = event.x - (event.x - origin_x) * (new_zoom / old_zoom)
        new_origin_y = event.y - (event.y - origin_y) * (new_zoom / old_zoom)

        new_offset_x = (new_origin_x - fit.offset_x - fit.draw_width * (1 - new_zoom) / 2) / w
        new_offset_y = (new_origin_y - fit.offset_y - fit.draw_height * (1 - new_zoom) / 2) / h

        self._map_zoom = new_zoom
        self._map_offset = (new_offset_x, new_offset_y)
        self._redraw_trackmap()

    def _on_map_drag_start(self, event) -> None:
        self._map_drag_start = (event.x, event.y)
        self._map_drag_offset_start = self._map_offset

    def _on_map_drag_move(self, event) -> None:
        if self._map_drag_start is None:
            return
        canvas = self._trackmap_canvas
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        dx = event.x - self._map_drag_start[0]
        dy = event.y - self._map_drag_start[1]
        self._map_offset = (
            self._map_drag_offset_start[0] + dx / w,
            self._map_drag_offset_start[1] + dy / h,
        )
        self._redraw_trackmap()

    def _on_map_drag_end(self, event) -> None:
        self._map_drag_start = None

    def _on_map_reset(self, event) -> None:
        self._map_zoom = 1.0
        self._map_offset = (0.0, 0.0)
        self._redraw_trackmap()

    # ------------------------------------------------------------------
    # Corner-Zoom interaction handlers
    # ------------------------------------------------------------------

    def _on_corner_wheel(self, event) -> None:
        if event.num == 4:
            delta = 1
        elif event.num == 5:
            delta = -1
        else:
            delta = event.delta / 120

        old_zoom = self._corner_zoom
        new_zoom = max(0.5, min(10.0, old_zoom * (1.1 ** delta)))
        if new_zoom == old_zoom:
            return

        canvas = self._zoom_canvas
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        fit = getattr(canvas, "_fit_context", None)
        if fit is None:
            return

        origin_x = fit.offset_x + self._corner_offset[0] * w + fit.draw_width * (1 - old_zoom) / 2
        origin_y = fit.offset_y + self._corner_offset[1] * h + fit.draw_height * (1 - old_zoom) / 2

        new_origin_x = event.x - (event.x - origin_x) * (new_zoom / old_zoom)
        new_origin_y = event.y - (event.y - origin_y) * (new_zoom / old_zoom)

        new_offset_x = (new_origin_x - fit.offset_x - fit.draw_width * (1 - new_zoom) / 2) / w
        new_offset_y = (new_origin_y - fit.offset_y - fit.draw_height * (1 - new_zoom) / 2) / h

        self._corner_zoom = new_zoom
        self._corner_offset = (new_offset_x, new_offset_y)
        self._redraw_corner_zoom()

    def _on_corner_drag_start(self, event) -> None:
        self._corner_drag_start = (event.x, event.y)
        self._corner_drag_offset_start = self._corner_offset

    def _on_corner_drag_move(self, event) -> None:
        if self._corner_drag_start is None:
            return
        canvas = self._zoom_canvas
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        dx = event.x - self._corner_drag_start[0]
        dy = event.y - self._corner_drag_start[1]
        self._corner_offset = (
            self._corner_drag_offset_start[0] + dx / w,
            self._corner_drag_offset_start[1] + dy / h,
        )
        self._redraw_corner_zoom()

    def _on_corner_drag_end(self, event) -> None:
        self._corner_drag_start = None

    def _on_corner_reset(self, event) -> None:
        self._corner_zoom = 1.0
        self._corner_offset = (0.0, 0.0)
        self._redraw_corner_zoom()

    # ------------------------------------------------------------------
    # Corner-Zoom overlay helpers
    # ------------------------------------------------------------------

    def _show_corner_zoom_overlay(self) -> None:
        """Place the overlay frame over the TrackMap canvas and render."""
        self._zoom_overlay.place(relx=0, rely=0, relwidth=1.0, relheight=1.0)
        self._zoom_overlay.lift()
        self._zoom_overlay_visible = True
        self._redraw_corner_zoom()

    def _hide_corner_zoom_overlay(self) -> None:
        """Remove the overlay, restoring the TrackMap."""
        self._zoom_overlay.place_forget()
        self._zoom_overlay_visible = False

    def _redraw_corner_zoom(self) -> None:
        """Re-render the corner zoom canvas (called on show or resize)."""
        if not self._zoom_overlay_visible:
            return
        if self._vm is None or self._selected_corner_id is None:
            return
        corner = next(
            (c for c in self._vm.corners if c.corner_id == self._selected_corner_id),
            None,
        )
        if corner is None:
            return
        w = self._zoom_canvas.winfo_width()
        h = self._zoom_canvas.winfo_height()
        if w < 2 or h < 2:
            self._zoom_canvas.after(50, self._redraw_corner_zoom)
            return

        # Prefer padded_start/end_lapdist_pct set by apply_corner_padding().
        # Fall back to corner_event_padding_m when padded fields are absent.
        if (
            corner.padded_start_lapdist_pct is not None
            and corner.padded_end_lapdist_pct is not None
        ):
            lo = corner.padded_start_lapdist_pct
            hi = corner.padded_end_lapdist_pct
        else:
            padding_m = _read_corner_event_padding_m()
            track_len = self._vm.track_length_m
            if track_len and track_len > 0:
                padding_pct = padding_m / track_len
            else:
                padding_pct = 0.01
            lo = corner.start_lapdist_pct - padding_pct
            hi = corner.end_lapdist_pct + padding_pct

        print(
            f"[CORNER-ZOOM-DEBUG] corner_id={corner.corner_id}"
            f" start={corner.start_lapdist_pct:.4f} end={corner.end_lapdist_pct:.4f}"
            f" lo={lo:.4f} hi={hi:.4f}"
        )

        events: list = self._vm.corner_events.get(corner.corner_id, [])
        if events:
            print(
                f"[CORNER-ZOOM-DEBUG] events source: corner_events"
                f" ({len(events)} events)"
            )
        else:
            print("[CORNER-ZOOM-DEBUG] events source: fallback_empty")

        render_corner_zoom(
            canvas=self._zoom_canvas,
            xy=self._vm.track_xy,
            corner=corner,
            events=events,
            width=w,
            height=h,
            lap_dist_pct=self._vm.lap_dist_pct,
            lo=lo,
            hi=hi,
            zoom=self._corner_zoom,
            offset=self._corner_offset,
        )

    def _on_zoom_canvas_resize(self, _event=None) -> None:
        self._redraw_corner_zoom()

    def _clear_corner_zoom(self) -> None:
        """Hide overlay."""
        self._hide_corner_zoom_overlay()

    # ------------------------------------------------------------------
    # Scorecard helpers
    # ------------------------------------------------------------------

    def _update_scorecard(self, features: Optional[dict]) -> None:
        if features:
            self._scorecard_label.configure(
                text=f"Corner {self._selected_corner_id} – {len(features)} features available (Story 3.5)",
                foreground="#888888",
            )
        else:
            self._scorecard_label.configure(
                text="Keinen Corner ausgewählt.",
                foreground="#666666",
            )

    # ------------------------------------------------------------------
    # Telemetrie-Trace toggle
    # ------------------------------------------------------------------

    def _toggle_trace(self) -> None:
        if self._trace_visible:
            self._trace_content.grid_forget()
            self._trace_visible = False
            self._trace_toggle_btn.configure(text="▶ Show")
        else:
            self._trace_content.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 4))
            self._trace_visible = True
            self._trace_toggle_btn.configure(text="▼ Hide")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_laptime(seconds: float) -> str:
    """Format lap time in seconds as ``M:SS.mmm``."""
    try:
        m = int(seconds // 60)
        s = seconds - m * 60
        return f"{m}:{s:06.3f}"
    except Exception:
        return "—:—.—"
