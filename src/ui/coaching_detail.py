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
_CORNER_ZOOM_LEGEND_ITEMS = (
    ("brake_start", "\u25bc", "#CC2222"),
    ("peak_brake", "\u25cf", "#770000"),
    ("turn_in", "\u25c0", "#FF8800"),
    ("min_speed", "\u2605", "#FFDD00"),
    ("throttle_on", "\u25b2", "#88FF44"),
    ("throttle_off", "\u25b2", "#88FF88"),
    ("throttle_full", "\u25b2", "#00CC00"),
    ("gear_change", "\u2b21", "#4488FF"),
    ("oversteer_event", "\u26a0", "#FF44FF"),
    ("understeer_event", "\u26a0", "#FF8000"),
    ("crest", "\u2312", "#00DDFF"),
)
_ENVIRONMENT_FIELDS = (
    ("Track", "track_temp_c"),
    ("Air", "air_temp_c"),
    ("Hum", "humidity_pct"),
    ("Fog", "fog_pct"),
    ("Skies", "skies"),
    ("Weather", "weather_type"),
    ("P", "air_pressure_hpa"),
)
_OPTIONAL_ENVIRONMENT_FIELDS = {"fog_pct", "weather_type"}


def _read_corner_event_padding_m() -> float:
    """Read corner_event_padding_m from config/defaults.ini (default 50 m)."""
    cp = configparser.ConfigParser()
    cp.read(_PROJECT_ROOT / "config" / "defaults.ini", encoding="utf-8-sig")
    try:
        val = float(cp.get("coaching_analysis", "corner_event_padding_m", fallback="50"))
        return max(0.0, min(10000.0, val))
    except Exception:
        return 50.0


class _Tooltip:
    """Minimal hover tooltip for clipped labels."""

    def __init__(self, widget: tk.Widget, *, should_show=None) -> None:
        self.widget = widget
        self._text = ""
        self._tip: tk.Toplevel | None = None
        self._should_show = should_show
        self.widget.bind("<Enter>", self._on_enter, add="+")
        self.widget.bind("<Leave>", self._on_leave, add="+")
        self.widget.bind("<Motion>", self._on_motion, add="+")
        self.widget.bind("<Destroy>", self._on_leave, add="+")

    def set_text(self, text: str) -> None:
        self._text = text or ""
        if self._tip is not None and not self._text.strip():
            self._hide()

    def _on_enter(self, _event=None) -> None:
        if not self._text.strip():
            return
        if self._should_show is not None and not self._should_show():
            return
        self._show()

    def _on_leave(self, _event=None) -> None:
        self._hide()

    def _on_motion(self, event) -> None:
        if self._tip is None:
            return
        self._tip.geometry(f"+{event.x_root + 12}+{event.y_root + 12}")

    def _show(self) -> None:
        if self._tip is not None:
            return
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        try:
            self._tip.attributes("-topmost", True)
        except tk.TclError:
            pass
        tk.Label(
            self._tip,
            text=self._text,
            justify="left",
            relief="solid",
            borderwidth=1,
            padx=6,
            pady=4,
            background="#ffffe0",
        ).pack()
        self._tip.geometry(
            f"+{self.widget.winfo_pointerx() + 12}+{self.widget.winfo_pointery() + 12}"
        )

    def _hide(self) -> None:
        if self._tip is None:
            return
        try:
            self._tip.destroy()
        except Exception:
            pass
        self._tip = None


class _EnvironmentSummary(ttk.Frame):
    """Single-line environment summary for the track map header."""

    def __init__(self, master: tk.Widget, **kw) -> None:
        super().__init__(master, **kw)
        self.columnconfigure(0, weight=1)
        self._text_var = tk.StringVar(master=self, value="")
        self._label = ttk.Label(
            self,
            textvariable=self._text_var,
            anchor="w",
            justify="left",
        )
        self._label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._tooltip = _Tooltip(self._label, should_show=self._is_clipped)

    def set_environment(self, environment: dict | None, *, track_usage: str | None = None) -> None:
        summary = _build_environment_summary(environment, track_usage=track_usage)
        tooltip_summary = _build_environment_summary(environment, track_usage=track_usage, compact_track_usage=False)
        self._text_var.set(summary)
        self._tooltip.set_text(tooltip_summary)
        if summary:
            self.grid()
        else:
            self.grid_remove()

    def _is_clipped(self) -> bool:
        self.update_idletasks()
        return self._label.winfo_reqwidth() > max(1, self._label.winfo_width())


