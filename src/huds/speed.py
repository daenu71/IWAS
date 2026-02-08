from __future__ import annotations

import math
from typing import Any

from huds.common import COL_HUD_BG, draw_hud_background


def build_confirmed_max_speed_display(
    speed_frames_u: list[float] | None,
    threshold: float = 5.0,
) -> list[float | None]:
    if not speed_frames_u:
        return []

    n = len(speed_frames_u)
    thr = float(threshold)
    if (not math.isfinite(thr)) or thr < 0.0:
        thr = 5.0

    vals: list[float] = []
    for v in speed_frames_u:
        try:
            fv = float(v)
        except Exception:
            fv = 0.0
        if not math.isfinite(fv):
            fv = 0.0
        vals.append(float(fv))

    out: list[float | None] = [None] * n
    confirmed_max_value: float | None = None

    candidate_peak_value = float(vals[0])
    candidate_peak_index = 0
    min_since_candidate_start = float(vals[0])
    candidate_has_past_dip = False

    for i, v in enumerate(vals):
        out[i] = confirmed_max_value

        if float(v) > float(candidate_peak_value):
            candidate_peak_value = float(v)
            candidate_peak_index = int(i)
            candidate_has_past_dip = bool(float(min_since_candidate_start) <= float(candidate_peak_value - thr))
        elif (
            i > candidate_peak_index
            and candidate_has_past_dip
            and float(v) <= float(candidate_peak_value - thr)
        ):
            confirmed_max_value = float(candidate_peak_value)
            for k in range(int(candidate_peak_index), i + 1):
                out[k] = confirmed_max_value

            candidate_peak_value = float(v)
            candidate_peak_index = int(i)
            min_since_candidate_start = float(v)
            candidate_has_past_dip = False
            continue

        if float(v) < float(min_since_candidate_start):
            min_since_candidate_start = float(v)

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

        def _draw_centered_text(xc: float, y_top: float, text: str, font_obj: Any, col: Any) -> None:
            tw, _th = _text_wh(str(text), font_obj)
            x_txt = int(round(float(xc) - (float(tw) / 2.0)))
            dr.text((x_txt, int(round(float(y_top)))), str(text), fill=col, font=font_obj)

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

        font_title = _load_font(18)
        header_h = 0
        for lbl in header_labels:
            _tw, th = _text_wh(lbl, font_title)
            if th > header_h:
                header_h = th
        if header_h <= 0:
            header_h = 18

        header_y0 = int(table_y0)
        header_y1 = int(header_y0 + header_h)
        row_gap = int(max(2, min(12, round(float(h) * 0.05))))
        value_y0 = int(header_y1 + row_gap)
        value_y1 = int(table_y1)
        value_h = int(value_y1 - value_y0)
        if value_h < 8:
            value_y0 = int(header_y1 + 2)
            value_y1 = int(y0 + h - 2)
            value_h = int(max(8, value_y1 - value_y0))

        col_w = float(table_w) / 3.0
        fit_w = int(max(8, round(col_w) - 6))
        fit_h = int(max(8, value_h - 2))

        probe_values = list(slow_values) + list(fast_values)
        max_font = int(max(10, min(120, fit_h)))
        min_font = 10
        font_val = _load_font(min_font)
        for sz in range(max_font, min_font - 1, -1):
            fnt = _load_font(sz)
            if fnt is None:
                continue
            ok = True
            for txt in probe_values:
                tw, th = _text_wh(txt, fnt)
                if tw > fit_w or th > fit_h:
                    ok = False
                    break
            if ok:
                font_val = fnt
                break

        def _draw_table(table_x: int, vals: tuple[str, str, str], col: Any) -> None:
            for c, lbl in enumerate(header_labels):
                cx = float(table_x) + (float(c) + 0.5) * col_w
                _tw_h, th_h = _text_wh(lbl, font_title)
                y_h = float(header_y0) + max(0.0, (float(header_h) - float(th_h)) / 2.0)
                _draw_centered_text(cx, y_h, lbl, font_title, col)

            _tw_v, th_v = _text_wh("9999", font_val)
            y_v = float(value_y0) + max(0.0, (float(value_h) - float(th_v)) / 2.0)
            for c, txt in enumerate(vals):
                cx = float(table_x) + (float(c) + 0.5) * col_w
                _draw_centered_text(cx, y_v, txt, font_val, col)

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
