from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from core import persistence
from core.models import AppModel, OutputFormat, RenderPayload


TIME_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{3})")


def _extract_time_ms(path: Path) -> int | None:
    m = TIME_RE.search(path.name)
    if not m:
        return None
    mm, ss, ms = m.groups()
    return int(mm) * 60_000 + int(ss) * 1_000 + int(ms)


def _emit_progress(
    on_progress: Callable[[float, str], None] | None,
    pct: float,
    text: str,
) -> None:
    if on_progress is None:
        return
    try:
        on_progress(float(pct), str(text))
    except Exception:
        pass


def build_payload(
    *,
    videos: list[Path],
    csvs: list[Path],
    slow_p: Path,
    fast_p: Path,
    out_path: Path,
    out_aspect: str,
    out_preset: str,
    out_quality: str,
    hud_w: int,
    hud_enabled: dict[str, bool],
    app_model: AppModel,
    get_hud_boxes_for_current: Callable[[], list[dict]],
    png_save_state_for_current: Callable[[], None],
    png_view_key: Callable[[], str],
    png_state: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    vnames = [p.name for p in videos[:2]]
    cnames = [p.name for p in csvs[:2]]

    gear_rpm_update_hz = persistence._cfg_int("video_compare", "gear_rpm_update_hz", 60)
    if gear_rpm_update_hz < 1:
        gear_rpm_update_hz = 1
    if gear_rpm_update_hz > 60:
        gear_rpm_update_hz = 60

    speed_units = str(persistence.cfg_get("video_compare", "speed_units", "kmh")).strip().lower()
    if speed_units not in ("kmh", "mph"):
        speed_units = "kmh"

    speed_update_hz = persistence._cfg_int("video_compare", "speed_update_hz", 60)
    if speed_update_hz < 1:
        speed_update_hz = 1
    if speed_update_hz > 60:
        speed_update_hz = 60

    hud_win_default_before = persistence._cfg_float("video_compare", "hud_window_default_before_s", 10.0)
    hud_win_default_after = persistence._cfg_float("video_compare", "hud_window_default_after_s", 10.0)

    hud_win_overrides: dict[str, dict[str, float]] = {}

    def _add_override(hud_name: str, ini_prefix: str) -> None:
        b = persistence._cfg_float_opt("video_compare", f"hud_window_{ini_prefix}_before_s")
        a = persistence._cfg_float_opt("video_compare", f"hud_window_{ini_prefix}_after_s")
        if b is None and a is None:
            return
        d: dict[str, float] = {}
        if b is not None:
            d["before_s"] = float(b)
        if a is not None:
            d["after_s"] = float(a)
        hud_win_overrides[hud_name] = d

    _add_override("Throttle / Brake", "throttle_brake")
    _add_override("Steering", "steering")
    _add_override("Delta", "delta")
    _add_override("Line Delta", "line_delta")
    _add_override("Under-/Oversteer", "under_oversteer")

    hud_pts_default = persistence._cfg_int("video_compare", "hud_curve_points_default", 180)

    hud_pts_overrides: dict[str, int] = {}

    def _add_pts_override(hud_name: str, ini_suffix: str) -> None:
        v = persistence._cfg_int_opt("video_compare", f"hud_curve_points_{ini_suffix}")
        if v is None:
            return
        hud_pts_overrides[hud_name] = int(v)

    _add_pts_override("Throttle / Brake", "throttle_brake")
    _add_pts_override("Steering", "steering")
    _add_pts_override("Delta", "delta")
    _add_pts_override("Line Delta", "line_delta")
    _add_pts_override("Under-/Oversteer", "under_oversteer")

    payload = {
        "version": 1,
        "videos": vnames,
        "csvs": cnames,
        "slow_video": str(slow_p),
        "fast_video": str(fast_p),
        "out_video": str(out_path),
        "output": {
            "aspect": str(out_aspect),
            "preset": str(out_preset),
            "quality": str(out_quality),
            "hud_width_px": int(hud_w),
        },
        "hud_enabled": hud_enabled,
        "hud_boxes": {},
        "hud_window": {
            "default_before_s": float(hud_win_default_before),
            "default_after_s": float(hud_win_default_after),
            "overrides": hud_win_overrides,
        },
        "hud_speed": {
            "units": str(speed_units),
            "update_hz": int(speed_update_hz),
        },
        "hud_curve_points": {
            "default": int(hud_pts_default),
            "overrides": hud_pts_overrides,
        },
        "hud_gear_rpm": {
            "update_hz": int(gear_rpm_update_hz),
        },
        "png_view_key": "",
        "png_view_state": {"L": {}, "R": {}},
        "hud_layout_data": app_model.hud_layout.hud_layout_data,
        "png_view_data": app_model.png_view.png_view_data,
    }
    payload = RenderPayload(
        version=payload.get("version", 1),
        videos=payload.get("videos", []),
        csvs=payload.get("csvs", []),
        slow_video=payload.get("slow_video", ""),
        fast_video=payload.get("fast_video", ""),
        out_video=payload.get("out_video", ""),
        output=OutputFormat.from_dict(payload.get("output") if isinstance(payload.get("output"), dict) else {}),
        hud_enabled=payload.get("hud_enabled", {}),
        hud_boxes=payload.get("hud_boxes", {}),
        hud_window=payload.get("hud_window", {}),
        hud_speed=payload.get("hud_speed", {}),
        hud_curve_points=payload.get("hud_curve_points", {}),
        hud_gear_rpm=payload.get("hud_gear_rpm", {}),
        png_view_key=payload.get("png_view_key", ""),
        png_view_state=payload.get("png_view_state", {"L": {}, "R": {}}),
        hud_layout_data=payload.get("hud_layout_data", {}),
        png_view_data=payload.get("png_view_data", {}),
    ).to_dict()

    try:
        boxes_list = get_hud_boxes_for_current()
        boxes_map = {}
        for b in boxes_list:
            if not isinstance(b, dict):
                continue
            t = str(b.get("type") or "").strip()
            if not t:
                continue
            boxes_map[t] = {
                "x": int(b.get("x") or 0),
                "y": int(b.get("y") or 0),
                "w": int(b.get("w") or 0),
                "h": int(b.get("h") or 0),
            }
        payload["hud_boxes"] = boxes_map
    except Exception:
        pass

    try:
        hb = payload.get("hud_boxes") or {}
        if isinstance(hb, dict):
            for hud_name, v in list(hud_pts_overrides.items()):
                try:
                    vv = int(v)
                except Exception:
                    continue

                if vv == 0:
                    box = hb.get(hud_name) or {}
                    try:
                        w = int(box.get("w") or 0)
                    except Exception:
                        w = 0

                    if w > 0:
                        hud_pts_overrides[hud_name] = int(w)
                    else:
                        try:
                            del hud_pts_overrides[hud_name]
                        except Exception:
                            pass
    except Exception:
        pass

    try:
        cp = payload.get("hud_curve_points")
        if isinstance(cp, dict):
            cp["overrides"] = hud_pts_overrides
    except Exception:
        pass

    try:
        png_save_state_for_current()
        payload["png_view_key"] = str(png_view_key())
        payload["png_view_state"] = {
            "L": {
                "zoom": float(png_state["L"].get("zoom", 1.0)),
                "off_x": int(png_state["L"].get("off_x", 0)),
                "off_y": int(png_state["L"].get("off_y", 0)),
                "fit_to_height": bool(png_state["L"].get("fit_to_height", False)),
            },
            "R": {
                "zoom": float(png_state["R"].get("zoom", 1.0)),
                "off_x": int(png_state["R"].get("off_x", 0)),
                "off_y": int(png_state["R"].get("off_y", 0)),
                "fit_to_height": bool(png_state["R"].get("fit_to_height", False)),
            },
        }
    except Exception:
        pass

    return payload


def start_render(
    *,
    project_root: Path,
    videos: list[Path],
    csvs: list[Path],
    slow_p: Path,
    fast_p: Path,
    out_path: Path,
    out_aspect: str,
    out_preset: str,
    out_quality: str,
    hud_w: int,
    hud_enabled: dict[str, bool],
    app_model: AppModel,
    get_hud_boxes_for_current: Callable[[], list[dict]],
    png_save_state_for_current: Callable[[], None],
    png_view_key: Callable[[], str],
    png_state: dict[str, dict[str, Any]],
    is_cancelled: Callable[[], bool] | None = None,
    on_progress: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    run_json_path = project_root / "config" / "ui_last_run.json"
    try:
        run_json_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    payload = build_payload(
        videos=videos,
        csvs=csvs,
        slow_p=slow_p,
        fast_p=fast_p,
        out_path=out_path,
        out_aspect=out_aspect,
        out_preset=out_preset,
        out_quality=out_quality,
        hud_w=hud_w,
        hud_enabled=hud_enabled,
        app_model=app_model,
        get_hud_boxes_for_current=get_hud_boxes_for_current,
        png_save_state_for_current=png_save_state_for_current,
        png_view_key=png_view_key,
        png_state=png_state,
    )

    try:
        run_json_path.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )
    except Exception:
        return {"status": "error", "error": "ui_json_write_failed"}

    main_py = project_root / "src" / "main.py"
    if not main_py.exists():
        return {"status": "error", "error": "main_py_not_found"}

    p = None
    try:
        _emit_progress(on_progress, 0.0, "main.py l\u00e4uft\u2026 (Abbruch m\u00f6glich)")

        total_ms_a = _extract_time_ms(slow_p) or 0
        total_ms_b = _extract_time_ms(fast_p) or 0
        total_ms = int(max(total_ms_a, total_ms_b))
        total_sec = float(total_ms) / 1000.0 if total_ms > 0 else 0.0

        import sys as _sys

        cmd = [_sys.executable, "-u", str(main_py), "--ui-json", str(run_json_path)]
        p = subprocess.Popen(
            cmd,
            cwd=str(project_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        q_lines: "queue.Queue[str]" = queue.Queue()

        def _reader(stream) -> None:
            try:
                if stream is None:
                    return
                for raw in stream:
                    q_lines.put(raw)
            except Exception:
                pass

        threading.Thread(target=_reader, args=(p.stdout,), daemon=True).start()

        last_ui_update = 0.0
        last_sec = 0.0
        last_pct = -1.0

        time_re = re.compile(r"time=(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?")
        out_time_ms_re = re.compile(r"out_time_ms=(\d+)")
        out_time_re = re.compile(r"out_time=(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?")

        show_live = (os.environ.get("IRVC_UI_SHOW_LOG") or "").strip() == "1"

        dbg_max_s = 0.0
        try:
            dbg_max_s = float((os.environ.get("IRVC_DEBUG_MAX_S") or "").strip() or "0")
        except Exception:
            dbg_max_s = 0.0
        if dbg_max_s > 0.0 and total_sec > 0:
            total_sec = min(total_sec, dbg_max_s)

        while True:
            if is_cancelled is not None and is_cancelled():
                _emit_progress(on_progress, max(0.0, last_pct), "Abbruch\u2026")
                try:
                    if p is not None and p.pid:
                        subprocess.run(
                            ["taskkill", "/PID", str(p.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False,
                        )
                except Exception:
                    pass
                break

            rc = p.poll()
            if rc is not None and q_lines.empty():
                break

            try:
                line = q_lines.get(timeout=0.1)
            except Exception:
                line = ""

            if line:
                if show_live:
                    try:
                        print(line, end="", flush=True)
                    except Exception:
                        pass

                m = time_re.search(line)
                if m:
                    try:
                        hh = int(m.group(1))
                        mm = int(m.group(2))
                        ss = int(m.group(3))
                        frac = m.group(4) or "0"
                        frac = (frac + "000")[:3]
                        ms = int(frac)
                        sec = hh * 3600.0 + mm * 60.0 + ss + (ms / 1000.0)
                        if sec > last_sec:
                            last_sec = sec
                    except Exception:
                        pass

                m2 = out_time_ms_re.search(line)
                if m2:
                    try:
                        sec = float(int(m2.group(1))) / 1000000.0
                        if sec > last_sec:
                            last_sec = sec
                    except Exception:
                        pass

                m3 = out_time_re.search(line)
                if m3:
                    try:
                        hh = int(m3.group(1))
                        mm = int(m3.group(2))
                        ss = int(m3.group(3))
                        frac = m3.group(4) or "0"
                        frac = (frac + "000000")[:6]
                        us = int(frac[:6])
                        sec = hh * 3600.0 + mm * 60.0 + ss + (us / 1000000.0)
                        if sec > last_sec:
                            last_sec = sec
                    except Exception:
                        pass

            now = time.time()
            if total_sec > 0 and (now - last_ui_update) >= 1.0:
                pct = (last_sec / total_sec) * 100.0
                if pct < 0.0:
                    pct = 0.0
                if pct > 100.0:
                    pct = 100.0

                if abs(pct - last_pct) >= 0.5 or pct >= 100.0:
                    _emit_progress(on_progress, float(pct), f"Render l\u00e4uft\u2026 {pct:.0f}%")
                    last_pct = pct

                last_ui_update = now

        cancelled = is_cancelled is not None and is_cancelled()
        if not cancelled:
            _emit_progress(on_progress, 100.0, "")

        try:
            if p is not None:
                p.wait(timeout=5)
        except Exception:
            pass

        if cancelled:
            return {"status": "cancelled"}
        return {"status": "ok"}
    except Exception:
        return {"status": "error", "error": "render_failed"}
    finally:
        try:
            if p is not None and p.poll() is None:
                p.kill()
        except Exception:
            pass
