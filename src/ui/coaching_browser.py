"""Runtime module for ui/coaching_browser.py."""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from datetime import datetime
from typing import Callable

from core.coaching.indexer import CoachingIndex, CoachingTreeNode, NodeSummary


RefreshCallback = Callable[[], CoachingIndex | None]
NodeCallback = Callable[[CoachingTreeNode], None]

_PURPLE = "#BF7FFF"


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
    ) -> None:
        """Implement init logic."""
        super().__init__(master)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self._on_refresh = on_refresh
        self._on_open_folder = on_open_folder
        self._on_delete_node = on_delete_node
        self._on_select_node = on_select_node

        self._index: CoachingIndex | None = None
        self._expanded_ids: set[str] = set()
        self._message_var = tk.StringVar(value="")
        self._stats_var = tk.StringVar(value="No sessions loaded.")
        self._best_overlays: list[tk.Label] = []
        self._best_text: dict[str, str] = {}  # iid → purple time text
        self._overlay_after_id: str | None = None
        self._overlay_font: tkfont.Font | None = None
        self._overlay_row_bg: str = "#FFFFFF"
        self._overlay_sel_bg: str = "#0078D4"

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
            columns=("kind", "time", "lap", "last"),
            show="tree headings",
            selectmode="browse",
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.heading("#0", text="Name")
        self.tree.heading("kind", text="Type")
        self.tree.heading("time", text="Time")
        self.tree.heading("lap", text="Laps")
        self.tree.heading("last", text="Last Driven")
        self.tree.column("#0", width=280, minwidth=200, stretch=True)
        self.tree.column("kind", width=80, stretch=False, anchor="w")
        self.tree.column("time", width=130, stretch=False, anchor="w")
        self.tree.column("lap", width=70, stretch=False, anchor="e")
        self.tree.column("last", width=150, stretch=False, anchor="w")

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
        """Destroy all existing overlay labels."""
        for lbl in self._best_overlays:
            lbl.destroy()
        self._best_overlays.clear()

    def _refresh_overlays(self) -> None:
        """Recreate purple overlay labels over the best-time text in the Time column."""
        self._overlay_after_id = None
        self._clear_overlays()
        if not self._best_text:
            return
        font = self._overlay_font
        row_bg = self._overlay_row_bg
        sel_bg = self._overlay_sel_bg
        selected = set(self.tree.selection())
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
            lbl_bg = row_bg
            lbl = tk.Label(
                self.tree,
                text=purple_text,
                fg=_PURPLE,
                bg=lbl_bg,
                font=font,
                anchor="w",
                borderwidth=0,
                padx=0,
                pady=0,
            )
            lbl.place(x=lbl_x, y=y + 1, width=font.measure(purple_text) + 2, height=h - 2)
            self._best_overlays.append(lbl)

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
