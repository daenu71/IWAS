from __future__ import annotations

from typing import Any

from huds.common import (
    COL_HUD_BG,
    build_value_boundaries,
    choose_tick_step,
    draw_hud_background,
    draw_stripe_grid,
    value_boundaries_to_y,
)


def render_speed(ctx: dict[str, Any], box: tuple[int, int, int, int], dr: Any) -> None:
    x0, y0, w, h = box
    _ = w, h

    hud_key = ctx["hud_key"]
    i = int(ctx["i"])
    fi = int(ctx["fi"])
    slow_speed_u = ctx["slow_speed_u"]
    fast_speed_u = ctx["fast_speed_u"]
    slow_min_u = ctx["slow_min_u"]
    fast_min_u = ctx["fast_min_u"]
    speed_axis_min_u = float(ctx.get("speed_axis_min_u", 0.0))
    speed_axis_max_u = float(ctx.get("speed_axis_max_u", 1.0))
    unit_label = ctx["unit_label"]
    col_slow_darkred = ctx["COL_SLOW_DARKRED"]
    col_fast_darkblue = ctx["COL_FAST_DARKBLUE"]

    if hud_key != "Speed":
        return

    draw_hud_background(dr, box, col_bg=COL_HUD_BG)

    xL = int(x0 + 6)
    xR = int(x0 + (w // 2) + 6)
    y1 = int(y0 + 6)
    y2 = int(y0 + 26)

    if not (slow_speed_u and i < len(slow_speed_u) and fast_speed_u and fi < len(fast_speed_u)):
        return

    sv = int(round(float(slow_speed_u[i])))
    fv = int(round(float(fast_speed_u[fi])))

    smin = sv
    fmin = fv
    if slow_min_u and i < len(slow_min_u):
        smin = int(round(float(slow_min_u[i])))
    if fast_min_u and fi < len(fast_min_u):
        fmin = int(round(float(fast_min_u[fi])))

    # Story 10: Speed-Achse (Einheit wie HUD-Config, ohne Einheitstext bei Ticks).
    v_min = float(speed_axis_min_u)
    v_max = float(speed_axis_max_u)
    if v_max <= v_min:
        v_max = v_min + 1.0

    def _y_from_speed(v: float) -> int:
        vv = float(v)
        if vv < v_min:
            vv = v_min
        if vv > v_max:
            vv = v_max
        den = max(1e-9, (v_max - v_min))
        frac = (vv - v_min) / den
        yy = float(y0 + h - 1) - (frac * float(max(1, h - 1)))
        return int(round(yy))

    try:
        step = choose_tick_step(v_min, v_max, min_segments=2, max_segments=5, target_segments=5)
        if step is not None:
            val_bounds = build_value_boundaries(v_min, v_max, float(step), anchor="bottom")
            y_bounds = value_boundaries_to_y(val_bounds, _y_from_speed, int(y0), int(y0 + h - 1))
            draw_stripe_grid(
                dr,
                int(x0),
                int(w),
                int(y0),
                int(y0 + h - 1),
                y_bounds,
                col_bg=COL_HUD_BG,
                darken_delta=6,
            )
    except Exception:
        pass

    # Fonts (ähnlich wie Throttle / Brake)
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
                try:
                    return ImageFont.truetype("DejaVuSans.ttf", sz)
                except Exception:
                    return None

        font_title = _load_font(18)
        font_val = _load_font(22)
        font_axis = _load_font(12)
        font_axis_small = _load_font(11)

        y_title = int(y0 + 6)
        dr.text((xL, y_title), f"Speed / Min ({unit_label})", fill=col_slow_darkred, font=font_title)
        dr.text((xR, y_title), f"Speed / Min ({unit_label})", fill=col_fast_darkblue, font=font_title)

        y_val = int(y0 + 30)
        dr.text((xL, y_val), f"{sv} / {smin}", fill=col_slow_darkred, font=font_val)
        dr.text((xR, y_val), f"{fv} / {fmin}", fill=col_fast_darkblue, font=font_val)

    except Exception:
        # Fallback ohne Fonts
        dr.text((xL, y1), f"Speed / Min ({unit_label})", fill=col_slow_darkred)
        dr.text((xR, y1), f"Speed / Min ({unit_label})", fill=col_fast_darkblue)
        dr.text((xL, y2), f"{sv} / {smin}", fill=col_slow_darkred)
        dr.text((xR, y2), f"{fv} / {fmin}", fill=col_fast_darkblue)
