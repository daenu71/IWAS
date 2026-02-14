from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Callable
import tkinter as tk

from PIL import Image, ImageTk

from .layout_preview import OutputFormat
from core.output_geometry import build_output_geometry_for_size


def pil_paste_clipped(dst: Image.Image, src: Image.Image, region_box: tuple[int, int, int, int], pos_xy: tuple[int, int]) -> None:
    # region_box is the allowed region in dst coords.
    rx0, ry0, rx1, ry1 = region_box
    px, py = pos_xy

    sx0 = px
    sy0 = py
    sx1 = px + src.size[0]
    sy1 = py + src.size[1]

    ix0 = max(rx0, sx0)
    iy0 = max(ry0, sy0)
    ix1 = min(rx1, sx1)
    iy1 = min(ry1, sy1)

    if ix1 <= ix0 or iy1 <= iy0:
        return

    crop_x0 = ix0 - sx0
    crop_y0 = iy0 - sy0
    crop_x1 = crop_x0 + (ix1 - ix0)
    crop_y1 = crop_y0 + (iy1 - iy0)

    src_c = src.crop((int(crop_x0), int(crop_y0), int(crop_x1), int(crop_y1)))
    dst.paste(src_c, (int(ix0), int(iy0)))


def _png_region_out(
    side: str,
    out_w: int,
    out_h: int,
    hud_w: int,
    layout_config: Any | None = None,
) -> tuple[int, int, int, int]:
    try:
        geom = build_output_geometry_for_size(
            out_w=int(out_w),
            out_h=int(out_h),
            hud_width_px=int(hud_w),
            layout_config=layout_config,
        )
    except Exception:
        geom = build_output_geometry_for_size(
            out_w=int(out_w),
            out_h=int(out_h),
            hud_width_px=int(hud_w),
            layout_config=None,
        )
    rect = geom.video_slow_rect if side == "L" else geom.video_fast_rect
    return int(rect.x), int(rect.y), int(rect.x) + int(rect.w), int(rect.y) + int(rect.h)


