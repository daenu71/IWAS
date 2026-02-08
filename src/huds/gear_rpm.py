from __future__ import annotations

import math
from typing import Any

from huds.common import COL_HUD_BG, draw_hud_background


_RPM_PEAK_CACHE: dict[int, tuple[int, list[int]]] = {}


def _safe_int(arr: Any, idx: int) -> int:
    if not arr or idx < 0 or idx >= len(arr):
        return 0
    try:
        v = float(arr[idx])
    except Exception:
        return 0
    if not math.isfinite(v):
        return 0
    return int(v)


def _monotonic_peak_upto(arr: Any, idx: int) -> int:
    if not arr or idx < 0:
        return 0
    n = min(int(idx) + 1, int(len(arr)))
    if n <= 0:
        return 0

    key = int(id(arr))
    cached = _RPM_PEAK_CACHE.get(key)
    if cached is None or cached[0] != int(len(arr)):
        peaks: list[int] = []
        peak = 0
        for j in range(int(len(arr))):
            v = _safe_int(arr, j)
            if j == 0:
                peak = int(v)
            else:
                peak = int(max(peak, v))
            peaks.append(int(peak))
        _RPM_PEAK_CACHE[key] = (int(len(arr)), peaks)
        cached = _RPM_PEAK_CACHE.get(key)

    if cached is None:
        return 0
    peaks_cached = cached[1]
    if not peaks_cached:
        return 0
    out_idx = int(min(n - 1, len(peaks_cached) - 1))
    return int(peaks_cached[out_idx])


