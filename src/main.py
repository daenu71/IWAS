from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Tuple

from cfg import load_cfg
from log import make_logger
from core.models import LayoutConfig, migrate_layout_contract_dict
from render_split import render_split_screen, render_split_screen_sync
from csv_g61 import get_float_col, load_g61_csv
from resample_lapdist import build_lapdist_grid, resample_run_linear
from sync_map import build_sync_map_by_lapdist


TIME_RE = re.compile(r"(\d{2}\.\d{2}\.\d{3})")


def _time_to_ms(t: str) -> int:
    mm, ss, ms = t.split(".")
    return int(mm) * 60_000 + int(ss) * 1_000 + int(ms)


def _extract_time_from_name(p: Path) -> Tuple[str, int]:
    m = TIME_RE.search(p.name)
    if not m:
        raise ValueError(f"Keine Zeit im Dateinamen gefunden: {p.name}")
    t = m.group(1)
    return t, _time_to_ms(t)


def _load_ui_json(p: Path) -> tuple[dict, bool]:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}, False
        migrated = migrate_layout_contract_dict(data)
        return data, migrated
    except Exception:
        return {}, False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ui-json", default="", help="UI Übergabe (JSON)")
    args = ap.parse_args()

    project_root = Path(__file__).resolve().parents[1]

    cfg = load_cfg(project_root)
    log = make_logger(project_root, name="video_compare")

    log.msg("iracing-video-compare start")

    ui = {}
    ui_json = (args.ui_json or "").strip()
    if ui_json:
        ui_path = Path(ui_json).resolve()
        ui, migrated_layout = _load_ui_json(ui_path)
        log.kv("ui_json", str(ui_path))
        debug_swallowed = str(os.environ.get("IRVC_DEBUG_SWALLOWED", "") or "").strip().lower() in ("1", "true", "yes", "on")
        if migrated_layout and debug_swallowed:
            log.msg("LayoutConfig: migrated legacy JSON (missing keys) -> defaults applied.")

    try:
        out = ui.get("output") if isinstance(ui, dict) else {}
        if isinstance(out, dict):
            log.kv("ui_output_aspect", str(out.get("aspect", "")))
            log.kv("ui_output_preset", str(out.get("preset", "")))
            log.kv("ui_output_quality", str(out.get("quality", "")))
            log.kv("ui_hud_width_px_ui", str(out.get("hud_width_px", "")))
    except Exception:
        pass

    try:
        res = ui.get("output_resolution") if isinstance(ui, dict) else {}
        if isinstance(res, dict):
            log.kv("ui_out_w", str(res.get("w", "")))
            log.kv("ui_out_h", str(res.get("h", "")))
    except Exception:
        pass

    try:
        log.kv("ui_slow_video", str(ui.get("slow_video", "")))
        log.kv("ui_fast_video", str(ui.get("fast_video", "")))
        log.kv("ui_out_video", str(ui.get("out_video", "")))
        log.kv("ui_slow_csv", str(ui.get("slow_csv", "")))
        log.kv("ui_fast_csv", str(ui.get("fast_csv", "")))
    except Exception:
        pass

    try:
        # PNG: bevorzugt neuer Key "png_view_state"
        png_state = ui.get("png_view_state") if isinstance(ui, dict) else {}
        if not isinstance(png_state, dict) or ("L" not in png_state and "R" not in png_state):
            # Fallback: altes Format
            png_state = ui.get("png_view_data") if isinstance(ui, dict) else {}

        if isinstance(png_state, dict):
            s = png_state.get("L") or png_state.get("slow") or {}
            f = png_state.get("R") or png_state.get("fast") or {}

            if isinstance(s, dict):
                log.kv(
                    "ui_png_slow",
                    f"zoom={s.get('zoom')},offx={s.get('off_x')},offy={s.get('off_y')},fit={s.get('fit_to_height')}",
                )
            if isinstance(f, dict):
                log.kv(
                    "ui_png_fast",
                    f"zoom={f.get('zoom')},offx={f.get('off_x')},offy={f.get('off_y')},fit={f.get('fit_to_height')}",
                )

        # Optional: Key mitloggen
        try:
            log.kv("ui_png_view_key", str(ui.get("png_view_key", "")))
        except Exception:
            pass
    except Exception:
        pass

    try:
        # HUD enabled
        enabled = ui.get("hud_enabled") if isinstance(ui, dict) else {}
        if isinstance(enabled, dict):
            on = [k for k, v in enabled.items() if bool(v)]
            off = [k for k, v in enabled.items() if not bool(v)]
            log.kv("ui_hud_enabled_on", ",".join(on))
            log.kv("ui_hud_enabled_off", ",".join(off))
    except Exception:
        pass

    try:
        # HUD Boxen: bevorzugt aktueller Key "hud_boxes"
        boxes = ui.get("hud_boxes") if isinstance(ui, dict) else {}
        if not isinstance(boxes, dict):
            boxes = {}

        log.kv("ui_hud_boxes_count", str(len(boxes)))
        for name, b in boxes.items():
            if isinstance(b, dict):
                log.kv(
                    f"ui_hud_box_{name}",
                    f"x={b.get('x')},y={b.get('y')},w={b.get('w')},h={b.get('h')}",
                )
    except Exception:
        pass
    
    # Defaults aus cfg, optional überschrieben durch UI
    # Regel: UI gewinnt immer (ui["output"]["hud_width_px"])
    hud_width_px = int(getattr(cfg, "hud_width_px", 0) or 0)

    # 1) Neu: UI-Output-Block (korrekt)
    try:
        out = ui.get("output") if isinstance(ui, dict) else {}
        if isinstance(out, dict):
            v = out.get("hud_width_px", None)
            if v is not None:
                vv = int(float(v) or 0)
                if vv > 0:
                    hud_width_px = vv
    except Exception:
        pass

    # 2) Fallback: altes UI-Format (Top-Level) – nur wenn oben nichts gesetzt wurde
    try:
        if hud_width_px <= 0 and isinstance(ui, dict) and ("hud_width_px" in ui):
            vv = int(float(ui.get("hud_width_px") or 0))
            if vv > 0:
                hud_width_px = vv
    except Exception:
        pass

    log.kv("hud_width_px", hud_width_px)

    # Output Pfad
    out_video = project_root / "output" / "video" / "slow_fast_split.mp4"
    try:
        ov = (ui.get("out_video") or "").strip()
        if ov:
            out_video = Path(ov).resolve()
    except Exception:
        pass
    log.kv("out_video", str(out_video))

    # Videos: bevorzugt aus UI, sonst aus input/video + Matching
    slow_video = None
    fast_video = None
    slow_csv = None
    fast_csv = None

    try:
        sv = (ui.get("slow_video") or "").strip()
        fv = (ui.get("fast_video") or "").strip()
        if sv and fv:
            slow_video = Path(sv).resolve()
            fast_video = Path(fv).resolve()
    except Exception:
        slow_video = None
        fast_video = None

    if slow_video is None or fast_video is None:
        video_dir = project_root / "input" / "video"
        csv_dir = project_root / "input" / "csv"

        videos = sorted(video_dir.glob("*.mp4"))
        csvs = sorted(csv_dir.glob("*.csv"))

        if len(videos) < 2:
            raise RuntimeError("Es muessen mindestens zwei Videos vorhanden sein.")
        if len(csvs) < 2:
            raise RuntimeError("Es muessen mindestens zwei CSVs vorhanden sein.")

        pairs = []
        for v in videos:
            base = v.stem
            for c in csvs:
                if c.stem == base:
                    pairs.append((v, c))

        if len(pairs) != 2:
            raise RuntimeError(f"Erwartet genau 2 Video/CSV-Paare, gefunden: {len(pairs)}")

        (v1, c1), (v2, c2) = pairs

        _t1_str, t1_ms = _extract_time_from_name(v1)
        _t2_str, t2_ms = _extract_time_from_name(v2)

        if t1_ms == t2_ms:
            raise RuntimeError("Zeiten sind identisch, kein fast/slow moeglich.")

        if t1_ms < t2_ms:
            fast_video, slow_video = v1, v2
            fast_csv, slow_csv = c1, c2
        else:
            fast_video, slow_video = v2, v1
            fast_csv, slow_csv = c2, c1

    if slow_video is None or fast_video is None:
        raise RuntimeError("slow/fast Video fehlt.")

    log.msg("Paar erkannt")
    log.kv("slow_video", slow_video.name)
    log.kv("fast_video", fast_video.name)
    
    # CSVs zuordnen (UI bevorzugt, sonst aus Auto-Matching, sonst aus ui["csvs"])
    try:
        csv_dir = project_root / "input" / "csv"

        def _resolve_csv(p: str) -> Path:
            s = (p or "").strip()
            if not s:
                return Path()
            pp = Path(s)
            if pp.is_absolute():
                return pp.resolve()
            return (csv_dir / pp.name).resolve()

        # 1) Direkte Keys (falls vorhanden)
        sc = (ui.get("slow_csv") or "").strip() if isinstance(ui, dict) else ""
        fc = (ui.get("fast_csv") or "").strip() if isinstance(ui, dict) else ""
        if sc and fc:
            slow_csv = _resolve_csv(sc)
            fast_csv = _resolve_csv(fc)

        # 2) Fallback: ui["csvs"] Liste -> Matching per Dateiname (stem)
        if (slow_csv is None or fast_csv is None) and isinstance(ui, dict):
            csvs = ui.get("csvs")
            if isinstance(csvs, list):
                by_stem: dict[str, Path] = {}
                for item in csvs:
                    if isinstance(item, str) and item.strip():
                        p = _resolve_csv(item)
                        by_stem[p.stem] = p

                if slow_video is not None and slow_video.stem in by_stem:
                    slow_csv = by_stem[slow_video.stem]
                if fast_video is not None and fast_video.stem in by_stem:
                    fast_csv = by_stem[fast_video.stem]
    except Exception:
        pass

    # Wenn Auto-Matching aktiv war, sind slow_csv/fast_csv schon gesetzt
    if slow_csv is not None:
        log.kv("slow_csv", slow_csv.name)
    if fast_csv is not None:
        log.kv("fast_csv", fast_csv.name)