class PngPreviewController:
    def __init__(
        self,
        canvas: tk.Canvas,
        get_preview_area_size: Callable[[], tuple[int, int]],
        get_output_format: Callable[[], OutputFormat],
        is_png_mode: Callable[[], bool],
        get_png_view_key: Callable[[], str],
        load_png_view_data: Callable[[], dict],
        save_png_view_data: Callable[[dict], None],
        choose_slow_fast_paths: Callable[[], tuple[Path | None, Path | None]],
        get_start_for_video: Callable[[Path], int],
        read_frame_as_pil: Callable[[Path, int], Image.Image | None],
        get_hud_boxes: Callable[[], list[dict]] | None = None,
        get_enabled_types: Callable[[], set[str]] | None = None,
        get_overlay_flags: Callable[[], dict[str, bool]] | None = None,
        on_preview_geometry: Callable[[Any, int, int, float, int, int, int], None] | None = None,
        on_video_transform_changed: Callable[[], None] | None = None,
    ) -> None:
        self.canvas = canvas
        self._get_preview_area_size = get_preview_area_size
        self._get_output_format = get_output_format
        self._is_png_mode = is_png_mode
        self._get_png_view_key = get_png_view_key
        self._load_png_view_data = load_png_view_data
        self._save_png_view_data = save_png_view_data
        self._choose_slow_fast_paths = choose_slow_fast_paths
        self._get_start_for_video = get_start_for_video
        self._read_frame_as_pil = read_frame_as_pil
        self._get_hud_boxes = get_hud_boxes
        self._get_enabled_types = get_enabled_types
        self._get_overlay_flags = get_overlay_flags
        self._on_preview_geometry = on_preview_geometry
        self._on_video_transform_changed = on_video_transform_changed

        self.PNG_DEBUG = False

        # PNG State (pro Seite)
        self.png_state: dict[str, Any] = {
            "L": {"zoom": 1.0, "off_x": 0, "off_y": 0, "fit_to_height": False},
            "R": {"zoom": 1.0, "off_x": 0, "off_y": 0, "fit_to_height": False},
            "drag": False,
            "drag_side": "",
            "drag_x": 0,
            "drag_y": 0,
        }

        # Letzte Frame-Geometrie (fuer Hit-Tests)
        self.png_frame_last: dict[str, Any] = {
            "x0": 0, "y0": 0, "x1": 0, "y1": 0,
            "lx0": 0, "lx1": 0,
            "mx0": 0, "mx1": 0,
            "rx0": 0, "rx1": 0,
            "slow_x0": 0, "slow_y0": 0, "slow_x1": 0, "slow_y1": 0,
            "fast_x0": 0, "fast_y0": 0, "fast_x1": 0, "fast_y1": 0,
            "valid": False,
            "scale": 1.0,
        }

        self.png_img_left: Image.Image | None = None
        self.png_img_right: Image.Image | None = None
        self.png_left_name = ""
        self.png_right_name = ""
        self.png_left_start = -1
        self.png_right_start = -1

    def _notify_video_transform_changed(self) -> None:
        if self._on_video_transform_changed is None:
            return
        try:
            self._on_video_transform_changed()
        except Exception:
            pass

    def _png_dbg(self, msg: str) -> None:
        if not self.PNG_DEBUG:
            return
        try:
            ts = time.strftime("%H:%M:%S")
        except Exception:
            ts = "??:??:??"
        try:
            print(f"[PNGDBG {ts}] {msg}")
        except Exception:
            pass

    def _layout_config(self) -> Any | None:
        try:
            output = self._get_output_format()
        except Exception:
            return None
        return getattr(output, "layout_config", None)

    def _overlay_flags(self) -> dict[str, bool]:
        if self._get_overlay_flags is None:
            return {"video_rects": True, "hud_boxes": True, "labels": True}
        try:
            flags = self._get_overlay_flags()
        except Exception:
            return {"video_rects": True, "hud_boxes": True, "labels": True}
        if not isinstance(flags, dict):
            return {"video_rects": True, "hud_boxes": True, "labels": True}
        return {
            "video_rects": bool(flags.get("video_rects", True)),
            "hud_boxes": bool(flags.get("hud_boxes", True)),
            "labels": bool(flags.get("labels", True)),
        }

    def _sync_state_from_video_transform(self) -> None:
        cfg = self._layout_config()
        vt = getattr(cfg, "video_transform", None) if cfg is not None else None
        if vt is None:
            return
        try:
            scale_pct = int(round(float(getattr(vt, "scale_pct", 100))))
        except Exception:
            scale_pct = 100
        if scale_pct < 10:
            scale_pct = 10
        if scale_pct > 300:
            scale_pct = 300
        zoom = float(scale_pct) / 100.0
        try:
            off_x = int(getattr(vt, "shift_x_px", 0))
        except Exception:
            off_x = 0
        try:
            off_y = int(getattr(vt, "shift_y_px", 0))
        except Exception:
            off_y = 0
        try:
            fit_mode = str(getattr(vt, "fit_button_mode", "fit_height") or "fit_height").strip().lower()
        except Exception:
            fit_mode = "fit_height"
        if fit_mode not in ("fit_height", "fit_width"):
            fit_mode = "fit_height"
        fit_to_height = fit_mode == "fit_height"
        for side in ("L", "R"):
            self.png_state[side]["zoom"] = float(zoom)
            self.png_state[side]["off_x"] = int(off_x)
            self.png_state[side]["off_y"] = int(off_y)
            self.png_state[side]["fit_to_height"] = bool(fit_to_height)

    def _sync_video_transform_from_state(self, side: str = "L") -> None:
        cfg = self._layout_config()
        vt = getattr(cfg, "video_transform", None) if cfg is not None else None
        if vt is None:
            return
        s = self.png_state.get(side)
        if not isinstance(s, dict):
            return
        try:
            z = float(s.get("zoom", 1.0))
        except Exception:
            z = 1.0
        if z < 0.1:
            z = 0.1
        if z > 3.0:
            z = 3.0
        scale_pct = int(round(z * 100.0))
        if scale_pct < 10:
            scale_pct = 10
        if scale_pct > 300:
            scale_pct = 300
        z = float(scale_pct) / 100.0
        s["zoom"] = float(z)
        changed = False
        try:
            shift_x_px = int(s.get("off_x", 0))
        except Exception:
            shift_x_px = 0
        try:
            shift_y_px = int(s.get("off_y", 0))
        except Exception:
            shift_y_px = 0
        try:
            if int(getattr(vt, "scale_pct", 100)) != int(scale_pct):
                vt.scale_pct = int(scale_pct)
                changed = True
        except Exception:
            try:
                vt.scale_pct = int(scale_pct)
                changed = True
            except Exception:
                pass
        try:
            if int(getattr(vt, "shift_x_px", 0)) != int(shift_x_px):
                vt.shift_x_px = int(shift_x_px)
                changed = True
        except Exception:
            try:
                vt.shift_x_px = int(shift_x_px)
                changed = True
            except Exception:
                pass
        try:
            if int(getattr(vt, "shift_y_px", 0)) != int(shift_y_px):
                vt.shift_y_px = int(shift_y_px)
                changed = True
        except Exception:
            try:
                vt.shift_y_px = int(shift_y_px)
                changed = True
            except Exception:
                pass
        for other in ("L", "R"):
            if other == side:
                continue
            self.png_state[other]["zoom"] = float(self.png_state[side].get("zoom", 1.0))
            self.png_state[other]["off_x"] = int(self.png_state[side].get("off_x", 0))
            self.png_state[other]["off_y"] = int(self.png_state[side].get("off_y", 0))
            self.png_state[other]["fit_to_height"] = bool(self.png_state[side].get("fit_to_height", False))
        if changed:
            self._notify_video_transform_changed()

    def png_load_state_for_current(self) -> None:
        self._sync_state_from_video_transform()

    def png_save_state_for_current(self) -> None:
        self._sync_state_from_video_transform()
        key = self._get_png_view_key()
        png_view_data = self._load_png_view_data()
        if not isinstance(png_view_data, dict):
            png_view_data = {}
        png_view_data[key] = {
            "zoom_l": float(self.png_state["L"]["zoom"]),
            "off_lx": int(self.png_state["L"]["off_x"]),
            "off_ly": int(self.png_state["L"]["off_y"]),
            "fit_l": bool(self.png_state["L"].get("fit_to_height", False)),
            "zoom_r": float(self.png_state["R"]["zoom"]),
            "off_rx": int(self.png_state["R"]["off_x"]),
            "off_ry": int(self.png_state["R"]["off_y"]),
            "fit_r": bool(self.png_state["R"].get("fit_to_height", False)),
        }
        self._save_png_view_data(png_view_data)

    def compute_frame_rect_for_preview(self) -> tuple[int, int, int, int, float, int, int, int]:
        # Returns: x0,y0,x1,y1,scale,out_w,out_h,hud_w
        area_w_raw, area_h_raw = self._get_preview_area_size()
        area_w = max(200, int(area_w_raw))
        area_h = max(200, int(area_h_raw))

        output = self._get_output_format()
        out_w = int(output.out_w)
        out_h = int(output.out_h)
        if out_w <= 0 or out_h <= 0:
            out_w, out_h = 1280, 720

        hud_w = int(output.hud_w)
        hud_w = max(0, min(int(hud_w), max(0, out_w - 2)))

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
        return x0, y0, x1, y1, float(scale), int(out_w), int(out_h), int(hud_w)

    def _clamp_png_cover(
        self,
        side: str,
        src_img: Image.Image,
        out_w: int,
        out_h: int,
        hud_w: int,
        layout_config: Any | None = None,
        enforce_cover: bool = True,
        enforce_cover_zoom: bool = True,
    ) -> None:
        try:
            self._png_dbg(
                f"CLAMP ENTER side={side} "
                f"fit={bool(self.png_state[side].get('fit_to_height', False))} "
                f"zoom={float(self.png_state[side].get('zoom', 0.0)):.6f} "
                f"off=({int(self.png_state[side].get('off_x', 0))},{int(self.png_state[side].get('off_y', 0))}) "
                f"src=({src_img.size[0]}x{src_img.size[1]}) out=({out_w}x{out_h}) hud_w={hud_w} "
                f"enforce_cover={bool(enforce_cover)} enforce_cover_zoom={bool(enforce_cover_zoom)}"
            )
        except Exception:
            pass

        """
        enforce_cover=True:
          - Offsets werden so geclamped, dass (wenn moeglich) keine Luecken sichtbar sind.
        enforce_cover_zoom=True zusaetzlich:
          - Zoom wird mindestens auf min_zoom (Cover) angehoben.
        Wenn das Bild kleiner als die Region ist (ow<rw oder oh<rh):
          - Offsets werden auf 0 gesetzt (zentriert), weil "Cover" nicht moeglich ist.
        Offsets bleiben in Output-Pixeln.
        """
        if src_img is None:
            return

        rx0, ry0, rx1, ry1 = _png_region_out(side, out_w, out_h, hud_w, layout_config=layout_config)
        rw = max(1, rx1 - rx0)
        rh = max(1, ry1 - ry0)

        sw = max(1.0, float(src_img.size[0]))
        sh = max(1.0, float(src_img.size[1]))

        # Minimaler Zoom, damit Region komplett abgedeckt wird (Cover)
        min_zoom = max(float(rw) / sw, float(rh) / sh)

        # Grund-Zoom aus State
        try:
            z_out = float(self.png_state[side].get("zoom", 1.0))
        except Exception:
            z_out = 1.0

        # Zoom clamp (immer)
        if z_out < 0.1:
            z_out = 0.1
        if z_out > 3.0:
            z_out = 3.0

        # Cover-Zoom nur wenn gewuenscht
        if enforce_cover and enforce_cover_zoom and z_out < min_zoom:
            z_out = float(min_zoom)
            if z_out > 3.0:
                z_out = 3.0

        self.png_state[side]["zoom"] = float(z_out)

        # Skaliertes Bild in Output-Pixeln
        ow = max(1, int(round(sw * z_out)))
        oh = max(1, int(round(sh * z_out)))

        # Center-Base (wie im Render)
        base_cx = float(rx0) + (float(rw) - float(ow)) / 2.0
        base_cy = float(ry0) + (float(rh) - float(oh)) / 2.0

        if enforce_cover:
            try:
                ox = float(self.png_state[side].get("off_x", 0))
            except Exception:
                ox = 0.0
            try:
                oy = float(self.png_state[side].get("off_y", 0))
            except Exception:
                oy = 0.0

            # Wenn Cover in einer Achse nicht moeglich ist: Offset neutralisieren
            if ow < rw:
                ox = 0.0
            else:
                min_off_x = float(rx1 - ow) - base_cx
                max_off_x = float(rx0) - base_cx
                if min_off_x > max_off_x:
                    min_off_x, max_off_x = max_off_x, min_off_x
                if ox < min_off_x:
                    ox = min_off_x
                if ox > max_off_x:
                    ox = max_off_x

            if oh < rh:
                oy = 0.0
            else:
                min_off_y = float(ry1 - oh) - base_cy
                max_off_y = float(ry0) - base_cy
                if min_off_y > max_off_y:
                    min_off_y, max_off_y = max_off_y, min_off_y
                if oy < min_off_y:
                    oy = min_off_y
                if oy > max_off_y:
                    oy = max_off_y

            self.png_state[side]["off_x"] = int(round(ox))
            self.png_state[side]["off_y"] = int(round(oy))

        try:
            self._png_dbg(
                f"CLAMP EXIT  side={side} "
                f"fit={bool(self.png_state[side].get('fit_to_height', False))} "
                f"zoom={float(self.png_state[side].get('zoom', 0.0)):.6f} "
                f"off=({int(self.png_state[side].get('off_x', 0))},{int(self.png_state[side].get('off_y', 0))}) "
                f"min_zoom={float(min_zoom):.6f} rw={rw} rh={rh} ow={ow} oh={oh} "
                f"enforce_cover={bool(enforce_cover)} enforce_cover_zoom={bool(enforce_cover_zoom)}"
            )
        except Exception:
            pass

    def render_png_preview(self, force_reload: bool = False) -> None:
        if not self._is_png_mode():
            return
        self._sync_state_from_video_transform()

        slow_p, fast_p = self._choose_slow_fast_paths()
        if slow_p is None or fast_p is None:
            self.canvas.delete("all")
            self.canvas.create_text(20, 20, anchor="nw", text="PNG-Vorschau: Bitte 2 Videos mit Zeit im Namen w\u00e4hlen.")
            self.png_frame_last["valid"] = False
            return

        s_start = self._get_start_for_video(slow_p)
        f_start = self._get_start_for_video(fast_p)

        need_reload = force_reload
        if slow_p.name != self.png_left_name or fast_p.name != self.png_right_name:
            need_reload = True
        if s_start != self.png_left_start or f_start != self.png_right_start:
            need_reload = True

        if need_reload:
            self.png_left_name = slow_p.name
            self.png_right_name = fast_p.name
            self.png_left_start = s_start
            self.png_right_start = f_start

            img_l = self._read_frame_as_pil(slow_p, s_start)
            img_r = self._read_frame_as_pil(fast_p, f_start)

            if img_l is None or img_r is None:
                self.canvas.delete("all")
                self.canvas.create_text(20, 20, anchor="nw", text="PNG-Vorschau: Kann Frames nicht lesen (Codec?).")
                self.png_frame_last["valid"] = False
                return

            self.png_img_left = img_l
            self.png_img_right = img_r

        if self.png_img_left is None or self.png_img_right is None:
            self.png_frame_last["valid"] = False
            return

        x0, y0, x1, y1, scale, out_w, out_h, hud_w = self.compute_frame_rect_for_preview()
        output = self._get_output_format()
        layout_config = getattr(output, "layout_config", None)
        mirror_shift_x = False
        mirror_shift_y = False
        try:
            vt = getattr(layout_config, "video_transform", None) if layout_config is not None else None
            if vt is not None:
                mirror_shift_x = bool(getattr(vt, "mirror_shift_x", False))
                mirror_shift_y = bool(getattr(vt, "mirror_shift_y", False))
        except Exception:
            mirror_shift_x = False
            mirror_shift_y = False
        try:
            geom = build_output_geometry_for_size(
                out_w=int(out_w),
                out_h=int(out_h),
                hud_width_px=int(hud_w),
                layout_config=layout_config,
            )
        except Exception:
            geom = build_output_geometry_for_size(
                out_w=int(out_w),
                out_h=int(out_h),
                hud_width_px=int(hud_w),
                layout_config=None,
            )
        if self._on_preview_geometry is not None:
            try:
                self._on_preview_geometry(geom, int(x0), int(y0), float(scale), int(out_w), int(out_h), int(hud_w))
            except Exception:
                pass

        draw_w = max(1, x1 - x0)
        draw_h = max(1, y1 - y0)
        overlays = self._overlay_flags()

        def out_rect_to_frame(x: int, y: int, w: int, h: int) -> tuple[int, int, int, int]:
            fx0 = int(round(float(x) * float(scale)))
            fy0 = int(round(float(y) * float(scale)))
            fx1 = int(round(float(x + w) * float(scale)))
            fy1 = int(round(float(y + h) * float(scale)))
            fx0 = max(0, min(draw_w, fx0))
            fx1 = max(0, min(draw_w, fx1))
            fy0 = max(0, min(draw_h, fy0))
            fy1 = max(0, min(draw_h, fy1))
            if fx1 <= fx0:
                fx1 = min(draw_w, fx0 + 1)
            if fy1 <= fy0:
                fy1 = min(draw_h, fy0 + 1)
            return fx0, fy0, fx1, fy1

        slow_region = out_rect_to_frame(
            int(geom.video_slow_rect.x),
            int(geom.video_slow_rect.y),
            int(geom.video_slow_rect.w),
            int(geom.video_slow_rect.h),
        )
        fast_region = out_rect_to_frame(
            int(geom.video_fast_rect.x),
            int(geom.video_fast_rect.y),
            int(geom.video_fast_rect.w),
            int(geom.video_fast_rect.h),
        )
        hud_regions = [
            out_rect_to_frame(int(r.x), int(r.y), int(r.w), int(r.h))
            for r in tuple(geom.hud_rects)
        ]
        lx0, ly0, lx1, ly1 = slow_region
        rx0, ry0, rx1, ry1 = fast_region
        if hud_regions:
            mx0 = min(r[0] for r in hud_regions)
            mx1 = max(r[2] for r in hud_regions)
        else:
            mx0 = lx1
            mx1 = lx1

        # Merker fuer Hit-Test (Canvas-Koords)
        self.png_frame_last["x0"] = int(x0)
        self.png_frame_last["y0"] = int(y0)
        self.png_frame_last["x1"] = int(x1)
        self.png_frame_last["y1"] = int(y1)
        self.png_frame_last["lx0"] = int(x0 + lx0)
        self.png_frame_last["lx1"] = int(x0 + lx1)
        self.png_frame_last["mx0"] = int(x0 + mx0)
        self.png_frame_last["mx1"] = int(x0 + mx1)
        self.png_frame_last["rx0"] = int(x0 + rx0)
        self.png_frame_last["rx1"] = int(x0 + rx1)
        self.png_frame_last["slow_x0"] = int(x0 + lx0)
        self.png_frame_last["slow_y0"] = int(y0 + ly0)
        self.png_frame_last["slow_x1"] = int(x0 + lx1)
        self.png_frame_last["slow_y1"] = int(y0 + ly1)
        self.png_frame_last["fast_x0"] = int(x0 + rx0)
        self.png_frame_last["fast_y0"] = int(y0 + ry0)
        self.png_frame_last["fast_x1"] = int(x0 + rx1)
        self.png_frame_last["fast_y1"] = int(y0 + ry1)
        self.png_frame_last["valid"] = True
        self.png_frame_last["scale"] = float(scale)

        # Composite (Frame als 1 Bild)
        bg = Image.new("RGB", (draw_w, draw_h), (245, 245, 245))

        def render_side(side: str, src_img: Image.Image, region: tuple[int, int, int, int]) -> None:
            # Zoom/Offset sind in Output-Pixeln gespeichert (stabil, egal wie gross das App-Fenster ist)
            rx0, ry0, rx1, ry1 = region
            rw = max(1, rx1 - rx0)
            rh = max(1, ry1 - ry0)

            z_out = float(self.png_state[side].get("zoom", 1.0))

            if z_out < 0.1:
                z_out = 0.1
            if z_out > 3.0:
                z_out = 3.0

            # Zoom in Preview-Pixel umrechnen
            z_px = float(z_out) * float(scale)

            ow = max(1, int(round(src_img.size[0] * z_px)))
            oh = max(1, int(round(src_img.size[1] * z_px)))

            img2 = src_img.resize((ow, oh), Image.LANCZOS)

            # Offsets sind in Output-Pixeln (stabil bei Resize)
            off_x_out = int(self.png_state[side].get("off_x", 0))
            off_y_out = int(self.png_state[side].get("off_y", 0))
            if side == "R":
                if mirror_shift_x:
                    off_x_out = -off_x_out
                if mirror_shift_y:
                    off_y_out = -off_y_out
            off_x_px = int(round(off_x_out * scale))
            off_y_px = int(round(off_y_out * scale))

            rx0, ry0, rx1, ry1 = region
            rw = max(1, rx1 - rx0)
            rh = max(1, ry1 - ry0)

            base_x = int(rx0 + (rw - ow) / 2) + off_x_px
            base_y = int(ry0 + (rh - oh) / 2) + off_y_px

            pil_paste_clipped(bg, img2, region, (base_x, base_y))

        render_side("L", self.png_img_left, slow_region)
        render_side("R", self.png_img_right, fast_region)

        # Rahmen / Trenner zeichnen wir im Canvas (nicht im Bild)
        tk_img = ImageTk.PhotoImage(bg)

        self.canvas.delete("all")
        self.canvas.create_image(x0, y0, anchor="nw", image=tk_img)

        # Output-Frame always visible
        self.canvas.create_rectangle(x0, y0, x1, y1)

        if overlays["video_rects"]:
            self.canvas.create_rectangle(x0 + lx0, y0 + ly0, x0 + lx1, y0 + ly1)
            self.canvas.create_rectangle(x0 + rx0, y0 + ry0, x0 + rx1, y0 + ry1)
            for hr in hud_regions:
                hx0, hy0, hx1, hy1 = hr
                self.canvas.create_rectangle(x0 + hx0, y0 + hy0, x0 + hx1, y0 + hy1)

        if overlays["labels"]:
            self.canvas.create_text(int(x0 + (lx0 + lx1) / 2), int(y0 + ly0 + 14), anchor="n", text="Video 1")
            self.canvas.create_text(int(x0 + (rx0 + rx1) / 2), int(y0 + ry0 + 14), anchor="n", text="Video 2")
            for idx, hr in enumerate(hud_regions):
                hx0, hy0, hx1, hy1 = hr
                self.canvas.create_text(
                    int(x0 + (hx0 + hx1) / 2),
                    int(y0 + (hy0 + hy1) / 2),
                    anchor="center",
                    text=f"HUD {idx + 1}",
                )

        hud_boxes: list[dict] = []
        if self._get_hud_boxes is not None:
            try:
                boxes = self._get_hud_boxes()
                if isinstance(boxes, list):
                    hud_boxes = boxes
            except Exception:
                hud_boxes = []
        enabled_types: set[str] = set()
        if self._get_enabled_types is not None:
            try:
                enabled_types = set(self._get_enabled_types())
            except Exception:
                enabled_types = set()

        if overlays["hud_boxes"] or overlays["labels"]:
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
                cx0, cy0, cx1, cy1 = out_rect_to_frame(bx, by, bw, bh)
                tag = f"hud_{t.replace(' ', '_').replace('/', '_')}"
                if overlays["hud_boxes"]:
                    self.canvas.create_rectangle(
                        x0 + cx0,
                        y0 + cy0,
                        x0 + cx1,
                        y0 + cy1,
                        fill="white",
                        outline="black",
                        tags=("hud_box", tag),
                    )
                    hx0 = max(x0 + cx0, x0 + cx1 - 12)
                    hy0 = max(y0 + cy0, y0 + cy1 - 12)
                    self.canvas.create_rectangle(
                        hx0,
                        hy0,
                        x0 + cx1,
                        y0 + cy1,
                        fill="white",
                        outline="black",
                        tags=("hud_handle", tag),
                    )
                if overlays["labels"]:
                    self.canvas.create_text(
                        int(x0 + (cx0 + cx1) / 2),
                        int(y0 + (cy0 + cy1) / 2),
                        text=t,
                        tags=("hud_box", tag),
                    )

        # Wichtig: Referenz halten
        self.canvas._tk_img = tk_img

    def png_hit_side(self, x: int, y: int) -> str:
        if not bool(self.png_frame_last.get("valid")):
            return ""
        if x < int(self.png_frame_last["x0"]) or x > int(self.png_frame_last["x1"]):
            return ""
        if y < int(self.png_frame_last["y0"]) or y > int(self.png_frame_last["y1"]):
            return ""
        if (
            x >= int(self.png_frame_last.get("slow_x0", self.png_frame_last.get("lx0", 0)))
            and x <= int(self.png_frame_last.get("slow_x1", self.png_frame_last.get("lx1", 0)))
            and y >= int(self.png_frame_last.get("slow_y0", self.png_frame_last.get("y0", 0)))
            and y <= int(self.png_frame_last.get("slow_y1", self.png_frame_last.get("y1", 0)))
        ):
            return "L"
        if (
            x >= int(self.png_frame_last.get("fast_x0", self.png_frame_last.get("rx0", 0)))
            and x <= int(self.png_frame_last.get("fast_x1", self.png_frame_last.get("rx1", 0)))
            and y >= int(self.png_frame_last.get("fast_y0", self.png_frame_last.get("y0", 0)))
            and y <= int(self.png_frame_last.get("fast_y1", self.png_frame_last.get("y1", 0)))
        ):
            return "R"
        return ""

    def png_on_wheel(self, e: Any) -> None:
        if not self._is_png_mode():
            return
        self._sync_state_from_video_transform()

        try:
            self._png_dbg(
                f"WHEEL ENTER side=? x={int(e.x)} y={int(e.y)} "
                f"delta={getattr(e, 'delta', None)}"
            )
        except Exception:
            pass

        side = self.png_hit_side(int(e.x), int(e.y))
        if side not in ("L", "R"):
            return

        try:
            self._png_dbg(
                f"WHEEL SIDE side={side} "
                f"fit={bool(self.png_state[side].get('fit_to_height', False))} "
                f"zoom={float(self.png_state[side].get('zoom', 0.0)):.6f} "
                f"off=({int(self.png_state[side].get('off_x', 0))},{int(self.png_state[side].get('off_y', 0))}) "
                f"e.xy=({int(e.x)},{int(e.y)}) delta={getattr(e, 'delta', None)}"
            )
        except Exception:
            pass

        try:
            d = int(e.delta)
        except Exception:
            d = 0
        if d == 0:
            return
        if d > 0:
            notches = max(1, int(abs(d) // 120))
        else:
            notches = -max(1, int(abs(d) // 120))

        # Bild holen
        if self.png_img_left is None or self.png_img_right is None:
            self.render_png_preview(force_reload=True)

        src_img = self.png_img_left if side == "L" else self.png_img_right
        if src_img is None:
            return

        # Output-Region bestimmen (fuer Cover-Minimum)
        output = self._get_output_format()
        out_w = int(output.out_w)
        out_h = int(output.out_h)
        if out_w <= 0 or out_h <= 0:
            out_w, out_h = 1280, 720
        hud_w = int(output.hud_w)
        hud_w = max(0, min(hud_w, max(0, out_w - 2)))
        layout_config = getattr(output, "layout_config", None)

        try:
            cur_z = float(self.png_state[side].get("zoom", 1.0))
        except Exception:
            cur_z = 1.0

        cur_scale_pct = int(round(float(cur_z) * 100.0))
        if cur_scale_pct < 10:
            cur_scale_pct = 10
        if cur_scale_pct > 300:
            cur_scale_pct = 300
        new_scale_pct = cur_scale_pct + int(notches)
        if new_scale_pct < 10:
            new_scale_pct = 10
        if new_scale_pct > 300:
            new_scale_pct = 300
        self.png_state[side]["zoom"] = float(new_scale_pct) / 100.0

        # Nur clampen ohne "Snap"/Cover
        try:
            self._clamp_png_cover(
                side,
                src_img,
                out_w,
                out_h,
                hud_w,
                layout_config=layout_config,
                enforce_cover=False,
            )
        except Exception:
            pass
        self._sync_video_transform_from_state(side)

        try:
            self._png_dbg(
                f"WHEEL EXIT side={side} "
                f"fit={bool(self.png_state[side].get('fit_to_height', False))} "
                f"zoom={float(self.png_state[side].get('zoom', 0.0)):.6f} "
                f"off=({int(self.png_state[side].get('off_x', 0))},{int(self.png_state[side].get('off_y', 0))}) "
                f"notches={int(notches)}"
            )
        except Exception:
            pass

        self.png_save_state_for_current()
        self.render_png_preview(force_reload=False)

    def png_on_down(self, e: Any) -> None:
        if not self._is_png_mode():
            return
        self._sync_state_from_video_transform()
        side = self.png_hit_side(int(e.x), int(e.y))
        if side not in ("L", "R"):
            return
        self.png_state["drag"] = True
        self.png_state["drag_side"] = side
        self.png_state["drag_x"] = int(e.x)
        self.png_state["drag_y"] = int(e.y)

    def png_on_move(self, e: Any) -> None:
        if not self._is_png_mode():
            return
        self._sync_state_from_video_transform()
        if not self.png_state.get("drag"):
            return
        side = str(self.png_state.get("drag_side") or "")
        if side not in ("L", "R"):
            return

        try:
            self._png_dbg(
                f"MOVE ENTER side={side} "
                f"fit={bool(self.png_state[side].get('fit_to_height', False))} "
                f"zoom={float(self.png_state[side].get('zoom', 0.0)):.6f} "
                f"off=({int(self.png_state[side].get('off_x', 0))},{int(self.png_state[side].get('off_y', 0))}) "
                f"e.xy=({int(e.x)},{int(e.y)})"
            )
        except Exception:
            pass

        dx_px = int(e.x) - int(self.png_state["drag_x"])
        dy_px = int(e.y) - int(self.png_state["drag_y"])
        self.png_state["drag_x"] = int(e.x)
        self.png_state["drag_y"] = int(e.y)

        scale = float(self.png_frame_last.get("scale") or 1.0)
        if scale <= 0.0001:
            scale = 1.0

        dx_out = int(round(dx_px / scale))
        dy_out = int(round(dy_px / scale))

        self.png_state[side]["off_x"] = int(self.png_state[side]["off_x"]) + dx_out
        self.png_state[side]["off_y"] = int(self.png_state[side]["off_y"]) + dy_out
        self._sync_video_transform_from_state(side)

        self.render_png_preview(force_reload=False)

    def png_on_up(self, _e: Any = None) -> None:
        try:
            self._png_dbg("UP ENTER")
        except Exception:
            pass
        if not self._is_png_mode():
            return
        self._sync_state_from_video_transform()

        side = str(self.png_state.get("drag_side") or "")
        self.png_state["drag"] = False
        self.png_state["drag_side"] = ""

        # Beim Loslassen: automatisch "snappen" (keine Luecken)
        try:
            output = self._get_output_format()
            out_w = int(output.out_w)
            out_h = int(output.out_h)
            if out_w <= 0 or out_h <= 0:
                out_w, out_h = 1280, 720
            hud_w = int(output.hud_w)
            hud_w = max(0, min(hud_w, max(0, out_w - 2)))
            layout_config = getattr(output, "layout_config", None)

            if side in ("L", "R"):
                src_img = self.png_img_left if side == "L" else self.png_img_right
                if src_img is not None:
                    self._clamp_png_cover(
                        side,
                        src_img,
                        out_w,
                        out_h,
                        hud_w,
                        layout_config=layout_config,
                        enforce_cover=True,
                        enforce_cover_zoom=False,
                    )
        except Exception:
            pass

        try:
            if side in ("L", "R"):
                self._png_dbg(
                    f"UP EXIT side={side} "
                    f"fit={bool(self.png_state[side].get('fit_to_height', False))} "
                    f"zoom={float(self.png_state[side].get('zoom', 0.0)):.6f} "
                    f"off=({int(self.png_state[side].get('off_x', 0))},{int(self.png_state[side].get('off_y', 0))})"
                )
        except Exception:
            pass
        if side in ("L", "R"):
            self._sync_video_transform_from_state(side)

        self.png_save_state_for_current()
        self.render_png_preview(force_reload=False)

    def _fit_video_both_for_mode(self, fit_button_mode: str) -> None:
        if not self._is_png_mode():
            return
        self._sync_state_from_video_transform()
        if self.png_img_left is None or self.png_img_right is None:
            self.render_png_preview(force_reload=True)
        if self.png_img_left is None or self.png_img_right is None:
            return

        output = self._get_output_format()
        out_w = int(output.out_w)
        out_h = int(output.out_h)
        if out_w <= 0 or out_h <= 0:
            out_w, out_h = 1280, 720

        hud_w = int(output.hud_w)
        hud_w = max(0, min(hud_w, max(0, out_w - 2)))
        layout_config = getattr(output, "layout_config", None)
        fit_mode = str(fit_button_mode or "fit_height").strip().lower()
        if fit_mode not in ("fit_height", "fit_width"):
            fit_mode = "fit_height"

        def fit_scale_pct_for(side: str, img: Image.Image) -> int:
            rx0, ry0, rx1, ry1 = _png_region_out(side, out_w, out_h, hud_w, layout_config=layout_config)
            rw = max(1, int(rx1 - rx0))
            rh = max(1, int(ry1 - ry0))
            sw = max(1.0, float(img.size[0]))
            sh = max(1.0, float(img.size[1]))
            if fit_mode == "fit_width":
                # Render-Basis fuer TB: scale={target_w}:-2, danach * scale_pct.
                base_w = float(rw)
                target_w = float(rw)
                pct = (target_w / max(1e-6, base_w)) * 100.0
            else:
                # Render-Basis fuer LR: scale=-2:{target_h}, danach * scale_pct.
                _base_w = sw * (float(rh) / max(1.0, sh))
                _base_h = float(rh)
                target_h = float(rh)
                pct = (target_h / max(1e-6, _base_h)) * 100.0
            p = int(round(pct))
            if p < 10:
                p = 10
            if p > 300:
                p = 300
            return int(p)

        pcts = [
            fit_scale_pct_for("L", self.png_img_left),
            fit_scale_pct_for("R", self.png_img_right),
        ]
        scale_pct = int(round(sum(pcts) / max(1, len(pcts))))
        if scale_pct < 10:
            scale_pct = 10
        if scale_pct > 300:
            scale_pct = 300

        cfg = self._layout_config()
        vt = getattr(cfg, "video_transform", None) if cfg is not None else None
        if vt is not None:
            vt.fit_button_mode = str(fit_mode)
            vt.scale_pct = int(scale_pct)
            vt.shift_x_px = 0
            vt.shift_y_px = 0
            self._notify_video_transform_changed()
            self._sync_state_from_video_transform()
        else:
            self.png_state["L"]["zoom"] = float(scale_pct) / 100.0
            self.png_state["R"]["zoom"] = float(scale_pct) / 100.0
            self.png_state["L"]["off_x"] = 0
            self.png_state["L"]["off_y"] = 0
            self.png_state["R"]["off_x"] = 0
            self.png_state["R"]["off_y"] = 0

        self.png_save_state_for_current()
        self.render_png_preview(force_reload=False)

    def fit_video_for_LR(self) -> None:
        self._fit_video_both_for_mode("fit_height")

    def fit_video_for_TB(self) -> None:
        self._fit_video_both_for_mode("fit_width")

    def png_fit_to_height_both(self) -> None:
        layout_mode = "LR"
        try:
            cfg = self._layout_config()
            layout_mode = str(getattr(cfg, "video_layout", "LR") or "LR").strip().upper()
        except Exception:
            layout_mode = "LR"
        if layout_mode == "TB":
            self.fit_video_for_TB()
        else:
            self.fit_video_for_LR()
