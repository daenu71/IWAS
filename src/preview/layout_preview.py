from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
import tkinter as tk


@dataclass(frozen=True)
class OutputFormat:
    out_w: int
    out_h: int
    hud_w: int


class LayoutPreviewController:
    def __init__(
        self,
        canvas: tk.Canvas,
        save_current_boxes: Callable[[], None],
        redraw_preview: Callable[[], None],
        is_locked: Callable[[], bool],
    ) -> None:
        self.canvas = canvas
        self._save_current_boxes = save_current_boxes
        self._redraw_preview = redraw_preview
        self._is_locked = is_locked

        # Transform-Marker (for layout mouse events)
        self.layout_last: dict[str, int | float] = {
            "out_w": 0,
            "out_h": 0,
            "hud_w": 0,
            "side_w": 0,
            "x0": 0,
            "y0": 0,
            "scale": 1.0,
        }

        self.hud_active_id: str | None = None
        self.hud_mode: str = ""  # "drag" or "resize"
        self.hud_drag_dx: int = 0
        self.hud_drag_dy: int = 0
        self.hud_start_mouse_ox: float = 0.0
        self.hud_start_mouse_oy: float = 0.0
        self.hud_start_x: float = 0.0
        self.hud_start_y: float = 0.0
        self.hud_start_w: float = 0.0
        self.hud_start_h: float = 0.0

    @staticmethod
    def _clamp(v: int, lo: int, hi: int) -> int:
        if v < lo:
            return lo
        if v > hi:
            return hi
        return v

    def ensure_boxes_in_hud_area(self, hud_boxes: list[dict]) -> None:
        out_w = int(self.layout_last.get("out_w") or 0)
        out_h = int(self.layout_last.get("out_h") or 0)
        hud_w = int(self.layout_last.get("hud_w") or 0)
        side_w = int(self.layout_last.get("side_w") or 0)
        if out_w <= 0 or out_h <= 0:
            return

        hud_x0 = side_w
        hud_x1 = side_w + hud_w

        for b in hud_boxes:
            try:
                w = max(40, int(b.get("w", 200)))
                h = max(30, int(b.get("h", 100)))
                x = int(b.get("x", 0))
                y = int(b.get("y", 0))
            except Exception:
                continue

            if x == 0:
                x = hud_x0 + 10

            x = self._clamp(x, hud_x0, max(hud_x0, hud_x1 - w))
            y = self._clamp(y, 0, max(0, out_h - h))

            b["x"] = int(x)
            b["y"] = int(y)
            b["w"] = int(w)
            b["h"] = int(h)

    def draw_layout_preview(
        self,
        output_format: OutputFormat,
        hud_boxes: list[dict],
        enabled_types: set[str],
        area_w: int,
        area_h: int,
        load_current_boxes: Callable[[], list[dict]] | None = None,
    ) -> list[dict]:
        area_w = max(200, int(area_w))
        area_h = max(200, int(area_h))

        out_w = int(output_format.out_w)
        out_h = int(output_format.out_h)
        if out_w <= 0 or out_h <= 0:
            out_w, out_h = 1280, 720

        hud_w = int(output_format.hud_w)
        hud_w = max(0, min(int(hud_w), max(0, out_w - 2)))

        side_w = int((out_w - hud_w) / 2)

        pad = 10
        avail_w = max(50, area_w - 2 * pad)
        avail_h = max(50, area_h - 2 * pad)

        scale = min(avail_w / max(1, out_w), avail_h / max(1, out_h))
        draw_w = int(out_w * scale)
        draw_h = int(out_h * scale)

        x0 = int((area_w - draw_w) / 2)
        y0 = int((area_h - draw_h) / 2)
        x1 = x0 + draw_w
        y1 = y0 + draw_h

        self.layout_last["out_w"] = int(out_w)
        self.layout_last["out_h"] = int(out_h)
        self.layout_last["hud_w"] = int(hud_w)
        self.layout_last["side_w"] = int(side_w)
        self.layout_last["x0"] = int(x0)
        self.layout_last["y0"] = int(y0)
        self.layout_last["scale"] = float(scale)

        side_w_px = int(side_w * scale)
        hud_w_px = int(hud_w * scale)

        self.canvas.delete("all")

        self.canvas.create_rectangle(x0, y0, x1, y1)

        lx0 = x0
        lx1 = x0 + side_w_px
        mx0 = lx1
        mx1 = mx0 + hud_w_px
        rx0 = mx1
        rx1 = x1

        self.canvas.create_rectangle(lx0, y0, lx1, y1)
        self.canvas.create_rectangle(mx0, y0, mx1, y1)
        self.canvas.create_rectangle(rx0, y0, rx1, y1)

        self.canvas.create_text(int((lx0 + lx1) / 2), int((y0 + y1) / 2), text="Slow")
        self.canvas.create_text(int((mx0 + mx1) / 2), int((y0 + y1) / 2) - 40, text=f"HUD\n{hud_w}px")
        self.canvas.create_text(int((rx0 + rx1) / 2), int((y0 + y1) / 2), text="Fast")

        if (self.hud_active_id is None) and (self.hud_mode == ""):
            if load_current_boxes is not None:
                hud_boxes = load_current_boxes()
            self.ensure_boxes_in_hud_area(hud_boxes)
        else:
            self.ensure_boxes_in_hud_area(hud_boxes)

        self.canvas.create_rectangle(mx0, y0, mx1, y1)

        def out_to_canvas(x: int, y: int) -> tuple[int, int]:
            cx = int(x0 + (x * scale))
            cy = int(y0 + (y * scale))
            return cx, cy

        for b in hud_boxes:
            t = str(b.get("type") or "")
            if t not in enabled_types:
                continue

            try:
                bx = int(b.get("x", 0))
                by = int(b.get("y", 0))
                bw = int(b.get("w", 200))
                bh = int(b.get("h", 100))
            except Exception:
                continue

            c0x, c0y = out_to_canvas(bx, by)
            c1x, c1y = out_to_canvas(bx + bw, by + bh)

            tag = f"hud_{t.replace(' ', '_').replace('/', '_')}"
            self.canvas.create_rectangle(c0x, c0y, c1x, c1y, tags=("hud_box", tag))
            self.canvas.create_text(int((c0x + c1x) / 2), int((c0y + c1y) / 2), text=t, tags=("hud_box", tag))

            hx0 = max(c0x, c1x - 12)
            hy0 = max(c0y, c1y - 12)
            self.canvas.create_rectangle(hx0, hy0, c1x, c1y, tags=("hud_handle", tag))

        return hud_boxes

    @staticmethod
    def _get_active_box_by_type(hud_boxes: list[dict], box_type: str) -> dict | None:
        for b in hud_boxes:
            if str(b.get("type") or "") == box_type:
                return b
        return None

    def canvas_to_out_xy(self, cx: float, cy: float) -> tuple[float, float]:
        x0 = float(self.layout_last.get("x0") or 0)
        y0 = float(self.layout_last.get("y0") or 0)
        scale = float(self.layout_last.get("scale") or 1.0)
        if scale <= 0.0001:
            scale = 1.0
        return ((cx - x0) / scale, (cy - y0) / scale)

    def hud_bounds_out(self) -> tuple[int, int, int, int]:
        out_h = int(self.layout_last.get("out_h") or 0)
        hud_w = int(self.layout_last.get("hud_w") or 0)
        side_w = int(self.layout_last.get("side_w") or 0)
        hud_x0 = side_w
        hud_x1 = side_w + hud_w
        return hud_x0, hud_x1, 0, out_h

    def clamp_box_in_hud(self, b: dict) -> None:
        hud_x0, hud_x1, y0, out_h = self.hud_bounds_out()
        try:
            x = float(b.get("x", 0))
            y = float(b.get("y", 0))
            w = float(b.get("w", 200))
            h = float(b.get("h", 100))
        except Exception:
            return

        w = max(40.0, w)
        h = max(30.0, h)

        max_x = max(float(hud_x0), float(hud_x1) - w)
        max_y = max(float(y0), float(out_h) - h)

        if x < hud_x0:
            x = float(hud_x0)
        if x > max_x:
            x = float(max_x)
        if y < y0:
            y = float(y0)
        if y > max_y:
            y = float(max_y)

        b["x"] = int(round(x))
        b["y"] = int(round(y))
        b["w"] = int(round(w))
        b["h"] = int(round(h))

    def hit_test_box(self, event_x: int, event_y: int, hud_boxes: list[dict], enabled_types: set[str]) -> tuple[str | None, str]:
        if int(self.layout_last.get("out_w") or 0) <= 0:
            return None, ""

        ox, oy = self.canvas_to_out_xy(float(event_x), float(event_y))

        scale = float(self.layout_last.get("scale") or 1.0)
        edge_tol_out = 8.0 / max(0.0001, scale)

        hit_t: str | None = None
        hit_mode: str = ""

        for b in hud_boxes:
            t = str(b.get("type") or "")
            if t not in enabled_types:
                continue

            bx = float(b.get("x", 0))
            by = float(b.get("y", 0))
            bw = float(b.get("w", 200))
            bh = float(b.get("h", 100))

            if ox < bx or oy < by or ox > (bx + bw) or oy > (by + bh):
                continue

            left = abs(ox - bx) <= edge_tol_out
            right = abs(ox - (bx + bw)) <= edge_tol_out
            top = abs(oy - by) <= edge_tol_out
            bottom = abs(oy - (by + bh)) <= edge_tol_out

            mode = "move"
            if top and left:
                mode = "nw"
            elif top and right:
                mode = "ne"
            elif bottom and left:
                mode = "sw"
            elif bottom and right:
                mode = "se"
            elif top:
                mode = "n"
            elif bottom:
                mode = "s"
            elif left:
                mode = "w"
            elif right:
                mode = "e"

            hit_t = t
            hit_mode = mode

        return hit_t, hit_mode

    @staticmethod
    def cursor_for_mode(mode: str) -> str:
        if mode == "move":
            return "fleur"
        if mode in ("n", "s"):
            return "sb_v_double_arrow"
        if mode in ("e", "w"):
            return "sb_h_double_arrow"
        if mode in ("ne", "sw"):
            return "top_right_corner"
        if mode in ("nw", "se"):
            return "top_left_corner"
        return ""

    def on_layout_hover(self, e: Any, hud_boxes: list[dict], enabled_types: set[str]) -> None:
        if self._is_locked():
            try:
                self.canvas.configure(cursor="")
            except Exception:
                pass
            return

        t, mode = self.hit_test_box(int(e.x), int(e.y), hud_boxes, enabled_types)
        cur = self.cursor_for_mode(mode) if t is not None else ""
        try:
            self.canvas.configure(cursor=cur)
        except Exception:
            pass

    def on_layout_leave(self, _e: Any = None) -> None:
        try:
            self.canvas.configure(cursor="")
        except Exception:
            pass

    def on_layout_mouse_down(self, e: Any, hud_boxes: list[dict], enabled_types: set[str]) -> None:
        if self._is_locked():
            return

        t, mode = self.hit_test_box(int(e.x), int(e.y), hud_boxes, enabled_types)
        if t is None:
            self.hud_active_id = None
            self.hud_mode = ""
            return

        self.hud_active_id = t
        self.hud_mode = mode

        ox, oy = self.canvas_to_out_xy(float(e.x), float(e.y))
        self.hud_start_mouse_ox = ox
        self.hud_start_mouse_oy = oy

        b = self._get_active_box_by_type(hud_boxes, t)
        if b is None:
            self.hud_active_id = None
            self.hud_mode = ""
            return

        self.hud_start_x = float(b.get("x", 0))
        self.hud_start_y = float(b.get("y", 0))
        self.hud_start_w = float(b.get("w", 200))
        self.hud_start_h = float(b.get("h", 100))

    def on_layout_mouse_move(self, e: Any, hud_boxes: list[dict]) -> None:
        if self._is_locked():
            return
        if self.hud_active_id is None or self.hud_mode == "":
            return

        b = self._get_active_box_by_type(hud_boxes, self.hud_active_id)
        if b is None:
            return

        ox, oy = self.canvas_to_out_xy(float(e.x), float(e.y))
        dx = ox - self.hud_start_mouse_ox
        dy = oy - self.hud_start_mouse_oy

        min_w = 40.0
        min_h = 30.0

        x = self.hud_start_x
        y = self.hud_start_y
        w = self.hud_start_w
        h = self.hud_start_h

        if self.hud_mode == "move":
            x = self.hud_start_x + dx
            y = self.hud_start_y + dy
        else:
            if "e" in self.hud_mode:
                w = max(min_w, self.hud_start_w + dx)
            if "s" in self.hud_mode:
                h = max(min_h, self.hud_start_h + dy)

            if "w" in self.hud_mode:
                x = self.hud_start_x + dx
                w = max(min_w, self.hud_start_w - dx)

            if "n" in self.hud_mode:
                y = self.hud_start_y + dy
                h = max(min_h, self.hud_start_h - dy)

        b["x"] = int(round(x))
        b["y"] = int(round(y))
        b["w"] = int(round(w))
        b["h"] = int(round(h))

        self.clamp_box_in_hud(b)
        self._redraw_preview()

    def on_layout_mouse_up(self, _e: Any = None) -> None:
        if self._is_locked():
            return
        if self.hud_active_id is None:
            return

        try:
            self.canvas.configure(cursor="")
        except Exception:
            pass

        self.hud_active_id = None
        self.hud_mode = ""
        self._save_current_boxes()
