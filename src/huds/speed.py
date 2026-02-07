from __future__ import annotations

from typing import Any


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
    unit_label = ctx["unit_label"]
    col_slow_darkred = ctx["COL_SLOW_DARKRED"]
    col_fast_darkblue = ctx["COL_FAST_DARKBLUE"]

    xL = int(x0 + 6)
    xR = int(x0 + (w // 2) + 6)
    y1 = int(y0 + 6)
    y2 = int(y0 + 26)

    if hud_key == "Speed":
        if slow_speed_u and i < len(slow_speed_u) and fast_speed_u and fi < len(fast_speed_u):
            sv = int(round(float(slow_speed_u[i])))
            fv = int(round(float(fast_speed_u[fi])))

            smin = sv
            fmin = fv
            if slow_min_u and i < len(slow_min_u):
                smin = int(round(float(slow_min_u[i])))
            if fast_min_u and fi < len(fast_min_u):
                fmin = int(round(float(fast_min_u[fi])))

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

                # Titel
                y_title = int(y0 + 6)
                dr.text((xL, y_title), f"Speed / Min ({unit_label})", fill=col_slow_darkred, font=font_title)
                dr.text((xR, y_title), f"Speed / Min ({unit_label})", fill=col_fast_darkblue, font=font_title)

                # Werte
                y_val = int(y0 + 30)
                dr.text((xL, y_val), f"{sv} / {smin}", fill=col_slow_darkred, font=font_val)
                dr.text((xR, y_val), f"{fv} / {fmin}", fill=col_fast_darkblue, font=font_val)

            except Exception:
                # Fallback ohne Fonts
                dr.text((xL, y1), f"Speed / Min ({unit_label})", fill=col_slow_darkred)
                dr.text((xR, y1), f"Speed / Min ({unit_label})", fill=col_fast_darkblue)
                dr.text((xL, y2), f"{sv} / {smin}", fill=col_slow_darkred)
                dr.text((xR, y2), f"{fv} / {fmin}", fill=col_fast_darkblue)
