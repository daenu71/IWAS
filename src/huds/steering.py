from __future__ import annotations

import math
import os
from typing import Any


def render_steering(ctx: dict[str, Any], box: tuple[int, int, int, int], dr: Any) -> None:
    x0, y0, w, h = box
    hud_key = ctx["hud_key"]
    i = int(ctx["i"])
    iL = int(ctx["iL"])
    iR = int(ctx["iR"])
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
    
            # 0-Lenkung Mittellinie
            try:
                y_mid = int(round(mid_y))
                dr.line([(int(x0), y_mid), (int(x0 + w - 1), y_mid)], fill=(COL_WHITE[0], COL_WHITE[1], COL_WHITE[2], 180), width=1)
            except Exception:
                pass
    
            # Titel oben links + Werte am Marker (grÃ¶ÃŸer, gleiche HÃ¶he, stabil formatiert)
            try:
                try:
                    from PIL import ImageFont
                except Exception:
                    ImageFont = None  # type: ignore
    
                # SchriftgrÃ¶ÃŸen: bewusst kleiner und ruhiger
                # Titel etwas kleiner als Werte
                font_sz = int(round(max(10.0, min(18.0, float(h) * 0.13))))
                font_val_sz = int(round(max(11.0, min(20.0, float(h) * 0.15))))
    
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
    
                def _text_w(text: str, font_obj):
                    try:
                        # Pillow >= 8: textbbox vorhanden
                        bb = dr.textbbox((0, 0), text, font=font_obj)
                        return int(bb[2] - bb[0])
                    except Exception:
                        try:
                            return int(dr.textlength(text, font=font_obj))
                        except Exception:
                            return int(len(text) * 8)
    
                y_txt = int(y0 + 2)  # gleiche HÃ¶he wie Titel
                dr.text((int(x0 + 4), y_txt), "Steering wheel angle", fill=COL_WHITE, font=font_title)
    
                # Werte holen (Radiant -> Grad)
                sv_cur = 0.0
                if slow_steer_frames:
                    si_cur = int(round(float(i) * float(steer_slow_scale)))
                    if si_cur < 0:
                        si_cur = 0
                    if si_cur >= len(slow_steer_frames):
                        si_cur = len(slow_steer_frames) - 1
                    sv_cur = float(slow_steer_frames[si_cur])
    
                fi_cur = int(i)
                if slow_to_fast_frame and int(i) < len(slow_to_fast_frame):
                    fi_cur = int(slow_to_fast_frame[int(i)])
                    if fi_cur < 0:
                        fi_cur = 0
    
                fv_cur = 0.0
                if fast_steer_frames:
                    fi_csv_cur = int(round(float(fi_cur) * float(steer_fast_scale)))
                    if fi_csv_cur < 0:
                        fi_csv_cur = 0
                    if fi_csv_cur >= len(fast_steer_frames):
                        fi_csv_cur = len(fast_steer_frames) - 1
                    fv_cur = float(fast_steer_frames[fi_csv_cur])
    
                sdeg = int(round(float(sv_cur) * 180.0 / math.pi))
                fdeg = int(round(float(fv_cur) * 180.0 / math.pi))
    
                # Stabil: immer Vorzeichen + 3 Ziffern -> kein Springen
                # Beispiel: +075Â°, -147Â°
                s_txt = f"{sdeg:+04d}Â°"
                f_txt = f"{fdeg:+04d}Â°"
    
                mx = int(x0 + (w // 2))
                gap = 12  # ~1 Ziffer Abstand zum Marker
    
                # Rot nÃ¤her an den Marker (links), Blau rechts
                f_w = _text_w(f_txt, font_val)
                f_x = int(mx - gap - f_w)
                s_x = int(mx + gap)
    
                # Clamp in die Box
                if f_x < int(x0 + 2):
                    f_x = int(x0 + 2)
                if s_x > int(x0 + w - 2):
                    s_x = int(x0 + w - 2)
    
                dr.text((f_x, y_txt), f_txt, fill=(255, 0, 0, 255), font=font_val)       # Fast (rot) links am Marker
                dr.text((s_x, y_txt), s_txt, fill=(0, 120, 255, 255), font=font_val)     # Slow (blau) rechts am Marker
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
    
                    k = int(iL)
                    n_print = 0
                    while k <= int(iR) and n_print < dbg_n:
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
                        k += step_dbg
                except Exception:
                    pass
            # --- /DEBUG ---
    
            pts_s: list[tuple[int, int]] = []
            pts_f: list[tuple[int, int]] = []
    
            # Symmetrisches Sampling um den Marker (wie bei Throttle/Brake),
            # damit links/rechts gleich â€žstabilâ€œ wirkt.
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
    
            for idx in idxs:
                if idx < int(iL) or idx > int(iR):
                    continue
    
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
    
                # Fast: idx -> frame_map -> fast idx
                fi2 = int(idx)
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
        except Exception:
            pass
    
    
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
                        f"abs_max={float(steer_abs_max):.6f}",
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
    
                    k = int(iL)
                    n_print = 0
                    while k <= int(iR) and n_print < dbg_n:
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
                        ys_dbg = int(round(mid_y - (sn_dbg * amp)))
    
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
                        yf_dbg = int(round(mid_y - (fn_dbg * amp)))
    
                        _log_print(
                            f"[hudpy][dbg-steer] k={int(k)} si={int(si_dbg)} sv={sv_dbg:.6f} sn={sn_dbg:.6f} ys={int(ys_dbg)} "
                            f"| fi2={int(fi2_dbg)} fi_csv={int(fi_csv_dbg)} fv={fv_dbg:.6f} fn={fn_dbg:.6f} yf={int(yf_dbg)}",
                            log_file,
                        )
    
                        n_print += 1
                        k += step_dbg
                except Exception:
                    pass
            # --- /DEBUG ---
    
            pts_s: list[tuple[int, int]] = []
            pts_f: list[tuple[int, int]] = []
    
            idx = iL
            while idx <= iR:
                ld_k = float(slow_frame_to_lapdist[idx]) % 1.0
                d = _wrap_delta_05(ld_k, ld_c)
                if (-before_span <= d) and (d <= after_span):
                    x = _delta_to_x(float(d))
    
                    if slow_steer_frames:
                        si = int(round(float(idx) * float(steer_slow_scale)))
                        if si < 0:
                            si = 0
                        if si >= len(slow_steer_frames):
                            si = len(slow_steer_frames) - 1
                        sv = float(slow_steer_frames[si])
                    sn = _clamp(sv / max(1e-6, steer_abs_max), -1.0, 1.0)
                    ys = int(round(mid_y - (sn * amp)))
                    pts_s.append((x, ys))
    
                    fi2 = idx
                    if slow_to_fast_frame and idx < len(slow_to_fast_frame):
                        fi2 = int(slow_to_fast_frame[idx])
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
                    yf = int(round(mid_y - (fn * amp)))
                    pts_f.append((x, yf))
    
                idx += stride
    
            if len(pts_s) >= 2:
                dr.line(pts_s, fill=(255, 0, 0, 255), width=2)
            if len(pts_f) >= 2:
                dr.line(pts_f, fill=(0, 120, 255, 255), width=2)
        except Exception:
            pass
