from __future__ import annotations

from typing import Any

from huds.common import (
    COL_HUD_BG,
    build_value_boundaries,
    choose_tick_step,
    draw_left_axis_labels,
    draw_stripe_grid,
    draw_text_with_shadow,
    filter_axis_labels_by_position,
    format_value_for_step,
    should_suppress_boundary_label,
    value_boundaries_to_y,
)


def render_delta(ctx: dict[str, Any], box: tuple[int, int, int, int], dr: Any) -> None:
    x0, y0, w, h = box
    hud_key = ctx["hud_key"]
    fps = float(ctx["fps"])
    i = int(ctx["i"])
    iL = int(ctx["iL"])
    iR = int(ctx["iR"])
    frame_window_mapping = ctx.get("frame_window_mapping")
    map_idxs_all = list(getattr(frame_window_mapping, "idxs", []) or [])
    map_offsets_all = list(getattr(frame_window_mapping, "offsets", []) or [])
    map_t_slow_all = list(getattr(frame_window_mapping, "t_slow", []) or [])
    map_t_fast_all = list(getattr(frame_window_mapping, "t_fast", []) or [])
    mx = int(ctx["mx"])
    _idx_to_x = ctx["_idx_to_x"]
    slow_frame_to_fast_time_s = ctx["slow_frame_to_fast_time_s"]
    delta_has_neg = bool(ctx["delta_has_neg"])
    delta_pos_max = float(ctx["delta_pos_max"])
    delta_neg_min = float(ctx["delta_neg_min"])
    hud_curve_points_default = int(ctx["hud_curve_points_default"])
    hud_curve_points_overrides = ctx["hud_curve_points_overrides"]
    hud_dbg = bool(ctx["hud_dbg"])
    _log_print = ctx["_log_print"]
    log_file = ctx["log_file"]
    COL_WHITE = ctx["COL_WHITE"]
    COL_SLOW_DARKRED = ctx["COL_SLOW_DARKRED"]
    COL_FAST_DARKBLUE = ctx["COL_FAST_DARKBLUE"]

    # Story 7: Delta (Time delta) HUD
    if hud_key == "Delta":
        try:
            # Fonts
            try:
                from PIL import ImageFont
            except Exception:
                ImageFont = None  # type: ignore

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
            font_axis = _load_font(max(8, int(font_sz - 2)))
            font_axis_small = _load_font(max(7, int(font_sz - 3)))

            # Text oben: reservierter Bereich (Headroom)
            top_pad = int(round(max(14.0, float(font_sz) + 8.0)))
            plot_y0 = int(y0) + top_pad
            plot_y1 = int(y0 + h - 2)

            if plot_y1 <= plot_y0 + 4:
                plot_y0 = int(y0) + 2
                plot_y1 = int(y0 + h - 2)

            map_idx_to_tslow: dict[int, float] = {}
            map_idx_to_tfast: dict[int, float] = {}
            if (
                map_idxs_all
                and len(map_idxs_all) == len(map_t_slow_all)
                and len(map_idxs_all) == len(map_t_fast_all)
            ):
                for idx_m, ts_m, tf_m in zip(map_idxs_all, map_t_slow_all, map_t_fast_all):
                    map_idx_to_tslow[int(idx_m)] = float(ts_m)
                    map_idx_to_tfast[int(idx_m)] = float(tf_m)

            # Delta-Funktion (aus Sync-Map)
            def _delta_at_slow_frame(idx0: int) -> float:
                fps_safe = float(fps) if float(fps) > 0.1 else 30.0
                ii = int(idx0)
                if ii in map_idx_to_tslow and ii in map_idx_to_tfast:
                    return float(map_idx_to_tslow[ii] - map_idx_to_tfast[ii])
                if not slow_frame_to_fast_time_s:
                    return 0.0
                if idx0 < 0:
                    idx0 = 0
                if idx0 >= len(slow_frame_to_fast_time_s):
                    idx0 = len(slow_frame_to_fast_time_s) - 1
                slow_t = float(idx0) / fps_safe
                fast_t = float(slow_frame_to_fast_time_s[idx0])
                return float(slow_t - fast_t)

            # Y-Skalierung wie gewünscht:
            # - wenn kein negatives Delta: 0-Linie unten, nur positive Skala
            # - wenn negatives Delta vorhanden: 0-Linie zwischen min_neg und max_pos
            y_top = float(plot_y0)
            y_bot = float(plot_y1)
            span = max(10.0, (y_bot - y_top))

            range_pos = float(delta_pos_max)
            range_neg = float(abs(delta_neg_min))
            if not delta_has_neg:
                y_zero = y_bot  # 0s ganz unten
                pos_span = max(4.0, (y_zero - y_top))

                def _y_from_delta(dsec: float) -> int:
                    d = float(dsec)
                    if d < 0.0:
                        d = 0.0
                    if d > float(delta_pos_max):
                        d = float(delta_pos_max)
                    yy = y_zero - (d / float(delta_pos_max)) * pos_span
                    return int(round(yy))
            else:
                # min_neg ist negativ, range_neg = abs(min_neg)
                total = max(1e-6, (range_neg + range_pos))

                # 0-Linie so, dass oben Platz für +Delta und unten für -Delta bleibt
                y_zero = y_top + (range_pos / total) * span
                y_zero = max(y_top + 2.0, min(y_bot - 2.0, y_zero))

                pos_span = max(4.0, (y_zero - y_top))
                neg_span = max(4.0, (y_bot - y_zero))

                def _y_from_delta(dsec: float) -> int:
                    d = float(dsec)
                    if d >= 0.0:
                        if d > range_pos:
                            d = range_pos
                        yy = y_zero - (d / range_pos) * pos_span
                    else:
                        ad = abs(d)
                        if ad > range_neg:
                            ad = range_neg
                        yy = y_zero + (ad / range_neg) * neg_span
                    return int(round(yy))

            # 2) Grid (nur oberhalb der dynamischen 0-Linie)
            axis_labels: list[tuple[int, str]] = []
            try:
                pos_max = float(range_pos if delta_has_neg else delta_pos_max)
                step = choose_tick_step(
                    0.0,
                    pos_max,
                    min_segments=2,
                    max_segments=5,
                    target_segments=5,
                )
                if step is not None and pos_max > 1e-9:
                    val_bounds = build_value_boundaries(0.0, pos_max, float(step), anchor="bottom")
                    y_bounds = value_boundaries_to_y(
                        val_bounds,
                        _y_from_delta,
                        int(round(y_top)),
                        int(round(y_zero)),
                    )
                    draw_stripe_grid(
                        dr,
                        int(x0),
                        int(w),
                        int(round(y_top)),
                        int(round(y_zero)),
                        y_bounds,
                        col_bg=COL_HUD_BG,
                        darken_delta=6,
                    )
                    for vv in val_bounds:
                        if should_suppress_boundary_label(float(vv), 0.0, pos_max, suppress_zero=True):
                            continue
                        axis_labels.append(
                            (
                                int(_y_from_delta(float(vv))),
                                format_value_for_step(float(vv), float(step), min_decimals=1, max_decimals=3),
                            )
                        )
            except Exception as e:
                if hud_dbg:
                    try:
                        _log_print(f"[hudpy][Delta][EXC][grid] {type(e).__name__}: {e}", log_file)
                    except Exception:
                        pass

            # 3) 0-Linie (Linie spaeter zeichnen).
            y_mid = int(round(y_zero))

            # 4) Kurve: Anzahl Punkte aus Override, ohne 600-Cap, bis zur HUD-Breite
            span_n = max(1, int(iR) - int(iL))
            pts_target = int(hud_curve_points_default or 180)
            try:
                ovs = hud_curve_points_overrides if isinstance(hud_curve_points_overrides, dict) else None
                if ovs and hud_key in ovs:
                    pts_target = int(float(ovs.get(hud_key) or pts_target))
            except Exception as e:
                if hud_dbg:
                    try:
                        _log_print(f"[hudpy][Delta][EXC][curve_points] {type(e).__name__}: {e}", log_file)
                    except Exception:
                        pass

            if pts_target < 10:
                pts_target = 10

            max_pts = max(10, min(int(w), int(pts_target)))
            stride = max(1, int(round(float(span_n) / float(max_pts))))

            # Debug: Warum wird ggf. keine Kurve sichtbar?
            if hud_dbg:
                try:
                    _log_print(
                        (
                            f"[hudpy][Delta] i={int(i)} iL={int(iL)} iR={int(iR)} "
                            f"span_n={int(span_n)} w={int(w)} pts_target={int(pts_target)} "
                            f"max_pts={int(max_pts)} stride={int(stride)}"
                        ),
                        log_file,
                    )
                    _log_print(
                        (
                            f"[hudpy][Delta] pos_max={float(delta_pos_max):.6f}s "
                            f"neg_min={float(delta_neg_min):.6f}s has_neg={bool(delta_has_neg)}"
                        ),
                        log_file,
                    )
                    _log_print(
                        f"[hudpy][Delta] sync_len={0 if not slow_frame_to_fast_time_s else len(slow_frame_to_fast_time_s)}",
                        log_file,
                    )
                    dL = float(_delta_at_slow_frame(int(iL)))
                    dC = float(_delta_at_slow_frame(int(i)))
                    dR = float(_delta_at_slow_frame(int(iR)))
                    _log_print(
                        f"[hudpy][Delta] samples: dL={dL:+.6f}s dC={dC:+.6f}s dR={dR:+.6f}s",
                        log_file,
                    )
                except Exception as e:
                    try:
                        _log_print(f"[hudpy][Delta][EXC][dbg] {type(e).__name__}: {e}", log_file)
                    except Exception:
                        pass

            sample_rows: list[tuple[int, int]] = []
            if map_idxs_all and len(map_idxs_all) == len(map_offsets_all):
                for idx_m, off_m in zip(map_idxs_all, map_offsets_all):
                    idx_i = int(idx_m)
                    if idx_i < int(iL) or idx_i > int(iR):
                        continue
                    use_point = (idx_i == int(iL)) or (idx_i == int(iR)) or ((int(off_m) % int(stride)) == 0)
                    if use_point:
                        sample_rows.append((idx_i, int(off_m)))

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
                    sample_rows.append((int(idx_fb), int(idx_fb) - int(i)))

            # Segmente nach Vorzeichen einfärben (blau >=0, rot <0)
            seg_pts = []
            seg_col = COL_FAST_DARKBLUE

            def _flush_segment():
                nonlocal seg_pts, seg_col
                if len(seg_pts) >= 2:
                    try:
                        dr.line(seg_pts, fill=seg_col, width=2)
                    except Exception as e:
                        if hud_dbg:
                            try:
                                _log_print(f"[hudpy][Delta][EXC][segment_line] {type(e).__name__}: {e}", log_file)
                            except Exception:
                                pass
                seg_pts = []

            last_sign = None

            for idx, _off in sample_rows:
                x = int(round(_idx_to_x(int(idx))))
                dsec = float(_delta_at_slow_frame(int(idx)))
                y = int(round(_y_from_delta(float(dsec))))

                sign = 1 if dsec >= 0.0 else -1
                col = COL_FAST_DARKBLUE if sign >= 0 else COL_SLOW_DARKRED

                if last_sign is None:
                    last_sign = sign
                    seg_col = col

                if sign != last_sign:
                    _flush_segment()
                    seg_col = col
                    last_sign = sign

                # Punktaufbereitung wie Steering: pro X nur ein Punkt (letzter gewinnt)
                if seg_pts and int(seg_pts[-1][0]) == int(x):
                    seg_pts[-1] = (int(x), int(y))
                else:
                    seg_pts.append((int(x), int(y)))

            _flush_segment()

            # 0-Linie nach der Kurve zeichnen, damit sie durchgehend bleibt.
            try:
                dr.line(
                    [(int(x0), int(y_mid)), (int(x0 + w - 1), int(y_mid))],
                    fill=(COL_SLOW_DARKRED[0], COL_SLOW_DARKRED[1], COL_SLOW_DARKRED[2], 200),
                    width=1,
                )
            except Exception as e:
                if hud_dbg:
                    try:
                        _log_print(f"[hudpy][Delta][EXC][zero_line] {type(e).__name__}: {e}", log_file)
                    except Exception:
                        pass

            # Vertical center marker: draw after curve segments, before text.
            try:
                dr.rectangle([int(mx), int(y0), int(mx + 1), int(y0 + h)], fill=(255, 255, 255, 230))
            except Exception:
                pass

            axis_labels = filter_axis_labels_by_position(
                axis_labels,
                int(round(y_top)),
                int(round(y_zero)),
                zero_y=int(round(y_zero)),
                pad_px=2,
            )
            if axis_labels:
                y_top_label = min(int(y_px) for y_px, _txt in axis_labels)
                axis_labels = [(int(y_px), str(txt)) for (y_px, txt) in axis_labels if int(y_px) != int(y_top_label)]

            # 5) Text (Y-Achse + Titel + aktueller Wert)
            draw_left_axis_labels(
                dr,
                int(x0),
                int(w),
                int(round(y_top)),
                int(round(y_zero)),
                axis_labels,
                font_axis,
                col_text=COL_WHITE,
                x_pad=6,
                fallback_font_obj=font_axis_small,
            )
            try:
                # Visible title is "Time Delta"; HUD key remains "Delta" (API contract).
                draw_text_with_shadow(dr, (int(x0 + 4), int(y0 + 2)), "Time Delta", fill=COL_WHITE, font=font_title)
            except Exception as e:
                if hud_dbg:
                    try:
                        _log_print(f"[hudpy][Delta][EXC][title] {type(e).__name__}: {e}", log_file)
                    except Exception:
                        pass
            try:
                d_cur = float(_delta_at_slow_frame(int(i)))
                col_cur = COL_FAST_DARKBLUE if d_cur >= 0.0 else COL_SLOW_DARKRED

                placeholder = "+999.999s"
                try:
                    bb = dr.textbbox((0, 0), placeholder, font=font_val)
                    w_fix = int(bb[2] - bb[0])
                except Exception:
                    w_fix = int(len(placeholder) * max(6, int(font_val_sz * 0.6)))

                # 1 Zeichen Abstand zum Marker
                x_val = int(mx) - 6 - int(w_fix)
                y_val = int(y0 + 2)

                txt = f"{d_cur:+.3f}s"
                if len(txt) < len(placeholder):
                    txt = txt.rjust(len(placeholder), " ")

                draw_text_with_shadow(dr, (x_val, y_val), txt, fill=col_cur, font=font_val)
            except Exception as e:
                if hud_dbg:
                    try:
                        _log_print(f"[hudpy][Delta][EXC][value_text] {type(e).__name__}: {e}", log_file)
                    except Exception:
                        pass

        except Exception as e:
            try:
                _log_print(
                    f"[hudpy][Delta][EXC][draw] {type(e).__name__}: {e}",
                    log_file,
                )
            except Exception:
                pass
