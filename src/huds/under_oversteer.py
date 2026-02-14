from __future__ import annotations

import math
import os
from typing import Any

from huds.common import (
    COL_HUD_BG,
    build_value_boundaries,
    choose_tick_step,
    draw_left_axis_labels,
    draw_stripe_grid,
    draw_text_with_shadow,
    filter_axis_labels_by_position,
    format_value_for_step,
    should_suppress_boundary_label,
    value_boundaries_to_y,
)

_UO_HUD_DEBUG_LOGGED = False


def _clamp_float(value: float, lo: float, hi: float) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _uo_debug_enabled() -> bool:
    raw = (os.environ.get("IRVC_DEBUG_UO") or "").strip()
    if raw == "":
        return False
    try:
        lvl = int(float(raw))
    except Exception:
        lvl = 1
    return lvl >= 1


def render_under_oversteer(ctx: dict[str, Any], box: tuple[int, int, int, int], dr: Any) -> None:
    x0, y0, w, h = box
    hud_key = ctx.get("hud_key")
    if hud_key != "Under-/Oversteer":
        return

    i = int(ctx.get("i", 0))
    before_f = max(1, int(ctx.get("before_f", 1)))
    after_f = max(1, int(ctx.get("after_f", 1)))
    frame_window_mapping = ctx.get("frame_window_mapping")
    map_idxs_all = list(getattr(frame_window_mapping, "idxs", []) or [])
    map_offsets_all = list(getattr(frame_window_mapping, "offsets", []) or [])
    slow_vals = ctx.get("under_oversteer_slow_frames")
    fast_vals = ctx.get("under_oversteer_fast_frames")
    y_abs_in = ctx.get("under_oversteer_y_abs")

    COL_WHITE = ctx.get("COL_WHITE", (255, 255, 255, 255))
    COL_SLOW_DARKRED = ctx.get("COL_SLOW_DARKRED", (234, 0, 0, 255))
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

    font_sz = int(round(max(10.0, min(18.0, float(h) * 0.13))))
    font_title = _load_font(font_sz)
    font_axis = _load_font(max(8, int(font_sz - 2)))
    font_axis_small = _load_font(max(7, int(font_sz - 3)))

    top_pad = int(round(max(14.0, float(font_sz) + 8.0)))
    plot_y0 = int(y0) + top_pad
    plot_y1 = int(y0 + h - 2)
    if plot_y1 <= plot_y0 + 4:
        plot_y0 = int(y0) + 2
        plot_y1 = int(y0 + h - 2)

    line_width = 2
    curve_margin = max(3, int(line_width) + 1)
    curve_top = int(plot_y0 + curve_margin)
    curve_bottom = int(plot_y1 - curve_margin)
    if curve_bottom <= curve_top:
        curve_top = int(plot_y0)
        curve_bottom = int(plot_y1)
    curve_height = float(max(1, curve_bottom - curve_top))
    pad_frac = max(
        0.02,
        min(0.06, (float(curve_margin) + 1.0) / max(1.0, curve_height)),
    )
    usable_frac_span = max(0.0, 1.0 - 2.0 * pad_frac)

    y_abs = 0.0
    try:
        y_abs = abs(float(y_abs_in))
    except Exception:
        y_abs = 0.0
    if (not math.isfinite(y_abs)) or y_abs < 1e-6:
        y_abs = 1.0

    y_min = -float(y_abs)
    y_max = float(y_abs)
    den = y_max - y_min
    if den <= 1e-12:
        den = 1.0

    def _y_from_val(v: float) -> int:
        vv = float(v)
        if vv < y_min:
            vv = y_min
        if vv > y_max:
            vv = y_max
        frac = (vv - y_min) / den
        frac = _clamp_float(frac, 0.0, 1.0)
        scaled_frac = pad_frac + frac * usable_frac_span
        span = float(curve_bottom - curve_top)
        yy = float(curve_bottom) - (scaled_frac * span)
        yy = _clamp_float(yy, float(curve_top), float(curve_bottom))
        return int(round(yy))

    label_x = int(x0 + 4)
    label_top_y = int(y0 + 2)
    label_bottom_y = int(y0 + h - font_sz - 2)

    # Story 10: 5 Segmente, symmetrisch um 0.
    axis_labels: list[tuple[int, str]] = []
    try:
        tick_ref_max = max(abs(float(y_min)), abs(float(y_max)))
        step = choose_tick_step(0.0, tick_ref_max, min_segments=2, max_segments=5, target_segments=5)
        if step is not None:
            val_bounds = build_value_boundaries(y_min, y_max, float(step), anchor="top")
            y_bounds = value_boundaries_to_y(val_bounds, _y_from_val, int(plot_y0), int(plot_y1))
            draw_stripe_grid(
                dr,
                int(x0),
                int(w),
                int(plot_y0),
                int(plot_y1),
                y_bounds,
                col_bg=COL_HUD_BG,
                darken_delta=6,
            )
            for vv in val_bounds:
                if should_suppress_boundary_label(float(vv), y_min, y_max, suppress_zero=True):
                    continue
                axis_labels.append((int(_y_from_val(float(vv))), format_value_for_step(float(vv), float(step), min_decimals=0)))
    except Exception:
        pass

    # Neutral baseline: Linie spaeter zeichnen.
    y_zero = _y_from_val(0.0)
    axis_labels = filter_axis_labels_by_position(
        axis_labels,
        int(plot_y0),
        int(plot_y1),
        zero_y=int(y_zero),
        pad_px=2,
    )

    marker_xf = float(x0) + (float(w) / 2.0)
    half_w = float(w) / 2.0

    slow_series = slow_vals if isinstance(slow_vals, list) else []
    fast_series = fast_vals if isinstance(fast_vals, list) else []
    n_s = len(slow_series)
    n_f = len(fast_series)

    debug_enabled = _uo_debug_enabled()
    global _UO_HUD_DEBUG_LOGGED
    if debug_enabled and not _UO_HUD_DEBUG_LOGGED:
        slow_peak_val = 0.0
        slow_peak_abs = 0.0
        for val in slow_series:
            if not math.isfinite(val):
                continue
            av = abs(float(val))
            if av > slow_peak_abs:
                slow_peak_abs = av
                slow_peak_val = float(val)
        fast_peak_val = 0.0
        fast_peak_abs = 0.0
        for val in fast_series:
            if not math.isfinite(val):
                continue
            av = abs(float(val))
            if av > fast_peak_abs:
                fast_peak_abs = av
                fast_peak_val = float(val)
        slow_peak_y = None
        fast_peak_y = None
        if slow_series:
            slow_peak_y = _y_from_val(slow_peak_val)
        if fast_series:
            fast_peak_y = _y_from_val(fast_peak_val)
        print(
            f"[uo-hud] y_abs={y_abs:.6f} plot_bounds={plot_y0}..{plot_y1} "
            f"curve_bounds={curve_top}..{curve_bottom} curve_height={curve_height:.6f} margin={curve_margin}"
        )
        if slow_series:
            slow_in_bounds = slow_peak_y is not None and curve_top <= slow_peak_y <= curve_bottom
            print(
                f"[uo-hud] slow peak={slow_peak_val:+.6f} abs={slow_peak_abs:.6f} mapped_y={slow_peak_y} "
                f"in_bounds={slow_in_bounds}"
            )
        if fast_series:
            fast_in_bounds = fast_peak_y is not None and curve_top <= fast_peak_y <= curve_bottom
            print(
                f"[uo-hud] fast peak={fast_peak_val:+.6f} abs={fast_peak_abs:.6f} mapped_y={fast_peak_y} "
                f"in_bounds={fast_in_bounds}"
            )
        _UO_HUD_DEBUG_LOGGED = True

    pts_slow: list[tuple[int, int]] = []
    pts_fast: list[tuple[int, int]] = []

    if map_idxs_all and len(map_idxs_all) == len(map_offsets_all):
        iter_rows = [(int(idx_m), int(ofs_m)) for idx_m, ofs_m in zip(map_idxs_all, map_offsets_all)]
    else:
        iter_rows = [
            (int(i - before_f), int(-before_f)),
            (int(i), 0),
            (int(i + after_f), int(after_f)),
        ]

    for idx_raw, ofs in iter_rows:
        if ofs < -int(before_f) or ofs > int(after_f):
            continue

        idx_s = int(idx_raw)
        if idx_s < 0:
            idx_s = 0
        if n_s > 0 and idx_s >= n_s:
            idx_s = n_s - 1

        idx_f = int(idx_raw)
        if idx_f < 0:
            idx_f = 0
        if n_f > 0 and idx_f >= n_f:
            idx_f = n_f - 1

        s_val = 0.0
        if n_s > 0:
            try:
                s_val = float(slow_series[idx_s])
            except Exception:
                s_val = 0.0
        if not math.isfinite(s_val):
            s_val = 0.0

        f_val = 0.0
        if n_f > 0:
            try:
                f_val = float(fast_series[idx_f])
            except Exception:
                f_val = 0.0
        if not math.isfinite(f_val):
            f_val = 0.0

        if ofs <= 0:
            x = marker_xf + (float(ofs) / float(before_f)) * half_w
        else:
            x = marker_xf + (float(ofs) / float(after_f)) * half_w

        xi = int(round(x))
        if xi < int(x0):
            xi = int(x0)
        if xi > int(x0 + w - 1):
            xi = int(x0 + w - 1)

        ys = _y_from_val(s_val)
        yf = _y_from_val(f_val)

        if pts_slow and int(pts_slow[-1][0]) == int(xi):
            pts_slow[-1] = (int(xi), int(ys))
        else:
            pts_slow.append((int(xi), int(ys)))

        if pts_fast and int(pts_fast[-1][0]) == int(xi):
            pts_fast[-1] = (int(xi), int(yf))
        else:
            pts_fast.append((int(xi), int(yf)))

    if len(pts_slow) >= 2:
        try:
            dr.line(pts_slow, fill=COL_SLOW_DARKRED, width=line_width)  # slow = red
        except Exception:
            pass

    if len(pts_fast) >= 2:
        try:
            dr.line(pts_fast, fill=COL_FAST_DARKBLUE, width=line_width)  # fast = blue
        except Exception:
            pass

    # Neutral baseline nach den Kurven zeichnen, damit sie durchgehend bleibt.
    try:
        dr.line([(int(x0), int(y_zero)), (int(x0 + w - 1), int(y_zero))], fill=COL_WHITE, width=1)
    except Exception:
        pass

    marker_x = int(round(marker_xf))
    try:
        dr.rectangle([marker_x, int(y0), marker_x + 1, int(y0 + h - 1)], fill=(255, 255, 255, 230))
    except Exception:
        pass

    # Text zuletzt: Y-Achse + Titel.
    draw_left_axis_labels(
        dr,
        int(x0),
        int(w),
        int(plot_y0),
        int(plot_y1),
        axis_labels,
        font_axis,
        col_text=COL_WHITE,
        x_pad=6,
        fallback_font_obj=font_axis_small,
    )
    try:
        draw_text_with_shadow(dr, (label_x, label_top_y), "Oversteer", fill=COL_WHITE, font=font_title)
    except Exception:
        pass
    try:
        draw_text_with_shadow(dr, (label_x, label_bottom_y), "Understeer", fill=COL_WHITE, font=font_title)
    except Exception:
        pass