def render_gear_rpm(ctx: dict[str, Any], box: tuple[int, int, int, int], dr: Any) -> None:
    x0, y0, w, h = box

    hud_key = ctx["hud_key"]
    i = int(ctx["i"])
    fi = int(ctx["fi"])
    slow_gear_h = ctx["slow_gear_h"]
    fast_gear_h = ctx["fast_gear_h"]
    slow_rpm_h = ctx["slow_rpm_h"]
    fast_rpm_h = ctx["fast_rpm_h"]
    col_slow_darkred = ctx["COL_SLOW_DARKRED"]
    col_fast_darkblue = ctx["COL_FAST_DARKBLUE"]

    if hud_key != "Gear & RPM":
        return

    draw_hud_background(dr, box, col_bg=COL_HUD_BG)

    if not (slow_gear_h and i < len(slow_gear_h) and fast_gear_h and fi < len(fast_gear_h)):
        return

    sg = _safe_int(slow_gear_h, i)
    fg = _safe_int(fast_gear_h, fi)
    sr = _safe_int(slow_rpm_h, i)
    fr = _safe_int(fast_rpm_h, fi)
    smax = _monotonic_peak_upto(slow_rpm_h, i)
    fmax = _monotonic_peak_upto(fast_rpm_h, fi)

    try:
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

        def _text_wh(text: str, font_obj: Any) -> tuple[int, int]:
            try:
                bb = dr.textbbox((0, 0), str(text), font=font_obj)
                return int(bb[2] - bb[0]), int(bb[3] - bb[1])
            except Exception:
                return int(max(1, len(str(text))) * 7), 12

        def _draw_centered_text(
            x0_cell: int,
            y0_cell: int,
            x1_cell: int,
            y1_cell: int,
            text: str,
            font_obj: Any,
            col: Any,
            pad_y: int = 0,
        ) -> None:
            txt = str(text)
            try:
                bb = dr.textbbox((0, 0), txt, font=font_obj)
                tw = float(bb[2] - bb[0])
                th = float(bb[3] - bb[1])
                bx0 = float(bb[0])
                by0 = float(bb[1])
            except Exception:
                tw_i, th_i = _text_wh(txt, font_obj)
                tw = float(tw_i)
                th = float(th_i)
                bx0 = 0.0
                by0 = 0.0

            inner_y0 = float(y0_cell + max(0, int(pad_y)))
            inner_y1 = float(y1_cell - max(0, int(pad_y)))
            if inner_y1 < inner_y0:
                mid = (float(y0_cell) + float(y1_cell)) * 0.5
                inner_y0 = mid
                inner_y1 = mid

            tx = (((float(x0_cell) + float(x1_cell)) - tw) * 0.5) - bx0
            ty = (((inner_y0 + inner_y1) - th) * 0.5) - by0
            dr.text((int(round(tx)), int(round(ty))), txt, fill=col, font=font_obj)

        header_labels = ("Gear", "RPM", "Max. RPM")
        slow_values = (str(sg), str(sr), str(smax))
        fast_values = (str(fg), str(fr), str(fmax))

        outer_pad_x = int(max(4, min(10, round(float(w) * 0.02))))
        outer_pad_y = int(max(4, min(10, round(float(h) * 0.08))))
        table_gap = int(max(4, min(16, round(float(w) * 0.03))))
        avail_tables_w = int(w - (2 * outer_pad_x) - table_gap)
        if avail_tables_w < 12:
            avail_tables_w = max(12, int(w - 2))
            table_gap = 0
            outer_pad_x = 1

        table_w = int(max(6, avail_tables_w // 2))
        left_x = int(x0 + outer_pad_x)
        right_x = int(left_x + table_w + table_gap)
        table_y0 = int(y0 + outer_pad_y)
        table_y1 = int(y0 + h - outer_pad_y)
        if table_y1 <= table_y0:
            table_y0 = int(y0)
            table_y1 = int(y0 + h)

        table_top = int(table_y0)
        table_bottom = int(table_y1 - 1)
        if table_bottom <= table_top:
            table_bottom = int(table_top + 1)

        table_h = int(max(2, table_bottom - table_top + 1))
        col_w = float(table_w) / 3.0
        cell_pad_y = int(max(1, min(4, round(float(h) * 0.01))))
        cell_pad_x = int(max(2, min(8, round(col_w * 0.08))))
        fit_w = int(max(8, round(col_w) - (2 * cell_pad_x)))

        font_title = _load_font(18)
        try:
            header_bbox = dr.textbbox((0, 0), "Max. RPM", font=font_title)
            header_text_h = int(header_bbox[3] - header_bbox[1])
        except Exception:
            header_text_h = _text_wh("Max. RPM", font_title)[1]
        header_pad_top = 2
        header_pad_bottom = 3
        header_row_h = int(header_text_h + header_pad_top + header_pad_bottom)
        header_row_h = int(max(header_row_h, header_text_h + 4))
        header_row_cap = int(max(1, math.floor(float(table_h) * 0.40)))
        if header_row_h > header_row_cap:
            header_row_h = int(header_row_cap)
        if table_h > 1:
            header_row_h = int(max(1, min(header_row_h, table_h - 1)))
        else:
            header_row_h = 1
        row_sep = int(table_top + header_row_h)
        if row_sep >= table_bottom:
            row_sep = int(table_bottom - 1)
        if row_sep < table_top:
            row_sep = int(table_top)
        value_row_h = int(max(1, table_bottom - row_sep))

        header_text_top = int(table_top)
        header_text_bottom = int(row_sep - 1)
        if header_text_bottom < header_text_top:
            header_text_bottom = int(header_text_top)

        value_text_top = int(row_sep + 1)
        if value_text_top > table_bottom:
            value_text_top = int(table_bottom)
        value_text_bottom = int(table_bottom)

        value_pad_top = cell_pad_y
        value_pad_bottom = cell_pad_y
        value_fit_h = int(max(1, value_row_h - (value_pad_top + value_pad_bottom)))

        probe_values = ["9999", str(sg), str(sr), str(smax), str(fg), str(fr), str(fmax)]
        max_font = int(max(10, min(120, value_fit_h)))
        min_font = 10
        font_val = _load_font(min_font)
        for sz in range(max_font, min_font - 1, -1):
            fnt = _load_font(sz)
            if fnt is None:
                continue
            ok = True
            for txt in probe_values:
                tw, th = _text_wh(txt, fnt)
                if tw > fit_w or th > value_fit_h:
                    ok = False
                    break
            if ok:
                font_val = fnt
                break

        bg_r, bg_g, bg_b, bg_a = COL_HUD_BG
        grid_col = (
            int(min(255, bg_r + 20)),
            int(min(255, bg_g + 20)),
            int(min(255, bg_b + 20)),
            int(min(255, max(int(bg_a), 150))),
        )

        def _draw_table_grid(table_x: int) -> tuple[list[int], list[int]]:
            table_left = int(table_x)
            table_right = int(table_x + table_w - 1)
            c1 = int(table_x + round(float(table_w) / 3.0))
            c2 = int(table_x + round((2.0 * float(table_w)) / 3.0))

            x_lines = [table_left, c1, c2, table_right]
            dedup_x: list[int] = []
            for x in x_lines:
                if not dedup_x or x != dedup_x[-1]:
                    dedup_x.append(int(x))
            for x in dedup_x:
                dr.line([(int(x), int(table_top)), (int(x), int(table_bottom))], fill=grid_col, width=1)

            y_lines = [table_top, row_sep, table_bottom]
            dedup_y: list[int] = []
            for y in y_lines:
                if not dedup_y or y != dedup_y[-1]:
                    dedup_y.append(int(y))
            for y in dedup_y:
                dr.line([(int(table_left), int(y)), (int(table_right), int(y))], fill=grid_col, width=1)
            return dedup_x, dedup_y

        def _draw_table(table_x: int, vals: tuple[str, str, str], col: Any) -> None:
            x_lines, _ = _draw_table_grid(table_x)
            if len(x_lines) < 4:
                return
            cell_x_ranges = [
                (x_lines[0], x_lines[1]),
                (x_lines[1], x_lines[2]),
                (x_lines[2], x_lines[3]),
            ]

            header_cell_pad_y = int(max(1, cell_pad_y - 1))
            for c, lbl in enumerate(header_labels):
                x_l, x_r = cell_x_ranges[c]
                _draw_centered_text(
                    x_l,
                    header_text_top,
                    x_r,
                    header_text_bottom,
                    lbl,
                    font_title,
                    col,
                    pad_y=header_cell_pad_y,
                )

            for c, txt in enumerate(vals):
                x_l, x_r = cell_x_ranges[c]
                _draw_centered_text(
                    x_l,
                    value_text_top,
                    x_r,
                    value_text_bottom,
                    txt,
                    font_val,
                    col,
                    pad_y=cell_pad_y,
                )

        _draw_table(left_x, slow_values, col_slow_darkred)
        _draw_table(right_x, fast_values, col_fast_darkblue)

    except Exception:
        xL = int(x0 + 6)
        xR = int(x0 + (w // 2) + 6)
        y1 = int(y0 + 6)
        y2 = int(y0 + 26)
        dr.text((xL, y1), "Gear | RPM | Max. RPM", fill=col_slow_darkred)
        dr.text((xR, y1), "Gear | RPM | Max. RPM", fill=col_fast_darkblue)
        dr.text((xL, y2), f"{sg} | {sr} | {smax}", fill=col_slow_darkred)
        dr.text((xR, y2), f"{fg} | {fr} | {fmax}", fill=col_fast_darkblue)
