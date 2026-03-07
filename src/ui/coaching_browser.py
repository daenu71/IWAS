"""Runtime module for ui/coaching_browser.py."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from core.coaching.indexer import CoachingIndex, CoachingTreeNode, NodeSummary


RefreshCallback = Callable[[], CoachingIndex | None]
NodeCallback = Callable[[CoachingTreeNode], None]
AnalyzeLapCallback = Callable[[CoachingTreeNode], None]
AnalyzeRunCallback = Callable[[CoachingTreeNode, list[CoachingTreeNode]], None]

_PURPLE = "#BF7FFF"
_ENVIRONMENT_LAYOUT = (
    (("Track", "track_temp_c"), ("Air", "air_temp_c")),
    (("Humidity", "humidity_pct"), ("Fog", "fog_pct")),
    (("Wind", "wind_speed_ms"), ("Dir", "wind_dir_deg")),
    (("Skies", "skies"), ("Weather", "weather_type")),
    (("Pressure", "air_pressure_hpa"),),
)

COACHING_TREE_COLUMN_WIDTHS: dict[str, int] = {
    "#0": 280,
    "analyze": 80,
    "kind": 80,
    "time": 130,
    "lap": 110,
    "last": 150,
}
# Width allowance for the vertical scrollbar in the current ttk dark theme.
COACHING_TREE_SCROLLBAR_WIDTH_PX = 16
# Treeview left/right border + inner chrome allowance.
COACHING_TREE_BORDER_PX = 2
COACHING_BROWSER_CONTENT_WIDTH_PX = (
    sum(COACHING_TREE_COLUMN_WIDTHS.values())
    + COACHING_TREE_SCROLLBAR_WIDTH_PX
    + COACHING_TREE_BORDER_PX
)


@dataclass
class FilterIndex:
    """Gecachte, filterbare Felder aus dem CoachingIndex."""
    tracks: list[str]
    cars: list[str]
    drivers: list[str]
    environments: list[str]
    session_types: list[str]
    lap_statuses: list[str]
    date_min: float | None
    date_max: float | None
    track_temp_min: float | None
    track_temp_max: float | None
    air_temp_min: float | None
    air_temp_max: float | None
    humidity_min: float | None
    humidity_max: float | None
    wind_speed_min: float | None
    wind_speed_max: float | None
    air_pressure_min: float | None
    air_pressure_max: float | None
    skies_values: list[str]
    weather_types: list[str]


@dataclass
class FilterState:
    """Aktiver Filter-Zustand. None-Felder = keine Einschränkung."""

    tracks: list[str] = field(default_factory=list)
    cars: list[str] = field(default_factory=list)
    drivers: list[str] = field(default_factory=list)
    environments: list[str] = field(default_factory=list)
    session_types: list[str] = field(default_factory=list)
    lap_statuses: list[str] = field(default_factory=list)
    skies: list[str] = field(default_factory=list)
    weather_types: list[str] = field(default_factory=list)
    date_from: float | None = None
    date_to: float | None = None
    track_temp_from: float | None = None
    track_temp_to: float | None = None
    air_temp_from: float | None = None
    air_temp_to: float | None = None
    humidity_from: float | None = None
    humidity_to: float | None = None
    wind_speed_from: float | None = None
    wind_speed_to: float | None = None
    air_pressure_from: float | None = None
    air_pressure_to: float | None = None

    def is_empty(self) -> bool:
        """True wenn kein Filter aktiv ist."""
        return (
            not self.tracks and not self.cars and not self.drivers
            and not self.environments and not self.session_types
            and not self.lap_statuses and not self.skies and not self.weather_types
            and self.date_from is None and self.date_to is None
            and self.track_temp_from is None and self.track_temp_to is None
            and self.air_temp_from is None and self.air_temp_to is None
            and self.humidity_from is None and self.humidity_to is None
            and self.wind_speed_from is None and self.wind_speed_to is None
            and self.air_pressure_from is None and self.air_pressure_to is None
        )


class FilterDialog(tk.Toplevel):
    """Modales Filter-Popup fuer den Coaching Browser."""

    _CATEGORY_FIELDS: tuple[tuple[str, str], ...] = (
        ("tracks", "Strecke"),
        ("cars", "Auto"),
        ("drivers", "Fahrer"),
        ("environments", "Umgebung / Event-Typ"),
        ("session_types", "Session-Typ"),
        ("lap_statuses", "Lap-Status"),
    )
    _RANGE_FIELDS: tuple[tuple[str, str, str, str, str], ...] = (
        ("track_temp", "Streckentemperatur", "track_temp_min", "track_temp_max", "°C"),
        ("air_temp", "Lufttemperatur", "air_temp_min", "air_temp_max", "°C"),
        ("humidity", "Luftfeuchtigkeit", "humidity_min", "humidity_max", "%"),
        ("wind_speed", "Windgeschwindigkeit", "wind_speed_min", "wind_speed_max", "m/s"),
        ("air_pressure", "Luftdruck", "air_pressure_min", "air_pressure_max", "hPa"),
    )

    _COMPACT_RANGE_ROWS: tuple[tuple[str, str, str, str, str], ...] = (
        ("Streckentemp.", "track_temp", "track_temp_min", "track_temp_max", "°C"),
        ("Lufttemp.", "air_temp", "air_temp_min", "air_temp_max", "°C"),
        ("Luftfeucht.", "humidity", "humidity_min", "humidity_max", "%"),
        ("Wind", "wind_speed", "wind_speed_min", "wind_speed_max", "m/s"),
        ("Luftdruck", "air_pressure", "air_pressure_min", "air_pressure_max", "hPa"),
    )

    def __init__(
        self,
        parent: tk.Widget,
        filter_index: FilterIndex,
        current: FilterState | None,
        on_apply: Callable[[FilterState | None], None],
    ) -> None:
        super().__init__(parent)
        self.title("Filter")
        self.resizable(False, False)
        self.transient(parent)
        self._filter_index = filter_index
        self._current = current or FilterState()
        self._on_apply_cb = on_apply
        self._checkbox_vars: dict[str, dict[str, tk.BooleanVar]] = {}
        self._visible_range_fields: set[str] = set()
        self._error_var = tk.StringVar(value="")
        self._mousewheel_bound = False
        self._date_from_var = tk.StringVar(value=self._format_date_value(self._current.date_from))
        self._date_to_var = tk.StringVar(value=self._format_date_value(self._current.date_to))
        self._range_vars: dict[str, tuple[tk.StringVar, tk.StringVar, str]] = {
            "track_temp": (
                tk.StringVar(value=self._format_number_value(self._current.track_temp_from)),
                tk.StringVar(value=self._format_number_value(self._current.track_temp_to)),
                "Streckentemperatur",
            ),
            "air_temp": (
                tk.StringVar(value=self._format_number_value(self._current.air_temp_from)),
                tk.StringVar(value=self._format_number_value(self._current.air_temp_to)),
                "Lufttemperatur",
            ),
            "humidity": (
                tk.StringVar(value=self._format_number_value(self._current.humidity_from)),
                tk.StringVar(value=self._format_number_value(self._current.humidity_to)),
                "Luftfeuchtigkeit",
            ),
            "wind_speed": (
                tk.StringVar(value=self._format_number_value(self._current.wind_speed_from)),
                tk.StringVar(value=self._format_number_value(self._current.wind_speed_to)),
                "Windgeschwindigkeit",
            ),
            "air_pressure": (
                tk.StringVar(value=self._format_number_value(self._current.air_pressure_from)),
                tk.StringVar(value=self._format_number_value(self._current.air_pressure_to)),
                "Luftdruck",
            ),
        }

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        content_wrap = ttk.Frame(self, padding=(12, 12, 12, 0))
        content_wrap.grid(row=0, column=0, sticky="nsew")
        content_wrap.columnconfigure(0, weight=1)
        content_wrap.rowconfigure(0, weight=1)

        self._canvas = tk.Canvas(
            content_wrap,
            highlightthickness=0,
            borderwidth=0,
            width=488,
            height=560,
        )
        self._canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(content_wrap, orient="vertical", command=self._canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._canvas.configure(yscrollcommand=scrollbar.set)

        self._scroll_frame = ttk.Frame(self._canvas)
        self._scroll_frame.columnconfigure(0, weight=1)
        self._canvas_window = self._canvas.create_window((0, 0), window=self._scroll_frame, anchor="nw")
        self._scroll_frame.bind("<Configure>", self._on_scroll_frame_configure, add="+")
        self._canvas.bind("<Configure>", self._on_canvas_configure, add="+")
        self.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self._mousewheel_bound = True
        self.bind("<Destroy>", self._on_destroy, add="+")

        footer = ttk.Frame(self, padding=12)
        footer.grid(row=1, column=0, sticky="ew")
        footer.columnconfigure(1, weight=1)
        ttk.Button(footer, text="Reset", command=self._on_reset).grid(row=0, column=0, sticky="w")
        buttons = ttk.Frame(footer)
        buttons.grid(row=0, column=2, sticky="e")
        ttk.Button(buttons, text="Abbrechen", command=self._on_cancel).grid(row=0, column=0, sticky="e")
        ttk.Button(buttons, text="Anwenden", command=self._on_apply).grid(
            row=0,
            column=1,
            sticky="e",
            padx=(6, 0),
        )

        self._build_sections()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.bind("<Escape>", lambda _event: self._on_cancel(), add="+")
        self.update_idletasks()
        self._position_over_parent(parent)

    def _build_sections(self) -> None:
        """Build all filter UI sections."""
        row = 0
        hint = tk.Label(
            self._scroll_frame,
            text="Mehrfachauswahl innerhalb einer Gruppe: OR  ·  Zwischen Gruppen: AND",
            font=("TkDefaultFont", 8),
            fg="#888888",
            anchor="w",
            justify="left",
        )
        hint.grid(row=row, column=0, sticky="ew", padx=8, pady=(4, 8))
        row += 1
        for field_name, title in self._CATEGORY_FIELDS:
            values = list(getattr(self._filter_index, field_name))
            selected = list(getattr(self._current, field_name))
            if not values:
                continue
            frame = ttk.LabelFrame(self._scroll_frame, text=title, padding=(10, 8))
            frame.grid(row=row, column=0, sticky="ew", pady=(0, 10))
            frame.columnconfigure(0, weight=1)
            self._build_checkbox_grid(frame, field_name, values, selected)
            row += 1

        date_frame = ttk.LabelFrame(self._scroll_frame, text="Datum", padding=(10, 8))
        date_frame.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        date_frame.columnconfigure(1, weight=1)
        date_frame.columnconfigure(4, weight=1)
        ttk.Label(date_frame, text="Von").grid(row=0, column=0, sticky="w")
        ttk.Entry(date_frame, textvariable=self._date_from_var, width=14).grid(row=0, column=1, sticky="w")
        ttk.Label(date_frame, text="Bis").grid(row=0, column=3, sticky="w", padx=(16, 0))
        ttk.Entry(date_frame, textvariable=self._date_to_var, width=14).grid(row=0, column=4, sticky="w")
        if self._filter_index.date_min is not None or self._filter_index.date_max is not None:
            ttk.Label(
                date_frame,
                text=(
                    f"Verfuegbar: {self._format_date_value(self._filter_index.date_min) or '-'}"
                    f" bis {self._format_date_value(self._filter_index.date_max) or '-'}"
                ),
            ).grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))
        tk.Label(
            date_frame,
            textvariable=self._error_var,
            fg="#b91c1c",
            anchor="w",
            justify="left",
        ).grid(row=2, column=0, columnspan=5, sticky="ew", pady=(4, 0))
        row += 1

        if self._build_compact_range_section(row):
            row += 1

        if self._filter_index.skies_values or self._filter_index.weather_types:
            weather_frame = ttk.LabelFrame(self._scroll_frame, text="Himmel / Wetter", padding=(10, 8))
            weather_frame.grid(row=row, column=0, sticky="ew", pady=(0, 10))
            weather_frame.columnconfigure(0, weight=1)
            weather_row = 0
            if self._filter_index.skies_values:
                ttk.Label(weather_frame, text="Himmel").grid(row=weather_row, column=0, sticky="w", pady=(0, 4))
                weather_row += 1
                inner = ttk.Frame(weather_frame)
                inner.grid(row=weather_row, column=0, sticky="ew", pady=(0, 8))
                self._build_two_column_checkbox_grid(
                    inner,
                    "skies",
                    self._filter_index.skies_values,
                    self._current.skies,
                )
                weather_row += 1
            if self._filter_index.weather_types:
                ttk.Label(weather_frame, text="Wetter").grid(row=weather_row, column=0, sticky="w", pady=(0, 4))
                weather_row += 1
                inner = ttk.Frame(weather_frame)
                inner.grid(row=weather_row, column=0, sticky="ew")
                self._build_two_column_checkbox_grid(
                    inner,
                    "weather_types",
                    self._filter_index.weather_types,
                    self._current.weather_types,
                )

    def _build_checkbox_grid(
        self,
        parent: ttk.Frame | ttk.LabelFrame,
        field_name: str,
        values: list[str],
        selected: list[str],
    ) -> None:
        """Render a checkbox grid for one categorical filter group."""
        selected_values = set(selected)
        self._checkbox_vars[field_name] = {}
        grid = ttk.Frame(parent)
        grid.grid(row=0, column=0, sticky="ew")
        column_count = 1 if len(values) <= 6 else 2 if len(values) <= 12 else 3
        rows_per_column = (len(values) + column_count - 1) // column_count
        for column in range(column_count):
            grid.columnconfigure(column, weight=1)
        for index, value in enumerate(values):
            row = index % rows_per_column
            column = index // rows_per_column
            var = tk.BooleanVar(value=value in selected_values)
            ttk.Checkbutton(grid, text=value, variable=var).grid(
                row=row,
                column=column,
                sticky="w",
                padx=(0, 12),
                pady=2,
            )
            self._checkbox_vars[field_name][value] = var

    def _build_compact_range_section(self, row: int) -> bool:
        """Render the compact environment range grid."""
        visible_rows: list[tuple[str, str, float | None, float | None, str]] = []
        for label, field_name, min_attr, max_attr, unit in self._COMPACT_RANGE_ROWS:
            minimum = getattr(self._filter_index, min_attr)
            maximum = getattr(self._filter_index, max_attr)
            if minimum is None and maximum is None:
                continue
            visible_rows.append((label, field_name, minimum, maximum, unit))
            self._visible_range_fields.add(field_name)

        if not visible_rows:
            return False

        frame = ttk.LabelFrame(self._scroll_frame, text="Session Conditions", padding=(10, 8))
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        for column in range(7):
            frame.columnconfigure(column, weight=0)

        for row_index, (label, field_name, minimum, maximum, unit) in enumerate(visible_rows):
            from_var, to_var, _field_label = self._range_vars[field_name]
            tk.Label(frame, text=label, width=12, anchor="w").grid(
                row=row_index,
                column=0,
                sticky="w",
                padx=(0, 6),
                pady=2,
            )
            tk.Label(frame, text="Von:", anchor="w").grid(row=row_index, column=1, sticky="w", pady=2)
            tk.Entry(frame, textvariable=from_var, width=6).grid(
                row=row_index,
                column=2,
                sticky="w",
                padx=(0, 8),
                pady=2,
            )
            tk.Label(frame, text="Bis:", anchor="w").grid(row=row_index, column=3, sticky="w", pady=2)
            tk.Entry(frame, textvariable=to_var, width=6).grid(
                row=row_index,
                column=4,
                sticky="w",
                padx=(0, 8),
                pady=2,
            )
            tk.Label(frame, text=unit, anchor="w").grid(
                row=row_index,
                column=5,
                sticky="w",
                padx=(0, 8),
                pady=2,
            )
            if minimum is not None and maximum is not None:
                tk.Label(
                    frame,
                    text=f"({minimum:.0f}–{maximum:.0f})",
                    font=("TkDefaultFont", 8),
                    fg="#888888",
                    anchor="w",
                ).grid(row=row_index, column=6, sticky="w", pady=2)
        return True

    def _build_two_column_checkbox_grid(
        self,
        parent: ttk.Frame,
        field_name: str,
        values: list[str],
        selected: list[str],
    ) -> None:
        """Render a compact two-column checkbox grid for one field."""
        selected_values = set(selected)
        self._checkbox_vars[field_name] = {}
        for column in range(2):
            parent.columnconfigure(column, weight=1)
        for index, value in enumerate(values):
            row = index // 2
            column = index % 2
            var = tk.BooleanVar(value=value in selected_values)
            tk.Checkbutton(parent, text=value, variable=var, anchor="w").grid(
                row=row,
                column=column,
                sticky="w",
                padx=4,
                pady=2,
            )
            self._checkbox_vars[field_name][value] = var

    def _selected_values(self, field_name: str) -> list[str]:
        """Return all selected checkbox values for the given field."""
        return [
            value
            for value, var in self._checkbox_vars.get(field_name, {}).items()
            if var.get()
        ]

    def _on_apply(self) -> None:
        """Collect filter values and close the dialog if valid."""
        state = self._build_filter_state()
        if state is None:
            return
        self._on_apply_cb(state)
        self.destroy()

    def _on_reset(self) -> None:
        """Reset all filters and close the dialog."""
        self._on_apply_cb(FilterState())
        self.destroy()

    def _on_cancel(self) -> None:
        """Close without applying changes."""
        self._on_apply_cb(None)
        self.destroy()

    def _build_filter_state(self) -> FilterState | None:
        """Collect filter values and return them if validation succeeds."""
        self._error_var.set("")
        try:
            result = FilterState(
                tracks=self._selected_values("tracks"),
                cars=self._selected_values("cars"),
                drivers=self._selected_values("drivers"),
                environments=self._selected_values("environments"),
                session_types=self._selected_values("session_types"),
                lap_statuses=self._selected_values("lap_statuses"),
                skies=self._selected_values("skies"),
                weather_types=self._selected_values("weather_types"),
                date_from=self._parse_date_value("Von", self._date_from_var.get()),
                date_to=self._parse_date_value("Bis", self._date_to_var.get(), end_of_day=True),
            )
            if result.date_from is not None and result.date_to is not None and result.date_from > result.date_to:
                raise ValueError("Datum: 'Von' darf nicht nach 'Bis' liegen.")
            for field_name, _title, _min_attr, _max_attr, _unit in self._RANGE_FIELDS:
                if field_name not in self._visible_range_fields:
                    continue
                from_var, to_var, label = self._range_vars[field_name]
                range_from = self._parse_number_value(f"{label} von", from_var.get())
                range_to = self._parse_number_value(f"{label} bis", to_var.get())
                if range_from is not None and range_to is not None and range_from > range_to:
                    raise ValueError(f"{label}: 'Von' darf nicht groesser als 'Bis' sein.")
                setattr(result, f"{field_name}_from", range_from)
                setattr(result, f"{field_name}_to", range_to)
        except ValueError as exc:
            self._error_var.set(str(exc))
            return None
        return result

    def _on_scroll_frame_configure(self, _event=None) -> None:
        """Update the canvas scrollregion when inner content changes."""
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        """Keep the scrollable frame width in sync with the canvas width."""
        self._canvas.itemconfigure(self._canvas_window, width=event.width)

    def _on_mousewheel(self, event) -> str:
        """Scroll the filter body with the mouse wheel on Windows."""
        if event.widget is not None and event.widget.winfo_toplevel() is not self:
            return ""
        if event.delta:
            self._canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _on_destroy(self, event) -> None:
        """Remove global mouse wheel bindings when the dialog closes."""
        if event.widget is self and self._mousewheel_bound:
            self.unbind_all("<MouseWheel>")
            self._mousewheel_bound = False

    def _position_over_parent(self, parent: tk.Widget) -> None:
        """Center the dialog over its parent window."""
        width = 520
        max_height = max(420, self.winfo_screenheight() - 120)
        height = min(max_height, max(420, self.winfo_reqheight()))
        parent_root = parent.winfo_toplevel()
        parent_root.update_idletasks()
        x = parent_root.winfo_rootx() + max(0, (parent_root.winfo_width() - width) // 2)
        y = parent_root.winfo_rooty() + max(0, (parent_root.winfo_height() - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

    @staticmethod
    def _format_date_value(timestamp: float | None) -> str:
        """Convert a timestamp into a YYYY-MM-DD string."""
        if timestamp is None:
            return ""
        try:
            return datetime.fromtimestamp(float(timestamp)).strftime("%Y-%m-%d")
        except Exception:
            return ""

    @staticmethod
    def _format_number_value(value: float | None) -> str:
        """Convert an optional float into a compact entry string."""
        if value is None:
            return ""
        try:
            return f"{float(value):g}"
        except Exception:
            return ""

    @staticmethod
    def _parse_date_value(label: str, raw_value: str, *, end_of_day: bool = False) -> float | None:
        """Parse an optional YYYY-MM-DD string into a Unix timestamp."""
        text = raw_value.strip()
        if not text:
            return None
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"{label}: Bitte YYYY-MM-DD eingeben.") from exc
        if end_of_day:
            return (parsed + timedelta(days=1)).timestamp() - 0.001
        return parsed.timestamp()

    @staticmethod
    def _parse_number_value(label: str, raw_value: str) -> float | None:
        """Parse an optional numeric entry value."""
        text = raw_value.strip().replace(",", ".")
        if not text:
            return None
        try:
            return float(text)
        except ValueError as exc:
            raise ValueError(f"{label}: Bitte eine Zahl eingeben.") from exc


class _EnvironmentTooltip:
    """Compact hover tooltip for lap environment data."""

    def __init__(self, owner: tk.Widget) -> None:
        self._owner = owner
        self._window: tk.Toplevel | None = None
        self._row_frames: list[tk.Frame] = []
        self._value_labels: dict[str, tk.Label] = {}

    @property
    def visible(self) -> bool:
        return self._window is not None

    def show(self, environment: dict[str, Any] | None, *, x_root: int, y_root: int) -> None:
        normalized = _normalize_environment(environment)
        if normalized is None:
            self.hide()
            return
        self._ensure_window()
        if self._window is None:
            return
        any_row_visible = False
        for row_frame, row_fields in zip(self._row_frames, _ENVIRONMENT_LAYOUT):
            row_visible = False
            for _label_text, key in row_fields:
                text = _format_environment_field(key, normalized.get(key))
                self._value_labels[key].configure(text=text)
                if text:
                    row_visible = True
            if row_visible:
                row_frame.grid()
                any_row_visible = True
            else:
                row_frame.grid_remove()
        if not any_row_visible:
            self.hide()
            return
        self.move(x_root=x_root, y_root=y_root)
        self._window.deiconify()
        self._window.lift()

    def move(self, *, x_root: int, y_root: int) -> None:
        if self._window is None:
            return
        self._window.geometry(f"+{int(x_root) + 14}+{int(y_root) + 14}")

    def hide(self) -> None:
        if self._window is None:
            return
        try:
            self._window.destroy()
        except Exception:
            pass
        self._window = None
        self._row_frames = []
        self._value_labels = {}

    def _ensure_window(self) -> None:
        if self._window is not None:
            return
        window = tk.Toplevel(self._owner)
        window.overrideredirect(True)
        try:
            window.attributes("-topmost", True)
        except Exception:
            pass
        window.configure(bg="#4b5563")
        body = tk.Frame(window, bg="#111827", padx=8, pady=6)
        body.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(
            body,
            text="Session Conditions",
            bg="#111827",
            fg="#f9fafb",
            anchor="w",
            font=("TkDefaultFont", 9, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))
        table = tk.Frame(body, bg="#111827")
        table.grid(row=1, column=0, sticky="w")
        for row_index, row_fields in enumerate(_ENVIRONMENT_LAYOUT):
            row_frame = tk.Frame(table, bg="#111827")
            row_frame.grid(row=row_index, column=0, sticky="w")
            self._row_frames.append(row_frame)
            for pair_index, (label_text, key) in enumerate(row_fields):
                base_col = pair_index * 4
                tk.Label(
                    row_frame,
                    text=f"{label_text}:",
                    bg="#111827",
                    fg="#9ca3af",
                    anchor="e",
                ).grid(row=0, column=base_col, sticky="e", padx=(0, 4))
                value_label = tk.Label(
                    row_frame,
                    text="",
                    bg="#111827",
                    fg="#f9fafb",
                    anchor="w",
                )
                value_label.grid(
                    row=0,
                    column=base_col + 1,
                    sticky="w",
                    padx=(0, 12 if pair_index < len(row_fields) - 1 else 0),
                )
                self._value_labels[key] = value_label
        self._window = window


class CoachingBrowser(ttk.Frame):
    """Container and behavior for Coaching Browser."""
    def __init__(
        self,
        master: tk.Widget,
        *,
        on_refresh: RefreshCallback | None = None,
        on_open_folder: NodeCallback | None = None,
        on_delete_node: NodeCallback | None = None,
        on_select_node: NodeCallback | None = None,
        on_analyze_lap: AnalyzeLapCallback | None = None,
        on_analyze_run: AnalyzeRunCallback | None = None,
    ) -> None:
        """Implement init logic."""
        super().__init__(master)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self._on_refresh = on_refresh
        self._on_open_folder = on_open_folder
        self._on_delete_node = on_delete_node
        self._on_select_node = on_select_node
        self._on_analyze_lap = on_analyze_lap
        self._on_analyze_run = on_analyze_run

        self._index: CoachingIndex | None = None
        self._filter_index: FilterIndex | None = None
        self._filter_dialog: FilterDialog | None = None
        self._active_filter: FilterState | None = None
        self._filtered_nodes_by_id: dict[str, CoachingTreeNode] | None = None
        self._expanded_ids: set[str] = set()
        self._message_var = tk.StringVar(value="")
        self._stats_var = tk.StringVar(value="No sessions loaded.")
        self._best_overlays: list[tk.Label] = []
        self._analyze_buttons: list[tk.Button] = []
        self._best_text: dict[str, str] = {}  # iid → purple time text
        self._overlay_after_id: str | None = None
        self._overlay_font: tkfont.Font | None = None
        self._overlay_row_bg: str = "#FFFFFF"
        self._overlay_sel_bg: str = "#0078D4"
        self._environment_hover_after_id: str | None = None
        self._environment_hover_iid: str | None = None
        self._environment_tooltip_iid: str | None = None
        self._environment_pointer: tuple[int, int] = (0, 0)

        top = ttk.Frame(self)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        top.columnconfigure(2, weight=1)
        ttk.Button(top, text="Refresh", command=self.refresh).grid(row=0, column=0, sticky="w")
        self._filter_btn = ttk.Button(top, text="⊽ Filter", command=self._open_filter_dialog)
        self._filter_btn.grid(row=0, column=1, sticky="w", padx=(6, 0))
        ttk.Label(top, textvariable=self._stats_var).grid(row=0, column=2, sticky="e", padx=(8, 0))

        tree_wrap = ttk.Frame(self)
        tree_wrap.grid(row=1, column=0, sticky="nsew")
        tree_wrap.columnconfigure(0, weight=1)
        tree_wrap.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            tree_wrap,
            columns=("analyze", "kind", "time", "lap", "last"),
            show="tree headings",
            selectmode="browse",
        )
        self._environment_tooltip = _EnvironmentTooltip(self.tree)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.heading("#0", text="Name", anchor="w")
        self.tree.heading("analyze", text="", anchor="center")
        self.tree.heading("kind", text="Type", anchor="w")
        self.tree.heading("time", text="Time", anchor="w")
        self.tree.heading("lap", text="Laps", anchor="w")
        self.tree.heading("last", text="Last Driven", anchor="w")
        self.tree.column(
            "#0",
            width=COACHING_TREE_COLUMN_WIDTHS["#0"],
            minwidth=COACHING_TREE_COLUMN_WIDTHS["#0"],
            stretch=False,
            anchor="w",
        )
        self.tree.column(
            "analyze",
            width=COACHING_TREE_COLUMN_WIDTHS["analyze"],
            minwidth=COACHING_TREE_COLUMN_WIDTHS["analyze"],
            stretch=False,
            anchor="center",
        )
        self.tree.column(
            "kind",
            width=COACHING_TREE_COLUMN_WIDTHS["kind"],
            minwidth=COACHING_TREE_COLUMN_WIDTHS["kind"],
            stretch=False,
            anchor="w",
        )
        self.tree.column(
            "time",
            width=COACHING_TREE_COLUMN_WIDTHS["time"],
            minwidth=COACHING_TREE_COLUMN_WIDTHS["time"],
            stretch=False,
            anchor="w",
        )
        self.tree.column(
            "lap",
            width=COACHING_TREE_COLUMN_WIDTHS["lap"],
            minwidth=COACHING_TREE_COLUMN_WIDTHS["lap"],
            stretch=False,
            anchor="w",
        )
        self.tree.column(
            "last",
            width=COACHING_TREE_COLUMN_WIDTHS["last"],
            minwidth=COACHING_TREE_COLUMN_WIDTHS["last"],
            stretch=False,
            anchor="w",
        )

        y_scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self._tree_yview)
        y_scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=y_scroll.set)

        actions = ttk.Frame(self)
        actions.grid(row=2, column=0, sticky="ew", pady=(6, 4))
        self._btn_open = ttk.Button(actions, text="Open Folder", command=self._handle_open_folder)
        self._btn_open.grid(row=0, column=0, sticky="w")
        self._btn_delete = ttk.Button(actions, text="Delete", command=self._handle_delete)
        self._btn_delete.grid(row=0, column=1, sticky="w", padx=(6, 0))

        ttk.Label(self, textvariable=self._message_var).grid(row=3, column=0, sticky="ew")

        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<<TreeviewOpen>>", lambda _: self._schedule_overlay_refresh(5), add="+")
        self.tree.bind("<<TreeviewClose>>", lambda _: self._schedule_overlay_refresh(5), add="+")
        self.tree.bind("<MouseWheel>", lambda _: self._schedule_overlay_refresh(30), add="+")
        self.tree.bind("<Configure>", lambda _: self._schedule_overlay_refresh(10), add="+")
        self.tree.bind("<MouseWheel>", lambda _e: self._on_tree_leave_for_environment(), add="+")
        self.tree.bind("<Button-4>", lambda _e: self._on_tree_leave_for_environment(), add="+")
        self.tree.bind("<Button-5>", lambda _e: self._on_tree_leave_for_environment(), add="+")
        self.tree.bind("<Motion>", self._on_tree_motion_for_environment, add="+")
        self.tree.bind("<Leave>", self._on_tree_leave_for_environment, add="+")
        self.tree.bind("<ButtonPress-1>", lambda _e: self._on_tree_leave_for_environment(), add="+")
        self._cache_overlay_style()
        self._update_filter_badge()
        self._update_action_buttons()

    def set_index(self, index: CoachingIndex | None) -> None:
        """Implement set index logic."""
        self._capture_expanded_state()
        selected_id = self._selected_id()
        self._index = index
        self._filter_index = _build_filter_index(index) if index is not None else None
        self._rebuild_tree(selected_id=selected_id)

    def refresh(self) -> None:
        """Implement refresh logic."""
        self._capture_expanded_state()
        selected_id = self._selected_id()
        if callable(self._on_refresh):
            try:
                new_index = self._on_refresh()
            except Exception as exc:
                self.set_message(f"Refresh failed: {exc}")
                return
            if new_index is not None:
                self._index = new_index
                self._filter_index = _build_filter_index(new_index)
        self._rebuild_tree(selected_id=selected_id)

    def set_message(self, message: str) -> None:
        """Implement set message logic."""
        self._message_var.set(str(message or ""))

    def selected_node(self) -> CoachingTreeNode | None:
        """Implement selected node logic."""
        lookup = self._filtered_nodes_by_id or (
            self._index.nodes_by_id if self._index is not None else {}
        )
        item_id = self._selected_id()
        if not item_id:
            return None
        return lookup.get(item_id)

    def _rebuild_tree(self, *, selected_id: str | None) -> None:
        """Implement rebuild tree logic."""
        self._cancel_environment_tooltip_schedule()
        self._environment_hover_iid = None
        self._hide_environment_tooltip()
        self._clear_overlays()
        self._best_text.clear()
        self.tree.delete(*self.tree.get_children(""))
        index = self._index
        if index is None:
            self._filtered_nodes_by_id = None
            self._stats_var.set("No sessions loaded.")
            self._update_action_buttons()
            return
        filter_active = self._active_filter is not None and not self._active_filter.is_empty()
        tracks = index.tracks
        if filter_active and self._active_filter is not None:
            tracks = _apply_filter(tracks, self._active_filter)
            filtered_nodes_by_id: dict[str, CoachingTreeNode] = {}

            def _register_filtered(node: CoachingTreeNode) -> None:
                filtered_nodes_by_id[node.id] = node
                for child in node.children:
                    _register_filtered(child)

            for track in tracks:
                _register_filtered(track)
            self._filtered_nodes_by_id = filtered_nodes_by_id
        else:
            self._filtered_nodes_by_id = None
        for node in tracks:
            self._insert_node("", node)
        best_ids = _compute_best_ids(index)
        self._build_best_text(index, best_ids)
        self._restore_expanded_state()
        if selected_id and self.tree.exists(selected_id):
            self.tree.selection_set(selected_id)
            self.tree.focus(selected_id)
            self.tree.see(selected_id)
        if filter_active:
            visible_sessions = sum(
                len(car.children)
                for track in tracks
                for car in track.children
            )
            visible_runs = sum(
                len(event.children)
                for track in tracks
                for car in track.children
                for event in car.children
            )
            visible_laps = sum(
                len(run.children)
                for track in tracks
                for car in track.children
                for event in car.children
                for run in event.children
            )
            self._stats_var.set(
                f"Sessions: {visible_sessions}  "
                f"Runs: {visible_runs}  "
                f"Laps: {visible_laps}  "
                f"(gefiltert)"
            )
        else:
            self._stats_var.set(
                f"Sessions: {index.session_count}  Runs: {index.run_count}  Laps: {index.lap_count}"
            )
        self._update_action_buttons()
        self._schedule_overlay_refresh(10)

    def _open_filter_dialog(self) -> None:
        """Open the filter dialog and rebuild the tree after applying."""
        if self._filter_index is None:
            return
        if self._filter_dialog is not None:
            try:
                self._filter_dialog.lift()
                self._filter_dialog.focus_force()
                return
            except tk.TclError:
                self._filter_dialog = None

        def on_apply(result: FilterState | None) -> None:
            self._filter_dialog = None
            if result is None:
                return
            self._active_filter = result if not result.is_empty() else None
            self._update_filter_badge()
            self._rebuild_tree(selected_id=self._selected_id())

        self._filter_dialog = FilterDialog(
            self,
            self._filter_index,
            self._active_filter,
            on_apply=on_apply,
        )

    def _update_filter_badge(self) -> None:
        """Update the filter button caption with the number of active groups."""
        active = self._active_filter
        if active is None or active.is_empty():
            self._filter_btn.config(text="⊽ Filter")
            return
        count = sum([
            bool(active.tracks),
            bool(active.cars),
            bool(active.drivers),
            bool(active.environments),
            bool(active.session_types),
            bool(active.lap_statuses),
            bool(active.skies),
            bool(active.weather_types),
            active.date_from is not None or active.date_to is not None,
            active.track_temp_from is not None or active.track_temp_to is not None,
            active.air_temp_from is not None or active.air_temp_to is not None,
            active.humidity_from is not None or active.humidity_to is not None,
            active.wind_speed_from is not None or active.wind_speed_to is not None,
            active.air_pressure_from is not None or active.air_pressure_to is not None,
        ])
        self._filter_btn.config(text=f"⊽ Filter ({count})")

    def _insert_node(self, parent_iid: str, node: CoachingTreeNode) -> None:
        """Implement insert node logic."""
        values = (
            "",  # analyze — button placed as overlay
            _type_col_value(node),
            _format_time_col(node),
            _format_lap_col(node),
            _format_last_driven(node.summary.last_driven_ts),
        )
        self.tree.insert(parent_iid, "end", iid=node.id, text=node.label, values=values, open=(node.id in self._expanded_ids))
        children = node.children
        if node.kind == "event":
            children = sorted(
                node.children,
                key=lambda child: (
                    child.run_id is None,
                    child.run_id if child.run_id is not None else 0,
                    child.label,
                ),
            )
        for child in children:
            self._insert_node(node.id, child)

    def _capture_expanded_state(self) -> None:
        """Implement capture expanded state logic."""
        expanded: set[str] = set()

        def walk(parent: str) -> None:
            """Implement walk logic."""
            for iid in self.tree.get_children(parent):
                if self.tree.item(iid, "open"):
                    expanded.add(iid)
                walk(iid)

        walk("")
        self._expanded_ids = expanded

    def _restore_expanded_state(self) -> None:
        """Implement restore expanded state logic."""
        for iid in list(self._expanded_ids):
            if self.tree.exists(iid):
                self.tree.item(iid, open=True)

    def _selected_id(self) -> str | None:
        """Implement selected id logic."""
        sel = self.tree.selection()
        if not sel:
            return None
        return str(sel[0])

    def _on_tree_select(self, _event=None) -> None:
        """Implement on tree select logic."""
        self._hide_environment_tooltip()
        self._update_action_buttons()
        self._schedule_overlay_refresh(1)
        node = self.selected_node()
        if node is None:
            return
        if callable(self._on_select_node):
            self._on_select_node(node)

    def _on_double_click(self, _event=None) -> None:
        """Implement on double click logic."""
        node = self.selected_node()
        if node is None:
            return
        if node.can_open_folder:
            self._handle_open_folder()

    def _on_tree_motion_for_environment(self, event) -> None:
        """Track lap-row hover state for the environment tooltip."""
        self._environment_pointer = (int(event.x_root), int(event.y_root))
        iid = str(self.tree.identify_row(event.y) or "")
        environment = self._tooltip_environment_for_iid(iid)
        if environment is None:
            self._cancel_environment_tooltip_schedule()
            self._environment_hover_iid = None
            self._hide_environment_tooltip()
            return
        if iid != self._environment_hover_iid:
            self._cancel_environment_tooltip_schedule()
            self._environment_hover_iid = iid
            self._hide_environment_tooltip()
            self._environment_hover_after_id = self.after(600, self._show_environment_tooltip)
            return
        if self._environment_tooltip.visible and self._environment_tooltip_iid == iid:
            self._environment_tooltip.move(x_root=event.x_root, y_root=event.y_root)

    def _on_tree_leave_for_environment(self, _event=None) -> None:
        """Hide tooltip immediately when the pointer leaves the tree."""
        self._cancel_environment_tooltip_schedule()
        self._environment_hover_iid = None
        self._hide_environment_tooltip()

    def _handle_open_folder(self) -> None:
        """Implement handle open folder logic."""
        node = self.selected_node()
        if node is None or not node.can_open_folder:
            return
        if callable(self._on_open_folder):
            self._on_open_folder(node)

    def _handle_delete(self) -> None:
        """Implement handle delete logic."""
        node = self.selected_node()
        if node is None or not node.can_delete:
            return
        if callable(self._on_delete_node):
            self._on_delete_node(node)

    def _tree_yview(self, *args) -> None:
        """Handle yview scroll and keep overlays in sync."""
        self._hide_environment_tooltip()
        self.tree.yview(*args)
        self._schedule_overlay_refresh(30)

    def _build_best_text(self, index: CoachingIndex, best_ids: set[str]) -> None:
        """Populate _best_text: maps iid → the exact time text to paint purple."""
        for iid in best_ids:
            node = index.nodes_by_id.get(iid)
            if node is None:
                continue
            if node.kind == "lap":
                lap_summary = _node_lap_summary(node)
                if not _lap_is_valid_for_best(node.summary, lap_summary=lap_summary):
                    continue
            purple_text = _format_time_col(node)
            if purple_text and purple_text != "na":
                self._best_text[iid] = purple_text

    def _cache_overlay_style(self) -> None:
        """Load and cache Treeview style values (row/sel background, font)."""
        style = ttk.Style()
        self._overlay_row_bg = style.lookup("Treeview", "fieldbackground") or "#FFFFFF"
        self._overlay_sel_bg = style.lookup("Treeview", "selectbackground") or "#0078D4"
        try:
            font_name = style.lookup("Treeview", "font")
            self._overlay_font = tkfont.nametofont(font_name) if font_name else tkfont.nametofont("TkDefaultFont")
        except Exception:
            self._overlay_font = tkfont.nametofont("TkDefaultFont")

    def _schedule_overlay_refresh(self, delay_ms: int) -> None:
        """Cancel any pending overlay refresh and schedule a new one after delay_ms."""
        if self._overlay_after_id is not None:
            try:
                self.after_cancel(self._overlay_after_id)
            except Exception:
                pass
        self._overlay_after_id = self.after(delay_ms, self._refresh_overlays)

    def _clear_overlays(self) -> None:
        """Destroy all existing overlay labels and buttons."""
        for lbl in self._best_overlays:
            lbl.destroy()
        self._best_overlays.clear()
        for btn in self._analyze_buttons:
            btn.destroy()
        self._analyze_buttons.clear()

    def _all_tree_iids(self) -> list[str]:
        """Return all item IDs currently in the tree (all levels)."""
        result: list[str] = []

        def walk(parent: str) -> None:
            for iid in self.tree.get_children(parent):
                result.append(iid)
                walk(iid)

        walk("")
        return result

    def _refresh_overlays(self) -> None:
        """Recreate purple overlay labels and analyze button overlays."""
        self._overlay_after_id = None
        self._clear_overlays()

        # Purple best-time overlays
        if self._best_text:
            font = self._overlay_font
            row_bg = self._overlay_row_bg
            for iid, purple_text in self._best_text.items():
                if not self.tree.exists(iid):
                    continue
                bbox = self.tree.bbox(iid, "time")
                if not bbox:
                    continue
                x, y, w, h = bbox
                cell_text = self.tree.set(iid, "time")
                purple_idx = cell_text.find(purple_text)
                prefix = cell_text[:purple_idx] if purple_idx >= 0 else ""
                lbl_x = x + 4 + font.measure(prefix)
                lbl = tk.Label(
                    self.tree,
                    text=purple_text,
                    fg=_PURPLE,
                    bg=row_bg,
                    font=font,
                    anchor="w",
                    borderwidth=0,
                    padx=0,
                    pady=0,
                )
                lbl.place(x=lbl_x, y=y + 1, width=font.measure(purple_text) + 2, height=h - 2)
                self._best_overlays.append(lbl)

        # Analyze button overlays
        index = self._index
        if index is None:
            return
        lookup = self._filtered_nodes_by_id or index.nodes_by_id
        for iid in self._all_tree_iids():
            node = lookup.get(iid)
            if node is None or node.kind not in ("lap", "run"):
                continue
            if node.kind == "lap":
                lap_sum = _node_lap_summary(node)
                if _lap_is_incomplete(node.summary, lap_summary=lap_sum):
                    continue
            else:
                analyzable = [
                    c for c in node.children
                    if c.kind == "lap"
                    and not _lap_is_incomplete(c.summary, lap_summary=_node_lap_summary(c))
                ]
                if not analyzable:
                    continue
            bbox = self.tree.bbox(iid, "analyze")
            if not bbox:
                continue
            x, y, w, h = bbox
            status = _has_analysis_data(node)
            if status is True:
                btn_bg = "#2d7a2d"
            elif status == "partial":
                btn_bg = "#7a6a00"
            else:
                btn_bg = "#555555"
            btn = tk.Button(
                self.tree,
                text="Analyze",
                bg=btn_bg,
                fg="white",
                activebackground=btn_bg,
                activeforeground="white",
                borderwidth=1,
                relief="flat",
                padx=2,
                pady=0,
                cursor="hand2",
                command=lambda n=node: self._handle_analyze(n),
            )
            btn.place(x=x + 2, y=y + 1, width=w - 4, height=h - 2)
            self._analyze_buttons.append(btn)

    def _handle_analyze(self, node: CoachingTreeNode) -> None:
        """Handle analyze button click for a lap or run node."""
        self._hide_environment_tooltip()
        if self.tree.exists(node.id):
            self.tree.selection_set(node.id)
            self.tree.focus(node.id)
            self.tree.see(node.id)
        if node.kind == "lap":
            if callable(self._on_analyze_lap):
                self._on_analyze_lap(node)
        elif node.kind == "run":
            missing = [
                c for c in node.children
                if c.kind == "lap"
                and not _lap_is_incomplete(c.summary, lap_summary=_node_lap_summary(c))
                and not _has_analysis_data(c)
            ]
            if callable(self._on_analyze_run):
                self._on_analyze_run(node, missing)
        self._schedule_overlay_refresh(50)

    def _update_action_buttons(self) -> None:
        """Update action buttons."""
        node = self.selected_node()
        if node is None:
            self._btn_open.state(["disabled"])
            self._btn_delete.state(["disabled"])
            return
        if node.can_open_folder:
            self._btn_open.state(["!disabled"])
        else:
            self._btn_open.state(["disabled"])
        if node.can_delete:
            self._btn_delete.state(["!disabled"])
        else:
            self._btn_delete.state(["disabled"])

    @property
    def best_ids(self) -> frozenset[str]:
        """Return the set of node IDs currently highlighted in purple."""
        return frozenset(self._best_text.keys())

    def find_lap_node_id(self, session_dir: str, run_id: int, lap_no: int) -> str | None:
        """Sucht den Node-ID für die angegebene Lap. Gibt None zurück, wenn nicht gefunden."""
        index = self._index
        if index is None:
            return None
        try:
            session_path_resolved = str(Path(session_dir).resolve())
        except Exception:
            session_path_resolved = session_dir
        for node_id, node in index.nodes_by_id.items():
            if node.kind != "lap":
                continue
            if node.run_id != run_id:
                continue
            if node.session_path is None:
                continue
            try:
                node_session_resolved = str(Path(node.session_path).resolve())
            except Exception:
                node_session_resolved = str(node.session_path)
            if node_session_resolved != session_path_resolved:
                continue
            meta = node.meta if isinstance(node.meta, dict) else {}
            try:
                if int(meta.get("lap_no", -1)) == lap_no:
                    return node_id
            except Exception:
                pass
        return None

    def _collapse_recursive(self, iid: str) -> None:
        """Klappt einen Node und alle seine Kinder rekursiv zu."""
        self.tree.item(iid, open=False)
        self._expanded_ids.discard(iid)
        for child in self.tree.get_children(iid):
            self._collapse_recursive(child)

    def expand_and_select(self, node_id: str) -> bool:
        """Klappt den Pfad zu node_id auf, selektiert und fokussiert den Node.
        Alle anderen Äste bleiben zugeklappt.
        Gibt True zurück, wenn node_id im Tree gefunden wurde."""
        if not self.tree.exists(node_id):
            return False
        # Alle Top-Level-Nodes zuklappen
        for iid in self.tree.get_children(""):
            self._collapse_recursive(iid)
        # Pfad von der Wurzel zum node_id aufbauen
        path: list[str] = []
        current = node_id
        while current:
            path.append(current)
            current = self.tree.parent(current)
        path.reverse()
        # Jeden Knoten auf dem Pfad (außer dem Blatt selbst) aufklappen
        for iid in path[:-1]:
            self.tree.item(iid, open=True)
            self._expanded_ids.add(iid)
        # Node selektieren und in Sicht bringen
        self.tree.selection_set(node_id)
        self.tree.focus(node_id)
        self.tree.see(node_id)
        return True

    def _show_environment_tooltip(self) -> None:
        """Show tooltip for the currently hovered lap row after the delay."""
        self._environment_hover_after_id = None
        iid = self._environment_hover_iid
        environment = self._tooltip_environment_for_iid(iid)
        if iid is None or environment is None:
            self._hide_environment_tooltip()
            return
        self._environment_tooltip.show(
            environment,
            x_root=self._environment_pointer[0],
            y_root=self._environment_pointer[1],
        )
        self._environment_tooltip_iid = iid

    def _hide_environment_tooltip(self) -> None:
        """Hide the environment tooltip and clear visible-row tracking."""
        self._cancel_environment_tooltip_schedule()
        self._environment_tooltip.hide()
        self._environment_tooltip_iid = None

    def _cancel_environment_tooltip_schedule(self) -> None:
        """Cancel any pending delayed tooltip show."""
        if self._environment_hover_after_id is None:
            return
        try:
            self.after_cancel(self._environment_hover_after_id)
        except Exception:
            pass
        self._environment_hover_after_id = None

    def _tooltip_environment_for_iid(self, iid: str | None) -> dict[str, Any] | None:
        """Return environment data for any visible row that has it."""
        if not iid:
            return None
        index = self._index
        if index is None:
            return None
        lookup = self._filtered_nodes_by_id or index.nodes_by_id
        node = lookup.get(iid)
        if node is None:
            return None
        return _normalize_environment(node.summary.environment)


def _has_analysis_data(node: CoachingTreeNode) -> bool | str:
    """Return True if analyzed, False if not, 'partial' if only some laps analyzed (run only)."""
    if node.kind == "lap":
        session_path = node.session_path
        run_id = node.run_id
        meta = node.meta if isinstance(node.meta, dict) else {}
        lap_no_raw = meta.get("lap_no")
        if session_path is None or run_id is None or lap_no_raw is None:
            return False
        try:
            lap_no = int(lap_no_raw)
        except Exception:
            return False
        analysis_path = (
            Path(session_path) / "laps" / f"lap_{lap_no:04d}" / "analysis" / "analysis_status.json"
        )
        return analysis_path.exists()
    if node.kind == "run":
        analyzable = [
            c for c in node.children
            if c.kind == "lap"
            and not _lap_is_incomplete(c.summary, lap_summary=_node_lap_summary(c))
        ]
        if not analyzable:
            return False
        analyzed_count = sum(1 for c in analyzable if _has_analysis_data(c) is True)
        if analyzed_count == 0:
            return False
        if analyzed_count == len(analyzable):
            return True
        return "partial"
    return False


def _best_time_for_node(node: CoachingTreeNode) -> float | None:
    """Return comparison time for best-time highlighting (None or ≤0 means no valid time)."""
    if node.kind == "lap":
        lap_summary = _node_lap_summary(node)
        if not _lap_is_valid_for_best(node.summary, lap_summary=lap_summary):
            return None
        lap_time_s = node.summary.total_time_s
        if lap_time_s is None:
            lap_time_s = _coerce_optional_float(lap_summary.get("lap_time_s"))
        return lap_time_s
    return node.summary.fastest_lap_s


def _find_best_id(nodes: list[CoachingTreeNode]) -> str | None:
    """Return the id of the node with the smallest valid time among *nodes*, or None."""
    best_id: str | None = None
    best_t: float | None = None
    for node in nodes:
        t = _best_time_for_node(node)
        if t is not None and t > 0 and (best_t is None or t < best_t):
            best_t = t
            best_id = node.id
    return best_id


def _compute_best_ids(index: CoachingIndex) -> set[str]:
    """Return the set of node IDs that should be highlighted in purple.

    Highlighting rules per level:
      L1 – Track nodes: NO highlighting
      L2 – one car node per track (fastest within track)
      L3 – one session node per car (fastest within car)
      L4 – one run node per car (fastest across ALL sessions of that car)
      L5 – one lap node per car (fastest valid lap — not incomplete, not pit-out, not offtrack)
    """
    result: set[str] = set()

    for track in index.tracks:
        # Level 2: fastest car within this track
        bid = _find_best_id(track.children)
        if bid:
            result.add(bid)

        for car in track.children:
            # Level 3: fastest session within this car
            bid = _find_best_id(car.children)
            if bid:
                result.add(bid)

            all_runs: list[CoachingTreeNode] = []
            all_laps: list[CoachingTreeNode] = []
            for session in car.children:
                for run in session.children:
                    all_runs.append(run)
                    for lap in run.children:
                        lap_sum = _node_lap_summary(lap)
                        if _lap_is_valid_for_best(lap.summary, lap_summary=lap_sum):
                            all_laps.append(lap)

            # Level 4: fastest run (one per car, across all sessions)
            bid = _find_best_id(all_runs)
            if bid:
                result.add(bid)

            # Level 5: fastest valid lap (not incomplete, not pit-out, not offtrack)
            bid = _find_best_id(all_laps)
            if bid:
                result.add(bid)

    return result


def _range_matches(
    value: Any,
    from_: float | None,
    to_: float | None,
) -> bool:
    """True wenn value im Bereich [from_, to_] liegt."""
    if from_ is None and to_ is None:
        return True
    if value is None:
        return False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    if from_ is not None and v < from_:
        return False
    if to_ is not None and v > to_:
        return False
    return True


def _filter_node_children(
    node: CoachingTreeNode,
    filtered_children: list[CoachingTreeNode],
) -> CoachingTreeNode:
    """Return a shallow node copy with filtered children."""
    return replace(node, children=filtered_children)


def _event_matches(node: CoachingTreeNode, f: FilterState) -> bool:
    """Return True when an event node satisfies all active event groups."""
    meta = node.meta if isinstance(node.meta, dict) else {}
    if f.environments:
        env = str(meta.get("environment") or "")
        if env not in f.environments:
            return False
    ts = node.summary.last_driven_ts
    if f.date_from is not None and (ts is None or ts < f.date_from):
        return False
    if f.date_to is not None and (ts is None or ts > f.date_to):
        return False
    cond = meta.get("session_conditions") or {}
    if not isinstance(cond, dict):
        cond = {}
    if not _range_matches(cond.get("track_temp_c"), f.track_temp_from, f.track_temp_to):
        return False
    if not _range_matches(cond.get("air_temp_c"), f.air_temp_from, f.air_temp_to):
        return False
    if not _range_matches(cond.get("humidity_pct"), f.humidity_from, f.humidity_to):
        return False
    if not _range_matches(cond.get("wind_speed_ms"), f.wind_speed_from, f.wind_speed_to):
        return False
    if not _range_matches(cond.get("air_pressure_hpa"), f.air_pressure_from, f.air_pressure_to):
        return False
    if f.skies:
        sky = str(cond.get("skies") or "")
        if sky not in f.skies:
            return False
    if f.weather_types:
        wt = str(cond.get("weather_type") or "")
        if wt not in f.weather_types:
            return False
    return True


def _apply_filter(
    tracks: list[CoachingTreeNode],
    f: FilterState,
) -> list[CoachingTreeNode]:
    """Filtert die Track-Node-Liste anhand des FilterState."""
    filtered_tracks: list[CoachingTreeNode] = []
    session_types = {value.lower() for value in f.session_types}
    lap_statuses = set(f.lap_statuses)

    for track_node in tracks:
        if f.tracks and track_node.label not in f.tracks:
            continue
        filtered_cars: list[CoachingTreeNode] = []
        for car_node in track_node.children:
            if f.cars and car_node.label not in f.cars:
                continue
            filtered_events: list[CoachingTreeNode] = []
            for event_node in car_node.children:
                if not _event_matches(event_node, f):
                    continue
                filtered_runs: list[CoachingTreeNode] = []
                for run_node in event_node.children:
                    run_meta = run_node.meta if isinstance(run_node.meta, dict) else {}
                    if session_types:
                        session_type = str(run_meta.get("session_type") or "").lower()
                        if session_type not in session_types:
                            continue
                    if f.drivers:
                        driver = str(run_meta.get("driver") or "").strip()
                        if driver and driver not in f.drivers:
                            continue
                    if lap_statuses:
                        filtered_laps = [
                            lap_node
                            for lap_node in run_node.children
                            if _lap_status_text(lap_node) in lap_statuses
                        ]
                        if not filtered_laps:
                            continue
                    else:
                        filtered_laps = list(run_node.children)
                    filtered_runs.append(_filter_node_children(run_node, filtered_laps))
                if filtered_runs:
                    filtered_events.append(_filter_node_children(event_node, filtered_runs))
            if filtered_events:
                filtered_cars.append(_filter_node_children(car_node, filtered_events))
        if filtered_cars:
            filtered_tracks.append(_filter_node_children(track_node, filtered_cars))
    return filtered_tracks


def _build_filter_index(index: CoachingIndex) -> FilterIndex:
    """Traversiert den CoachingIndex und baut den FilterIndex auf."""
    tracks: set[str] = set()
    cars: set[str] = set()
    drivers: set[str] = set()
    environments: set[str] = set()
    session_types: set[str] = set()
    lap_statuses: set[str] = set()
    skies_values: set[str] = set()
    weather_types: set[str] = set()
    date_min: float | None = None
    date_max: float | None = None
    numeric_ranges: dict[str, list[float | None]] = {
        "track_temp_c": [None, None],
        "air_temp_c": [None, None],
        "humidity_pct": [None, None],
        "wind_speed_ms": [None, None],
        "air_pressure_hpa": [None, None],
    }

    for track_node in index.tracks:
        _collect_filter_string(tracks, track_node.label)
        for car_node in track_node.children:
            _collect_filter_string(cars, car_node.label)
            for event_node in car_node.children:
                _collect_filter_string(environments, event_node.meta.get("environment"))
                event_ts = _coerce_optional_float(event_node.summary.last_driven_ts)
                if event_ts is not None:
                    date_min = event_ts if date_min is None else min(date_min, event_ts)
                    date_max = event_ts if date_max is None else max(date_max, event_ts)
                conditions = event_node.meta.get("session_conditions")
                if isinstance(conditions, dict):
                    _update_filter_range(numeric_ranges, "track_temp_c", conditions.get("track_temp_c"))
                    _update_filter_range(numeric_ranges, "air_temp_c", conditions.get("air_temp_c"))
                    _update_filter_range(numeric_ranges, "humidity_pct", conditions.get("humidity_pct"))
                    _update_filter_range(numeric_ranges, "wind_speed_ms", conditions.get("wind_speed_ms"))
                    _update_filter_range(numeric_ranges, "air_pressure_hpa", conditions.get("air_pressure_hpa"))
                    _collect_filter_string(skies_values, conditions.get("skies"))
                    _collect_filter_string(weather_types, conditions.get("weather_type"))
                for run_node in event_node.children:
                    _collect_filter_string(session_types, run_node.meta.get("session_type"))
                    _collect_filter_string(drivers, run_node.meta.get("driver") or event_node.meta.get("driver"))
                    for lap_node in run_node.children:
                        lap_statuses.add(_lap_status_text(lap_node))

    return FilterIndex(
        tracks=_sorted_filter_strings(tracks),
        cars=_sorted_filter_strings(cars),
        drivers=_sorted_filter_strings(drivers),
        environments=_sorted_filter_strings(environments),
        session_types=_sorted_filter_strings(session_types),
        lap_statuses=_sorted_filter_strings(lap_statuses),
        date_min=date_min,
        date_max=date_max,
        track_temp_min=numeric_ranges["track_temp_c"][0],
        track_temp_max=numeric_ranges["track_temp_c"][1],
        air_temp_min=numeric_ranges["air_temp_c"][0],
        air_temp_max=numeric_ranges["air_temp_c"][1],
        humidity_min=numeric_ranges["humidity_pct"][0],
        humidity_max=numeric_ranges["humidity_pct"][1],
        wind_speed_min=numeric_ranges["wind_speed_ms"][0],
        wind_speed_max=numeric_ranges["wind_speed_ms"][1],
        air_pressure_min=numeric_ranges["air_pressure_hpa"][0],
        air_pressure_max=numeric_ranges["air_pressure_hpa"][1],
        skies_values=_sorted_filter_strings(skies_values),
        weather_types=_sorted_filter_strings(weather_types),
    )


def _collect_filter_string(target: set[str], value: object) -> None:
    """Add a non-empty string value to a filter set."""
    text = str(value or "").strip()
    if text:
        target.add(text)


def _sorted_filter_strings(values: set[str]) -> list[str]:
    """Return case-insensitively sorted filter values."""
    return sorted(values, key=lambda value: (value.casefold(), value))


def _update_filter_range(
    numeric_ranges: dict[str, list[float | None]],
    key: str,
    value: object,
) -> None:
    """Update min/max bounds for one numeric filter field."""
    number = _coerce_optional_float(value)
    if number is None:
        return
    current_min, current_max = numeric_ranges[key]
    numeric_ranges[key][0] = number if current_min is None else min(current_min, number)
    numeric_ranges[key][1] = number if current_max is None else max(current_max, number)


def _format_summary(node: CoachingTreeNode) -> str:
    """Format summary."""
    summary = node.summary
    if node.kind == "lap":
        return _format_lap_summary(summary, lap_summary=_node_lap_summary(node))

    parts: list[str] = []
    if summary.total_time_s is not None:
        parts.append(f"t={_format_seconds(summary.total_time_s)}")
    if summary.laps is not None:
        laps_text = f"{int(summary.laps)}"
        if summary.laps_total_display is not None and int(summary.laps_total_display) > int(summary.laps):
            delta = int(summary.laps_total_display) - int(summary.laps)
            laps_text = f"{laps_text} (+{delta} cur)"
        parts.append(f"laps={laps_text}")
    if summary.fastest_lap_s is not None:
        parts.append(f"best={_format_seconds(summary.fastest_lap_s)}")
    elif summary.laps is not None and int(summary.laps) > 0:
        parts.append("best=na")
    return "  ".join(parts) if parts else "-"


def _format_lap_summary(summary: NodeSummary, *, lap_summary: dict[str, object]) -> str:
    """Format lap summary."""
    parts: list[str] = []
    lap_time_s = summary.total_time_s
    if lap_time_s is None:
        lap_time_s = _coerce_optional_float(lap_summary.get("lap_time_s"))
    if lap_time_s is None:
        start_ts = _coerce_optional_float(lap_summary.get("start_ts"))
        end_ts = _coerce_optional_float(lap_summary.get("end_ts"))
        if start_ts is not None and end_ts is not None and end_ts >= start_ts:
            lap_time_s = end_ts - start_ts
    parts.append(_format_lap_seconds(lap_time_s))
    status = _lap_status(summary, lap_summary=lap_summary)
    if status is not None:
        parts.append(status)
    return " ".join(parts)


def _format_time_col(node: CoachingTreeNode) -> str:
    """Zeit-Spalte: beste Zeit (Track/Car/Session/Run) oder Lap-Zeit (Lap)."""
    summary = node.summary
    if node.kind == "lap":
        t = summary.total_time_s
        if t is None:
            lap_summary = _node_lap_summary(node)
            t = _coerce_optional_float(lap_summary.get("lap_time_s"))
        return _format_lap_seconds(t)
    if summary.fastest_lap_s is not None:
        return _format_lap_seconds(summary.fastest_lap_s)
    return "na"


def _type_col_value(node: CoachingTreeNode) -> str:
    """Return the display value for the tree Type column."""
    if node.kind == "event":
        environment = str(node.meta.get("environment") or "").strip()
        return environment if environment else "event"
    if node.kind == "run":
        session_type = str(node.meta.get("session_type") or "").strip()
        return session_type.capitalize() if session_type else "run"
    return node.kind


def _format_lap_col(node: CoachingTreeNode) -> str:
    """Lap-Spalte: Summe der Laps (Track/Car/Session/Run) oder Status (Lap)."""
    summary = node.summary
    if node.kind == "lap":
        return _lap_status_text(node)
    total = int(summary.laps_total_display) if summary.laps_total_display is not None else int(summary.laps or 0)
    return str(total)


def _lap_status_text(node: CoachingTreeNode) -> str:
    """Return the displayable lap status text for one lap node."""
    return _lap_status(node.summary, lap_summary=_node_lap_summary(node)) or "OK"


def _lap_status(summary: NodeSummary, *, lap_summary: dict[str, object]) -> str | None:
    """Implement lap status logic."""
    if _lap_is_incomplete(summary, lap_summary=lap_summary):
        return "incomplete"
    if _lap_is_pit_out(lap_summary):
        return "Pit (out)"
    if _lap_is_offtrack(summary, lap_summary=lap_summary):
        return "offtrack"
    return None


def _node_lap_summary(node: CoachingTreeNode) -> dict[str, object]:
    """Implement node lap summary logic."""
    meta = getattr(node, "meta", {})
    if isinstance(meta, dict):
        summary = meta.get("lap_summary")
        if isinstance(summary, dict):
            return summary
    return {}


def _lap_is_incomplete(summary: NodeSummary, *, lap_summary: dict[str, object]) -> bool:
    """Implement lap is incomplete logic."""
    if bool(getattr(summary, "lap_incomplete", False)):
        return True
    if "incomplete" in lap_summary:
        explicit = _coerce_optional_bool(lap_summary.get("incomplete"))
        if explicit is not None:
            return bool(explicit)
    if "lap_incomplete" in lap_summary:
        explicit = _coerce_optional_bool(lap_summary.get("lap_incomplete"))
        if explicit is not None:
            return bool(explicit)
    lap_complete = _coerce_optional_bool(lap_summary.get("lap_complete"))
    if lap_complete is not None:
        return not bool(lap_complete)
    return False


def _lap_is_offtrack(summary: NodeSummary, *, lap_summary: dict[str, object]) -> bool:
    """Implement lap is offtrack logic."""
    if bool(getattr(summary, "lap_offtrack", False)):
        return True
    for key in ("offtrack_surface", "lap_offtrack", "offtrack"):
        if key in lap_summary:
            explicit = _coerce_optional_bool(lap_summary.get(key))
            if explicit is not None:
                return bool(explicit)
    return False


def _lap_is_pit_out(lap_summary: dict[str, object]) -> bool:
    """Return whether the lap should be displayed as pit-out."""
    explicit = _coerce_optional_bool(lap_summary.get("lap_pit_out"))
    return bool(explicit) if explicit is not None else False


def _lap_is_valid_for_best(summary: NodeSummary, *, lap_summary: dict[str, object]) -> bool:
    """Return whether the lap may participate in best-time highlighting."""
    explicit = _coerce_optional_bool(lap_summary.get("valid_lap"))
    if explicit is None:
        explicit = True
    return (
        bool(explicit)
        and not _lap_is_incomplete(summary, lap_summary=lap_summary)
        and not _lap_is_pit_out(lap_summary)
        and not _lap_is_offtrack(summary, lap_summary=lap_summary)
    )


def _format_seconds(seconds: float) -> str:
    """Format seconds."""
    try:
        value = float(seconds)
    except Exception:
        return "na"
    if value < 0:
        return "na"
    minutes = int(value // 60)
    remainder = value - (minutes * 60)
    if minutes > 0:
        return f"{minutes}:{remainder:05.2f}"
    return f"{remainder:.2f}s"


def _format_lap_seconds(seconds: float | None) -> str:
    """Format lap seconds."""
    if seconds is None:
        return "na"
    try:
        value = float(seconds)
    except Exception:
        return "na"
    if value < 0:
        return "na"
    minutes = int(value // 60)
    remainder = value - (minutes * 60)
    if minutes > 0:
        return f"{minutes}:{remainder:06.3f}"
    return f"{remainder:.3f}"


def _format_last_driven(ts: float | None) -> str:
    """Format last driven."""
    if ts is None:
        return "-"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "-"


def _coerce_optional_float(value: object) -> float | None:
    """Coerce optional float."""
    try:
        return float(value)
    except Exception:
        return None


def _coerce_optional_bool(value: object) -> bool | None:
    """Coerce optional bool."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value == 0:
            return False
        if value == 1:
            return True
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"0", "false", "no", "n", "off"}:
            return False
        if text in {"1", "true", "yes", "y", "on"}:
            return True
    return None


