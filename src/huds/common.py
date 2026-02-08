from __future__ import annotations

import math
from typing import Any, Callable

# Shared HUD colors (RGBA). Keep exact values to preserve output.
COL_SLOW_DARKRED = (234, 0, 0, 255)
COL_SLOW_BRIGHTRED = (255, 137, 117, 255)
COL_FAST_DARKBLUE = (36, 0, 250, 255)
COL_FAST_BRIGHTBLUE = (1, 253, 255, 255)
COL_WHITE = (255, 255, 255, 255)
COL_HUD_BG = (18, 18, 18, 96)

ALLOWED_TICK_STEPS: tuple[float, ...] = (
    100.0,
    50.0,
    20.0,
    10.0,
    5.0,
    2.0,
    1.0,
    0.5,
    0.2,
    0.1,
    0.05,
    0.02,
    0.01,
)

# HUD-name groups used by the orchestrator.
SCROLL_HUD_NAMES: set[str] = {
    "Throttle / Brake",
    "Steering",
    "Delta",
    "Line Delta",
    "Under-/Oversteer",
}

TABLE_HUD_NAMES: set[str] = {
    "Speed",
    "Gear & RPM",
}


def _coerce_rgba(col: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    r, g, b, a = col
    return (
        int(max(0, min(255, int(r)))),
        int(max(0, min(255, int(g)))),
        int(max(0, min(255, int(b)))),
        int(max(0, min(255, int(a)))),
    )


def _darken_rgba(
    col: tuple[int, int, int, int],
    delta: int = 8,
    min_alpha: int = 0,
) -> tuple[int, int, int, int]:
    r, g, b, a = _coerce_rgba(col)
    # Keep stripes subtle but visible in encoded video output.
    d = int(max(2, round(float(delta) * 2.0)))
    a_out = max(int(a), int(min_alpha))
    if a_out > 255:
        a_out = 255
    return (max(0, r - d), max(0, g - d), max(0, b - d), int(a_out))


def draw_hud_background(
    dr: Any,
    box: tuple[int, int, int, int],
    col_bg: tuple[int, int, int, int] = COL_HUD_BG,
) -> None:
    x0, y0, w, h = box
    if int(w) <= 0 or int(h) <= 0:
        return
    x1 = int(x0 + w - 1)
    y1 = int(y0 + h - 1)
    try:
        dr.rectangle([int(x0), int(y0), x1, y1], fill=_coerce_rgba(col_bg))
    except Exception:
        pass


def choose_tick_step(
    y_min: float,
    y_max: float,
    min_segments: int = 2,
    max_segments: int = 5,
    target_segments: int | None = None,
) -> float | None:
    lo = float(min(y_min, y_max))
    hi = float(max(y_min, y_max))
    span = hi - lo
    if (not math.isfinite(span)) or span <= 1e-9:
        return None

    valid: list[tuple[float, int]] = []
    for step in ALLOWED_TICK_STEPS:
        seg = int(math.ceil(span / step - 1e-9))
        if seg < int(min_segments) or seg > int(max_segments):
            continue
        valid.append((float(step), int(seg)))

    if not valid:
        # Fallback: keep nice steps and prefer the largest one that still
        # produces at least the minimum stripe count.
        for step in ALLOWED_TICK_STEPS:
            seg = int(math.ceil(span / step - 1e-9))
            if seg >= int(min_segments):
                return float(step)
        # Very small spans cannot satisfy min_segments with allowed steps.
        # Return the smallest allowed step so callers can still render.
        return float(ALLOWED_TICK_STEPS[-1])

    if target_segments is not None:
        exact = [v for v in valid if int(v[1]) == int(target_segments)]
        if exact:
            return float(max(v[0] for v in exact))
        valid = sorted(valid, key=lambda v: (abs(int(v[1]) - int(target_segments)), -float(v[0])))
        return float(valid[0][0])

    return float(max(v[0] for v in valid))


def build_value_boundaries(
    y_min: float,
    y_max: float,
    step: float,
    anchor: str = "bottom",
) -> list[float]:
    lo = float(min(y_min, y_max))
    hi = float(max(y_min, y_max))
    s = float(step)
    if s <= 0.0 or hi <= lo:
        return [lo, hi]

    vals: list[float]
    eps = max(1e-9, s * 1e-6)
    if str(anchor).strip().lower() == "top":
        vals = [hi]
        v = math.floor((hi + eps) / s) * s
        while v > lo + eps:
            if v < hi - eps:
                vals.append(v)
            v -= s
        vals.append(lo)
        vals = sorted(vals)
    else:
        vals = [lo]
        v = math.ceil((lo - eps) / s) * s
        while v < hi - eps:
            if v > lo + eps:
                vals.append(v)
            v += s
        vals.append(hi)

    out: list[float] = []
    for v in sorted(vals):
        vv = float(round(float(v), 6))
        if (not out) or abs(vv - out[-1]) > 1e-6:
            out.append(vv)
    if len(out) < 2:
        return [lo, hi]
    return out


def draw_stripe_grid(
    dr: Any,
    x0: int,
    w: int,
    y_top: int,
    y_bottom: int,
    y_boundaries: list[int],
    col_bg: tuple[int, int, int, int] = COL_HUD_BG,
    darken_delta: int = 8,
) -> None:
    if int(w) <= 0 or int(y_bottom) <= int(y_top):
        return

    x1 = int(x0 + w - 1)
    y0i = int(y_top)
    y1i = int(y_bottom)

    ys: list[int] = [y0i]
    for y in y_boundaries:
        yi = int(y)
        if yi <= y0i or yi >= y1i:
            continue
        ys.append(yi)
    ys.append(y1i)
    ys = sorted(set(ys))
    if len(ys) < 2:
        return

    bg_rgba = _coerce_rgba(col_bg)
    # Note: HUD PNGs are flattened over black before ffmpeg overlay. Very dark,
    # low-alpha stripes can collapse visually; keep stripe fills visibly distinct.
    stripe_alpha = max(int(bg_rgba[3]), min(210, int(bg_rgba[3]) + 70))
    stripe_lift = max(6, min(16, int(darken_delta) + 6))
    lifted = (
        min(255, int(bg_rgba[0]) + stripe_lift),
        min(255, int(bg_rgba[1]) + stripe_lift),
        min(255, int(bg_rgba[2]) + stripe_lift),
    )
    col_dark = (*lifted, int(stripe_alpha))
    for i in range(len(ys) - 1):
        if (i % 2) == 0:
            continue
        y_a = int(ys[i])
        y_b = int(ys[i + 1]) - 1
        if y_b < y_a:
            continue
        try:
            dr.rectangle([int(x0), y_a, x1, y_b], fill=col_dark)
        except Exception:
            pass

    # Keep separators in the same lifted hue so they stay aligned with the
    # softened stripes even after the PNG is flattened over black.
    for y_line in ys[1:-1]:
        try:
            dr.line([(int(x0), int(y_line)), (x1, int(y_line))], fill=col_dark, width=1)
        except Exception:
            pass


def _text_size(dr: Any, text: str, font_obj: Any) -> tuple[int, int]:
    try:
        bb = dr.textbbox((0, 0), str(text), font=font_obj)
        return int(bb[2] - bb[0]), int(bb[3] - bb[1])
    except Exception:
        try:
            w = int(dr.textlength(str(text), font=font_obj))
            return w, 10
        except Exception:
            return int(len(str(text)) * 6), 10


def draw_left_axis_labels(
    dr: Any,
    x0: int,
    w: int,
    y_top: int,
    y_bottom: int,
    labels: list[tuple[int, str]],
    font_obj: Any,
    col_text: tuple[int, int, int, int] = COL_WHITE,
    x_pad: int = 4,
    fallback_font_obj: Any | None = None,
) -> None:
    if int(w) <= 0 or int(y_bottom) <= int(y_top):
        return
    if not labels:
        return

    x_min = int(x0 + max(4, int(x_pad)))
    x_max = int(x0 + w - 2)
    y_min = int(y_top + 2)
    y_max = int(y_bottom - 2)
    if y_max <= y_min:
        y_min = int(y_top)
        y_max = int(y_bottom)

    for y_px, txt in labels:
        text = str(txt)
        if text == "":
            continue
        use_font = font_obj
        tw, th = _text_size(dr, text, use_font)
        if fallback_font_obj is not None and tw > max(6, (x_max - x_min)):
            use_font = fallback_font_obj
            tw, th = _text_size(dr, text, use_font)

        x_txt = x_min
        if x_txt + tw > x_max:
            x_txt = max(x_min, x_max - tw)

        y_txt = int(round(float(y_px) - (float(th) / 2.0)))
        if y_txt < y_min:
            y_txt = y_min
        if y_txt + th > y_max:
            y_txt = max(y_min, y_max - th)

        try:
            dr.text((int(x_txt), int(y_txt)), text, fill=_coerce_rgba(col_text), font=use_font)
        except Exception:
            pass


def format_int_or_1dp(v: float) -> str:
    vf = float(v)
    if abs(vf - round(vf)) < 1e-6:
        return str(int(round(vf)))
    return f"{vf:.1f}"


def should_suppress_boundary_label(
    value: float,
    v_min: float,
    v_max: float,
    suppress_zero: bool = False,
) -> bool:
    lo = float(min(v_min, v_max))
    hi = float(max(v_min, v_max))
    tol = max(1e-6, abs(hi - lo) * 1e-6)
    vv = float(value)

    if abs(vv - lo) <= tol:
        return True
    if abs(vv - hi) <= tol:
        return True

    if suppress_zero and (lo - tol) <= 0.0 <= (hi + tol) and abs(vv) <= tol:
        return True

    return False


def format_value_for_step(value: float, step: float, min_decimals: int = 0, max_decimals: int = 4) -> str:
    s = abs(float(step))
    dec = 0
    while dec < int(max_decimals):
        scaled = s * (10 ** dec)
        if abs(scaled - round(scaled)) <= 1e-6:
            break
        dec += 1
    dec = max(int(min_decimals), min(int(max_decimals), int(dec)))

    txt = f"{float(value):.{dec}f}"
    if dec > int(min_decimals):
        txt = txt.rstrip("0").rstrip(".")
    if txt == "-0":
        txt = "0"
    return txt


def filter_axis_labels_by_position(
    labels: list[tuple[int, str]],
    y_top: int,
    y_bottom: int,
    zero_y: int | None = None,
    pad_px: int = 2,
) -> list[tuple[int, str]]:
    if not labels:
        return []
    y0 = int(min(y_top, y_bottom))
    y1 = int(max(y_top, y_bottom))
    out: list[tuple[int, str]] = []
    tol = max(1, int(pad_px))
    for y_px, txt in labels:
        yy = int(y_px)
        if abs(yy - y0) <= tol:
            continue
        if abs(yy - y1) <= tol:
            continue
        if zero_y is not None and abs(yy - int(zero_y)) <= tol:
            continue
        out.append((yy, str(txt)))
    return out


def value_boundaries_to_y(
    boundaries: list[float],
    y_from_value: Callable[[float], int],
    y_top: int,
    y_bottom: int,
) -> list[int]:
    ys: list[int] = []
    y0 = int(min(y_top, y_bottom))
    y1 = int(max(y_top, y_bottom))
    for v in boundaries:
        try:
            yy = int(y_from_value(float(v)))
        except Exception:
            continue
        if yy < y0:
            yy = y0
        if yy > y1:
            yy = y1
        ys.append(int(yy))
    return sorted(set(ys))
