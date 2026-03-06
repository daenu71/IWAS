"""Runtime module for ui/coaching_browser.py."""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from datetime import datetime
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
        top.columnconfigure(1, weight=1)
        ttk.Button(top, text="Refresh", command=self.refresh).grid(row=0, column=0, sticky="w")
        ttk.Label(top, textvariable=self._stats_var).grid(row=0, column=1, sticky="e", padx=(8, 0))

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
        self._update_action_buttons()

    def set_index(self, index: CoachingIndex | None) -> None:
        """Implement set index logic."""
        self._capture_expanded_state()
        selected_id = self._selected_id()
        self._index = index
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
        self._rebuild_tree(selected_id=selected_id)

    def set_message(self, message: str) -> None:
        """Implement set message logic."""
        self._message_var.set(str(message or ""))

    def selected_node(self) -> CoachingTreeNode | None:
        """Implement selected node logic."""
        index = self._index
        if index is None:
            return None
        item_id = self._selected_id()
        if not item_id:
            return None
        return index.nodes_by_id.get(item_id)

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
            self._stats_var.set("No sessions loaded.")
            self._update_action_buttons()
            return
        for node in index.tracks:
            self._insert_node("", node)
        best_ids = _compute_best_ids(index)
        self._build_best_text(index, best_ids)
        self._restore_expanded_state()
        if selected_id and self.tree.exists(selected_id):
            self.tree.selection_set(selected_id)
            self.tree.focus(selected_id)
            self.tree.see(selected_id)
        self._stats_var.set(
            f"Sessions: {index.session_count}  Runs: {index.run_count}  Laps: {index.lap_count}"
        )
        self._update_action_buttons()
        self._schedule_overlay_refresh(10)

    def _insert_node(self, parent_iid: str, node: CoachingTreeNode) -> None:
        """Implement insert node logic."""
        values = (
            "",  # analyze — button placed as overlay
            node.kind,
            _format_time_col(node),
            _format_lap_col(node),
            _format_last_driven(node.summary.last_driven_ts),
        )
        self.tree.insert(parent_iid, "end", iid=node.id, text=node.label, values=values, open=(node.id in self._expanded_ids))
        for child in node.children:
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
        for iid in self._all_tree_iids():
            node = index.nodes_by_id.get(iid)
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
        """Return environment data for lap rows that should show a tooltip."""
        if not iid:
            return None
        index = self._index
        if index is None:
            return None
        node = index.nodes_by_id.get(iid)
        if node is None or node.kind != "lap":
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
        return node.summary.total_time_s
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
      L5 – one lap node per car (fastest valid lap — not incomplete, not offtrack)
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
                        if not _lap_is_incomplete(lap.summary, lap_summary=lap_sum) \
                                and not _lap_is_offtrack(lap.summary, lap_summary=lap_sum):
                            all_laps.append(lap)

            # Level 4: fastest run (one per car, across all sessions)
            bid = _find_best_id(all_runs)
            if bid:
                result.add(bid)

            # Level 5: fastest valid lap (not incomplete, not offtrack)
            bid = _find_best_id(all_laps)
            if bid:
                result.add(bid)

    return result


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


def _format_lap_col(node: CoachingTreeNode) -> str:
    """Lap-Spalte: Summe der Laps (Track/Car/Session/Run) oder Status (Lap)."""
    summary = node.summary
    if node.kind == "lap":
        lap_summary = _node_lap_summary(node)
        if _lap_is_incomplete(summary, lap_summary=lap_summary):
            return "incomplete"
        if _lap_is_offtrack(summary, lap_summary=lap_summary):
            return "offtrack"
        return "OK"
    total = int(summary.laps_total_display) if summary.laps_total_display is not None else int(summary.laps or 0)
    return str(total)


def _lap_status(summary: NodeSummary, *, lap_summary: dict[str, object]) -> str | None:
    """Implement lap status logic."""
    if _lap_is_incomplete(summary, lap_summary=lap_summary):
        return "incomplete"
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
        return f"{minutes}:{remainder:05.2f}"
    return f"{remainder:.2f}"


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
