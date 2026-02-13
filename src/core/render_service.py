from __future__ import annotations

import json
import os
import queue
import math
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from core import persistence
from log import build_log_file_path
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


PREP_TEXT = "Preparing HUDs..."
RENDER_TEXT = "Rendering in progress..."
FINAL_TEXT = "Finalizing..."
DONE_TEXT = "Done."

PROGRESS_FINAL_END = 100.0


def _load_preparing_pct() -> float:
    try:
        raw = persistence._cfg_int("video_compare", "progress_preparing_time", 80)
        pct = int(raw)
    except Exception:
        pct = 80
    if pct < 0:
        pct = 0
    if pct > 100:
        pct = 100
    return float(pct)


def _map_render_progress(pct: float, preparing_pct: float) -> float:
    start = max(0.0, min(100.0, float(preparing_pct)))
    span = max(0.0, PROGRESS_FINAL_END - start)
    mapped = start + (pct / 100.0) * span
    if mapped < start:
        return start
    if mapped > PROGRESS_FINAL_END:
        return PROGRESS_FINAL_END
    return mapped


class _HudPreparingMonitor:
    POLL_INTERVAL = 0.35
    STEP_COUNT = 20

    def __init__(self, project_root: Path, target_pct: float):
        self._target_pct = float(max(0.0, min(100.0, target_pct)))
        self._sync_cache_path = project_root / "output" / "debug" / "sync_cache.json"
        self._expected_frames = 0
        self._stream_written = 0
        self._last_pct = 0.0
        self._last_step = 0
        self._last_check = 0.0
        self._sync_cache_mtime = 0.0
        self._done = False

    def _to_int(self, value: Any) -> int:
        try:
            return int(float(value))
        except Exception:
            return 0

    def _refresh_expected_frames(self) -> None:
        if self._target_pct <= 0.0 or self._done:
            return
        try:
            stat = self._sync_cache_path.stat()
        except Exception:
            return
        mtime = float(stat.st_mtime)
        if mtime <= self._sync_cache_mtime:
            return
        self._sync_cache_mtime = mtime
        try:
            data = json.loads(self._sync_cache_path.read_text(encoding="utf-8"))
        except Exception:
            return
        cut_i0 = self._to_int(data.get("cut_i0", 0))
        cut_i1 = self._to_int(data.get("cut_i1", 0))
        frame_count = self._to_int(data.get("frame_count", 0))
        candidate = max(0, cut_i1 - cut_i0)
        if candidate <= 0 and frame_count > 0:
            candidate = frame_count
        if candidate <= 0:
            candidate = self._to_int(data.get("frames", 0))
        if candidate <= 0:
            return
        self._expected_frames = candidate
        self._last_pct = 0.0
        self._last_step = 0

    def update_stream_written(self, written: int, total: int | None = None) -> None:
        try:
            w = int(written)
        except Exception:
            return
        if w < 0:
            w = 0
        if w > self._stream_written:
            self._stream_written = int(w)
        if total is None:
            return
        try:
            t = int(total)
        except Exception:
            return
        if t > 0 and t > self._expected_frames:
            self._expected_frames = int(t)

    def poll(self, now: float) -> tuple[float | None, bool]:
        if self._done or self._target_pct <= 0.0:
            return None, False
        if (now - self._last_check) < self.POLL_INTERVAL:
            return None, False
        self._last_check = now
        self._refresh_expected_frames()
        if self._expected_frames <= 0:
            return None, False
        current = int(self._stream_written)
        if current <= 0:
            return None, False
        if current > self._expected_frames:
            current = self._expected_frames
        step = int(current * self.STEP_COUNT // self._expected_frames)
        if current >= self._expected_frames:
            step = self.STEP_COUNT
        step = max(0, min(self.STEP_COUNT, step))
        if step <= self._last_step:
            return None, False
        self._last_step = step
        frac = float(step) / float(self.STEP_COUNT)
        pct = frac * self._target_pct
        if pct < self._last_pct:
            pct = self._last_pct
        self._last_pct = pct
        if step >= self.STEP_COUNT:
            self._done = True
            return pct, True
        return pct, False


def _write_duration_line(log_file: Path | None, step: str, seconds: float) -> None:
    if log_file is None:
        return
    try:
        line = f"[Duration][{step}] {seconds:.3f}s"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _log_stage_durations(
    log_file: Path | None,
    start: float,
    prep_end: float | None,
    render_start: float | None,
    render_end: float | None,
    final_end: float,
) -> None:
    if log_file is None:
        return
    try:
        if prep_end is not None:
            _write_duration_line(log_file, "Preparing HUDs", max(0.0, prep_end - start))
        if render_start is not None and render_end is not None:
            _write_duration_line(log_file, "Rendering", max(0.0, render_end - render_start))
        if render_end is not None and final_end is not None and final_end >= render_end:
            _write_duration_line(log_file, "Finalizing", max(0.0, final_end - render_end))
        _write_duration_line(log_file, "Total", max(0.0, final_end - start))
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

    pedals_sample_mode = str(persistence.cfg_get("video_compare", "hud_pedals_sample_mode", "time")).strip().lower()
    if pedals_sample_mode not in ("time", "legacy"):
        pedals_sample_mode = "time"

    pedals_abs_debounce_ms = persistence._cfg_int("video_compare", "hud_pedals_abs_debounce_ms", 60)
    if pedals_abs_debounce_ms < 0:
        pedals_abs_debounce_ms = 0
    if pedals_abs_debounce_ms > 500:
        pedals_abs_debounce_ms = 500

    hud_win_default_before = persistence._cfg_float("video_compare", "hud_window_default_before_s", 10.0)
    hud_win_default_after = persistence._cfg_float("video_compare", "hud_window_default_after_s", 10.0)

    # Story 4.2: per-HUD window INI overrides are ignored; only global defaults are active.
    hud_win_overrides: dict[str, dict[str, float]] = {}

    hud_pts_default = persistence._cfg_int("video_compare", "hud_curve_points_default", 180)
    under_oversteer_curve_center = persistence._cfg_float("video_compare", "under_oversteer_curve_center", 0.0)
    if under_oversteer_curve_center < -50.0:
        under_oversteer_curve_center = -50.0
    if under_oversteer_curve_center > 50.0:
        under_oversteer_curve_center = 50.0

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
            "under_oversteer_curve_center": float(under_oversteer_curve_center),
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
        "hud_pedals": {
            "sample_mode": str(pedals_sample_mode),
            "abs_debounce_ms": int(pedals_abs_debounce_ms),
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
        hud_pedals=payload.get("hud_pedals", {}),
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
    log_file_path: Path | None = None
    try:
        log_file_path = build_log_file_path(project_root, name="video_compare")
    except Exception:
        log_file_path = None

    start_time = time.time()
    prep_end: float | None = None
    render_start: float | None = None
    render_end: float | None = None
    final_end: float | None = None

    preparing_pct = _load_preparing_pct()
    preparing_done = preparing_pct <= 0.0
    hud_monitor = _HudPreparingMonitor(project_root, target_pct=preparing_pct)
    if preparing_done:
        prep_end = start_time
        render_start = start_time
        _emit_progress(on_progress, 0.0, RENDER_TEXT)
    else:
        _emit_progress(on_progress, 0.0, PREP_TEXT)

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

    env = os.environ.copy()
    if log_file_path is not None:
        env["IRVC_LOG_FILE"] = str(log_file_path)

    p = None
    try:
        cancelled = False
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
            env=env,
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
        last_pct = float(preparing_pct if preparing_done else 0.0)

        time_re = re.compile(r"time=(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?")
        out_time_ms_re = re.compile(r"out_time_ms=(\d+)")
        out_time_re = re.compile(r"out_time=(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?")
        hud_stream_re = re.compile(r"hud_stream_frame=(\d+)(?:/(\d+))?")

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
                cancelled = True
                _emit_progress(on_progress, max(0.0, last_pct), "Canceling…")
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

                m4 = hud_stream_re.search(line)
                if m4:
                    try:
                        written = int(m4.group(1))
                    except Exception:
                        written = 0
                    try:
                        total = int(m4.group(2)) if m4.group(2) is not None else None
                    except Exception:
                        total = None
                    hud_monitor.update_stream_written(written, total)

            now = time.time()
            if not preparing_done:
                prep_pct, finished = hud_monitor.poll(now)
                if prep_pct is not None:
                    _emit_progress(on_progress, float(prep_pct), PREP_TEXT)
                    if prep_pct > last_pct:
                        last_pct = float(prep_pct)
                if finished:
                    preparing_done = True
                    prep_end = now
                    if render_start is None:
                        render_start = now
                    _emit_progress(on_progress, float(preparing_pct), RENDER_TEXT)
                    last_pct = float(preparing_pct)
            if preparing_done and total_sec > 0 and preparing_pct < PROGRESS_FINAL_END and (now - last_ui_update) >= 1.0:
                pct = (last_sec / total_sec) * 100.0
                if pct < 0.0:
                    pct = 0.0
                if pct > 100.0:
                    pct = 100.0

                overall_pct = _map_render_progress(pct, preparing_pct)
                if abs(overall_pct - last_pct) >= 0.5 or overall_pct >= PROGRESS_FINAL_END:
                    _emit_progress(on_progress, float(overall_pct), f"{RENDER_TEXT} {pct:.0f}%")
                    last_pct = overall_pct

                last_ui_update = now

        if not preparing_done and preparing_pct > 0.0:
            now = time.time()
            prep_end = prep_end or now
            render_start = render_start or now
            preparing_done = True
            _emit_progress(on_progress, float(preparing_pct), RENDER_TEXT)
            last_pct = float(preparing_pct)

        render_end = time.time()
        cancelled = is_cancelled is not None and is_cancelled()
        if not cancelled:
            _emit_progress(on_progress, PROGRESS_FINAL_END, FINAL_TEXT)

        try:
            if p is not None:
                p.wait(timeout=5)
        except Exception:
            pass

        if cancelled:
            return {"status": "cancelled"}

        final_end = time.time()
        _emit_progress(on_progress, PROGRESS_FINAL_END, DONE_TEXT)
        return {"status": "ok"}
    except Exception:
        return {"status": "error", "error": "render_failed"}
    finally:
        try:
            if p is not None and p.poll() is None:
                p.kill()
        except Exception:
            pass
        final_end = final_end or time.time()
        if render_end is None:
            render_end = final_end
        _log_stage_durations(log_file_path, start_time, prep_end, render_start, render_end, final_end)
