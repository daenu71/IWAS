from __future__ import annotations

import math
from typing import Any


def render_line_delta(ctx: dict[str, Any], box: tuple[int, int, int, int], dr: Any) -> None:
    x0, y0, w, h = box
    hud_key = ctx["hud_key"]
    if hud_key != "Line Delta":
        return

    i = int(ctx.get("i", 0))
    before_f = max(1, int(ctx.get("before_f", 1)))
    after_f = max(1, int(ctx.get("after_f", 1)))
    line_delta_m_frames = ctx.get("line_delta_m_frames")
    line_delta_y_abs_m = ctx.get("line_delta_y_abs_m")
    COL_WHITE = ctx.get("COL_WHITE", (255, 255, 255, 255))
    COL_FAST_DARKBLUE = ctx.get("COL_FAST_DARKBLUE", (36, 0, 250, 255))

    try:
        from PIL import ImageFont
    except Exception:
        ImageFont = None  # type: ignore

    def _load_font(sz: int):
        if ImageFont is None:
            return None
        try:
            return ImageFont.truetype("arial.ttf", sz)
        except Exception:
            pass
        try:
            return ImageFont.truetype("DejaVuSans.ttf", sz)
        except Exception:
            pass
        try:
            return ImageFont.load_default()
        except Exception:
            return None

    def _text_w(text: str, font_obj: Any) -> int:
        try:
            bb = dr.textbbox((0, 0), text, font=font_obj)
            return int(bb[2] - bb[0])
        except Exception:
            try:
                return int(dr.textlength(text, font=font_obj))
            except Exception:
                return int(len(text) * 8)

    font_sz = int(round(max(10.0, min(18.0, float(h) * 0.13))))
    font_val_sz = int(round(max(11.0, min(20.0, float(h) * 0.15))))
    font_title = _load_font(font_sz)
    font_val = _load_font(font_val_sz)

    top_pad = int(round(max(14.0, float(font_sz) + 8.0)))
    plot_y0 = int(y0) + top_pad
    plot_y1 = int(y0 + h - 2)
    if plot_y1 <= plot_y0 + 4:
        plot_y0 = int(y0) + 2
        plot_y1 = int(y0 + h - 2)

    marker_xf = float(x0) + (float(w) / 2.0)
    marker_x = int(round(marker_xf))

    vals = line_delta_m_frames if isinstance(line_delta_m_frames, list) else []
    n_vals = len(vals)

    cur_idx = int(i)
    if cur_idx < 0:
        cur_idx = 0
    if n_vals > 0 and cur_idx >= n_vals:
        cur_idx = n_vals - 1

    window: list[tuple[int, float]] = []
    for ofs in range(-before_f, after_f + 1):
        idx = int(i + ofs)
        if idx < 0:
            idx = 0
        if n_vals > 0 and idx >= n_vals:
            idx = n_vals - 1
        v = 0.0
        if n_vals > 0:
            try:
                v = float(vals[idx])
            except Exception:
                v = 0.0
        if not math.isfinite(v):
            v = 0.0
        window.append((int(ofs), float(v)))

    y_abs = 0.0
    try:
        y_abs = float(line_delta_y_abs_m)
    except Exception:
        y_abs = 0.0
    if not math.isfinite(y_abs) or y_abs < 0.0:
        y_abs = 0.0
    y_min_m = -float(y_abs)
    y_max_m = float(y_abs)
    if y_max_m <= y_min_m:
        y_max_m = y_min_m + 1e-6

    def _y_from_m(v_m: float) -> int:
        vv = float(v_m)
        if vv < y_min_m:
            vv = y_min_m
        if vv > y_max_m:
            vv = y_max_m
        den = (y_max_m - y_min_m)
        if den <= 1e-12:
            return int(round((plot_y0 + plot_y1) / 2.0))
        frac = (vv - y_min_m) / den
        yy = float(plot_y1) - (frac * float(plot_y1 - plot_y0))
        return int(round(yy))

    # Titel
    try:
        dr.text((int(x0 + 4), int(y0 + 2)), "Line delta", fill=COL_WHITE, font=font_title)
    except Exception:
        pass

    # 0m Referenzlinie
    y_zero = int(_y_from_m(0.0))
    try:
        dr.line([(int(x0), y_zero), (int(x0 + w - 1), y_zero)], fill=COL_WHITE, width=1)
    except Exception:
        pass

    # Kurve (blau): X-Mapping strikt ueber Frame-Offset relativ zum Marker.
    half_w = float(w) / 2.0
    pts: list[tuple[int, int]] = []
    for ofs, v in window:
        if ofs < 0:
            x = marker_xf + (float(ofs) / float(before_f)) * half_w
        else:
            x = marker_xf + (float(ofs) / float(after_f)) * half_w
        xi = int(round(x))
        if xi < int(x0):
            xi = int(x0)
        if xi > int(x0 + w - 1):
            xi = int(x0 + w - 1)
        yi = int(_y_from_m(float(v)))
        if pts and int(pts[-1][0]) == int(xi):
            pts[-1] = (int(xi), int(yi))
        else:
            pts.append((int(xi), int(yi)))

    if len(pts) >= 2:
        try:
            dr.line(pts, fill=COL_FAST_DARKBLUE, width=2)
        except Exception:
            pass

    # Center-Marker bleibt fix bei x = w/2.
    try:
        dr.rectangle([marker_x, y0, marker_x + 1, y0 + h], fill=(255, 255, 255, 230))
    except Exception:
        pass

    cur_delta = 0.0
    if n_vals > 0:
        try:
            cur_delta = float(vals[cur_idx])
        except Exception:
            cur_delta = 0.0
    if not math.isfinite(cur_delta):
        cur_delta = 0.0
    if abs(cur_delta) < 0.005:
        cur_delta = 0.0

    prefix = "L" if cur_delta >= 0.0 else "R"
    txt = f"{prefix} {abs(cur_delta):.2f} m"
    placeholder = "R 999.99 m"
    w_fix = _text_w(placeholder, font_val)
    if len(txt) < len(placeholder):
        txt = txt.rjust(len(placeholder), " ")
    x_val = int(marker_x - 6 - w_fix)
    if x_val < int(x0 + 4):
        x_val = int(x0 + 4)
    y_val = int(y0 + 2)
    try:
        dr.text((x_val, y_val), txt, fill=COL_WHITE, font=font_val)
    except Exception:
        pass
