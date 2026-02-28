from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from datetime import datetime
from typing import Callable

from core.coaching.indexer import CoachingIndex, CoachingTreeNode, NodeSummary


RefreshCallback = Callable[[], CoachingIndex | None]
NodeCallback = Callable[[CoachingTreeNode], None]


class CoachingBrowser(ttk.Frame):
    def __init__(
        self,
        master: tk.Widget,
        *,
        on_refresh: RefreshCallback | None = None,
        on_open_folder: NodeCallback | None = None,
        on_delete_node: NodeCallback | None = None,
        on_select_node: NodeCallback | None = None,
    ) -> None:
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
            columns=("kind", "summary", "last"),
            show="tree headings",
            selectmode="browse",
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.heading("#0", text="Name")
        self.tree.heading("kind", text="Type")
        self.tree.heading("summary", text="Summary")
        self.tree.heading("last", text="Last Driven")
        self.tree.column("#0", width=280, stretch=True)
        self.tree.column("kind", width=80, stretch=False, anchor="w")
        self.tree.column("summary", width=260, stretch=True, anchor="w")
        self.tree.column("last", width=150, stretch=False, anchor="w")

        y_scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
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
        self._update_action_buttons()

    def set_index(self, index: CoachingIndex | None) -> None:
        self._capture_expanded_state()
        selected_id = self._selected_id()
        self._index = index
        self._rebuild_tree(selected_id=selected_id)

    def refresh(self) -> None:
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
        self._message_var.set(str(message or ""))

    def selected_node(self) -> CoachingTreeNode | None:
        index = self._index
        if index is None:
            return None
        item_id = self._selected_id()
        if not item_id:
            return None
        return index.nodes_by_id.get(item_id)

    def _rebuild_tree(self, *, selected_id: str | None) -> None:
        self.tree.delete(*self.tree.get_children(""))
        index = self._index
        if index is None:
            self._stats_var.set("No sessions loaded.")
            self._update_action_buttons()
            return
        for node in index.tracks:
            self._insert_node("", node)
        self._restore_expanded_state()
        if selected_id and self.tree.exists(selected_id):
            self.tree.selection_set(selected_id)
            self.tree.focus(selected_id)
            self.tree.see(selected_id)
        self._stats_var.set(
            f"Sessions: {index.session_count}  Runs: {index.run_count}  Laps: {index.lap_count}"
        )
        self._update_action_buttons()

    def _insert_node(self, parent_iid: str, node: CoachingTreeNode) -> None:
        values = (
            node.kind,
            _format_summary(node),
            _format_last_driven(node.summary.last_driven_ts),
        )
        self.tree.insert(parent_iid, "end", iid=node.id, text=node.label, values=values, open=(node.id in self._expanded_ids))
        for child in node.children:
            self._insert_node(node.id, child)

    def _capture_expanded_state(self) -> None:
        expanded: set[str] = set()

        def walk(parent: str) -> None:
            for iid in self.tree.get_children(parent):
                if self.tree.item(iid, "open"):
                    expanded.add(iid)
                walk(iid)

        walk("")
        self._expanded_ids = expanded

    def _restore_expanded_state(self) -> None:
        for iid in list(self._expanded_ids):
            if self.tree.exists(iid):
                self.tree.item(iid, open=True)

    def _selected_id(self) -> str | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return str(sel[0])

    def _on_tree_select(self, _event=None) -> None:
        self._update_action_buttons()
        node = self.selected_node()
        if node is None:
            return
        if callable(self._on_select_node):
            self._on_select_node(node)

    def _on_double_click(self, _event=None) -> None:
        node = self.selected_node()
        if node is None:
            return
        if node.can_open_folder:
            self._handle_open_folder()

    def _handle_open_folder(self) -> None:
        node = self.selected_node()
        if node is None or not node.can_open_folder:
            return
        if callable(self._on_open_folder):
            self._on_open_folder(node)

    def _handle_delete(self) -> None:
        node = self.selected_node()
        if node is None or not node.can_delete:
            return
        if callable(self._on_delete_node):
            self._on_delete_node(node)

    def _update_action_buttons(self) -> None:
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


def _format_summary(node: CoachingTreeNode) -> str:
    summary = node.summary
    if node.kind == "lap":
        return _format_lap_summary(summary)

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


def _format_lap_summary(summary: NodeSummary) -> str:
    parts: list[str] = []
    parts.append(_format_lap_seconds(summary.total_time_s))
    status = _lap_status(summary)
    if status is not None:
        parts.append(status)
    return " ".join(parts)


def _lap_status(summary: NodeSummary) -> str | None:
    if bool(getattr(summary, "lap_incomplete", False)):
        return "incomplete"
    if bool(getattr(summary, "lap_offtrack", False)):
        return "offtrack"
    return None


def _format_seconds(seconds: float) -> str:
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
    if ts is None:
        return "-"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "-"
