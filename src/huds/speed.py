from __future__ import annotations

import math
import os
from typing import Any

from huds.common import COL_HUD_BG, draw_hud_background


def build_confirmed_max_speed_display(
    speed_frames_u: list[float] | None,
    threshold: float = 5.0,
) -> list[float | None]:
    if not speed_frames_u:
        return []

    thr = float(threshold)
    if (not math.isfinite(thr)) or thr < 0.0:
        thr = 5.0

    debug_raw = (os.environ.get("IRVC_DEBUG_SPEED_MAX") or "").strip().lower()
    debug_enabled = debug_raw not in ("", "0", "false", "off", "no")

    vals: list[float] = []
    for v in speed_frames_u:
        try:
            fv = float(v)
        except Exception:
            fv = 0.0
        if not math.isfinite(fv):
            fv = 0.0
        vals.append(float(fv))

    out: list[float | None] = [None] * len(vals)
    confirmed_max_value: float | None = None

    valley_value = float(vals[0])
    candidate_peak_value = float(vals[0])
    candidate_peak_index = 0
    rise_confirmed = False

    for i, current_u in enumerate(vals):
        if float(current_u) <= float(valley_value):
            valley_value = float(current_u)
            candidate_peak_value = float(current_u)
            candidate_peak_index = int(i)
            rise_confirmed = False
        else:
            if float(current_u) > float(candidate_peak_value):
                candidate_peak_value = float(current_u)
                candidate_peak_index = int(i)
            if float(candidate_peak_value - valley_value) >= float(thr):
                rise_confirmed = True
            if (
                rise_confirmed
                and i > candidate_peak_index
                and float(current_u) <= float(candidate_peak_value - thr)
            ):
                confirmed_max_value = float(candidate_peak_value)
                if debug_enabled:
                    print(
                        "[speed-max] i="
                        + str(i)
                        + " raw="
                        + str(speed_frames_u[i])
                        + " converted_u="
                        + str(float(current_u))
                        + " displayed_u="
                        + str(float(confirmed_max_value))
                    )
                valley_value = float(current_u)
                candidate_peak_value = float(current_u)
                candidate_peak_index = int(i)
                rise_confirmed = False

        out[i] = confirmed_max_value

    return out


def _load_table_font(sz: int) -> Any:
    try:
        from PIL import ImageFont
    except Exception:
        ImageFont = None  # type: ignore
    if ImageFont is None:
        return None
    try:
        return ImageFont.truetype("arial.ttf", int(sz))
    except Exception:
        pass
    try:
        return ImageFont.truetype("DejaVuSans.ttf", int(sz))
    except Exception:
        pass
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _text_wh(dr: Any, text: str, font_obj: Any) -> tuple[int, int]:
    try:
        bb = dr.textbbox((0, 0), str(text), font=font_obj)
        return int(bb[2] - bb[0]), int(bb[3] - bb[1])
    except Exception:
        return int(max(1, len(str(text))) * 7), 12