def _normalize_environment(value: object) -> dict[str, Any] | None:
    """Return a shallow-copied environment dict or None."""
    if not isinstance(value, dict):
        return None
    copied = dict(value)
    return copied or None


def _format_environment_field(key: str, value: object) -> str:
    """Format one environment field for tooltip display."""
    if value is None:
        return ""
    if key in {"track_temp_c", "air_temp_c"}:
        return _format_environment_number(value, suffix=" C", decimals=1)
    if key in {"humidity_pct", "fog_pct"}:
        return _format_environment_number(value, suffix=" %", decimals=1)
    if key == "wind_speed_ms":
        return _format_environment_number(value, suffix=" m/s", decimals=1)
    if key == "wind_dir_deg":
        degrees = _format_environment_number(value, suffix=" deg", decimals=0)
        cardinal = _wind_cardinal(value)
        return f"{degrees} {cardinal}".strip() if degrees else ""
    if key == "air_pressure_hpa":
        return _format_environment_number(value, suffix=" hPa", decimals=1)
    return str(value).strip()


def _format_environment_number(value: object, *, suffix: str, decimals: int) -> str:
    """Format an environment number with compact rounding."""
    try:
        number = float(value)
    except Exception:
        return ""
    if decimals <= 0 or abs(number - round(number)) < 0.05:
        text = str(int(round(number)))
    else:
        text = f"{number:.{decimals}f}".rstrip("0").rstrip(".")
    return f"{text}{suffix}"


def _wind_cardinal(value: object) -> str:
    """Return a compact cardinal direction string for degrees."""
    try:
        degrees = float(value) % 360.0
    except Exception:
        return ""
    directions = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    index = int((degrees + 22.5) // 45) % len(directions)
    return directions[index]