def _normalize_environment(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    copied = dict(value)
    return copied or None


def _build_environment_summary(
    environment: dict | None,
    *,
    track_usage: str | None = None,
    compact_track_usage: bool = True,
) -> str:
    normalized = _normalize_environment(environment)
    if not normalized:
        normalized = {}

    if not normalized and not str(track_usage or "").strip():
        return ""

    parts: list[str] = []
    for label_text, key in _ENVIRONMENT_FIELDS:
        value_text = _format_environment_field(key, normalized.get(key))
        if not value_text and key in _OPTIONAL_ENVIRONMENT_FIELDS:
            continue
        if value_text:
            parts.append(f"{label_text}: {value_text}")

    wind_text = _build_wind_summary(normalized)
    if wind_text:
        insert_at = 3 if len(parts) >= 3 else len(parts)
        parts.insert(insert_at, f"Wind: {wind_text}")
    elif normalized.get("wind_dir_deg") is not None:
        dir_text = _format_wind_direction(normalized.get("wind_dir_deg"))
        if dir_text:
            insert_at = 3 if len(parts) >= 3 else len(parts)
            parts.insert(insert_at, f"Dir: {dir_text}")

    usage_value = track_usage if str(track_usage or "").strip() else normalized.get("track_usage")
    usage_text = _format_track_usage(usage_value, compact=compact_track_usage)
    if usage_text:
        parts.append(f"Usage: {usage_text}")

    return "  ".join(parts)


def _format_environment_field(key: str, value: object) -> str:
    if value is None:
        return ""
    if key in {"track_temp_c", "air_temp_c"}:
        return _format_environment_number(value, suffix="°C", decimals=1)
    if key in {"humidity_pct", "fog_pct"}:
        return _format_environment_number(value, suffix="%", decimals=1)
    if key == "wind_speed_ms":
        return _format_environment_number(value, suffix=" m/s", decimals=1)
    if key == "wind_dir_deg":
        return _format_wind_direction(value)
    if key == "air_pressure_hpa":
        return _format_environment_number(value, suffix=" hPa", decimals=1)
    return str(value).strip()


def _build_wind_summary(environment: dict[str, object]) -> str:
    speed_text = _format_environment_field("wind_speed_ms", environment.get("wind_speed_ms"))
    dir_text = _format_wind_direction(environment.get("wind_dir_deg"))
    return " ".join(part for part in (speed_text, dir_text) if part)


def _format_wind_direction(value: object) -> str:
    degrees = _format_environment_number(value, suffix="°", decimals=0)
    cardinal = _wind_cardinal(value)
    return f"{degrees}{cardinal}" if degrees else cardinal


def _format_environment_number(value: object, *, suffix: str, decimals: int) -> str:
    try:
        number = float(value)
    except Exception:
        return ""
    if decimals <= 0 or abs(number - round(number)) < 0.05:
        text = str(int(round(number)))
    else:
        text = f"{number:.{decimals}f}".rstrip("0").rstrip(".")
    return f"{text}{suffix}"


def _format_track_usage(value: object, *, compact: bool) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not compact:
        return text
    compact_text = text.removesuffix(" Usage").strip()
    return compact_text or text


def _wind_cardinal(value: object) -> str:
    try:
        degrees = float(value) % 360.0
    except Exception:
        return ""
    directions = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    index = int((degrees + 22.5) // 45) % len(directions)
    return directions[index]


class CoachingDetailView(ttk.Frame):
    """Stable layout container for single-lap visualisation.

    All four sub-areas are independent frames so Sprint 4–6 can extend
    them individually without touching the surrounding structure.
    """

    def __init__(self, master: tk.Widget, **kw) -> None:
        super().__init__(master, **kw)
        self._vm: Optional[LapViewModel] = None
        self._selected_corner_id: Optional[int] = None
        self._event_visibility: dict[str, tk.BooleanVar] = {
            name: tk.BooleanVar(master=self, value=True)
            for name, _, _ in _CORNER_ZOOM_LEGEND_ITEMS
        }
        self._current_events: list = []
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
        self._conditions_summary.set_environment(
            vm.meta.environment if vm.meta is not None else None,
            track_usage=(vm.meta.track_usage if vm.meta is not None else None),
        )
        self._redraw_trackmap()
        self._clear_corner_zoom()
        self._update_scorecard(None)

    def clear(self) -> None:
        """Reset all sub-widgets to the empty / placeholder state."""
        self._vm = None
        self._selected_corner_id = None
        self._set_header_empty()
        self._conditions_summary.set_environment(None, track_usage=None)
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

        trackmap_wrap = ttk.Frame(frame)
        trackmap_wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        trackmap_wrap.columnconfigure(0, weight=1)
        trackmap_wrap.rowconfigure(1, weight=1)

        trackmap_header = ttk.Frame(trackmap_wrap)
        trackmap_header.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        trackmap_header.columnconfigure(0, minsize=90)
        trackmap_header.columnconfigure(1, weight=1)
        ttk.Label(
            trackmap_header,
            text="Track Map",
            font=("", 10, "bold"),
            width=10,
        ).grid(
            row=0,
            column=0,
            sticky="w",
        )
        self._conditions_summary = _EnvironmentSummary(trackmap_header)
        self._conditions_summary.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self._conditions_summary.grid_remove()

        # TrackMap Canvas
        trackmap_lf = ttk.Frame(trackmap_wrap, borderwidth=1, relief="groove")
        trackmap_lf.grid(row=1, column=0, sticky="nsew")
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
        self._zoom_overlay.columnconfigure(0, weight=1)
        self._zoom_overlay.rowconfigure(0, weight=1)
        self._zoom_overlay_visible = False

        self._zoom_back_btn = ttk.Button(
            self._zoom_overlay, text="← Back",
            command=self._hide_corner_zoom_overlay,
        )

        self._zoom_legend_frame = self._build_corner_zoom_legend(self._zoom_overlay)

        self._zoom_canvas = tk.Canvas(
            self._zoom_overlay, bg="#1e1e1e", highlightthickness=0,
        )
        self._zoom_canvas.grid(row=0, column=0, sticky="nsew")
        self._zoom_canvas.bind("<Configure>", self._on_zoom_canvas_resize)
        self._zoom_canvas.bind("<MouseWheel>", self._on_corner_wheel)
        self._zoom_canvas.bind("<Button-4>", self._on_corner_wheel)
        self._zoom_canvas.bind("<Button-5>", self._on_corner_wheel)
        self._zoom_canvas.bind("<ButtonPress-1>", self._on_corner_drag_start)
        self._zoom_canvas.bind("<B1-Motion>", self._on_corner_drag_move)
        self._zoom_canvas.bind("<ButtonRelease-1>", self._on_corner_drag_end)
        self._zoom_canvas.bind("<Double-Button-1>", self._on_corner_reset)
        self._place_corner_zoom_legend()

        return frame

    def _build_corner_zoom_legend(self, master: tk.Widget) -> tk.Frame:
        frame = tk.Frame(master, bg="#1e1e1e", bd=0, highlightthickness=0)
        for row_index, (event_name, symbol, color) in enumerate(_CORNER_ZOOM_LEGEND_ITEMS):
            row = tk.Frame(frame, bg="#1e1e1e", bd=0, highlightthickness=0)
            row.grid(row=row_index, column=0, sticky="w")

            tk.Checkbutton(
                row,
                variable=self._event_visibility[event_name],
                bg="#1e1e1e",
                activebackground="#2e2e2e",
                fg="white",
                selectcolor="#444444",
                relief="flat",
                highlightthickness=0,
                bd=0,
            ).grid(row=0, column=0, sticky="w")
            tk.Label(
                row,
                text=symbol,
                fg=color,
                bg="#1e1e1e",
                width=2,
            ).grid(row=0, column=1, sticky="w", padx=(2, 6))
            tk.Label(row, text=event_name, fg="#f0f0f0", bg="#1e1e1e").grid(
                row=0, column=2, sticky="w"
            )

            self._event_visibility[event_name].trace_add(
                "write",
                lambda *_args: self._redraw_corner_zoom(),
            )
        return frame

    def _place_corner_zoom_legend(self) -> None:
        self._zoom_back_btn.place(in_=self._zoom_canvas, anchor="nw", x=8, y=8)
        self._zoom_back_btn.lift()
        self._zoom_legend_frame.place(in_=self._zoom_canvas, anchor="nw", x=8, y=44)
        self._zoom_legend_frame.lift()

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
            road_geometry=vm.track_road_geometry,
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
        self._place_corner_zoom_legend()
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
        self._current_events = list(events)
        if events:
            print(
                f"[CORNER-ZOOM-DEBUG] events source: corner_events"
                f" ({len(events)} events)"
            )
        else:
            print("[CORNER-ZOOM-DEBUG] events source: fallback_empty")

        visible_events = [
            event for event in self._current_events if self._is_corner_event_visible(event)
        ]

        render_corner_zoom(
            canvas=self._zoom_canvas,
            xy=self._vm.track_xy,
            corner=corner,
            events=visible_events,
            width=w,
            height=h,
            road_geometry=self._vm.track_road_geometry,
            lap_dist_pct=self._vm.lap_dist_pct,
            lo=lo,
            hi=hi,
            zoom=self._corner_zoom,
            offset=self._corner_offset,
            track_length_m=self._vm.track_length_m,
        )
        self._place_corner_zoom_legend()

    def _on_zoom_canvas_resize(self, _event=None) -> None:
        self._redraw_corner_zoom()

    def _clear_corner_zoom(self) -> None:
        """Hide overlay."""
        self._current_events = []
        self._hide_corner_zoom_overlay()

    def _is_corner_event_visible(self, event) -> bool:
        event_name = self._get_corner_event_name(event)
        if not event_name:
            return True
        visibility_var = self._event_visibility.get(event_name)
        if visibility_var is None:
            return True
        return bool(visibility_var.get())

    @staticmethod
    def _get_corner_event_name(event) -> str:
        if isinstance(event, dict):
            raw_name = event.get("name") or event.get("event_type")
        else:
            raw_name = getattr(event, "event_type", None) or getattr(event, "name", None)
        if raw_name is None:
            return ""
        return str(raw_name)

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