def _draw_centered_text(
    dr: Any,
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
        tw, th = _text_wh(dr, txt, font_obj)
        bx0 = 0.0
        by0 = 0.0

    inner_top = float(y0_cell + max(0, int(pad_y)))
    inner_bottom = float(y1_cell - max(0, int(pad_y)))
    if inner_bottom < inner_top:
        cy = (float(y0_cell) + float(y1_cell)) / 2.0
        inner_top = cy
        inner_bottom = cy

    tx = ((float(x0_cell) + float(x1_cell)) - float(tw)) / 2.0 - bx0
    ty = (inner_top + inner_bottom - float(th)) / 2.0 - by0
    dr.text((int(round(tx)), int(round(ty))), txt, fill=col, font=font_obj)


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


def extract_speed_table_values(ctx: dict[str, Any]) -> tuple[tuple[str, str, str], tuple[str, str, str]] | None:
    i = int(ctx["i"])
    fi = int(ctx["fi"])
    slow_speed_u = ctx["slow_speed_u"]
    fast_speed_u = ctx["fast_speed_u"]
    slow_min_u = ctx["slow_min_u"]
    fast_min_u = ctx["fast_min_u"]
    slow_max_u = ctx.get("slow_max_u")
    fast_max_u = ctx.get("fast_max_u")

    if not (slow_speed_u and i < len(slow_speed_u) and fast_speed_u and fi < len(fast_speed_u)):
        return None

    sv0 = _safe_val(slow_speed_u, i)
    fv0 = _safe_val(fast_speed_u, fi)
    if sv0 is None or fv0 is None:
        return None

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

    slow_values = (str(sv), str(smin), str(smax_txt))
    fast_values = (str(fv), str(fmin), str(fmax_txt))
    return slow_values, fast_values


def build_speed_table_state(
    box: tuple[int, int],
    probe_values: tuple[tuple[str, str, str], tuple[str, str, str]] | None = None,
) -> dict[str, Any]:
    w, h = box
    from PIL import Image, ImageDraw

    w_i = int(max(1, int(w)))
    h_i = int(max(1, int(h)))
    probe_img = Image.new("RGBA", (w_i, h_i), (0, 0, 0, 0))
    probe_dr = ImageDraw.Draw(probe_img)

    header_labels = ("Speed", "Min. Speed", "Max. Speed")

    outer_pad_x = int(max(4, min(10, round(float(w_i) * 0.02))))
    outer_pad_y = int(max(4, min(10, round(float(h_i) * 0.08))))
    table_gap = int(max(4, min(16, round(float(w_i) * 0.03))))
    avail_tables_w = int(w_i - (2 * outer_pad_x) - table_gap)
    if avail_tables_w < 12:
        avail_tables_w = max(12, int(w_i - 2))
        table_gap = 0
        outer_pad_x = 1
    table_w = int(max(6, avail_tables_w // 2))
    left_x = int(outer_pad_x)
    right_x = int(left_x + table_w + table_gap)
    table_y0 = int(outer_pad_y)
    table_y1 = int(h_i - outer_pad_y)
    if table_y1 <= table_y0:
        table_y0 = 0
        table_y1 = int(h_i)

    table_top = int(table_y0)
    table_bottom = int(table_y1 - 1)
    if table_bottom <= table_top:
        table_bottom = int(table_top + 1)

    table_h = int(max(2, table_bottom - table_top + 1))
    cell_pad_y = int(max(1, min(4, round(float(h_i) * 0.01))))
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
    font_title = _load_table_font(min_header_font)
    for sz in range(max_header_font, min_header_font - 1, -1):
        fnt = _load_table_font(sz)
        if fnt is None:
            continue
        ok = True
        for lbl in header_labels:
            tw, th = _text_wh(probe_dr, lbl, fnt)
            if tw > fit_w or th > header_fit_h:
                ok = False
                break
        if ok:
            font_title = fnt
            break

    header_text_h = _text_wh(probe_dr, "Max. Speed", font_title)[1]
    if header_text_h > header_hard_fit_h:
        for sz in range(max_header_font, min_header_font - 1, -1):
            fnt = _load_table_font(sz)
            if fnt is None:
                continue
            ok = True
            for lbl in header_labels:
                tw, th = _text_wh(probe_dr, lbl, fnt)
                if tw > fit_w or th > header_hard_fit_h:
                    ok = False
                    break
            if ok:
                font_title = fnt
                header_text_h = _text_wh(probe_dr, "Max. Speed", font_title)[1]
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

    probe_values_for_fit: list[str] = []
    if probe_values is not None:
        try:
            probe_values_for_fit = [str(x) for x in list(probe_values[0]) + list(probe_values[1])]
        except Exception:
            probe_values_for_fit = []
    if not probe_values_for_fit:
        probe_values_for_fit = ["9999", "9999", "9999", "9999", "9999", "9999"]
    max_font = int(max(10, min(120, value_fit_h)))
    min_font = 10
    font_val = _load_table_font(min_font)
    for sz in range(max_font, min_font - 1, -1):
        fnt = _load_table_font(sz)
        if fnt is None:
            continue
        ok = True
        for txt in probe_values_for_fit:
            tw, th = _text_wh(probe_dr, txt, fnt)
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

    def _grid_x_lines(table_x: int) -> list[int]:
        table_left = int(table_x)
        table_right = int(table_x + table_w - 1)
        c1 = int(table_x + round(float(table_w) / 3.0))
        c2 = int(table_x + round((2.0 * float(table_w)) / 3.0))
        x_lines = [table_left, c1, c2, table_right]
        dedup_x: list[int] = []
        for xv in x_lines:
            if not dedup_x or int(xv) != int(dedup_x[-1]):
                dedup_x.append(int(xv))
        return dedup_x

    left_x_lines = _grid_x_lines(int(left_x))
    right_x_lines = _grid_x_lines(int(right_x))
    y_lines = [int(table_top), int(row_sep), int(table_bottom)]
    dedup_y: list[int] = []
    for yv in y_lines:
        if not dedup_y or int(yv) != int(dedup_y[-1]):
            dedup_y.append(int(yv))

    left_cells: list[tuple[int, int]] = []
    if len(left_x_lines) >= 4:
        left_cells = [
            (int(left_x_lines[0]), int(left_x_lines[1])),
            (int(left_x_lines[1]), int(left_x_lines[2])),
            (int(left_x_lines[2]), int(left_x_lines[3])),
        ]

    right_cells: list[tuple[int, int]] = []
    if len(right_x_lines) >= 4:
        right_cells = [
            (int(right_x_lines[0]), int(right_x_lines[1])),
            (int(right_x_lines[1]), int(right_x_lines[2])),
            (int(right_x_lines[2]), int(right_x_lines[3])),
        ]

    return {
        "box_w": int(w_i),
        "box_h": int(h_i),
        "header_labels": tuple(header_labels),
        "table_w": int(table_w),
        "left_x": int(left_x),
        "right_x": int(right_x),
        "table_top": int(table_top),
        "table_bottom": int(table_bottom),
        "row_sep": int(row_sep),
        "header_text_top": int(header_text_top),
        "header_text_bottom": int(header_text_bottom),
        "value_text_top": int(value_text_top),
        "value_text_bottom": int(value_text_bottom),
        "cell_pad_y": int(cell_pad_y),
        "font_title": font_title,
        "font_val": font_val,
        "grid_col": grid_col,
        "left_x_lines": left_x_lines,
        "right_x_lines": right_x_lines,
        "y_lines": dedup_y,
        "left_cells": left_cells,
        "right_cells": right_cells,
        "value_cells": {
            "slow": left_cells,
            "fast": right_cells,
        },
        "dynamic_bbox": (
            min([c[0] for c in (left_cells + right_cells)] or [0]),
            int(value_text_top),
            max([c[1] for c in (left_cells + right_cells)] or [0]),
            int(value_text_bottom),
        ),
    }


def render_speed_table_static(state: dict[str, Any], col_slow_darkred: Any, col_fast_darkblue: Any) -> Any:
    from PIL import Image, ImageDraw

    w = int(state["box_w"])
    h = int(state["box_h"])
    img = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)

    dr.rectangle([0, 0, int(w) - 1, int(h) - 1], fill=COL_HUD_BG)

    table_top = int(state["table_top"])
    table_bottom = int(state["table_bottom"])
    y_lines = list(state["y_lines"])
    grid_col = state["grid_col"]

    def _draw_grid(x_lines: list[int]) -> None:
        if not x_lines:
            return
        table_left = int(x_lines[0])
        table_right = int(x_lines[-1])
        for xv in x_lines:
            dr.line([(int(xv), int(table_top)), (int(xv), int(table_bottom))], fill=grid_col, width=1)
        for yv in y_lines:
            dr.line([(int(table_left), int(yv)), (int(table_right), int(yv))], fill=grid_col, width=1)

    _draw_grid(list(state["left_x_lines"]))
    _draw_grid(list(state["right_x_lines"]))

    header_labels = tuple(state["header_labels"])
    header_text_top = int(state["header_text_top"])
    header_text_bottom = int(state["header_text_bottom"])
    font_title = state["font_title"]

    left_cells = list(state["left_cells"])
    right_cells = list(state["right_cells"])
    for c_idx, lbl in enumerate(header_labels):
        if c_idx < len(left_cells):
            x_l, x_r = left_cells[c_idx]
            _draw_centered_text(
                dr,
                int(x_l),
                int(header_text_top),
                int(x_r),
                int(header_text_bottom),
                str(lbl),
                font_title,
                col_slow_darkred,
                pad_y=0,
            )
        if c_idx < len(right_cells):
            x_l, x_r = right_cells[c_idx]
            _draw_centered_text(
                dr,
                int(x_l),
                int(header_text_top),
                int(x_r),
                int(header_text_bottom),
                str(lbl),
                font_title,
                col_fast_darkblue,
                pad_y=0,
            )

    return img


def render_speed_table_dynamic(
    state: dict[str, Any],
    slow_values: tuple[str, str, str],
    fast_values: tuple[str, str, str],
    col_slow_darkred: Any,
    col_fast_darkblue: Any,
) -> Any:
    from PIL import Image, ImageDraw

    w = int(state["box_w"])
    h = int(state["box_h"])
    img = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)

    value_text_top = int(state["value_text_top"])
    value_text_bottom = int(state["value_text_bottom"])
    cell_pad_y = int(state["cell_pad_y"])
    font_val = state["font_val"]

    left_cells = list(state["left_cells"])
    right_cells = list(state["right_cells"])

    for c_idx, txt in enumerate(tuple(slow_values)):
        if c_idx >= len(left_cells):
            break
        x_l, x_r = left_cells[c_idx]
        _draw_centered_text(
            dr,
            int(x_l),
            int(value_text_top),
            int(x_r),
            int(value_text_bottom),
            str(txt),
            font_val,
            col_slow_darkred,
            pad_y=int(cell_pad_y),
        )

    for c_idx, txt in enumerate(tuple(fast_values)):
        if c_idx >= len(right_cells):
            break
        x_l, x_r = right_cells[c_idx]
        _draw_centered_text(
            dr,
            int(x_l),
            int(value_text_top),
            int(x_r),
            int(value_text_bottom),
            str(txt),
            font_val,
            col_fast_darkblue,
            pad_y=int(cell_pad_y),
        )

    return img


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
