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
    format_int_or_1dp,
    should_suppress_boundary_label,
    value_boundaries_to_y,
    _text_size,
)


def render_steering(ctx: dict[str, Any], box: tuple[int, int, int, int], dr: Any) -> None:
    x0, y0, w, h = box
    hud_key = ctx["hud_key"]
    i = int(ctx["i"])
    iL = int(ctx["iL"])
    iR = int(ctx["iR"])
    frame_window_mapping = ctx.get("frame_window_mapping")
    map_idxs_all = list(getattr(frame_window_mapping, "idxs", []) or [])
    map_offsets_all = list(getattr(frame_window_mapping, "offsets", []) or [])
    map_fast_idx_all = list(getattr(frame_window_mapping, "fast_idx", []) or [])
    slow_to_fast_frame = ctx["slow_to_fast_frame"]
    slow_steer_frames = ctx["slow_steer_frames"]
    fast_steer_frames = ctx["fast_steer_frames"]
    steer_slow_scale = float(ctx["steer_slow_scale"])
    steer_fast_scale = float(ctx["steer_fast_scale"])
    steer_abs_max = float(ctx["steer_abs_max"])
    hud_curve_points_default = int(ctx["hud_curve_points_default"])
    hud_curve_points_overrides = ctx["hud_curve_points_overrides"]
    hud_windows = ctx["hud_windows"]
    before_s_h = float(ctx["before_s_h"])
    after_s_h = float(ctx["after_s_h"])
    default_before_s = float(ctx["default_before_s"])
    default_after_s = float(ctx["default_after_s"])
    hud_dbg = bool(ctx["hud_dbg"])
    _clamp = ctx["_clamp"]
    _idx_to_x = ctx["_idx_to_x"]
    _log_print = ctx["_log_print"]
    _wrap_delta_05 = ctx["_wrap_delta_05"]
    slow_frame_to_lapdist = ctx["slow_frame_to_lapdist"]
    log_file = ctx["log_file"]
    COL_WHITE = ctx["COL_WHITE"]
    COL_SLOW_DARKRED = ctx["COL_SLOW_DARKRED"]
    COL_FAST_DARKBLUE = ctx["COL_FAST_DARKBLUE"]

    # Story 3: Steering-Linien zeichnen
    if hud_key == "Steering":
        try:
            title_pos: tuple[int, int] | None = None
            title_txt = "Steering wheel angle"
            fast_val_draw: tuple[int, int, str] | None = None
            slow_val_draw: tuple[int, int, str] | None = None
            axis_labels: list[tuple[int, str]] = []
            font_axis = None
            font_axis_small = None
            font_title = None
            font_val = None
            # --- Headroom nur oben (positiv) ---
            # Default: 1.20 (20% Headroom oben). Unten kein Headroom.
            try:
                headroom = float((os.environ.get("IRVC_STEER_HEADROOM") or "").strip() or "1.20")
            except Exception:
                headroom = 1.20
            if headroom < 1.00:
                headroom = 1.00
            if headroom > 2.00:
                headroom = 2.00
    
            mid_y = float(y0) + (float(h) / 2.0)
            amp_base = max(2.0, (float(h) / 2.0) - 2.0)
            amp_neg = amp_base
            amp_pos = amp_base / max(1.0, headroom)
    
            def _y_from_norm(sn: float) -> int:
                # sn in [-1..+1]
                if sn >= 0.0:
                    yy = mid_y - (sn * amp_pos)
                else:
                    yy = mid_y - (sn * amp_neg)
                return int(round(yy))

            # Story 10: 5 Segmente bevorzugt, Labels in bestehender Winkel-Logik.
            try:
                deg_scale = float(steer_abs_max) * 180.0 / math.pi
                if abs(deg_scale) < 1e-9:
                    deg_scale = 1.0

                def _y_from_deg(v_deg: float) -> int:
                    sn_v = float(v_deg) / deg_scale
                    if sn_v >= 0.0:
                        yy_v = mid_y - (sn_v * amp_pos)
                    else:
                        yy_v = mid_y - (sn_v * amp_neg)
                    return int(round(yy_v))

                y_top_i = int(max(int(data_area_top), int(y0 + 1)))
                y_bot_i = int(y0 + h - 1)
                if y_top_i >= y_bot_i:
                    y_top_i = max(int(y0 + 1), y_bot_i - 1)
                sn_top = (mid_y - float(y_top_i)) / max(1e-6, amp_pos)
                sn_bot = (mid_y - float(y_bot_i)) / max(1e-6, amp_neg)
                val_top = float(sn_top * deg_scale)
                val_bot = float(sn_bot * deg_scale)

                tick_ref_max = max(abs(float(val_bot)), abs(float(val_top)))
                step = choose_tick_step(0.0, tick_ref_max, min_segments=2, max_segments=5, target_segments=5)
                if step is not None:
                    val_bounds = build_value_boundaries(val_bot, val_top, float(step), anchor="top")
                    y_bounds = value_boundaries_to_y(val_bounds, _y_from_deg, y_top_i, y_bot_i)
                    draw_stripe_grid(
                        dr,
                        int(x0),
                        int(w),
                        y_top_i,
                        y_bot_i,
                        y_bounds,
                        col_bg=COL_HUD_BG,
                        darken_delta=6,
                    )
                    for vv in val_bounds:
                        if should_suppress_boundary_label(float(vv), val_bot, val_top, suppress_zero=True):
                            continue
                        axis_labels.append((int(_y_from_deg(float(vv))), format_int_or_1dp(float(vv))))
            except Exception:
                pass
     
            # 0-Lenkung Mittellinie (Linie spaeter zeichnen).
            y_mid = int(round(mid_y))

            text_pad_top = 12
            y_txt = int(y0 + text_pad_top)
            title_pos = (int(x0 + 4), y_txt)
            font_sz = int(round(max(10.0, min(18.0, float(h) * 0.13))))
            font_val_sz = int(round(max(11.0, min(20.0, float(h) * 0.15))))
            data_area_top = int(y_txt + 14)
            _text_w = lambda text, font_obj: int(len(str(text)) * 8)
            try:
                from PIL import ImageFont
            except Exception:
                ImageFont = None  # type: ignore

            try:
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

                font_title = _load_font(font_sz)
                font_val = _load_font(font_val_sz)
                font_axis = _load_font(max(8, int(font_sz - 2)))
                font_axis_small = _load_font(max(7, int(font_sz - 3)))

                def _safe_text_height(font_obj: Any, sample: str, fallback: float) -> int:
                    if font_obj is None:
                        return max(1, int(round(float(fallback))))
                    try:
                        _, height = _text_size(dr, sample, font_obj)
                        return max(1, int(height))
                    except Exception:
                        return max(1, int(round(float(fallback))))

                title_height = _safe_text_height(font_title, title_txt, font_sz)
                value_height = _safe_text_height(font_val, "+000°", font_val_sz)
                text_block_height = max(1, title_height, value_height)
                top_candidate = int(y_txt + text_block_height + 10)
                data_area_top = max(int(y0 + 8), int(min(int(y0 + h - 6), top_candidate)))

                def _text_w_local(text: str, font_obj: Any) -> int:
                    if font_obj is None:
                        return int(len(text) * 8)
                    try:
                        bb = dr.textbbox((0, 0), text, font=font_obj)
                        return int(bb[2] - bb[0])
                    except Exception:
                        try:
                            return int(dr.textlength(text, font=font_obj))
                        except Exception:
                            return int(len(text) * 8)

                _text_w = _text_w_local
            except Exception:
                pass

            # Wie dicht wir sampeln (nicht jeden Frame, damit es schnell bleibt)
            span_n = max(1, int(iR - iL))
    
            pts_target = int(hud_curve_points_default or 180)
            try:
                ovs = hud_curve_points_overrides if isinstance(hud_curve_points_overrides, dict) else None
                if ovs and hud_key in ovs:
                    pts_target = int(float(ovs.get(hud_key) or pts_target))
            except Exception:
                pass
    
            # Grenzen, damit es stabil bleibt
            if pts_target < 40:
                pts_target = 40
            if pts_target > 600:
                pts_target = 600
    
            # nicht mehr Punkte als Pixelbreite
            max_pts = max(40, min(int(w), int(pts_target)))
    
            stride = max(1, int(round(float(span_n) / float(max_pts))))
    
            # --- DEBUG: Welche CSV-Punkte werden fÃ¼r einen bestimmten Output-Frame benutzt? ---
            # Aktivieren per env:
            #   set RVA_HUD_STEER_DEBUG_FRAME=1440
            # Optional:
            #   set RVA_HUD_STEER_DEBUG_SAMPLES=12
            try:
                dbg_frame = int(os.environ.get("RVA_HUD_STEER_DEBUG_FRAME", "").strip() or "-1")
            except Exception:
                dbg_frame = -1
            try:
                dbg_n = int(os.environ.get("RVA_HUD_STEER_DEBUG_SAMPLES", "").strip() or "12")
            except Exception:
                dbg_n = 12
            if dbg_n < 4:
                dbg_n = 4
            if dbg_n > 60:
                dbg_n = 60
    
            if dbg_frame >= 0 and int(i) == int(dbg_frame):
                try:
                    ov_b = 0
                    ov_a = 0
                    try:
                        if isinstance(hud_windows, dict) and isinstance(hud_windows.get(hud_key), dict):
                            o = hud_windows.get(hud_key) or {}
                            if o.get("before_s") is not None:
                                ov_b = 1
                            if o.get("after_s") is not None:
                                ov_a = 1
                    except Exception:
                        pass
    
                    _log_print(
                        f"[hudpy][dbg-steer] i={int(i)} ld_c={ld_c:.6f} "
                        f"before_s_h={float(before_s_h):.6f} after_s_h={float(after_s_h):.6f} "
                        f"default_before_s={float(default_before_s):.6f} default_after_s={float(default_after_s):.6f} "
                        f"ov_b={int(ov_b)} ov_a={int(ov_a)} "
                        f"iL={int(iL)} iR={int(iR)} span_n={int(span_n)} "
                        f"pts_target={int(pts_target)} max_pts={int(max_pts)} stride={int(stride)} "
                        f"steer_slow_scale={float(steer_slow_scale):.6f} steer_fast_scale={float(steer_fast_scale):.6f} "
                        f"abs_max={float(steer_abs_max):.6f} headroom={float(headroom):.3f}",
                        log_file,
                    )
                except Exception:
                    pass
    
                # Probe: ein paar Samples gleichmÃ¤ÃŸig Ã¼ber das Fenster
                try:
                    if int(iR) > int(iL):
                        step_dbg = max(1, int(round(float(iR - iL) / float(dbg_n - 1))))
                    else:
                        step_dbg = 1
    
                    n_print = 0
                    for k in range(int(iL), int(iR) + 1, int(step_dbg)):
                        if n_print >= int(dbg_n):
                            break
                        # slow CSV index
                        try:
                            si_dbg = int(round(float(k) * float(steer_slow_scale)))
                        except Exception:
                            si_dbg = int(k)
                        if si_dbg < 0:
                            si_dbg = 0
                        if slow_steer_frames:
                            if si_dbg >= len(slow_steer_frames):
                                si_dbg = len(slow_steer_frames) - 1
                            sv_dbg = float(slow_steer_frames[si_dbg])
                        else:
                            sv_dbg = 0.0
    
                        sn_dbg = _clamp(sv_dbg / max(1e-6, steer_abs_max), -1.0, 1.0)
                        ys_dbg = _y_from_norm(float(sn_dbg))
    
                        # fast mapping
                        fi2_dbg = int(k)
                        if slow_to_fast_frame and int(k) < len(slow_to_fast_frame):
                            fi2_dbg = int(slow_to_fast_frame[int(k)])
                            if fi2_dbg < 0:
                                fi2_dbg = 0
    
                        try:
                            fi_csv_dbg = int(round(float(fi2_dbg) * float(steer_fast_scale)))
                        except Exception:
                            fi_csv_dbg = int(fi2_dbg)
                        if fi_csv_dbg < 0:
                            fi_csv_dbg = 0
                        if fast_steer_frames:
                            if fi_csv_dbg >= len(fast_steer_frames):
                                fi_csv_dbg = len(fast_steer_frames) - 1
                            fv_dbg = float(fast_steer_frames[fi_csv_dbg])
                        else:
                            fv_dbg = 0.0
                        fn_dbg = _clamp(fv_dbg / max(1e-6, steer_abs_max), -1.0, 1.0)
                        yf_dbg = _y_from_norm(float(fn_dbg))
    
                        _log_print(
                            f"[hudpy][dbg-steer] k={int(k)} si={int(si_dbg)} sv={sv_dbg:.6f} sn={sn_dbg:.6f} ys={int(ys_dbg)} "
                            f"| fi2={int(fi2_dbg)} fi_csv={int(fi_csv_dbg)} fv={fv_dbg:.6f} fn={fn_dbg:.6f} yf={int(yf_dbg)}",
                            log_file,
                        )
    
                        n_print += 1
                except Exception:
                    pass
            # --- /DEBUG ---
    
            pts_s: list[tuple[int, int]] = []
            pts_f: list[tuple[int, int]] = []

            # Story 2.1: gemeinsames Fenster-Mapping nutzen (einmal pro Frame berechnet).
            sample_rows: list[tuple[int, int, int]] = []
            if map_idxs_all and len(map_idxs_all) == len(map_offsets_all) and len(map_idxs_all) == len(map_fast_idx_all):
                for idx_m, off_m, fi_m in zip(map_idxs_all, map_offsets_all, map_fast_idx_all):
                    idx_i = int(idx_m)
                    if idx_i < int(iL) or idx_i > int(iR):
                        continue
                    use_point = (idx_i == int(iL)) or (idx_i == int(iR)) or ((int(off_m) % int(stride)) == 0)
                    if use_point:
                        sample_rows.append((idx_i, int(off_m), int(fi_m)))

            if not sample_rows:
                idxs_fallback: list[int] = []
                for idx_fb in (int(iL), int(i), int(iR)):
                    if idx_fb < int(iL) or idx_fb > int(iR):
                        continue
                    if idx_fb not in idxs_fallback:
                        idxs_fallback.append(int(idx_fb))
                if not idxs_fallback:
                    idxs_fallback = [int(i)]
                for idx_fb in idxs_fallback:
                    sample_rows.append((int(idx_fb), int(idx_fb) - int(i), int(idx_fb)))

            for idx, _off, fi2_map in sample_rows:
                x = _idx_to_x(int(idx))
    
                # Slow
                sv = 0.0
                if slow_steer_frames:
                    si = int(round(float(idx) * float(steer_slow_scale)))
                    if si < 0:
                        si = 0
                    if si >= len(slow_steer_frames):
                        si = len(slow_steer_frames) - 1
                    sv = float(slow_steer_frames[si])
    
                sn = _clamp(sv / max(1e-6, steer_abs_max), -1.0, 1.0)
                ys = _y_from_norm(float(sn))
                pts_s.append((x, int(ys)))
    
                # Fast: idx -> frame_map -> fast idx (aus globalem Mapping)
                fi2 = int(fi2_map)
                if fi2 < 0:
                    fi2 = 0
                if not map_fast_idx_all:
                    if slow_to_fast_frame and fi2 < len(slow_to_fast_frame):
                        fi2 = int(slow_to_fast_frame[fi2])
                        if fi2 < 0:
                            fi2 = 0
    
                fv = 0.0
                if fast_steer_frames:
                    fi_csv = int(round(float(fi2) * float(steer_fast_scale)))
                    if fi_csv < 0:
                        fi_csv = 0
                    if fi_csv >= len(fast_steer_frames):
                        fi_csv = len(fast_steer_frames) - 1
                    fv = float(fast_steer_frames[fi_csv])
    
                fn = _clamp(fv / max(1e-6, steer_abs_max), -1.0, 1.0)
                yf = _y_from_norm(float(fn))
                pts_f.append((x, int(yf)))
    
            if len(pts_s) >= 2:
                dr.line(pts_s, fill=COL_SLOW_DARKRED, width=2)   # slow = rot
            if len(pts_f) >= 2:
                dr.line(pts_f, fill=COL_FAST_DARKBLUE, width=2)  # fast = blau

            # 0-Lenkung Mittellinie: nach den Kurven, damit sie durchgehend bleibt.
            try:
                dr.line(
                    [(int(x0), int(y_mid)), (int(x0 + w - 1), int(y_mid))],
                    fill=(COL_WHITE[0], COL_WHITE[1], COL_WHITE[2], 180),
                    width=1,
                )
            except Exception:
                pass

            # Vertical center marker: draw after curves, before text.
            mx = int(x0 + (w // 2))
            try:
                dr.rectangle([int(mx), int(y0), int(mx + 1), int(y0 + h)], fill=(255, 255, 255, 230))
            except Exception:
                pass

            axis_labels = filter_axis_labels_by_position(
                axis_labels,
                int(data_area_top),
                int(y0 + h - 5),
                zero_y=int(round(mid_y)),
                pad_px=2,
            )

            try:
                sv_cur = 0.0
                if slow_steer_frames:
                    si_cur = int(round(float(i) * float(steer_slow_scale)))
                    si_cur = max(0, min(si_cur, len(slow_steer_frames) - 1))
                    sv_cur = float(slow_steer_frames[si_cur])

                fi_cur = int(i)
                if slow_to_fast_frame and int(i) < len(slow_to_fast_frame):
                    fi_cur = int(slow_to_fast_frame[int(i)])
                    if fi_cur < 0:
                        fi_cur = 0

                fv_cur = 0.0
                if fast_steer_frames:
                    fi_csv_cur = int(round(float(fi_cur) * float(steer_fast_scale)))
                    fi_csv_cur = max(0, min(fi_csv_cur, len(fast_steer_frames) - 1))
                    fv_cur = float(fast_steer_frames[fi_csv_cur])

                sdeg = int(round(float(sv_cur) * 180.0 / math.pi))
                fdeg = int(round(float(fv_cur) * 180.0 / math.pi))

                s_txt = f"{sdeg:+04d}Â°"
                f_txt = f"{fdeg:+04d}Â°"

                mx = int(x0 + (w // 2))
                gap = 12

                f_w = _text_w(f_txt, font_val)
                f_x = int(mx - gap - f_w)
                s_x = int(mx + gap)

                if f_x < int(x0 + 2):
                    f_x = int(x0 + 2)
                if s_x > int(x0 + w - 2):
                    s_x = int(x0 + w - 2)

                fast_val_draw = (f_x, y_txt, f_txt)
                slow_val_draw = (s_x, y_txt, s_txt)
            except Exception:
                fast_val_draw = None
                slow_val_draw = None

            # Story 10: Text zuletzt (Y-Achse, Titel, Werte).
            draw_left_axis_labels(
                dr,
                int(x0),
                int(w),
                int(data_area_top),
                int(y0 + h - 5),
                axis_labels,
                font_axis,
                col_text=COL_WHITE,
                x_pad=8,
                fallback_font_obj=font_axis_small,
            )
            if title_pos is not None:
                draw_text_with_shadow(dr, (int(title_pos[0]), int(title_pos[1])), title_txt, fill=COL_WHITE, font=font_title)
            if fast_val_draw is not None:
                draw_text_with_shadow(
                    dr,
                    (int(fast_val_draw[0]), int(fast_val_draw[1])),
                    str(fast_val_draw[2]),
                    fill=(255, 0, 0, 255),
                    font=font_val,
                )  # Fast (rot) links am Marker
            if slow_val_draw is not None:
                draw_text_with_shadow(
                    dr,
                    (int(slow_val_draw[0]), int(slow_val_draw[1])),
                    str(slow_val_draw[2]),
                    fill=(0, 120, 255, 255),
                    font=font_val,
                )  # Slow (blau) rechts am Marker
        except Exception:
            return
