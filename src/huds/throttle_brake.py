from __future__ import annotations

import os
import math
from typing import Any

from huds.common import (
    COL_HUD_BG,
    draw_left_axis_labels,
    draw_stripe_grid,
    value_boundaries_to_y,
)


def render_throttle_brake(ctx: dict[str, Any], box: tuple[int, int, int, int], dr: Any) -> None:
    x0, y0, w, h = box
    hud_key = ctx["hud_key"]
    i = int(ctx["i"])
    iL = int(ctx["iL"])
    iR = int(ctx["iR"])
    frame_window_mapping = ctx.get("frame_window_mapping")
    map_idxs_all = list(getattr(frame_window_mapping, "idxs", []) or [])
    map_offsets_all = list(getattr(frame_window_mapping, "offsets", []) or [])
    map_t_slow_all = list(getattr(frame_window_mapping, "t_slow", []) or [])
    map_fast_idx_all = list(getattr(frame_window_mapping, "fast_idx", []) or [])
    map_t_fast_all = list(getattr(frame_window_mapping, "t_fast", []) or [])
    fps = float(ctx.get("fps", 30.0) or 30.0)
    _idx_to_x = ctx["_idx_to_x"]
    _clamp = ctx["_clamp"]
    slow_frame_to_lapdist = ctx["slow_frame_to_lapdist"]
    slow_to_fast_frame = ctx["slow_to_fast_frame"]
    slow_frame_to_fast_time_s = ctx.get("slow_frame_to_fast_time_s")
    slow_throttle_frames = ctx["slow_throttle_frames"]
    fast_throttle_frames = ctx["fast_throttle_frames"]
    slow_brake_frames = ctx["slow_brake_frames"]
    fast_brake_frames = ctx["fast_brake_frames"]
    slow_abs_frames = ctx["slow_abs_frames"]
    fast_abs_frames = ctx["fast_abs_frames"]
    hud_pedals_sample_mode = str(ctx.get("hud_pedals_sample_mode", "time") or "time").strip().lower()
    hud_pedals_abs_debounce_ms = int(ctx.get("hud_pedals_abs_debounce_ms", 60) or 60)
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
            font_axis = _load_font(max(8, int(font_sz - 2)))
            font_axis_small = _load_font(max(7, int(font_sz - 3)))
     
            # Titel + Werte auf gleicher HÃ¶he (ruhig, kein Springen)
            y_txt = int(y0 + 2)
    
            if hud_pedals_sample_mode not in ("time", "legacy"):
                hud_pedals_sample_mode = "time"
            if hud_pedals_abs_debounce_ms < 0:
                hud_pedals_abs_debounce_ms = 0
            if hud_pedals_abs_debounce_ms > 500:
                hud_pedals_abs_debounce_ms = 500

            fps_safe = float(fps) if (math.isfinite(float(fps)) and float(fps) > 1e-6) else 30.0
            abs_window_s = float(hud_pedals_abs_debounce_ms) / 1000.0

            # Legacy Skalen (CSV -> Video-Frame-Index).
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

            def _sample_linear_time(vals: list[float] | None, t_s: float) -> float:
                if not vals:
                    return 0.0
                n = len(vals)
                if n <= 1:
                    return float(vals[0])
                pos = _clamp(float(t_s) * float(fps_safe), 0.0, float(n - 1))
                i0 = int(math.floor(pos))
                i1 = min(i0 + 1, n - 1)
                a = float(pos - float(i0))
                v0 = float(vals[i0])
                v1 = float(vals[i1])
                return float(v0 + ((v1 - v0) * a))

            def _fast_time_from_slow_idx(idx0: int) -> float:
                ii = int(idx0)
                if ii < 0:
                    ii = 0
                if slow_frame_to_fast_time_s:
                    if ii >= len(slow_frame_to_fast_time_s):
                        ii = len(slow_frame_to_fast_time_s) - 1
                    return float(slow_frame_to_fast_time_s[ii])
                fi = int(ii)
                if slow_to_fast_frame and fi < len(slow_to_fast_frame):
                    fi = int(slow_to_fast_frame[fi])
                    if fi < 0:
                        fi = 0
                return float(fi) / float(fps_safe)

            def _build_abs_prefix(vals: list[float] | None) -> list[int]:
                if not vals:
                    return [0]
                out = [0]
                acc = 0
                for vv in vals:
                    acc += 1 if float(vv) >= 0.5 else 0
                    out.append(acc)
                return out

            slow_abs_prefix = _build_abs_prefix(slow_abs_frames)
            fast_abs_prefix = _build_abs_prefix(fast_abs_frames)

            def _abs_on_majority_time(vals: list[float] | None, pref: list[int], t_s: float) -> float:
                if not vals:
                    return 0.0
                n = len(vals)
                if n <= 1:
                    return 1.0 if float(vals[0]) >= 0.5 else 0.0
                if abs_window_s <= 1e-9:
                    p = int(round(_clamp(float(t_s) * float(fps_safe), 0.0, float(n - 1))))
                    return 1.0 if float(vals[p]) >= 0.5 else 0.0
                half = 0.5 * float(abs_window_s)
                p0 = int(math.floor((float(t_s) - half) * float(fps_safe)))
                p1 = int(math.ceil((float(t_s) + half) * float(fps_safe)))
                if p0 < 0:
                    p0 = 0
                if p1 >= n:
                    p1 = n - 1
                if p1 < p0:
                    p1 = p0
                total = int(p1 - p0 + 1)
                on_cnt = int(pref[p1 + 1] - pref[p0])
                return 1.0 if (on_cnt * 2) > total else 0.0
    
            # Aktuelle Werte (am Marker)
            mx0 = int(x0 + (w // 2))
            gap = 12
    
            if hud_pedals_sample_mode == "time":
                t_cur_slow = float(i) / float(fps_safe)
                t_cur_fast = _fast_time_from_slow_idx(int(i))
                t_s = _sample_linear_time(slow_throttle_frames, t_cur_slow)
                b_s = _sample_linear_time(slow_brake_frames, t_cur_slow)
                t_f = _sample_linear_time(fast_throttle_frames, t_cur_fast)
                b_f = _sample_linear_time(fast_brake_frames, t_cur_fast)
            else:
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

            # Story 10: 5 Segmente (0..100%) mit festen Labels 20/40/60/80.
            grid_vals = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
            y_grid_top = int(_y_from_01(1.0))
            y_grid_bot = int(_y_from_01(0.0))
            y_grid_bounds = value_boundaries_to_y(grid_vals, _y_from_01, y_grid_top, y_grid_bot)
            draw_stripe_grid(
                dr,
                int(x0),
                int(w),
                int(min(y_grid_top, y_grid_bot)),
                int(max(y_grid_top, y_grid_bot)),
                y_grid_bounds,
                col_bg=COL_HUD_BG,
                darken_delta=6,
            )
            axis_labels = [
                (int(_y_from_01(0.2)), "20"),
                (int(_y_from_01(0.4)), "40"),
                (int(_y_from_01(0.6)), "60"),
                (int(_y_from_01(0.8)), "80"),
            ]
    
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

            # Story 2.1: gemeinsames Fenster-Mapping nutzen (einmal pro Frame berechnet).
            sample_rows: list[tuple[int, int, float, int, float]] = []
            if (
                map_idxs_all
                and len(map_idxs_all) == len(map_offsets_all)
                and len(map_idxs_all) == len(map_t_slow_all)
                and len(map_idxs_all) == len(map_fast_idx_all)
                and len(map_idxs_all) == len(map_t_fast_all)
            ):
                for idx_m, off_m, ts_m, fi_m, tf_m in zip(
                    map_idxs_all,
                    map_offsets_all,
                    map_t_slow_all,
                    map_fast_idx_all,
                    map_t_fast_all,
                ):
                    idx_i = int(idx_m)
                    if idx_i < int(iL) or idx_i > int(iR):
                        continue
                    use_point = (idx_i == int(iL)) or (idx_i == int(iR)) or ((int(off_m) % int(stride)) == 0)
                    if use_point:
                        sample_rows.append((idx_i, int(off_m), float(ts_m), int(fi_m), float(tf_m)))

            if not sample_rows:
                # Fallback (kompatibel), falls kein globales Mapping vorhanden ist.
                idxs_fallback: list[int] = []
                for idx_fb in (int(iL), int(i), int(iR)):
                    if idx_fb < int(iL) or idx_fb > int(iR):
                        continue
                    if idx_fb not in idxs_fallback:
                        idxs_fallback.append(int(idx_fb))
                if not idxs_fallback:
                    idxs_fallback = [int(i)]
                for idx_fb in idxs_fallback:
                    sample_rows.append(
                        (
                            int(idx_fb),
                            int(idx_fb) - int(i),
                            float(idx_fb) / float(fps_safe),
                            int(idx_fb),
                            float(idx_fb) / float(fps_safe),
                        )
                    )

            # Ensure stable left-to-right drawing order for scroll curves.
            sample_rows.sort(key=lambda row: int(row[0]))

            idxs = [int(row[0]) for row in sample_rows]
            idx_to_t_slow = {int(row[0]): float(row[2]) for row in sample_rows}
            idx_to_fast_idx = {int(row[0]): int(row[3]) for row in sample_rows}
            idx_to_t_fast = {int(row[0]): float(row[4]) for row in sample_rows}
    
            # Kurven sammeln
            pts_s_t: list[tuple[int, int]] = []
            pts_s_b: list[tuple[int, int]] = []
            pts_f_t: list[tuple[int, int]] = []
            pts_f_b: list[tuple[int, int]] = []

            for idx, _off, t_slow_m, fi_m, t_fast_m in sample_rows:
                x = int(round(_idx_to_x(int(idx))))

                if hud_pedals_sample_mode == "time":
                    t_slow = float(t_slow_m)
                    t_fast = float(t_fast_m)
                    st = _sample_linear_time(slow_throttle_frames, t_slow)
                    sb = _sample_linear_time(slow_brake_frames, t_slow)
                    ft = _sample_linear_time(fast_throttle_frames, t_fast)
                    fb = _sample_linear_time(fast_brake_frames, t_fast)
                else:
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

                    # Fast: aus gemeinsamem Mapping (fallback: idx)
                    fi = int(fi_m)
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

                pts_s_t.append((int(x), int(_y_from_01(st))))
                pts_s_b.append((int(x), int(_y_from_01(sb))))
                pts_f_t.append((int(x), int(_y_from_01(ft))))
                pts_f_b.append((int(x), int(_y_from_01(fb))))

            def _dense_curve_points(pts_in: list[tuple[int, int]]) -> list[tuple[int, int]]:
                if not pts_in:
                    return []
                out: list[tuple[int, int]] = []
                x_prev = int(pts_in[0][0])
                y_prev = int(pts_in[0][1])
                out.append((x_prev, y_prev))
                for x_raw, y_raw in pts_in[1:]:
                    x_cur = int(x_raw)
                    y_cur = int(y_raw)
                    if x_cur < x_prev:
                        # Ignore out-of-order points; curves should move left-to-right.
                        continue
                    if x_cur == x_prev:
                        out[-1] = (int(x_cur), int(y_cur))
                        y_prev = int(y_cur)
                        continue
                    dx = int(x_cur - x_prev)
                    dy = float(y_cur - y_prev)
                    for step in range(1, dx + 1):
                        xi = int(x_prev + step)
                        yi = int(round(float(y_prev) + (dy * (float(step) / float(dx)))))
                        if out and int(out[-1][0]) == int(xi):
                            out[-1] = (int(xi), int(yi))
                        else:
                            out.append((int(xi), int(yi)))
                    x_prev = int(x_cur)
                    y_prev = int(y_cur)
                return out

            def _draw_dense_curve(pts_in: list[tuple[int, int]], col: tuple[int, int, int, int]) -> None:
                pts_dense = _dense_curve_points(pts_in)
                if len(pts_dense) >= 2:
                    dr.line(pts_dense, fill=col, width=2)

            # Draw brake first, then throttle.
            _draw_dense_curve(pts_s_b, COL_SLOW_BRAKE)
            _draw_dense_curve(pts_s_t, COL_SLOW_THROTTLE)
            _draw_dense_curve(pts_f_b, COL_FAST_BRAKE)
            _draw_dense_curve(pts_f_t, COL_FAST_THROTTLE)
    
            # ABS-Balken: scrollende Segmente (LÃ¤nge = Dauer von ABS=1 im Fenster)
            def _abs_val_s(idx0: int) -> float:
                if hud_pedals_sample_mode == "time":
                    t_slow = float(idx_to_t_slow.get(int(idx0), float(idx0) / float(fps_safe)))
                    return _abs_on_majority_time(slow_abs_frames, slow_abs_prefix, t_slow)
                if not slow_abs_frames:
                    return 0.0
                si = int(round(float(idx0) * float(a_slow_scale)))
                si = max(0, min(si, len(slow_abs_frames) - 1))
                return float(slow_abs_frames[si])

            def _abs_val_f(idx0: int) -> float:
                if hud_pedals_sample_mode == "time":
                    t_fast = float(idx_to_t_fast.get(int(idx0), _fast_time_from_slow_idx(int(idx0))))
                    return _abs_on_majority_time(fast_abs_frames, fast_abs_prefix, t_fast)
                if not fast_abs_frames:
                    return 0.0
                fi = int(idx_to_fast_idx.get(int(idx0), int(idx0)))
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
                    x2 = int(round(_x_from_idx(idx2)))
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

            # Vertical center marker: draw after curves/segments, before text.
            try:
                dr.rectangle([int(mx0), int(y0), int(mx0 + 1), int(y0 + h)], fill=(255, 255, 255, 230))
            except Exception:
                pass

            # Story 10: Text zuletzt (Y-Achse + Titel + Werte).
            draw_left_axis_labels(
                dr,
                int(x0),
                int(w),
                int(min(y_grid_top, y_grid_bot)),
                int(max(y_grid_top, y_grid_bot)),
                axis_labels,
                font_axis,
                col_text=COL_WHITE,
                x_pad=6,
                fallback_font_obj=font_axis_small,
            )
            dr.text((int(x0 + 4), y_txt), "Throttle / Brake", fill=COL_WHITE, font=font_title)
            # Fast links, Slow rechts (wie Steering)
            dr.text((f_x, y_txt), f_txt, fill=COL_SLOW_BRAKE, font=font_val)
            dr.text((s_x, y_txt), s_txt, fill=COL_FAST_BRAKE, font=font_val)
     
        except Exception:
            pass
