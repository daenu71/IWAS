from __future__ import annotations

import math
import os
from typing import Any

from huds.common import COL_HUD_BG, draw_hud_background


def build_confirmed_max_speed_display(
    speed_frames_u: list[float] | None,
    threshold: float = 5.0,
) -> list[float | None]:
    _ = threshold
    if not speed_frames_u:
        return []

    debug_raw = (os.environ.get("IRVC_DEBUG_SPEED_MAX") or "").strip().lower()
    debug_enabled = debug_raw not in ("", "0", "false", "off", "no")

    out: list[float | None] = []
    prev_logged_value: float | None = None
    for i, v in enumerate(speed_frames_u):
        try:
            fv = float(v)
        except Exception:
            fv = 0.0
        if not math.isfinite(fv):
            fv = 0.0
        current_u = float(fv)
        out.append(current_u)

        if debug_enabled and (prev_logged_value is None or current_u != prev_logged_value):
            print(
                "[speed-max] i="
                + str(i)
                + " raw="
                + str(v)
                + " converted_u="
                + str(current_u)
                + " displayed_u="
                + str(current_u)
            )
            prev_logged_value = current_u

    return out


def render_speed(ctx: dict[str, Any], box: tuple[int, int, int, int], dr: Any) -> None:
    x0, y0, w, h = box
    _ = w

    hud_key = ctx["hud_key"]
    i = int(ctx["i"])
    fi = int(ctx["fi"])
    slow_speed_u = ctx["slow_speed_u"]
    fast_speed_u = ctx["fast_speed_u"]
    slow_min_u = ctx["slow_min_u"]
    fast_min_u = ctx["fast_min_u"]
    slow_max_u = ctx.get("slow_max_u")
    fast_max_u = ctx.get("fast_max_u")
    _ = float(ctx.get("speed_axis_min_u", 0.0))
    _ = float(ctx.get("speed_axis_max_u", 1.0))
    _ = ctx.get("unit_label", "")
    col_slow_darkred = ctx["COL_SLOW_DARKRED"]
    col_fast_darkblue = ctx["COL_FAST_DARKBLUE"]

    if hud_key != "Speed":
        return

    draw_hud_background(dr, box, col_bg=COL_HUD_BG)

    if not (slow_speed_u and i < len(slow_speed_u) and fast_speed_u and fi < len(fast_speed_u)):
        return

    def _safe_val(arr: Any, idx: int) -> float | None:
        if not arr or idx < 0 or idx >= len(arr):
            return None
        try:
            v = float(arr[idx])
        except Exception:
            return None
        if not math.isfinite(v):
            return None
        return float(v)

    sv0 = _safe_val(slow_speed_u, i)
    fv0 = _safe_val(fast_speed_u, fi)
    if sv0 is None or fv0 is None:
        return

    sv = int(round(float(sv0)))
    fv = int(round(float(fv0)))

    smin = sv
    fmin = fv
    smin0 = _safe_val(slow_min_u, i)
    fmin0 = _safe_val(fast_min_u, fi)
    if smin0 is not None:
        smin = int(round(float(smin0)))
    if fmin0 is not None:
        fmin = int(round(float(fmin0)))

    smax0 = _safe_val(slow_max_u, i)
    fmax0 = _safe_val(fast_max_u, fi)
    smax_txt = "na" if smax0 is None else str(int(round(float(smax0))))
    fmax_txt = "na" if fmax0 is None else str(int(round(float(fmax0))))

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
                tw = int(bb[2] - bb[0])
                th = int(bb[3] - bb[1])
                bx0 = float(bb[0])
                by0 = float(bb[1])
            except Exception:
                tw, th = _text_wh(txt, font_obj)
                bx0 = 0.0
                by0 = 0.0

            inner_top = float(y0_cell + max(0, pad_y))
            inner_bottom = float(y1_cell - max(0, pad_y))
            if inner_bottom < inner_top:
                cy = (float(y0_cell) + float(y1_cell)) / 2.0
                inner_top = cy
                inner_bottom = cy

            tx = ((float(x0_cell) + float(x1_cell)) - float(tw)) / 2.0 - bx0
            ty = (inner_top + inner_bottom - float(th)) / 2.0 - by0
            dr.text((int(round(tx)), int(round(ty))), txt, fill=col, font=font_obj)

        header_labels = ("Speed", "Min. Speed", "Max. Speed")
        slow_values = (str(sv), str(smin), str(smax_txt))
        fast_values = (str(fv), str(fmin), str(fmax_txt))

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
        cell_pad_y = int(max(1, min(4, round(float(h) * 0.01))))
        col_w = float(table_w) / 3.0
        cell_pad_x = int(max(2, min(8, round(col_w * 0.08))))
        fit_w = int(max(8, round(col_w) - (2 * cell_pad_x)))

        header_pad_top = 1
        header_pad_bottom = 1
        header_row_cap = int(max(1, math.floor(float(table_h) * 0.35)))
        header_fit_h = int(max(8, int(round(float(table_h) * 0.5)) - (2 * cell_pad_y)))
        header_hard_fit_h = int(max(1, header_row_cap - (header_pad_top + header_pad_bottom)))

        max_header_font = int(max(9, min(72, header_fit_h)))
        min_header_font = 9
        font_title = _load_font(min_header_font)
        for sz in range(max_header_font, min_header_font - 1, -1):
            fnt = _load_font(sz)
            if fnt is None:
                continue
            ok = True
            for lbl in header_labels:
                tw, th = _text_wh(lbl, fnt)
                if tw > fit_w or th > header_fit_h:
                    ok = False
                    break
            if ok:
                font_title = fnt
                break

        header_text_h = _text_wh("Max. Speed", font_title)[1]
        if header_text_h > header_hard_fit_h:
            for sz in range(max_header_font, min_header_font - 1, -1):
                fnt = _load_font(sz)
                if fnt is None:
                    continue
                ok = True
                for lbl in header_labels:
                    tw, th = _text_wh(lbl, fnt)
                    if tw > fit_w or th > header_hard_fit_h:
                        ok = False
                        break
                if ok:
                    font_title = fnt
                    header_text_h = _text_wh("Max. Speed", font_title)[1]
                    break
        header_row_h = int(header_text_h + header_pad_top + header_pad_bottom)
        if header_row_h > header_row_cap:
            header_row_h = int(header_row_cap)
        if table_h > 1:
            header_row_h = int(max(1, min(header_row_h, table_h - 1)))
        else:
            header_row_h = 1
        value_row_h = int(max(1, table_h - header_row_h))

        row_sep = int(table_top + header_row_h - 1)
        if row_sep > table_bottom:
            row_sep = int(table_bottom)

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

        probe_values = list(slow_values) + list(fast_values)
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
                    dedup_x.append(x)
            for x in dedup_x:
                dr.line([(int(x), int(table_top)), (int(x), int(table_bottom))], fill=grid_col, width=1)

            y_lines = [table_top, row_sep, table_bottom]
            dedup_y: list[int] = []
            for y in y_lines:
                if not dedup_y or y != dedup_y[-1]:
                    dedup_y.append(y)
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
                    pad_y=0,
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
        dr.text((xL, y1), "Speed | Min. Speed | Max. Speed", fill=col_slow_darkred)
        dr.text((xR, y1), "Speed | Min. Speed | Max. Speed", fill=col_fast_darkblue)
        dr.text((xL, y2), f"{sv} | {smin} | {smax_txt}", fill=col_slow_darkred)
        dr.text((xR, y2), f"{fv} | {fmin} | {fmax_txt}", fill=col_fast_darkblue)
