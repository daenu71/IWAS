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

import tkinter as tk
from tkinter import ttk
from typing import Optional

from core.coaching.lap_view_model import LapViewModel
from core.coaching.track_geometry import render_trackmap


class CoachingDetailView(ttk.Frame):
    """Stable layout container for single-lap visualisation.

    All four sub-areas are independent frames so Sprint 4–6 can extend
    them individually without touching the surrounding structure.
    """

    def __init__(self, master: tk.Widget, **kw) -> None:
        super().__init__(master, **kw)
        self._vm: Optional[LapViewModel] = None
        self._selected_corner_id: Optional[int] = None
        self._build_layout()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_lap(self, vm: Optional[LapViewModel]) -> None:
        """Load *vm* and refresh all sub-widgets.

        Passing ``None`` is equivalent to calling ``clear()``.
        """
        self._vm = vm
        if vm is None:
            self.clear()
            return
        self._update_header(vm)
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
        self._clear_corner_zoom()  # Sprint 3.3 will render the zoom here
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
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(0, weight=1)

        # TrackMap Canvas
        trackmap_lf = ttk.LabelFrame(frame, text="Track Map")
        trackmap_lf.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        trackmap_lf.columnconfigure(0, weight=1)
        trackmap_lf.rowconfigure(0, weight=1)

        self._trackmap_canvas = tk.Canvas(trackmap_lf, bg="#1e1e1e", highlightthickness=0)
        self._trackmap_canvas.grid(row=0, column=0, sticky="nsew")
        self._trackmap_canvas.bind("<Configure>", self._on_trackmap_resize)

        # Corner-Zoom Canvas (placeholder – Story 3.3 will populate)
        cornerzoom_lf = ttk.LabelFrame(frame, text="Corner Zoom")
        cornerzoom_lf.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        cornerzoom_lf.columnconfigure(0, weight=1)
        cornerzoom_lf.rowconfigure(0, weight=1)

        self._cornerzoom_canvas = tk.Canvas(cornerzoom_lf, bg="#1e1e1e", highlightthickness=0)
        self._cornerzoom_canvas.grid(row=0, column=0, sticky="nsew")
        self._cornerzoom_canvas.bind("<Configure>", self._on_cornerzoom_resize)

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

    def _update_header(self, vm: LapViewModel) -> None:
        meta = vm.meta
        if meta is None:
            self._set_header_empty()
            return
        self._lbl_track.configure(text=meta.track or "—")
        self._lbl_car.configure(text=meta.car or "—")
        self._lbl_lap.configure(text=f"Lap {meta.lap_no}")
        if meta.lap_time is not None:
            self._lbl_time.configure(text=_format_laptime(meta.lap_time))
        else:
            self._lbl_time.configure(text="—:—.—")

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
        )

    def _on_trackmap_resize(self, _event=None) -> None:
        if self._vm is not None:
            self._redraw_trackmap()

    # ------------------------------------------------------------------
    # Corner-Zoom helpers
    # ------------------------------------------------------------------

    def _clear_corner_zoom(self) -> None:
        """Clear the corner zoom canvas and show placeholder text."""
        self._cornerzoom_canvas.delete("all")
        self._cornerzoom_canvas.create_text(
            4, 4,
            anchor="nw",
            text="Select a corner on the Track Map",
            fill="#555555",
            font=("", 9),
        )

    def _on_cornerzoom_resize(self, _event=None) -> None:
        # Sprint 3.3 will re-render the zoom here; for now refresh placeholder
        if self._selected_corner_id is None:
            self._clear_corner_zoom()

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
