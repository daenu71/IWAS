from __future__ import annotations

from typing import Any


def render_gear_rpm(ctx: dict[str, Any], box: tuple[int, int, int, int], dr: Any) -> None:
    x0, y0, w, h = box
    _ = h

    hud_key = ctx["hud_key"]
    i = int(ctx["i"])
    fi = int(ctx["fi"])
    slow_gear_h = ctx["slow_gear_h"]
    fast_gear_h = ctx["fast_gear_h"]
    slow_rpm_h = ctx["slow_rpm_h"]
    fast_rpm_h = ctx["fast_rpm_h"]
    col_slow_darkred = ctx["COL_SLOW_DARKRED"]
    col_fast_darkblue = ctx["COL_FAST_DARKBLUE"]

    xL = int(x0 + 6)
    xR = int(x0 + (w // 2) + 6)
    y1 = int(y0 + 6)
    y2 = int(y0 + 26)

    if hud_key == "Gear & RPM":
        if slow_gear_h and i < len(slow_gear_h) and fast_gear_h and fi < len(fast_gear_h):
            sg = int(slow_gear_h[i])
            fg = int(fast_gear_h[fi])

            sr = 0
            fr = 0
            if slow_rpm_h and i < len(slow_rpm_h):
                sr = int(slow_rpm_h[i])
            if fast_rpm_h and fi < len(fast_rpm_h):
                fr = int(fast_rpm_h[fi])

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
                dr.text((xL, y_title), "Gear / RPM", fill=col_slow_darkred, font=font_title)
                dr.text((xR, y_title), "Gear / RPM", fill=col_fast_darkblue, font=font_title)

                # Werte
                y_val = int(y0 + 30)
                dr.text((xL, y_val), f"{sg} / {sr} rpm", fill=col_slow_darkred, font=font_val)
                dr.text((xR, y_val), f"{fg} / {fr} rpm", fill=col_fast_darkblue, font=font_val)

            except Exception:
                # Fallback ohne Fonts
                dr.text((xL, y1), "Gear / RPM", fill=col_slow_darkred)
                dr.text((xR, y1), "Gear / RPM", fill=col_fast_darkblue)
                dr.text((xL, y2), f"{sg} / {sr} rpm", fill=col_slow_darkred)
                dr.text((xR, y2), f"{fg} / {fr} rpm", fill=col_fast_darkblue)