# Story 4: CSV-basierte Vorbereitung (Resample + Sync-Map) als Debug
    try:
        if slow_csv is None or fast_csv is None:
            raise RuntimeError("slow_csv/fast_csv fehlt (keine CSV-Zuordnung).")
        if not slow_csv.exists():
            raise RuntimeError(f"slow_csv Datei nicht gefunden: {slow_csv}")
        if not fast_csv.exists():
            raise RuntimeError(f"fast_csv Datei nicht gefunden: {fast_csv}")

        run_s = load_g61_csv(slow_csv)
        run_f = load_g61_csv(fast_csv)

        ld_s = get_float_col(run_s, "LapDistPct")
        ld_f = get_float_col(run_f, "LapDistPct")

        def _unwrap(xs: list[float]) -> list[float]:
            out: list[float] = []
            add = 0.0
            prev = None
            for v in xs:
                x = float(v)
                if prev is not None and x < prev - 0.5:
                    add += 1.0
                out.append(x + add)
                prev = x
            return out

        ld_su = _unwrap(ld_s)
        ld_fu = _unwrap(ld_f)

        def _force_strictly_increasing(xs: list[float], eps: float = 1e-9) -> list[float]:
            out: list[float] = []
            prev = None
            for x in xs:
                v = float(x)
                if prev is None:
                    out.append(v)
                    prev = v
                else:
                    if v <= prev:
                        v = prev + eps
                    out.append(v)
                    prev = v
            return out

        ld_su = _force_strictly_increasing(ld_su)
        ld_fu = _force_strictly_increasing(ld_fu)

        step = 0.0005
        grid = build_lapdist_grid(ld_su, ld_fu, step=step)

        rs_s = resample_run_linear(
            lapdist_in=ld_su,
            channels_in=run_s.columns,
            lapdist_grid=grid,
            channel_names=["Speed"],
        )
        rs_f = resample_run_linear(
            lapdist_in=ld_fu,
            channels_in=run_f.columns,
            lapdist_grid=grid,
            channel_names=["Speed"],
        )

        sm = build_sync_map_by_lapdist(
            slow_lapdist_by_frame=ld_su,
            fast_lapdist_samples=ld_fu,
        )

        log.msg("csv-sync prep ok")
        log.kv("lapdist_step", step)
        log.kv("lapdist_grid_n", len(grid))
        log.kv("sync_map_n", len(sm.slow_to_fast_idx))

        dbg_dir = project_root / "output" / "debug"
        dbg_dir.mkdir(parents=True, exist_ok=True)
        dbg_path = dbg_dir / "sync_debug.json"

        dbg = {
            "lapdist_step": step,
            "grid_n": len(grid),
            "speed_slow_sample": rs_s.channels["Speed"][:1000],
            "speed_fast_sample": rs_f.channels["Speed"][:1000],
            "sync_map_n": len(sm.slow_to_fast_idx),
            "sync_map_sample": sm.slow_to_fast_idx[:1000],
        }

        dbg_path.write_text(json.dumps(dbg, indent=2), encoding="utf-8")
        log.kv("sync_debug_json", str(dbg_path))

    except Exception as e:
        log.kv("csv_sync_prep_error", str(e))

 # Output-Preset aus UI (z.B. "3840x2160") -> preset_w/preset_h
    preset_w = 0
    preset_h = 0
    try:
        out_cfg = ui.get("output") if isinstance(ui, dict) else {}
        if isinstance(out_cfg, dict):
            s = (out_cfg.get("preset") or "").strip()
            if s and "x" in s:
                a, b = s.lower().split("x", 1)
                preset_w = int(a.strip() or 0)
                preset_h = int(b.strip() or 0)
    except Exception:
        preset_w = 0
        preset_h = 0

    # Log: was main wirklich verwendet
    log.kv("preset_w", str(preset_w))
    log.kv("preset_h", str(preset_h))

    # View-State (Zoom/Offsets) aus UI
    view_L = None
    view_R = None
    try:
        png_state = ui.get("png_view_state") if isinstance(ui, dict) else {}
        if isinstance(png_state, dict):
            view_L = png_state.get("L") if isinstance(png_state.get("L"), dict) else None
            view_R = png_state.get("R") if isinstance(png_state.get("R"), dict) else None
    except Exception:
        view_L = None
        view_R = None

    hud_enabled = ui.get("hud_enabled") if isinstance(ui, dict) else None
    hud_boxes = ui.get("hud_boxes") if isinstance(ui, dict) else None
    layout_config = LayoutConfig.from_dict(ui if isinstance(ui, dict) else {})
    try:
        hud_mode = str(layout_config.hud_mode or "frame").strip().lower()
    except Exception:
        hud_mode = "frame"
    if hud_mode == "free":
        try:
            free_boxes = layout_config.hud_free.boxes_abs_out if isinstance(layout_config.hud_free.boxes_abs_out, dict) else {}
        except Exception:
            free_boxes = {}
        if isinstance(free_boxes, dict) and len(free_boxes) > 0:
            hud_boxes = free_boxes
    
    hud_win = ui.get("hud_window") if isinstance(ui, dict) else None
    hud_win_default_before = 10.0
    hud_win_default_after = 10.0
    under_oversteer_curve_center = 0.0

    try:
        if isinstance(hud_win, dict):
            hud_win_default_before = float(hud_win.get("default_before_s") or 10.0)
            hud_win_default_after = float(hud_win.get("default_after_s") or 10.0)
            under_oversteer_curve_center = float(hud_win.get("under_oversteer_curve_center") or 0.0)
    except Exception:
        pass
    if under_oversteer_curve_center < -50.0:
        under_oversteer_curve_center = -50.0
    if under_oversteer_curve_center > 50.0:
        under_oversteer_curve_center = 50.0

    log.kv("hud_win_default_before_s", str(hud_win_default_before))
    log.kv("hud_win_default_after_s", str(hud_win_default_after))
    log.kv("under_oversteer_curve_center", str(under_oversteer_curve_center))

    hud_pts = ui.get("hud_curve_points") if isinstance(ui, dict) else None
    hud_pts_default = 180
    hud_pts_overrides = None

    try:
        if isinstance(hud_pts, dict):
            hud_pts_default = int(float(hud_pts.get("default") or 180))
            ovs = hud_pts.get("overrides")
            hud_pts_overrides = ovs if isinstance(ovs, dict) else None
    except Exception:
        pass

    log.kv("hud_curve_points_default", str(hud_pts_default))
   
    # Story 6: Gear & RPM HUD (Update-Rate)
    hud_gear_rpm_update_hz = 60
    try:
        gr = ui.get("hud_gear_rpm") if isinstance(ui, dict) else None
        if isinstance(gr, dict):
            hz = int(float(gr.get("update_hz") or 60))
            if hz < 1:
                hz = 1
            if hz > 60:
                hz = 60
            hud_gear_rpm_update_hz = hz
    except Exception:
        pass

    log.kv("hud_gear_rpm_update_hz", str(hud_gear_rpm_update_hz))
   
    # Story 5: Speed HUD (Einheit + Update-Rate)
    hud_speed_units = "kmh"
    hud_speed_update_hz = 60

    try:
        hs = ui.get("hud_speed") if isinstance(ui, dict) else None
        if isinstance(hs, dict):
            u = str(hs.get("units") or "kmh").strip().lower()
            if u in ("kmh", "mph"):
                hud_speed_units = u

            hz = int(float(hs.get("update_hz") or 60))
            if hz < 1:
                hz = 1
            if hz > 60:
                hz = 60
            hud_speed_update_hz = hz
    except Exception:
        pass

    log.kv("hud_speed_units", str(hud_speed_units))
    log.kv("hud_speed_update_hz", str(hud_speed_update_hz))

    hud_pedals_sample_mode = "time"
    hud_pedals_abs_debounce_ms = 60
    hud_max_brake_delay_distance = 0.003
    hud_max_brake_delay_pressure = 35.0
    try:
        hp = ui.get("hud_pedals") if isinstance(ui, dict) else None
        if isinstance(hp, dict):
            sm = str(hp.get("sample_mode") or "time").strip().lower()
            if sm in ("time", "legacy"):
                hud_pedals_sample_mode = sm

            ms = int(float(hp.get("abs_debounce_ms") or 60))
            if ms < 0:
                ms = 0
            if ms > 500:
                ms = 500
            hud_pedals_abs_debounce_ms = ms

            d_raw = hp.get("max_brake_delay_distance")
            if d_raw is not None:
                dd = float(d_raw)
                if dd != dd:  # NaN guard
                    dd = 0.003
                if dd < 0.0:
                    dd = 0.0
                if dd > 1.0:
                    dd = 1.0
                hud_max_brake_delay_distance = float(dd)

            p_raw = hp.get("max_brake_delay_pressure")
            if p_raw is not None:
                dp = float(p_raw)
                if dp != dp:  # NaN guard
                    dp = 35.0
                if dp < 0.0:
                    dp = 0.0
                if dp > 100.0:
                    dp = 100.0
                hud_max_brake_delay_pressure = float(dp)
    except Exception:
        pass

    log.kv("hud_pedals_sample_mode", str(hud_pedals_sample_mode))
    log.kv("hud_pedals_abs_debounce_ms", str(hud_pedals_abs_debounce_ms))
    log.kv("hud_max_brake_delay_distance", str(hud_max_brake_delay_distance))
    log.kv("hud_max_brake_delay_pressure", str(hud_max_brake_delay_pressure))

    if slow_csv and fast_csv:
        render_split_screen_sync(
            slow=slow_video,
            fast=fast_video,
            slow_csv=slow_csv,
            fast_csv=fast_csv,
            outp=out_video,
            start_s=0.0,
            duration_s=0.0,
            preset_w=int(preset_w),
            preset_h=int(preset_h),
            hud_width_px=int(hud_width_px),
            view_L=view_L,
            view_R=view_R,
            audio_source="none",
            hud_enabled=hud_enabled,
            hud_boxes=hud_boxes,
            hud_window_default_before_s=float(hud_win_default_before),
            hud_window_default_after_s=float(hud_win_default_after),
            hud_window_overrides=None,
            hud_gear_rpm_update_hz=int(hud_gear_rpm_update_hz),
            hud_curve_points_default=int(hud_pts_default),
            hud_curve_points_overrides=hud_pts_overrides,
            hud_speed_units=str(hud_speed_units),
            hud_speed_update_hz=int(hud_speed_update_hz),
            hud_pedals_sample_mode=str(hud_pedals_sample_mode),
            hud_pedals_abs_debounce_ms=int(hud_pedals_abs_debounce_ms),
            hud_max_brake_delay_distance=float(hud_max_brake_delay_distance),
            hud_max_brake_delay_pressure=float(hud_max_brake_delay_pressure),
            under_oversteer_curve_center=float(under_oversteer_curve_center),
            layout_config=layout_config,
            log_file=log.log_file,
        )
    else:
        render_split_screen(
            slow=slow_video,
            fast=fast_video,
            outp=out_video,
            start_s=0.0,
            duration_s=0.0,
            preset_w=int(preset_w),
            preset_h=int(preset_h),
            hud_width_px=int(hud_width_px),
            view_L=view_L,
            view_R=view_R,
            audio_source="none",
            hud_enabled=hud_enabled,
            hud_boxes=hud_boxes,
            hud_window_default_before_s=float(hud_win_default_before),
            hud_window_default_after_s=float(hud_win_default_after),
            hud_window_overrides=None,
            hud_gear_rpm_update_hz=int(hud_gear_rpm_update_hz),
            hud_curve_points_default=int(hud_pts_default),
            hud_curve_points_overrides=hud_pts_overrides,
            hud_speed_units=str(hud_speed_units),
            hud_speed_update_hz=int(hud_speed_update_hz),
            hud_pedals_sample_mode=str(hud_pedals_sample_mode),
            hud_pedals_abs_debounce_ms=int(hud_pedals_abs_debounce_ms),
            hud_max_brake_delay_distance=float(hud_max_brake_delay_distance),
            hud_max_brake_delay_pressure=float(hud_max_brake_delay_pressure),
            layout_config=layout_config,
            log_file=log.log_file,
        )
    log.msg("render done")

if __name__ == "__main__":
    main()
