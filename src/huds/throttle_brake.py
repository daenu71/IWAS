from __future__ import annotations

import os
from typing import Any


def render_throttle_brake(ctx: dict[str, Any], box: tuple[int, int, int, int], dr: Any) -> None:
    x0, y0, w, h = box
    hud_key = ctx["hud_key"]
    i = int(ctx["i"])
    iL = int(ctx["iL"])
    iR = int(ctx["iR"])
    _idx_to_x = ctx["_idx_to_x"]
    _clamp = ctx["_clamp"]
    slow_frame_to_lapdist = ctx["slow_frame_to_lapdist"]
    slow_to_fast_frame = ctx["slow_to_fast_frame"]
    slow_throttle_frames = ctx["slow_throttle_frames"]
    fast_throttle_frames = ctx["fast_throttle_frames"]
    slow_brake_frames = ctx["slow_brake_frames"]
    fast_brake_frames = ctx["fast_brake_frames"]
    slow_abs_frames = ctx["slow_abs_frames"]
    fast_abs_frames = ctx["fast_abs_frames"]
    hud_curve_points_default = int(ctx["hud_curve_points_default"])
    hud_curve_points_overrides = ctx["hud_curve_points_overrides"]
    COL_SLOW_DARKRED = ctx["COL_SLOW_DARKRED"]
    COL_SLOW_BRIGHTRED = ctx["COL_SLOW_BRIGHTRED"]
    COL_FAST_DARKBLUE = ctx["COL_FAST_DARKBLUE"]
    COL_FAST_BRIGHTBLUE = ctx["COL_FAST_BRIGHTBLUE"]
    COL_WHITE = ctx["COL_WHITE"]

    # Story 4: Throttle / Brake + ABS (scrollend)
    if hud_key == "Throttle / Brake":
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
    
            def _text_w(text: str, font_obj):
                try:
                    bb = dr.textbbox((0, 0), text, font=font_obj)
                    return int(bb[2] - bb[0])
                except Exception:
                    try:
                        return int(dr.textlength(text, font=font_obj))
                    except Exception:
                        return int(len(text) * 8)
    
            # Farben global definiert (oben im File)
            COL_SLOW_BRAKE = COL_SLOW_DARKRED
            COL_SLOW_THROTTLE = COL_SLOW_BRIGHTRED
            COL_FAST_BRAKE = COL_FAST_DARKBLUE
            COL_FAST_THROTTLE = COL_FAST_BRIGHTBLUE
    
            # Headroom nur oben (analog Steering, aber 0..1)
            try:
                headroom = float((os.environ.get("IRVC_PEDAL_HEADROOM") or "").strip() or "1.12")
            except Exception:
                headroom = 1.12
            if headroom < 1.00:
                headroom = 1.00
            if headroom > 2.00:
                headroom = 2.00
    
            font_sz = int(round(max(10.0, min(18.0, float(h) * 0.13))))
            font_val_sz = int(round(max(11.0, min(20.0, float(h) * 0.15))))
            font_title = _load_font(font_sz)
            font_val = _load_font(font_val_sz)
    
            # Titel + Werte auf gleicher HÃ¶he (ruhig, kein Springen)
            y_txt = int(y0 + 2)
            dr.text((int(x0 + 4), y_txt), "Throttle / Brake", fill=COL_WHITE, font=font_title)
    
            # Skalen (CSV ~60Hz -> Video-Frames)
            n_frames = float(max(1, len(slow_frame_to_lapdist) - 1))
            t_slow_scale = 1.0
            t_fast_scale = 1.0
            b_slow_scale = 1.0
            b_fast_scale = 1.0
            a_slow_scale = 1.0
            a_fast_scale = 1.0
            try:
                if slow_throttle_frames and len(slow_throttle_frames) >= 2:
                    t_slow_scale = float(len(slow_throttle_frames) - 1) / n_frames
                if fast_throttle_frames and len(fast_throttle_frames) >= 2:
                    t_fast_scale = float(len(fast_throttle_frames) - 1) / n_frames
                if slow_brake_frames and len(slow_brake_frames) >= 2:
                    b_slow_scale = float(len(slow_brake_frames) - 1) / n_frames
                if fast_brake_frames and len(fast_brake_frames) >= 2:
                    b_fast_scale = float(len(fast_brake_frames) - 1) / n_frames
                if slow_abs_frames and len(slow_abs_frames) >= 2:
                    a_slow_scale = float(len(slow_abs_frames) - 1) / n_frames
                if fast_abs_frames and len(fast_abs_frames) >= 2:
                    a_fast_scale = float(len(fast_abs_frames) - 1) / n_frames
            except Exception:
                pass
    
            # Aktuelle Werte (am Marker)
            mx0 = int(x0 + (w // 2))
            gap = 12
    
            # Slow Index (am Marker)
            ti_cur = int(round(float(i) * float(t_slow_scale)))
            bi_cur = int(round(float(i) * float(b_slow_scale)))
            if slow_throttle_frames:
                ti_cur = max(0, min(ti_cur, len(slow_throttle_frames) - 1))
                t_s = float(slow_throttle_frames[ti_cur])
            else:
                t_s = 0.0
            if slow_brake_frames:
                bi_cur = max(0, min(bi_cur, len(slow_brake_frames) - 1))
                b_s = float(slow_brake_frames[bi_cur])
            else:
                b_s = 0.0
    
            # Fast Index (am Marker)
            fi_cur = int(i)
            if slow_to_fast_frame and int(i) < len(slow_to_fast_frame):
                fi_cur = int(slow_to_fast_frame[int(i)])
                if fi_cur < 0:
                    fi_cur = 0
    
            tf_cur = int(round(float(fi_cur) * float(t_fast_scale)))
            bf_cur = int(round(float(fi_cur) * float(b_fast_scale)))
            if fast_throttle_frames:
                tf_cur = max(0, min(tf_cur, len(fast_throttle_frames) - 1))
                t_f = float(fast_throttle_frames[tf_cur])
            else:
                t_f = 0.0
            if fast_brake_frames:
                bf_cur = max(0, min(bf_cur, len(fast_brake_frames) - 1))
                b_f = float(fast_brake_frames[bf_cur])
            else:
                b_f = 0.0
    
            # Format: immer 3 Stellen, kein Springen
            s_txt = f"T{int(round(_clamp(t_s, 0.0, 1.0) * 100.0)):03d}% B{int(round(_clamp(b_s, 0.0, 1.0) * 100.0)):03d}%"
            f_txt = f"T{int(round(_clamp(t_f, 0.0, 1.0) * 100.0)):03d}% B{int(round(_clamp(b_f, 0.0, 1.0) * 100.0)):03d}%"
    
            f_w = _text_w(f_txt, font_val)
            f_x = int(mx0 - gap - f_w)
            s_x = int(mx0 + gap)
    
            if f_x < int(x0 + 2):
                f_x = int(x0 + 2)
            if s_x > int(x0 + w - 2):
                s_x = int(x0 + w - 2)
    
            # Fast links, Slow rechts (wie Steering)
            dr.text((f_x, y_txt), f_txt, fill=COL_SLOW_BRAKE, font=font_val)
            dr.text((s_x, y_txt), s_txt, fill=COL_FAST_BRAKE, font=font_val)
    
            # Layout: ABS-Balken direkt unter Titelzeile, danach Plot
            abs_h = int(max(10, min(15, round(float(h) * 0.085))))
            abs_gap_y = 2
            y_abs0 = int(y0 + font_val_sz + 5)
            y_abs_s = y_abs0
            y_abs_f = y_abs0 + abs_h + abs_gap_y
    
            plot_top = y_abs_f + abs_h + 4
            plot_bottom = int(y0 + h - 2)
            if plot_bottom <= plot_top + 5:
                plot_top = int(y0 + int(h * 0.30))
            plot_h = max(10, plot_bottom - plot_top)
    
            def _y_from_01(v01: float) -> int:
                v01 = _clamp(v01, 0.0, 1.0)
                v_scaled = v01 / max(1.0, headroom)
                yy = float(plot_top) + float(plot_h) - (v_scaled * float(plot_h))
                return int(round(yy))
    
            # Stride / Punktdichte (wie Steering)
            span_n = max(1, int(iR - iL))
            pts_target = int(hud_curve_points_default or 180)
            try:
                ovs = hud_curve_points_overrides if isinstance(hud_curve_points_overrides, dict) else None
                if ovs and hud_key in ovs:
                    pts_target = int(float(ovs.get(hud_key) or pts_target))
            except Exception:
                pass
            if pts_target < 40:
                pts_target = 40
            if pts_target > 600:
                pts_target = 600
            max_pts = max(40, min(int(w), int(pts_target)))
            stride = max(1, int(round(float(span_n) / float(max_pts))))
    
            # Helper: X aus Frame-Index (Zeit-Achse, stabil)
            def _x_from_idx(idx0: int) -> int:
                return _idx_to_x(int(idx0))
    
            # Marker-zentriertes Sampling (symmetrisch links/rechts) + Endpunkte erzwingen
            idxs: list[int] = []
    
            k = int(i)
            while k >= int(iL):
                idxs.append(k)
                k -= int(stride)
    
            k = int(i) + int(stride)
            while k <= int(iR):
                idxs.append(k)
                k += int(stride)
    
            idxs = sorted(set(idxs))
            if idxs:
                if idxs[0] != int(iL):
                    idxs.insert(0, int(iL))
                if idxs[-1] != int(iR):
                    idxs.append(int(iR))
            else:
                idxs = [int(iL), int(i), int(iR)]
    
            # Kurven sammeln
            pts_s_t: list[tuple[int, int]] = []
            pts_s_b: list[tuple[int, int]] = []
            pts_f_t: list[tuple[int, int]] = []
            pts_f_b: list[tuple[int, int]] = []
    
            for idx in idxs:
                if idx < int(iL) or idx > int(iR):
                    continue
    
                x = _idx_to_x(int(idx))
    
                # Slow
                if slow_throttle_frames:
                    si = int(round(float(idx) * float(t_slow_scale)))
                    si = max(0, min(si, len(slow_throttle_frames) - 1))
                    st = float(slow_throttle_frames[si])
                else:
                    st = 0.0
                if slow_brake_frames:
                    si = int(round(float(idx) * float(b_slow_scale)))
                    si = max(0, min(si, len(slow_brake_frames) - 1))
                    sb = float(slow_brake_frames[si])
                else:
                    sb = 0.0
    
                pts_s_t.append((x, _y_from_01(st)))
                pts_s_b.append((x, _y_from_01(sb)))
    
                # Fast: idx -> frame_map -> fast idx
                fi = int(idx)
                if slow_to_fast_frame and fi < len(slow_to_fast_frame):
                    fi = int(slow_to_fast_frame[fi])
                    if fi < 0:
                        fi = 0
    
                if fast_throttle_frames:
                    fci = int(round(float(fi) * float(t_fast_scale)))
                    fci = max(0, min(fci, len(fast_throttle_frames) - 1))
                    ft = float(fast_throttle_frames[fci])
                else:
                    ft = 0.0
                if fast_brake_frames:
                    fci = int(round(float(fi) * float(b_fast_scale)))
                    fci = max(0, min(fci, len(fast_brake_frames) - 1))
                    fb = float(fast_brake_frames[fci])
                else:
                    fb = 0.0
    
                pts_f_t.append((x, _y_from_01(ft)))
                pts_f_b.append((x, _y_from_01(fb)))
    
            # Linien zeichnen (Bremse erst, dann Gas)
            if len(pts_s_b) >= 2:
                dr.line(pts_s_b, fill=COL_SLOW_BRAKE, width=2)
            if len(pts_s_t) >= 2:
                dr.line(pts_s_t, fill=COL_SLOW_THROTTLE, width=2)
            if len(pts_f_b) >= 2:
                dr.line(pts_f_b, fill=COL_FAST_BRAKE, width=2)
            if len(pts_f_t) >= 2:
                dr.line(pts_f_t, fill=COL_FAST_THROTTLE, width=2)
    
            # ABS-Balken: scrollende Segmente (LÃ¤nge = Dauer von ABS=1 im Fenster)
            def _abs_val_s(idx0: int) -> float:
                if not slow_abs_frames:
                    return 0.0
                si = int(round(float(idx0) * float(a_slow_scale)))
                si = max(0, min(si, len(slow_abs_frames) - 1))
                return float(slow_abs_frames[si])
    
            def _abs_val_f(idx0: int) -> float:
                if not fast_abs_frames:
                    return 0.0
                fi = int(idx0)
                if slow_to_fast_frame and fi < len(slow_to_fast_frame):
                    fi = int(slow_to_fast_frame[fi])
                    if fi < 0:
                        fi = 0
                fci = int(round(float(fi) * float(a_fast_scale)))
                fci = max(0, min(fci, len(fast_abs_frames) - 1))
                return float(fast_abs_frames[fci])
    
            def _draw_abs_segments(y_mid: int, col: tuple[int, int, int, int], val_fn):
                in_seg = False
                x_start = 0
                x_prev = 0
                for idx2 in idxs:
                    if idx2 < int(iL) or idx2 > int(iR):
                        continue
                    x2 = _x_from_idx(idx2)
                    v2 = val_fn(idx2)
                    on = (v2 >= 0.5)
                    if on and (not in_seg):
                        in_seg = True
                        x_start = x2
                    if in_seg:
                        x_prev = x2
                    if (not on) and in_seg:
                        try:
                            dr.line([(int(x_start), int(y_mid)), (int(x_prev), int(y_mid))], fill=col, width=int(abs_h))
                        except Exception:
                            pass
                        in_seg = False
                if in_seg:
                    try:
                        dr.line([(int(x_start), int(y_mid)), (int(x_prev), int(y_mid))], fill=col, width=int(abs_h))
                    except Exception:
                        pass
    
            _draw_abs_segments(int(y_abs_s + abs_h // 2), COL_SLOW_BRAKE, _abs_val_s)
            _draw_abs_segments(int(y_abs_f + abs_h // 2), COL_FAST_BRAKE, _abs_val_f)
    
        except Exception:
            pass
