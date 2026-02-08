from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from encoders import (
    build_encode_specs,
    detect_available_encoders,
    run_encode_with_fallback,
)
from ffmpeg_plan import (
    DecodeSpec,
    FilterSpec,
    build_plan,
    build_split_filter_from_geometry,
    build_stream_sync_filter,
    run_ffmpeg,
)
from huds.common import (
    COL_FAST_BRIGHTBLUE,
    COL_FAST_DARKBLUE,
    COL_HUD_BG,
    COL_SLOW_BRIGHTRED,
    COL_SLOW_DARKRED,
    COL_WHITE,
    SCROLL_HUD_NAMES as _SCROLL_HUD_NAMES,
    TABLE_HUD_NAMES as _TABLE_HUD_NAMES,
    build_value_boundaries,
    choose_tick_step,
    draw_left_axis_labels,
    draw_stripe_grid,
    filter_axis_labels_by_position,
    format_value_for_step,
    should_suppress_boundary_label,
    value_boundaries_to_y,
)
from huds.delta import render_delta
from huds.gear_rpm import render_gear_rpm
from huds.line_delta import render_line_delta
from huds.speed import build_confirmed_max_speed_display, render_speed
from huds.steering import render_steering
from huds.throttle_brake import render_throttle_brake
from huds.under_oversteer import render_under_oversteer

# Orchestrator flow used by render_split_screen_sync:
# 1) Config reading
# 2) Sync/Mapping
# 3) Layout
# 4) HUD Render
# 5) FFmpeg Run



@dataclass(frozen=True)
class VideoMeta:
    width: int
    height: int
    fps: float
    duration_s: float


def _fraction_to_float(frac: str) -> float:
    s = (frac or "").strip()
    if s == "":
        return 0.0
    if "/" in s:
        a, b = s.split("/", 1)
        try:
            num = float(a.strip())
            den = float(b.strip())
            return 0.0 if den == 0.0 else (num / den)
        except Exception:
            return 0.0
    try:
        return float(s)
    except Exception:
        return 0.0


def _to_float_safe(v: Any, default: float) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def probe_video_meta(video_path: Path) -> VideoMeta:
    # Stream-Meta + Format-Duration in einem Call
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=width,height,avg_frame_rate,r_frame_rate:format=duration",
        "-select_streams",
        "v:0",
        "-of",
        "json",
        str(video_path),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0 or not p.stdout.strip():
        raise RuntimeError(f"ffprobe failed: {video_path}")

    data = json.loads(p.stdout)
    streams = data.get("streams", [])
    if not streams:
        raise RuntimeError(f"ffprobe no streams: {video_path}")
    s0 = streams[0]

    w = int(s0.get("width") or 0)
    h = int(s0.get("height") or 0)
    afr = str(s0.get("avg_frame_rate") or "")
    rfr = str(s0.get("r_frame_rate") or "")
    fps = _fraction_to_float(afr) or _fraction_to_float(rfr)

    fmt = data.get("format", {}) if isinstance(data.get("format", {}), dict) else {}
    dur = _to_float_safe(fmt.get("duration"), 0.0)

    if w <= 0 or h <= 0 or fps <= 0.1:
        raise RuntimeError(f"invalid meta: {video_path}")
    if dur <= 0.01:
        # Fallback: sehr selten, aber wir brechen sauber ab
        raise RuntimeError(f"invalid duration from ffprobe: {video_path}")

    return VideoMeta(width=w, height=h, fps=fps, duration_s=dur)


def probe_has_audio(video_path: Path) -> bool:
    try:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(video_path),
        ]
        p = subprocess.run(cmd, capture_output=True, text=True)
        return p.returncode == 0 and (p.stdout or "").strip() != ""
    except Exception:
        return False


@dataclass(frozen=True)
class OutputGeometry:
    W: int
    H: int
    hud: int
    left_w: int
    right_w: int

    # Overlay-Positionen im Output
    left_x: int
    left_y: int
    fast_out_x: int
    fast_out_y: int


@dataclass(frozen=True)
class HudSyncMapping:
    slow_frame_to_lapdist: list[float]
    slow_to_fast_frame: list[int] | None = None
    slow_frame_to_fast_time_s: list[float] | None = None


@dataclass(frozen=True)
class HudSignals:
    slow_speed_frames: list[float] | None = None
    fast_speed_frames: list[float] | None = None
    slow_min_speed_frames: list[float] | None = None
    fast_min_speed_frames: list[float] | None = None
    slow_gear_frames: list[int] | None = None
    fast_gear_frames: list[int] | None = None
    slow_rpm_frames: list[float] | None = None
    fast_rpm_frames: list[float] | None = None
    slow_steer_frames: list[float] | None = None
    fast_steer_frames: list[float] | None = None
    slow_throttle_frames: list[float] | None = None
    fast_throttle_frames: list[float] | None = None
    slow_brake_frames: list[float] | None = None
    fast_brake_frames: list[float] | None = None
    slow_abs_frames: list[int] | None = None
    fast_abs_frames: list[int] | None = None
    line_delta_m_frames: list[float] | None = None
    line_delta_y_abs_m: float | None = None
    under_oversteer_slow_frames: list[float] | None = None
    under_oversteer_fast_frames: list[float] | None = None
    under_oversteer_y_abs: float | None = None


@dataclass(frozen=True)
class HudWindowParams:
    before_s: float = 10.0
    after_s: float = 10.0
    hud_name: str | None = None
    hud_windows: Any | None = None


@dataclass(frozen=True)
class HudRenderSettings:
    speed_units: str = "kmh"
    speed_update_hz: int = 60
    gear_rpm_update_hz: int = 60
    curve_points_default: int = 180
    curve_points_overrides: Any | None = None
    pedals_sample_mode: str = "time"
    pedals_abs_debounce_ms: int = 60


@dataclass(frozen=True)
class HudContext:
    fps: float
    cut_i0: int
    cut_i1: int
    geom: OutputGeometry
    hud_enabled: Any | None
    hud_boxes: Any | None
    sync: HudSyncMapping
    signals: HudSignals
    window: HudWindowParams
    settings: HudRenderSettings
    log_file: Path | None = None


@dataclass(frozen=True)
class FrameWindowMapping:
    i: int
    before_f: int
    after_f: int
    iL: int
    iR: int
    idxs: list[int]
    offsets: list[int]
    t_slow: list[float]
    fast_idx: list[int]
    t_fast: list[float]


def parse_output_preset(preset: str) -> tuple[int, int]:
    s = (preset or "").strip().lower()
    if "x" not in s:
        raise RuntimeError(f"output.preset ungueltig: {preset!r}")
    a, b = s.split("x", 1)
    try:
        W = int(a.strip())
        H = int(b.strip())
    except Exception:
        raise RuntimeError(f"output.preset ungueltig: {preset!r}")
    if W <= 0 or H <= 0:
        raise RuntimeError(f"output.preset ungueltig: {preset!r}")
    return W, H


def build_output_geometry(preset: str, hud_width_px: int) -> OutputGeometry:
    W, H = parse_output_preset(preset)

    hud = int(hud_width_px)
    if hud < 0:
        hud = 0
    if hud >= W - 2:
        raise RuntimeError("hud_width_px ist zu gross fuer die Zielbreite.")

    rest = W - hud
    left_w = rest // 2
    right_w = rest - left_w

    if left_w <= 10 or right_w <= 10:
        raise RuntimeError("links/rechts sind zu klein. hud_width_px reduzieren oder preset vergroessern.")

    left_x = 0
    left_y = 0
    fast_out_x = left_w + hud
    fast_out_y = 0

    return OutputGeometry(
        W=W,
        H=H,
        hud=hud,
        left_w=left_w,
        right_w=right_w,
        left_x=left_x,
        left_y=left_y,
        fast_out_x=fast_out_x,
        fast_out_y=fast_out_y,
    )


def _log_print(msg: str, log_file: Path | None) -> None:
    # [hudpy]-Logs separat schaltbar halten, um Konsole/Log-Rauschen zu reduzieren.
    try:
        msg_s = str(msg)
    except Exception:
        msg_s = ""
    try:
        if msg_s.lstrip().startswith("[hudpy]"):
            hudpy_dbg = str(os.environ.get("IRVC_DEBUG_HUDPY") or "").strip().lower()
            if hudpy_dbg not in ("1", "true", "yes", "on"):
                return
    except Exception:
        pass

    # Immer in die Konsole UND wenn möglich ins Log schreiben.
    try:
        print(msg_s, flush=True)
    except Exception:
        pass
    if log_file is None:
        return
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(msg_s.rstrip("\n") + "\n")
    except Exception:
        pass


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


def _unwrap_lapdist(xs: list[float]) -> list[float]:
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


def _clamp(x: float, lo: float, hi: float) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def _is_finite_float(v: Any) -> bool:
    try:
        x = float(v)
        return (x == x) and math.isfinite(x)
    except Exception:
        return False


def _prepare_xy_time_series(
    t_raw: list[float],
    lat_raw: list[float],
    lon_raw: list[float],
    lat0_rad: float,
    lon0_rad: float,
    cos_lat0: float,
) -> tuple[list[float], list[float], list[float]]:
    n = min(len(t_raw), len(lat_raw), len(lon_raw))
    ts: list[float] = []
    xs: list[float] = []
    ys: list[float] = []
    r = 6378137.0
    for k in range(n):
        tv = t_raw[k]
        lav = lat_raw[k]
        lov = lon_raw[k]
        if (not _is_finite_float(tv)) or (not _is_finite_float(lav)) or (not _is_finite_float(lov)):
            continue
        tr = float(tv)
        lat_rad = math.radians(float(lav))
        lon_rad = math.radians(float(lov))
        # Lokale equirectangular-Projektion in Meter mit gemeinsamem Ursprung.
        x_m = (lon_rad - lon0_rad) * cos_lat0 * r
        y_m = (lat_rad - lat0_rad) * r
        ts.append(tr)
        xs.append(float(x_m))
        ys.append(float(y_m))
    if len(ts) < 2:
        return [], [], []
    ts = _force_strictly_increasing(ts)
    return ts, xs, ys


def _prepare_scalar_time_series(t_raw: list[float], v_raw: list[float]) -> tuple[list[float], list[float]]:
    n = min(len(t_raw), len(v_raw))
    ts: list[float] = []
    vs: list[float] = []
    for k in range(n):
        tv = t_raw[k]
        vv = v_raw[k]
        if (not _is_finite_float(tv)) or (not _is_finite_float(vv)):
            continue
        ts.append(float(tv))
        vs.append(float(vv))
    if len(ts) < 2:
        return [], []
    ts = _force_strictly_increasing(ts)
    return ts, vs


def _interp_linear_clamped_time(
    ts: list[float],
    vs: list[float],
    t_query: float,
    j_hint: int,
) -> tuple[float, int]:
    n = len(ts)
    if n <= 0 or len(vs) <= 0:
        return 0.0, 0
    if n == 1 or len(vs) == 1:
        return float(vs[0]), 0
    if t_query <= ts[0]:
        return float(vs[0]), 0
    if t_query >= ts[n - 1]:
        return float(vs[n - 1]), max(0, n - 2)

    j = int(j_hint)
    if j < 0:
        j = 0
    if j > n - 2:
        j = n - 2

    while j < (n - 2) and ts[j + 1] < t_query:
        j += 1
    while j > 0 and ts[j] > t_query:
        j -= 1

    t0 = float(ts[j])
    t1 = float(ts[j + 1])
    v0 = float(vs[j])
    v1 = float(vs[j + 1])

    if t1 <= t0:
        return v0, j

    a = (float(t_query) - t0) / (t1 - t0)
    if a < 0.0:
        a = 0.0
    if a > 1.0:
        a = 1.0
    return (v0 + (v1 - v0) * a), j
    
def _csv_time_axis_or_fallback(run, duration_s: float) -> list[float]:
    from csv_g61 import get_float_col, has_col
    if has_col(run, "Time_s"):
        return get_float_col(run, "Time_s")
    n = int(getattr(run, "row_count", 0) or 0)
    if n < 2:
        raise RuntimeError("CSV hat zu wenig Zeilen fÃ¼r Fallback-Zeitachse.")
    dt = float(duration_s) / float(n - 1)
    out: list[float] = []
    for i in range(n):
        out.append(float(i) * dt)
    return out

def _sample_csv_col_to_frames_float(csv_path: Path, duration_s: float, fps: float, col: str) -> list[float]:
    from csv_g61 import get_float_col, has_col, load_g61_csv
    run = load_g61_csv(csv_path)
    if not has_col(run, col):
        return []
    t = _force_strictly_increasing(_csv_time_axis_or_fallback(run, duration_s))
    y = get_float_col(run, col)
    if not t or not y or len(t) != len(y):
        return []
    n_frames = int(math.floor(max(0.0, float(duration_s)) * float(fps)))
    if n_frames <= 0:
        n_frames = 1

    out: list[float] = []
    j = 0
    for i in range(n_frames):
        ti = float(i) / max(1.0, float(fps))
        while (j + 1) < len(t) and t[j + 1] <= ti:
            j += 1
        if (j + 1) >= len(t):
            out.append(float(y[-1]))
            continue
        t0 = float(t[j])
        t1 = float(t[j + 1])
        v0 = float(y[j])
        v1 = float(y[j + 1])
        if t1 <= t0:
            out.append(v0)
        else:
            a = (ti - t0) / (t1 - t0)
            if a < 0.0:
                a = 0.0
            if a > 1.0:
                a = 1.0
            out.append(v0 * (1.0 - a) + v1 * a)
    return out

def _sample_csv_col_to_frames_int_nearest(csv_path: Path, duration_s: float, fps: float, col: str) -> list[int]:
    ys = _sample_csv_col_to_frames_float(csv_path, duration_s, fps, col)
    if not ys:
        return []
    out: list[int] = []
    for v in ys:
        try:
            out.append(int(round(float(v))))
        except Exception:
            out.append(0)
    return out


def _build_line_delta_frames_from_csv(
    *,
    slow_csv: Path,
    fast_csv: Path,
    slow_duration_s: float,
    fast_duration_s: float,
    fps: float,
    slow_frame_to_fast_time_s: list[float] | None,
    frame_count_hint: int,
) -> list[float]:
    from csv_g61 import get_float_col, has_col, load_g61_csv

    def _is_finite(v: Any) -> bool:
        try:
            x = float(v)
            return (x == x) and math.isfinite(x)
        except Exception:
            return False

    def _prepare_xy_series(
        t_raw: list[float],
        lat_raw: list[float],
        lon_raw: list[float],
        lat0_rad: float,
        lon0_rad: float,
        cos_lat0: float,
    ) -> tuple[list[float], list[float], list[float]]:
        n = min(len(t_raw), len(lat_raw), len(lon_raw))
        ts: list[float] = []
        xs: list[float] = []
        ys: list[float] = []
        r = 6378137.0
        for k in range(n):
            tv = t_raw[k]
            lav = lat_raw[k]
            lov = lon_raw[k]
            if (not _is_finite(tv)) or (not _is_finite(lav)) or (not _is_finite(lov)):
                continue
            tr = float(tv)
            lat_rad = math.radians(float(lav))
            lon_rad = math.radians(float(lov))
            # Lokale equirectangular-Projektion in Meter mit gemeinsamem Ursprung.
            x_m = (lon_rad - lon0_rad) * cos_lat0 * r
            y_m = (lat_rad - lat0_rad) * r
            ts.append(tr)
            xs.append(float(x_m))
            ys.append(float(y_m))
        if len(ts) < 2:
            return [], [], []
        ts = _force_strictly_increasing(ts)
        return ts, xs, ys

    def _prepare_scalar_series(t_raw: list[float], v_raw: list[float]) -> tuple[list[float], list[float]]:
        n = min(len(t_raw), len(v_raw))
        ts: list[float] = []
        vs: list[float] = []
        for k in range(n):
            tv = t_raw[k]
            vv = v_raw[k]
            if (not _is_finite(tv)) or (not _is_finite(vv)):
                continue
            ts.append(float(tv))
            vs.append(float(vv))
        if len(ts) < 2:
            return [], []
        ts = _force_strictly_increasing(ts)
        return ts, vs

    def _interp_linear_clamped(
        ts: list[float],
        vs: list[float],
        t_query: float,
        j_hint: int,
    ) -> tuple[float, int]:
        n = len(ts)
        if n <= 0 or len(vs) <= 0:
            return 0.0, 0
        if n == 1 or len(vs) == 1:
            return float(vs[0]), 0
        if t_query <= ts[0]:
            return float(vs[0]), 0
        if t_query >= ts[n - 1]:
            return float(vs[n - 1]), max(0, n - 2)

        j = int(j_hint)
        if j < 0:
            j = 0
        if j > n - 2:
            j = n - 2

        while j < (n - 2) and ts[j + 1] < t_query:
            j += 1
        while j > 0 and ts[j] > t_query:
            j -= 1

        t0 = float(ts[j])
        t1 = float(ts[j + 1])
        v0 = float(vs[j])
        v1 = float(vs[j + 1])

        if t1 <= t0:
            return v0, j

        a = (float(t_query) - t0) / (t1 - t0)
        if a < 0.0:
            a = 0.0
        if a > 1.0:
            a = 1.0
        return (v0 + (v1 - v0) * a), j

    try:
        if not slow_frame_to_fast_time_s:
            return []
        fps_safe = float(fps) if float(fps) > 0.1 else 30.0
        n_out = int(frame_count_hint)
        if n_out <= 0:
            n_out = int(len(slow_frame_to_fast_time_s))
        if n_out <= 0:
            return []

        run_s = load_g61_csv(slow_csv)
        run_f = load_g61_csv(fast_csv)
        for req in ("Lat", "Lon", "LapDistPct"):
            if not has_col(run_s, req):
                return []
        for req in ("Lat", "Lon"):
            if not has_col(run_f, req):
                return []

        t_s_raw = _force_strictly_increasing(_csv_time_axis_or_fallback(run_s, slow_duration_s))
        t_f_raw = _force_strictly_increasing(_csv_time_axis_or_fallback(run_f, fast_duration_s))
        lat_s_raw = get_float_col(run_s, "Lat")
        lon_s_raw = get_float_col(run_s, "Lon")
        lat_f_raw = get_float_col(run_f, "Lat")
        lon_f_raw = get_float_col(run_f, "Lon")
        ld_s_raw = get_float_col(run_s, "LapDistPct")

        lat0 = None
        lon0 = None
        n0 = min(len(lat_s_raw), len(lon_s_raw))
        for k in range(n0):
            if _is_finite(lat_s_raw[k]) and _is_finite(lon_s_raw[k]):
                lat0 = float(lat_s_raw[k])
                lon0 = float(lon_s_raw[k])
                break
        if lat0 is None or lon0 is None:
            return []

        lat0_rad = math.radians(float(lat0))
        lon0_rad = math.radians(float(lon0))
        cos_lat0 = math.cos(lat0_rad)
        if abs(cos_lat0) < 1e-6:
            cos_lat0 = 1e-6 if cos_lat0 >= 0.0 else -1e-6

        t_s_xy, x_s, y_s = _prepare_xy_series(t_s_raw, lat_s_raw, lon_s_raw, lat0_rad, lon0_rad, cos_lat0)
        t_f_xy, x_f, y_f = _prepare_xy_series(t_f_raw, lat_f_raw, lon_f_raw, lat0_rad, lon0_rad, cos_lat0)
        t_s_ld, ld_s = _prepare_scalar_series(t_s_raw, ld_s_raw)

        if len(t_s_xy) < 2 or len(t_f_xy) < 2:
            return []
        if len(t_s_ld) < 2:
            t_s_ld = t_s_xy
            ld_s = [0.0] * len(t_s_xy)

        t_s_min = float(t_s_xy[0])
        t_s_max = float(t_s_xy[len(t_s_xy) - 1])

        # Tangente aus +/- dt um die Slow-Zeit pro Output-Frame.
        dt = max(0.01, (0.5 / max(1.0, fps_safe)))

        out: list[float] = []
        j_sx = 0
        j_sy = 0
        j_fx = 0
        j_fy = 0
        j_ld = 0
        j_t0x = 0
        j_t0y = 0
        j_t1x = 0
        j_t1y = 0
        has_last_n = False
        last_nx = 0.0
        last_ny = 1.0

        n_map = len(slow_frame_to_fast_time_s)
        for frame_idx in range(n_out):
            t_slow = float(frame_idx) / fps_safe

            map_idx = frame_idx
            if map_idx < 0:
                map_idx = 0
            if map_idx >= n_map:
                map_idx = n_map - 1
            t_fast = float(slow_frame_to_fast_time_s[map_idx])

            xs, j_sx = _interp_linear_clamped(t_s_xy, x_s, t_slow, j_sx)
            ys, j_sy = _interp_linear_clamped(t_s_xy, y_s, t_slow, j_sy)
            # Story-Vorgabe: LapDistPct_slow(t_slow) wird ebenfalls über Time_s interpoliert.
            _ld_slow, j_ld = _interp_linear_clamped(t_s_ld, ld_s, t_slow, j_ld)
            _ = _ld_slow
            xf, j_fx = _interp_linear_clamped(t_f_xy, x_f, t_fast, j_fx)
            yf, j_fy = _interp_linear_clamped(t_f_xy, y_f, t_fast, j_fy)

            t0 = _clamp(t_slow - dt, t_s_min, t_s_max)
            t1 = _clamp(t_slow + dt, t_s_min, t_s_max)
            if t1 <= t0:
                t0 = _clamp(t_slow - (2.0 * dt), t_s_min, t_s_max)
                t1 = _clamp(t_slow + (2.0 * dt), t_s_min, t_s_max)

            x0, j_t0x = _interp_linear_clamped(t_s_xy, x_s, t0, j_t0x)
            y0, j_t0y = _interp_linear_clamped(t_s_xy, y_s, t0, j_t0y)
            x1, j_t1x = _interp_linear_clamped(t_s_xy, x_s, t1, j_t1x)
            y1, j_t1y = _interp_linear_clamped(t_s_xy, y_s, t1, j_t1y)

            tx = float(x1 - x0)
            ty = float(y1 - y0)
            norm_t = math.hypot(tx, ty)
            if norm_t > 1e-6:
                nx = -ty / norm_t
                ny = tx / norm_t
                last_nx = float(nx)
                last_ny = float(ny)
                has_last_n = True
            elif has_last_n:
                nx = float(last_nx)
                ny = float(last_ny)
            else:
                nx = 0.0
                ny = 1.0

            dx = float(xf - xs)
            dy = float(yf - ys)
            delta_m = (dx * nx) + (dy * ny)
            if not math.isfinite(delta_m):
                delta_m = 0.0
            out.append(float(delta_m))

        return out
    except Exception:
        return []


def _build_under_oversteer_proxy_frames_from_csv(
    *,
    slow_csv: Path,
    fast_csv: Path,
    slow_duration_s: float,
    fast_duration_s: float,
    fps: float,
    slow_frame_to_fast_time_s: list[float] | None,
    frame_count_hint: int,
    under_oversteer_curve_center: float = 0.0,
    log_file: Path | None = None,
) -> tuple[list[float], list[float], float]:
    from csv_g61 import get_float_col, has_col, load_g61_csv

    def _wrap_angle_pi(rad: float) -> float:
        two_pi = 2.0 * math.pi
        x = (float(rad) + math.pi) % two_pi - math.pi
        if x <= -math.pi:
            x += two_pi
        elif x > math.pi:
            x -= two_pi
        return float(x)

    def _median(vals: list[float]) -> float:
        if not vals:
            return 0.0
        xs = sorted(float(v) for v in vals if _is_finite_float(v))
        n = len(xs)
        if n <= 0:
            return 0.0
        m = n // 2
        if (n % 2) == 1:
            return float(xs[m])
        return float(0.5 * (xs[m - 1] + xs[m]))

    def _unwrap_over_time(vals_wrapped: list[float]) -> list[float]:
        if not vals_wrapped:
            return []
        out: list[float] = []
        prev_wrapped = 0.0
        running = 0.0
        have_prev = False
        # Guard against single-frame telemetry glitches causing a permanent +/-2pi branch shift.
        unwrap_delta_abs_cap = 1.2
        for a_in in vals_wrapped:
            a = float(a_in)
            if not _is_finite_float(a):
                a = prev_wrapped if have_prev else 0.0
            if not have_prev:
                prev_wrapped = float(a)
                running = float(a)
                out.append(float(running))
                have_prev = True
                continue
            delta = _wrap_angle_pi(float(a) - float(prev_wrapped))
            if abs(float(delta)) > float(unwrap_delta_abs_cap):
                delta = 0.0
            running = float(running + delta)
            prev_wrapped = float(a)
            out.append(float(running))
        return out

    def _percentile_sorted(vals_sorted: list[float], q: float) -> float:
        n = len(vals_sorted)
        if n <= 0:
            return 0.0
        if n == 1:
            return float(vals_sorted[0])
        qq = _clamp(float(q), 0.0, 100.0)
        pos = (qq / 100.0) * float(n - 1)
        i0 = int(math.floor(pos))
        i1 = int(math.ceil(pos))
        if i0 <= 0 and i1 <= 0:
            return float(vals_sorted[0])
        if i0 >= (n - 1) and i1 >= (n - 1):
            return float(vals_sorted[n - 1])
        w = float(pos - float(i0))
        return float((1.0 - w) * float(vals_sorted[i0]) + w * float(vals_sorted[i1]))

    dbg_raw = str(os.getenv("IRVC_DEBUG_UO") or "").strip()
    dbg_level = 0
    if dbg_raw:
        try:
            dbg_level = int(float(dbg_raw))
        except Exception:
            dbg_level = 1
    if dbg_level < 0:
        dbg_level = 0
    dbg_enabled = dbg_level >= 1
    dbg_spam = dbg_level >= 2
    hud_dbg = (os.environ.get("IRVC_HUD_DEBUG") or "0").strip().lower() in ("1", "true", "yes", "on")
    dbg_k = 732
    try:
        dbg_k = int(float((os.getenv("IRVC_DEBUG_UO_K") or "").strip() or "732"))
    except Exception:
        dbg_k = 732

    def _uo_log(msg: str) -> None:
        if dbg_enabled:
            _log_print(f"[uo] {msg}", log_file)

    def _uo_finite_minmax(vals: list[float]) -> tuple[int, float | None, float | None]:
        cnt = 0
        mn = math.inf
        mx = -math.inf
        for v in vals:
            if _is_finite_float(v):
                fv = float(v)
                cnt += 1
                if fv < mn:
                    mn = fv
                if fv > mx:
                    mx = fv
        if cnt <= 0:
            return 0, None, None
        return cnt, float(mn), float(mx)

    def _uo_head_tail(vals: list[float], n: int = 3) -> str:
        finite = [float(v) for v in vals if _is_finite_float(v)]
        if not finite:
            return "[]"
        head = finite[: max(0, int(n))]
        tail = finite[-max(0, int(n)) :]
        hs = ",".join(f"{v:+.6f}" for v in head)
        ts = ",".join(f"{v:+.6f}" for v in tail)
        if len(finite) <= int(n):
            return f"[{hs}]"
        return f"[{hs}]...[{ts}]"

    def _uo_inferred_dt(ts: list[float]) -> float | None:
        if len(ts) < 2:
            return None
        dts: list[float] = []
        prev = float(ts[0])
        for i in range(1, len(ts)):
            cur = float(ts[i])
            d = float(cur - prev)
            prev = cur
            if _is_finite_float(d) and d > 0.0:
                dts.append(float(d))
                if len(dts) >= 64:
                    break
        if not dts:
            return None
        return float(sum(dts) / float(len(dts)))

    def _uo_count_wrap_jumps(vals: list[float], threshold_abs: float) -> int:
        if len(vals) < 2:
            return 0
        n = 0
        prev = float(vals[0])
        for i in range(1, len(vals)):
            cur = float(vals[i])
            if _is_finite_float(prev) and _is_finite_float(cur):
                dd = _wrap_angle_pi(float(cur - prev))
                if abs(float(dd)) > float(threshold_abs):
                    n += 1
            prev = cur
        return int(n)

    def _uo_top_steps(vals: list[float], *, wrapped_delta: bool, top_n: int = 10) -> list[tuple[int, float, float, float, float]]:
        if len(vals) < 2:
            return []
        rows: list[tuple[float, int, float, float, float]] = []
        prev = float(vals[0])
        for i in range(1, len(vals)):
            cur = float(vals[i])
            if not (_is_finite_float(prev) and _is_finite_float(cur)):
                prev = cur
                continue
            dd = _wrap_angle_pi(float(cur - prev)) if wrapped_delta else float(cur - prev)
            rows.append((abs(float(dd)), int(i), float(prev), float(cur), float(dd)))
            prev = cur
        rows.sort(key=lambda x: x[0], reverse=True)
        out: list[tuple[int, float, float, float, float]] = []
        for rec in rows[: max(0, int(top_n))]:
            out.append((int(rec[1]), float(rec[2]), float(rec[3]), float(rec[4]), float(rec[0])))
        return out

    try:
        map_len_in = int(len(slow_frame_to_fast_time_s)) if slow_frame_to_fast_time_s else 0
        _uo_log(
            "entry "
            + f"fps_in={float(fps):.6f} slow_duration_s={float(slow_duration_s):.6f} fast_duration_s={float(fast_duration_s):.6f} "
            + f"frame_count_hint={int(frame_count_hint)} before_s=n/a after_s=n/a before_frames=n/a after_frames=n/a "
            + f"sync_map_present={bool(slow_frame_to_fast_time_s)} sync_map_len={map_len_in} dbg_level={dbg_level} dbg_k={dbg_k}"
        )
        if not slow_frame_to_fast_time_s:
            _uo_log("early_return reason=no_sync_map")
            return [], [], 1.0

        fps_safe = float(fps) if float(fps) > 0.1 else 30.0
        n_out = int(frame_count_hint)
        if n_out <= 0:
            n_out = int(len(slow_frame_to_fast_time_s))
        if n_out <= 0:
            _uo_log("early_return reason=nonpositive_output_frames")
            return [], [], 1.0

        run_s = load_g61_csv(slow_csv)
        run_f = load_g61_csv(fast_csv)

        for req in ("Lat", "Lon", "Yaw"):
            has_s = bool(has_col(run_s, req))
            has_f = bool(has_col(run_f, req))
            _uo_log(f"cols req={req} slow={has_s} fast={has_f}")
            if not has_s:
                _uo_log(f"early_return reason=missing_column run=slow col={req}")
                return [], [], 1.0
            if not has_f:
                _uo_log(f"early_return reason=missing_column run=fast col={req}")
                return [], [], 1.0

        t_s_raw = _force_strictly_increasing(_csv_time_axis_or_fallback(run_s, slow_duration_s))
        t_f_raw = _force_strictly_increasing(_csv_time_axis_or_fallback(run_f, fast_duration_s))
        lat_s_raw = get_float_col(run_s, "Lat")
        lon_s_raw = get_float_col(run_s, "Lon")
        lat_f_raw = get_float_col(run_f, "Lat")
        lon_f_raw = get_float_col(run_f, "Lon")
        yaw_s_raw = get_float_col(run_s, "Yaw")
        yaw_f_raw = get_float_col(run_f, "Yaw")

        if dbg_enabled:
            t_s_dt = _uo_inferred_dt(t_s_raw)
            t_f_dt = _uo_inferred_dt(t_f_raw)
            t_s_first = float(t_s_raw[0]) if t_s_raw else float("nan")
            t_s_last = float(t_s_raw[-1]) if t_s_raw else float("nan")
            t_f_first = float(t_f_raw[0]) if t_f_raw else float("nan")
            t_f_last = float(t_f_raw[-1]) if t_f_raw else float("nan")
            _uo_log(
                f"[slow] csv lens t={len(t_s_raw)} lat={len(lat_s_raw)} lon={len(lon_s_raw)} yaw={len(yaw_s_raw)} "
                + f"t_first={t_s_first:+.6f} t_last={t_s_last:+.6f} dt_inferred={(f'{t_s_dt:+.6f}' if t_s_dt is not None else 'n/a')}"
            )
            _uo_log(
                f"[fast] csv lens t={len(t_f_raw)} lat={len(lat_f_raw)} lon={len(lon_f_raw)} yaw={len(yaw_f_raw)} "
                + f"t_first={t_f_first:+.6f} t_last={t_f_last:+.6f} dt_inferred={(f'{t_f_dt:+.6f}' if t_f_dt is not None else 'n/a')}"
            )
            s_lat_cnt, s_lat_min, s_lat_max = _uo_finite_minmax(lat_s_raw)
            s_lon_cnt, s_lon_min, s_lon_max = _uo_finite_minmax(lon_s_raw)
            s_yaw_cnt, s_yaw_min, s_yaw_max = _uo_finite_minmax(yaw_s_raw)
            f_lat_cnt, f_lat_min, f_lat_max = _uo_finite_minmax(lat_f_raw)
            f_lon_cnt, f_lon_min, f_lon_max = _uo_finite_minmax(lon_f_raw)
            f_yaw_cnt, f_yaw_min, f_yaw_max = _uo_finite_minmax(yaw_f_raw)
            _uo_log(
                f"[slow] minmax lat(cnt={s_lat_cnt})={(f'{s_lat_min:+.8f}..{s_lat_max:+.8f}' if s_lat_min is not None and s_lat_max is not None else 'n/a')} "
                + f"lon(cnt={s_lon_cnt})={(f'{s_lon_min:+.8f}..{s_lon_max:+.8f}' if s_lon_min is not None and s_lon_max is not None else 'n/a')} "
                + f"yaw(cnt={s_yaw_cnt})={(f'{s_yaw_min:+.6f}..{s_yaw_max:+.6f}' if s_yaw_min is not None and s_yaw_max is not None else 'n/a')} "
                + f"samples lat={_uo_head_tail(lat_s_raw)} lon={_uo_head_tail(lon_s_raw)} yaw={_uo_head_tail(yaw_s_raw)}"
            )
            _uo_log(
                f"[fast] minmax lat(cnt={f_lat_cnt})={(f'{f_lat_min:+.8f}..{f_lat_max:+.8f}' if f_lat_min is not None and f_lat_max is not None else 'n/a')} "
                + f"lon(cnt={f_lon_cnt})={(f'{f_lon_min:+.8f}..{f_lon_max:+.8f}' if f_lon_min is not None and f_lon_max is not None else 'n/a')} "
                + f"yaw(cnt={f_yaw_cnt})={(f'{f_yaw_min:+.6f}..{f_yaw_max:+.6f}' if f_yaw_min is not None and f_yaw_max is not None else 'n/a')} "
                + f"samples lat={_uo_head_tail(lat_f_raw)} lon={_uo_head_tail(lon_f_raw)} yaw={_uo_head_tail(yaw_f_raw)}"
            )

        lat0 = None
        lon0 = None
        n0 = min(len(lat_s_raw), len(lon_s_raw))
        for k in range(n0):
            if _is_finite_float(lat_s_raw[k]) and _is_finite_float(lon_s_raw[k]):
                lat0 = float(lat_s_raw[k])
                lon0 = float(lon_s_raw[k])
                break
        if lat0 is None or lon0 is None:
            _uo_log("early_return reason=no_finite_latlon_seed")
            return [], [], 1.0

        lat0_rad = math.radians(float(lat0))
        lon0_rad = math.radians(float(lon0))
        cos_lat0 = math.cos(lat0_rad)
        if abs(cos_lat0) < 1e-6:
            cos_lat0 = 1e-6 if cos_lat0 >= 0.0 else -1e-6

        t_s_xy, x_s, y_s = _prepare_xy_time_series(t_s_raw, lat_s_raw, lon_s_raw, lat0_rad, lon0_rad, cos_lat0)
        t_f_xy, x_f, y_f = _prepare_xy_time_series(t_f_raw, lat_f_raw, lon_f_raw, lat0_rad, lon0_rad, cos_lat0)
        t_s_yaw, yaw_s = _prepare_scalar_time_series(t_s_raw, yaw_s_raw)
        t_f_yaw, yaw_f = _prepare_scalar_time_series(t_f_raw, yaw_f_raw)
        yaw_s_cos = [float(math.cos(float(v))) for v in yaw_s]
        yaw_s_sin = [float(math.sin(float(v))) for v in yaw_s]
        yaw_f_cos = [float(math.cos(float(v))) for v in yaw_f]
        yaw_f_sin = [float(math.sin(float(v))) for v in yaw_f]

        if len(t_s_xy) < 2 or len(t_f_xy) < 2:
            _uo_log(
                f"early_return reason=xy_too_short slow_xy={len(t_s_xy)} fast_xy={len(t_f_xy)}"
            )
            return [], [], 1.0
        if len(t_s_yaw) < 2 or len(t_f_yaw) < 2:
            _uo_log(
                f"early_return reason=yaw_too_short slow_yaw={len(t_s_yaw)} fast_yaw={len(t_f_yaw)}"
            )
            return [], [], 1.0

        t_s_min = float(t_s_xy[0])
        t_s_max = float(t_s_xy[len(t_s_xy) - 1])
        t_f_min = float(t_f_xy[0])
        t_f_max = float(t_f_xy[len(t_f_xy) - 1])

        dt = 0.15
        heading_eps_m = 0.05
        _uo_log(
            f"settings output_fps={fps_safe:.6f} n_out={n_out} heading_dt={dt:.6f} heading_eps_m={heading_eps_m:.6f} "
            + f"[slow]xy_len={len(t_s_xy)} yaw_len={len(t_s_yaw)} t=({t_s_min:+.6f}..{t_s_max:+.6f}) "
            + f"[fast]xy_len={len(t_f_xy)} yaw_len={len(t_f_yaw)} t=({t_f_min:+.6f}..{t_f_max:+.6f})"
        )

        def _heading_xy_at(
            t_query: float,
            ts_xy: list[float],
            xs: list[float],
            ys: list[float],
            t_min: float,
            t_max: float,
            j_t0x: int,
            j_t0y: int,
            j_t1x: int,
            j_t1y: int,
            last_heading: float,
        ) -> tuple[float, int, int, int, int, float, float, bool]:
            t0 = _clamp(float(t_query) - dt, t_min, t_max)
            t1 = _clamp(float(t_query) + dt, t_min, t_max)
            if t1 <= t0:
                t0 = _clamp(float(t_query) - (2.0 * dt), t_min, t_max)
                t1 = _clamp(float(t_query) + (2.0 * dt), t_min, t_max)

            x0, j_t0x_out = _interp_linear_clamped_time(ts_xy, xs, t0, j_t0x)
            y0, j_t0y_out = _interp_linear_clamped_time(ts_xy, ys, t0, j_t0y)
            x1, j_t1x_out = _interp_linear_clamped_time(ts_xy, xs, t1, j_t1x)
            y1, j_t1y_out = _interp_linear_clamped_time(ts_xy, ys, t1, j_t1y)

            dx = float(x1 - x0)
            dy = float(y1 - y0)
            nrm = math.hypot(dx, dy)
            if nrm >= heading_eps_m:
                hdg = float(math.atan2(dy, dx))
                return hdg, j_t0x_out, j_t0y_out, j_t1x_out, j_t1y_out, hdg, float(nrm), False
            return float(last_heading), j_t0x_out, j_t0y_out, j_t1x_out, j_t1y_out, float(last_heading), float(nrm), True

        def _seed_initial_heading(
            ts_xy: list[float],
            xs: list[float],
            ys: list[float],
            t_min: float,
            t_max: float,
        ) -> float | None:
            j_t0x = 0
            j_t0y = 0
            j_t1x = 0
            j_t1y = 0
            for t_query in ts_xy:
                t0 = _clamp(float(t_query) - dt, t_min, t_max)
                t1 = _clamp(float(t_query) + dt, t_min, t_max)
                if t1 <= t0:
                    t0 = _clamp(float(t_query) - (2.0 * dt), t_min, t_max)
                    t1 = _clamp(float(t_query) + (2.0 * dt), t_min, t_max)

                x0, j_t0x = _interp_linear_clamped_time(ts_xy, xs, t0, j_t0x)
                y0, j_t0y = _interp_linear_clamped_time(ts_xy, ys, t0, j_t0y)
                x1, j_t1x = _interp_linear_clamped_time(ts_xy, xs, t1, j_t1x)
                y1, j_t1y = _interp_linear_clamped_time(ts_xy, ys, t1, j_t1y)

                dx = float(x1 - x0)
                dy = float(y1 - y0)
                nrm = math.hypot(dx, dy)
                if nrm >= heading_eps_m:
                    return float(math.atan2(dy, dx))
            return None

        slow_err_wrapped: list[float] = []
        fast_err_wrapped: list[float] = []

        j_s_yaw = 0
        j_f_yaw = 0
        j_s_t0x = 0
        j_s_t0y = 0
        j_s_t1x = 0
        j_s_t1y = 0
        j_f_t0x = 0
        j_f_t0y = 0
        j_f_t1x = 0
        j_f_t1y = 0

        seed_hdg_s = _seed_initial_heading(t_s_xy, x_s, y_s, t_s_min, t_s_max)
        seed_hdg_f = _seed_initial_heading(t_f_xy, x_f, y_f, t_f_min, t_f_max)
        last_hdg_s = float(seed_hdg_s) if (seed_hdg_s is not None and _is_finite_float(seed_hdg_s)) else 0.0
        last_hdg_f = float(seed_hdg_f) if (seed_hdg_f is not None and _is_finite_float(seed_hdg_f)) else 0.0
        if dbg_enabled:
            _uo_log(
                f"[slow] seed_heading={(f'{last_hdg_s:+.6f}' if _is_finite_float(last_hdg_s) else 'n/a')} "
                + f"(raw_seed={(f'{seed_hdg_s:+.6f}' if seed_hdg_s is not None and _is_finite_float(seed_hdg_s) else 'none')})"
            )
            _uo_log(
                f"[fast] seed_heading={(f'{last_hdg_f:+.6f}' if _is_finite_float(last_hdg_f) else 'n/a')} "
                + f"(raw_seed={(f'{seed_hdg_f:+.6f}' if seed_hdg_f is not None and _is_finite_float(seed_hdg_f) else 'none')})"
            )
        last_t_fast = 0.0

        n_map = len(slow_frame_to_fast_time_s)
        if dbg_enabled:
            rep_idxs: list[int] = []
            for k_rep in (0, 1, 10, 100, 300, 700, n_out - 1):
                kk = int(k_rep)
                if kk < 0 or kk >= n_out:
                    continue
                if kk not in rep_idxs:
                    rep_idxs.append(kk)
            for kk in rep_idxs:
                map_idx = kk
                if map_idx < 0:
                    map_idx = 0
                if map_idx >= n_map:
                    map_idx = n_map - 1
                t_slow_rep = float(kk) / fps_safe
                try:
                    t_fast_rep = float(slow_frame_to_fast_time_s[map_idx])
                except Exception:
                    t_fast_rep = float("nan")
                dt_rep = float(t_fast_rep - t_slow_rep) if (_is_finite_float(t_fast_rep) and _is_finite_float(t_slow_rep)) else float("nan")
                _uo_log(
                    f"[map] k={kk} t_slow={t_slow_rep:+.6f} t_fast={t_fast_rep:+.6f} delta_t={dt_rep:+.6f} map_idx={map_idx}"
                )

            map_vals: list[float] = []
            map_invalid = 0
            for i_map in range(n_map):
                try:
                    v_map = float(slow_frame_to_fast_time_s[i_map])
                except Exception:
                    v_map = float("nan")
                if not _is_finite_float(v_map):
                    map_invalid += 1
                    map_vals.append(float("nan"))
                else:
                    map_vals.append(float(v_map))
            jump_rows: list[tuple[float, int, float, float, float]] = []
            nonmono = 0
            for i_map in range(1, n_map):
                prev_map = float(map_vals[i_map - 1])
                cur_map = float(map_vals[i_map])
                if not (_is_finite_float(prev_map) and _is_finite_float(cur_map)):
                    continue
                d_map = float(cur_map - prev_map)
                if d_map < 0.0:
                    nonmono += 1
                jump_rows.append((abs(float(d_map)), int(i_map), float(prev_map), float(cur_map), float(d_map)))
            jump_rows.sort(key=lambda x: x[0], reverse=True)
            _uo_log(f"[map] len={n_map} non_monotonic_count={nonmono} invalid_count={map_invalid}")
            for rec in jump_rows[:10]:
                _uo_log(
                    f"[map] jump_top idx={int(rec[1])} prev={float(rec[2]):+.6f} cur={float(rec[3]):+.6f} diff={float(rec[4]):+.6f}"
                )

        hold_count_s = 0
        hold_count_f = 0
        first_valid_s: int | None = None
        first_valid_f: int | None = None
        run_hold_s = 0
        run_hold_f = 0
        max_run_hold_s = 0
        max_run_hold_f = 0
        max_run_hold_s_start = -1
        max_run_hold_s_end = -1
        max_run_hold_f_start = -1
        max_run_hold_f_end = -1

        dbg_rows: list[
            tuple[int, float, float, float, float, float, float, int, float, float, float, float, int]
        ] = []
        for frame_idx in range(n_out):
            t_slow = float(frame_idx) / fps_safe

            map_idx = frame_idx
            if map_idx < 0:
                map_idx = 0
            if map_idx >= n_map:
                map_idx = n_map - 1

            try:
                t_fast = float(slow_frame_to_fast_time_s[map_idx])
            except Exception:
                t_fast = float(last_t_fast)
            if not _is_finite_float(t_fast):
                t_fast = float(last_t_fast)
            last_t_fast = float(t_fast)

            yaw_sv_cos, j_s_yaw = _interp_linear_clamped_time(t_s_yaw, yaw_s_cos, t_slow, j_s_yaw)
            yaw_sv_sin, j_s_yaw = _interp_linear_clamped_time(t_s_yaw, yaw_s_sin, t_slow, j_s_yaw)
            yaw_sv = float(math.atan2(float(yaw_sv_sin), float(yaw_sv_cos)))
            hdg_s, j_s_t0x, j_s_t0y, j_s_t1x, j_s_t1y, last_hdg_s, nrm_s, held_s = _heading_xy_at(
                t_slow, t_s_xy, x_s, y_s, t_s_min, t_s_max, j_s_t0x, j_s_t0y, j_s_t1x, j_s_t1y, last_hdg_s
            )
            err_s = _wrap_angle_pi(float(yaw_sv) - float(hdg_s))
            if not _is_finite_float(err_s):
                err_s = 0.0
            slow_err_wrapped.append(float(err_s))
            if dbg_enabled:
                if held_s:
                    hold_count_s += 1
                    if run_hold_s <= 0:
                        run_hold_s = 1
                        cur_start_s = frame_idx
                    else:
                        run_hold_s += 1
                        cur_start_s = frame_idx - run_hold_s + 1
                    if run_hold_s > max_run_hold_s:
                        max_run_hold_s = int(run_hold_s)
                        max_run_hold_s_start = int(cur_start_s)
                        max_run_hold_s_end = int(frame_idx)
                else:
                    run_hold_s = 0
                    if first_valid_s is None:
                        first_valid_s = int(frame_idx)

            yaw_fv_cos, j_f_yaw = _interp_linear_clamped_time(t_f_yaw, yaw_f_cos, t_fast, j_f_yaw)
            yaw_fv_sin, j_f_yaw = _interp_linear_clamped_time(t_f_yaw, yaw_f_sin, t_fast, j_f_yaw)
            yaw_fv = float(math.atan2(float(yaw_fv_sin), float(yaw_fv_cos)))
            hdg_f, j_f_t0x, j_f_t0y, j_f_t1x, j_f_t1y, last_hdg_f, nrm_f, held_f = _heading_xy_at(
                t_fast, t_f_xy, x_f, y_f, t_f_min, t_f_max, j_f_t0x, j_f_t0y, j_f_t1x, j_f_t1y, last_hdg_f
            )
            err_f = _wrap_angle_pi(float(yaw_fv) - float(hdg_f))
            if not _is_finite_float(err_f):
                err_f = 0.0
            fast_err_wrapped.append(float(err_f))
            if dbg_enabled:
                if held_f:
                    hold_count_f += 1
                    if run_hold_f <= 0:
                        run_hold_f = 1
                        cur_start_f = frame_idx
                    else:
                        run_hold_f += 1
                        cur_start_f = frame_idx - run_hold_f + 1
                    if run_hold_f > max_run_hold_f:
                        max_run_hold_f = int(run_hold_f)
                        max_run_hold_f_start = int(cur_start_f)
                        max_run_hold_f_end = int(frame_idx)
                else:
                    run_hold_f = 0
                    if first_valid_f is None:
                        first_valid_f = int(frame_idx)

            if dbg_spam:
                dbg_rows.append(
                    (
                        int(frame_idx),
                        float(t_slow),
                        float(t_fast),
                        float(hdg_s),
                        float(yaw_sv),
                        float(err_s),
                        float(nrm_s),
                        int(1 if held_s else 0),
                        float(hdg_f),
                        float(yaw_fv),
                        float(err_f),
                        float(nrm_f),
                        int(1 if held_f else 0),
                    )
                )

        if dbg_enabled:
            _uo_log(
                f"[slow] heading_hold_count={hold_count_s} first_valid_idx={(first_valid_s if first_valid_s is not None else -1)} "
                + f"longest_hold_run={max_run_hold_s} run_range={max_run_hold_s_start}..{max_run_hold_s_end}"
            )
            _uo_log(
                f"[fast] heading_hold_count={hold_count_f} first_valid_idx={(first_valid_f if first_valid_f is not None else -1)} "
                + f"longest_hold_run={max_run_hold_f} run_range={max_run_hold_f_start}..{max_run_hold_f_end}"
            )
            slow_raw_wrap_jump_count = _uo_count_wrap_jumps(slow_err_wrapped, 0.5 * math.pi)
            fast_raw_wrap_jump_count = _uo_count_wrap_jumps(fast_err_wrapped, 0.5 * math.pi)
            _uo_log(f"[slow] wrap_delta_gt_pi_over_2_raw={slow_raw_wrap_jump_count}")
            _uo_log(f"[fast] wrap_delta_gt_pi_over_2_raw={fast_raw_wrap_jump_count}")
            for idx_s, prev_s, cur_s, d_s, abs_d_s in _uo_top_steps(slow_err_wrapped, wrapped_delta=True, top_n=10):
                _uo_log(
                    f"[slow] top_step_raw idx={idx_s} prev={prev_s:+.6f} cur={cur_s:+.6f} delta={d_s:+.6f} abs={abs_d_s:.6f}"
                )
            for idx_f, prev_f, cur_f, d_f, abs_d_f in _uo_top_steps(fast_err_wrapped, wrapped_delta=True, top_n=10):
                _uo_log(
                    f"[fast] top_step_raw idx={idx_f} prev={prev_f:+.6f} cur={cur_f:+.6f} delta={d_f:+.6f} abs={abs_d_f:.6f}"
                )

        slow_err_unwrapped = _unwrap_over_time(slow_err_wrapped)
        fast_err_unwrapped = _unwrap_over_time(fast_err_wrapped)
        if dbg_enabled:
            for idx_s, prev_s, cur_s, d_s, abs_d_s in _uo_top_steps(slow_err_unwrapped, wrapped_delta=False, top_n=10):
                _uo_log(
                    f"[slow] top_step_unwrapped idx={idx_s} prev={prev_s:+.6f} cur={cur_s:+.6f} delta={d_s:+.6f} abs={abs_d_s:.6f}"
                )
            for idx_f, prev_f, cur_f, d_f, abs_d_f in _uo_top_steps(fast_err_unwrapped, wrapped_delta=False, top_n=10):
                _uo_log(
                    f"[fast] top_step_unwrapped idx={idx_f} prev={prev_f:+.6f} cur={cur_f:+.6f} delta={d_f:+.6f} abs={abs_d_f:.6f}"
                )

        slow_err = list(slow_err_unwrapped)
        fast_err = list(fast_err_unwrapped)

        # Global bias removal on unwrapped series (full precomputed series, stable over playback).
        slow_bias = _median(slow_err)
        fast_bias = _median(fast_err)
        if dbg_enabled:
            _uo_log(f"[slow] bias={slow_bias:+.6f}")
            _uo_log(f"[fast] bias={fast_bias:+.6f}")

        if slow_err:
            slow_err = [float(v) - float(slow_bias) for v in slow_err]
        if fast_err:
            fast_err = [float(v) - float(fast_bias) for v in fast_err]

        slow_max_abs_before_clamp = 0.0
        fast_max_abs_before_clamp = 0.0
        if dbg_enabled:
            for v in slow_err:
                av = abs(float(v))
                if math.isfinite(av) and av > slow_max_abs_before_clamp:
                    slow_max_abs_before_clamp = float(av)
            for v in fast_err:
                av = abs(float(v))
                if math.isfinite(av) and av > fast_max_abs_before_clamp:
                    fast_max_abs_before_clamp = float(av)

        abs_vals: list[float] = []
        for v in slow_err:
            av = abs(float(v))
            if math.isfinite(av):
                abs_vals.append(float(av))
        for v in fast_err:
            av = abs(float(v))
            if math.isfinite(av):
                abs_vals.append(float(av))
        abs_vals.sort()
        y_abs_base = _percentile_sorted(abs_vals, 99.0)
        if y_abs_base < 1e-6:
            y_abs_base = 0.05

        slow_clamped_count = 0
        fast_clamped_count = 0
        slow_err_clamped: list[float] = []
        fast_err_clamped: list[float] = []
        y_min = -float(y_abs_base)
        y_max = float(y_abs_base)
        for v in slow_err:
            fv = float(v)
            if dbg_enabled and (fv < y_min or fv > y_max):
                slow_clamped_count += 1
            slow_err_clamped.append(float(_clamp(fv, y_min, y_max)))
        for v in fast_err:
            fv = float(v)
            if dbg_enabled and (fv < y_min or fv > y_max):
                fast_clamped_count += 1
            fast_err_clamped.append(float(_clamp(fv, y_min, y_max)))
        slow_err = slow_err_clamped
        fast_err = fast_err_clamped

        curve_center_pct = 0.0
        try:
            curve_center_pct = float(under_oversteer_curve_center)
        except Exception:
            curve_center_pct = 0.0
        curve_center_pct = float(_clamp(curve_center_pct, -50.0, 50.0))
        offset_units = (float(curve_center_pct) / 100.0) * (2.0 * float(y_abs_base))
        if abs(float(offset_units)) > 0.0:
            slow_err = [float(_clamp(float(v) + float(offset_units), y_min, y_max)) for v in slow_err]
            fast_err = [float(_clamp(float(v) + float(offset_units), y_min, y_max)) for v in fast_err]
        if hud_dbg:
            _log_print(
                f"[uo] curve_center_pct={curve_center_pct:+.6f} offset_units={offset_units:+.6f} y_abs_base={float(y_abs_base):+.6f}",
                log_file,
            )

        slow_max_abs_after_clamp = 0.0
        fast_max_abs_after_clamp = 0.0
        if dbg_enabled:
            for v in slow_err:
                av = abs(float(v))
                if math.isfinite(av) and av > slow_max_abs_after_clamp:
                    slow_max_abs_after_clamp = float(av)
            for v in fast_err:
                av = abs(float(v))
                if math.isfinite(av) and av > fast_max_abs_after_clamp:
                    fast_max_abs_after_clamp = float(av)

        headroom_ratio = 0.15
        y_abs = float(y_abs_base) * (1.0 + headroom_ratio)
        if dbg_enabled:
            _uo_log(
                f"[scale] y_abs_base_p99={y_abs_base:+.6f} y_abs={y_abs:+.6f} headroom_ratio={headroom_ratio:.3f}"
            )
            _uo_log(
                f"[slow] max_abs_before_clamp={slow_max_abs_before_clamp:+.6f} max_abs_after_clamp={slow_max_abs_after_clamp:+.6f} "
                + f"clamped_points={slow_clamped_count}/{len(slow_err)}"
            )
            _uo_log(
                f"[fast] max_abs_before_clamp={fast_max_abs_before_clamp:+.6f} max_abs_after_clamp={fast_max_abs_after_clamp:+.6f} "
                + f"clamped_points={fast_clamped_count}/{len(fast_err)}"
            )

        if dbg_spam:
            k0 = max(0, int(dbg_k) - 20)
            k1 = min(n_out - 1, int(dbg_k) + 20)
            _uo_log(
                f"[kdbg] window center_k={int(dbg_k)} range={k0}..{k1} cols=k,t_slow,t_fast,hdg_s,yaw_s,err_s_raw,err_s_unw,err_s_final,mov_s,hold_s,hdg_f,yaw_f,err_f_raw,err_f_unw,err_f_final,mov_f,hold_f"
            )
            for rec in dbg_rows:
                k_i = int(rec[0])
                if k_i < k0 or k_i > k1:
                    continue
                s_unw = float(slow_err_unwrapped[k_i]) if k_i < len(slow_err_unwrapped) else float("nan")
                s_fin = float(slow_err[k_i]) if k_i < len(slow_err) else float("nan")
                f_unw = float(fast_err_unwrapped[k_i]) if k_i < len(fast_err_unwrapped) else float("nan")
                f_fin = float(fast_err[k_i]) if k_i < len(fast_err) else float("nan")
                _uo_log(
                    "[kdbg] "
                    + f"{k_i},{float(rec[1]):+.6f},{float(rec[2]):+.6f},{float(rec[3]):+.6f},{float(rec[4]):+.6f},{float(rec[5]):+.6f},{s_unw:+.6f},{s_fin:+.6f},{float(rec[6]):+.6f},{int(rec[7])},"
                    + f"{float(rec[8]):+.6f},{float(rec[9]):+.6f},{float(rec[10]):+.6f},{f_unw:+.6f},{f_fin:+.6f},{float(rec[11]):+.6f},{int(rec[12])}"
                )

        return slow_err, fast_err, y_abs
    except Exception as e:
        _uo_log(f"exception type={type(e).__name__} msg={e}")
        return [], [], 1.0


def _compute_min_speed_display(speed_frames: list[float], fps: float, units: str) -> list[float]:
    if not speed_frames:
        return []

    u = (units or "kmh").strip().lower()
    if u not in ("kmh", "mph"):
        u = "kmh"

    # -5/+5 Regel: laut Story in km/h (oder mph wenn Anzeige mph ist)
    # Wir prÃ¼fen aber in m/s, weil Speed-Quelle m/s ist.
    if u == "mph":
        thr_ms = 5.0 / 2.2369362920544
    else:
        thr_ms = 5.0 / 3.6

    r = max(1.0, float(fps))
    one_sec = int(round(r))

    look_s = 5.0
    try:
        look_s = float((os.environ.get("IRVC_SPEED_MIN_LOOK_S") or "").strip() or "5")
    except Exception:
        look_s = 5.0
    look_n = max(1, int(round(max(0.5, look_s) * r)))

    events: list[int] = []
    n = len(speed_frames)
    for i in range(1, n - 1):
        v = float(speed_frames[i])
        if v <= float(speed_frames[i - 1]) and v <= float(speed_frames[i + 1]):
            lo = max(0, i - look_n)
            hi = min(n - 1, i + look_n)
            before_max = max(float(speed_frames[k]) for k in range(lo, i))
            after_max = max(float(speed_frames[k]) for k in range(i + 1, hi + 1))
            if before_max >= v + thr_ms and after_max >= v + thr_ms:
                events.append(i)

    out = [float(speed_frames[0])] * n
    cur_min = float(speed_frames[0])
    e_idx = 0

    for i in range(n):
        while e_idx < len(events):
            e = events[e_idx]
            show_from = max(0, e)  # Anzeige genau beim Ereignis
            if i >= show_from:
                cur_min = float(speed_frames[e])
                e_idx += 1
            else:
                break
        out[i] = float(cur_min)
    return out



def _build_sync_cache_maps_from_csv(
    slow_csv: Path,
    fast_csv: Path,
    fps: float,
    slow_duration_s: float,
    fast_duration_s: float,
) -> tuple[list[int], list[float], list[float], list[float] | None]:
    # Wie bisher: Slow->Fast Mapping + LapDist pro Slow-Frame
    # Neu: zusÃ¤tzlich Fast-Zeit pro Slow-Frame (fÃ¼r Stream-Sync / Segment-Warp)
    # Neu: optional Speed-Differenz pro Slow-Frame (fÃ¼r dynamische Segmentierung)
    from csv_g61 import get_float_col, has_col, load_g61_csv

    run_s = load_g61_csv(slow_csv)
    run_f = load_g61_csv(fast_csv)

    ld_s = get_float_col(run_s, "LapDistPct")
    ld_f = get_float_col(run_f, "LapDistPct")

    has_speed_s = has_col(run_s, "Speed")
    has_speed_f = has_col(run_f, "Speed")
    sp_s = get_float_col(run_s, "Speed") if has_speed_s else []
    sp_f = get_float_col(run_f, "Speed") if has_speed_f else []

    t_s = _csv_time_axis_or_fallback(run_s, slow_duration_s)
    t_f = _csv_time_axis_or_fallback(run_f, fast_duration_s)

    t_s = _force_strictly_increasing(t_s)
    t_f = _force_strictly_increasing(t_f)

    ld_su = _force_strictly_increasing(_unwrap_lapdist(ld_s))
    ld_fu = _force_strictly_increasing(_unwrap_lapdist(ld_f))

    n_slow = int(math.floor(max(0.0, slow_duration_s) * fps))
    n_fast = int(math.floor(max(0.0, fast_duration_s) * fps))
    if n_slow <= 0:
        n_slow = 1
    if n_fast <= 0:
        n_fast = 1

    n_samp_s = len(t_s)
    n_samp_f = len(t_f)
    if n_samp_s < 2 or n_samp_f < 2:
        raise RuntimeError("CSV hat zu wenige Samples fÃ¼r Sync.")

    out_fast_frame: list[int] = []
    out_ld: list[float] = []
    out_fast_time_s: list[float] = []
    out_speed_diff: list[float] | None = [] if (has_speed_s and has_speed_f and sp_s and sp_f) else None

    j_s = 0
    j_f = 0

    for n in range(n_slow):
        ts = n / fps

        # slow sample (interp by Time_s) -> stabil bei 30/60/120fps
        vs_interp = None

        if ts <= t_s[0]:
            x = float(ld_su[0])
            if out_speed_diff is not None:
                try:
                    vs_interp = float(sp_s[0])
                except Exception:
                    vs_interp = 0.0

        elif ts >= t_s[-1]:
            x = float(ld_su[n_samp_s - 1])
            if out_speed_diff is not None:
                try:
                    vs_interp = float(sp_s[n_samp_s - 1])
                except Exception:
                    vs_interp = 0.0

        else:
            while j_s < n_samp_s - 2 and t_s[j_s + 1] < ts:
                j_s += 1

            a_t = float(t_s[j_s])
            b_t = float(t_s[j_s + 1])
            den_t = (b_t - a_t)
            if abs(den_t) < 1e-12:
                alpha_t = 0.0
            else:
                alpha_t = (float(ts) - a_t) / den_t

            if alpha_t < 0.0:
                alpha_t = 0.0
            if alpha_t > 1.0:
                alpha_t = 1.0

            a_x = float(ld_su[j_s])
            b_x = float(ld_su[j_s + 1])
            x = a_x + (b_x - a_x) * alpha_t

            if out_speed_diff is not None:
                try:
                    vsa = float(sp_s[j_s])
                    vsb = float(sp_s[j_s + 1])
                    vs_interp = vsa + (vsb - vsa) * alpha_t
                except Exception:
                    vs_interp = 0.0

        out_ld.append(float(x))

        # fast lapdist index (interp)
        tf = 0.0
        vf_interp = None

        if x <= ld_fu[0]:
            tf = float(t_f[0])
            if out_speed_diff is not None:
                try:
                    vf_interp = float(sp_f[0])
                except Exception:
                    vf_interp = 0.0

        elif x >= ld_fu[-1]:
            tf = float(t_f[n_samp_f - 1])
            if out_speed_diff is not None:
                try:
                    vf_interp = float(sp_f[n_samp_f - 1])
                except Exception:
                    vf_interp = 0.0

        else:
            while j_f < n_samp_f - 2 and ld_fu[j_f + 1] < x:
                j_f += 1

            a = float(ld_fu[j_f])
            b = float(ld_fu[j_f + 1])
            ta = float(t_f[j_f])
            tb = float(t_f[j_f + 1])

            den = (b - a)
            if abs(den) < 1e-12:
                alpha = 0.0
            else:
                alpha = (float(x) - a) / den
            if alpha < 0.0:
                alpha = 0.0
            if alpha > 1.0:
                alpha = 1.0

            tf = ta + (tb - ta) * alpha

            if out_speed_diff is not None:
                try:
                    vfa = float(sp_f[j_f])
                    vfb = float(sp_f[j_f + 1])
                    vf_interp = vfa + (vfb - vfa) * alpha
                except Exception:
                    vf_interp = 0.0

        out_fast_time_s.append(float(tf))

        nf = int(round(float(tf) * fps))
        if nf < 0:
            nf = 0
        if nf > n_fast - 1:
            nf = n_fast - 1
        out_fast_frame.append(nf)

        if out_speed_diff is not None:
            try:
                vs = float(vs_interp) if vs_interp is not None else 0.0
                vf = float(vf_interp) if vf_interp is not None else 0.0
                out_speed_diff.append(abs(vs - vf))
            except Exception:
                out_speed_diff.append(0.0)


    return out_fast_frame, out_ld, out_fast_time_s, out_speed_diff


def _compute_common_cut_by_fast_time(
    fast_time_s: list[float],
    fast_duration_s: float,
    fps: float,
) -> tuple[int, int]:
    # Wir schneiden auf den Bereich, wo Fast-Zeit gÃ¼ltig ist.
    # Kein Freeze: Wir rendern nur i0..i1.
    if not fast_time_s:
        raise RuntimeError("sync: fast_time_s leer.")
    eps = 1.0 / max(1.0, float(fps))
    lo = 0.0
    hi = max(0.0, float(fast_duration_s) - eps)

    i0 = None
    i1 = None
    for i, tf in enumerate(fast_time_s):
        if tf >= lo and tf <= hi:
            i0 = i
            break
    for i in range(len(fast_time_s) - 1, -1, -1):
        tf = fast_time_s[i]
        if tf >= lo and tf <= hi:
            i1 = i
            break

    if i0 is None or i1 is None or i1 <= i0:
        raise RuntimeError("sync: kein gemeinsamer Bereich (Fast-Zeit ungueltig).")
    return int(i0), int(i1)


def _build_frame_window_mapping(
    *,
    i: int,
    before_f: int,
    after_f: int,
    fps: float,
    slow_frame_count: int,
    fast_frame_count: int,
    slow_to_fast_frame: list[int] | None,
    slow_frame_to_fast_time_s: list[float] | None,
) -> FrameWindowMapping:
    fps_safe = float(fps) if float(fps) > 1e-6 else 30.0
    i0 = int(i)
    b = max(1, int(before_f))
    a = max(1, int(after_f))
    n_slow = max(0, int(slow_frame_count))

    if n_slow <= 0:
        return FrameWindowMapping(
            i=i0,
            before_f=b,
            after_f=a,
            iL=0,
            iR=0,
            idxs=[],
            offsets=[],
            t_slow=[],
            fast_idx=[],
            t_fast=[],
        )

    iL = max(0, i0 - b)
    iR = min(n_slow - 1, i0 + a)
    if iR < iL:
        iR = iL

    idxs = list(range(int(iL), int(iR) + 1))
    offsets: list[int] = []
    t_slow: list[float] = []
    fast_idx: list[int] = []
    t_fast: list[float] = []

    fast_hi = int(fast_frame_count) - 1 if int(fast_frame_count) > 0 else None
    n_tf = len(slow_frame_to_fast_time_s) if slow_frame_to_fast_time_s else 0

    for idx in idxs:
        off = int(idx - i0)
        offsets.append(int(off))
        t_slow.append(float(idx) / float(fps_safe))

        fi = int(idx)
        if slow_to_fast_frame and idx < len(slow_to_fast_frame):
            try:
                fi = int(slow_to_fast_frame[idx])
            except Exception:
                fi = int(idx)
        if fi < 0:
            fi = 0
        if fast_hi is not None and fi > int(fast_hi):
            fi = int(fast_hi)
        fast_idx.append(int(fi))

        if n_tf > 0 and idx < n_tf:
            try:
                t_fast.append(float(slow_frame_to_fast_time_s[idx]))
            except Exception:
                t_fast.append(float(fi) / float(fps_safe))
        else:
            t_fast.append(float(fi) / float(fps_safe))

    return FrameWindowMapping(
        i=i0,
        before_f=b,
        after_f=a,
        iL=int(iL),
        iR=int(iR),
        idxs=idxs,
        offsets=offsets,
        t_slow=t_slow,
        fast_idx=fast_idx,
        t_fast=t_fast,
    )


def _build_hud_context(
    *,
    fps: float,
    cut_i0: int,
    cut_i1: int,
    geom: OutputGeometry,
    hud_enabled: Any | None,
    hud_boxes: Any | None,
    sync: HudSyncMapping,
    signals: HudSignals,
    window: HudWindowParams,
    settings: HudRenderSettings,
    log_file: Path | None = None,
) -> HudContext:
    return HudContext(
        fps=float(fps),
        cut_i0=int(cut_i0),
        cut_i1=int(cut_i1),
        geom=geom,
        hud_enabled=hud_enabled,
        hud_boxes=hud_boxes,
        sync=sync,
        signals=signals,
        window=window,
        settings=settings,
        log_file=log_file,
    )


def _render_hud_scroll_frames_png(
    out_dir: Path,
    ctx: HudContext,
) -> str | None:
    """
    Rendert pro Frame eine PNG (transparent) fÃ¼r die HUD-Spalte (geom.hud x geom.H).
    Gibt ffmpeg-Pattern zurÃ¼ck: ".../hud_%06d.png" oder None.
    """
    fps = float(ctx.fps)
    cut_i0 = int(ctx.cut_i0)
    cut_i1 = int(ctx.cut_i1)
    geom = ctx.geom
    hud_enabled = ctx.hud_enabled
    hud_boxes = ctx.hud_boxes

    slow_frame_to_lapdist = ctx.sync.slow_frame_to_lapdist
    slow_to_fast_frame = ctx.sync.slow_to_fast_frame
    slow_frame_to_fast_time_s = ctx.sync.slow_frame_to_fast_time_s

    slow_speed_frames = ctx.signals.slow_speed_frames
    fast_speed_frames = ctx.signals.fast_speed_frames
    slow_min_speed_frames = ctx.signals.slow_min_speed_frames
    fast_min_speed_frames = ctx.signals.fast_min_speed_frames
    slow_gear_frames = ctx.signals.slow_gear_frames
    fast_gear_frames = ctx.signals.fast_gear_frames
    slow_rpm_frames = ctx.signals.slow_rpm_frames
    fast_rpm_frames = ctx.signals.fast_rpm_frames
    slow_steer_frames = ctx.signals.slow_steer_frames
    fast_steer_frames = ctx.signals.fast_steer_frames
    slow_throttle_frames = ctx.signals.slow_throttle_frames
    fast_throttle_frames = ctx.signals.fast_throttle_frames
    slow_brake_frames = ctx.signals.slow_brake_frames
    fast_brake_frames = ctx.signals.fast_brake_frames
    slow_abs_frames = ctx.signals.slow_abs_frames
    fast_abs_frames = ctx.signals.fast_abs_frames
    line_delta_m_frames = ctx.signals.line_delta_m_frames
    line_delta_y_abs_m = ctx.signals.line_delta_y_abs_m
    under_oversteer_slow_frames = ctx.signals.under_oversteer_slow_frames
    under_oversteer_fast_frames = ctx.signals.under_oversteer_fast_frames
    under_oversteer_y_abs = ctx.signals.under_oversteer_y_abs

    before_s = float(ctx.window.before_s)
    after_s = float(ctx.window.after_s)
    hud_name = ctx.window.hud_name
    hud_windows = ctx.window.hud_windows

    hud_speed_units = str(ctx.settings.speed_units)
    hud_speed_update_hz = int(ctx.settings.speed_update_hz)
    hud_gear_rpm_update_hz = int(ctx.settings.gear_rpm_update_hz)
    hud_curve_points_default = int(ctx.settings.curve_points_default)
    hud_curve_points_overrides = ctx.settings.curve_points_overrides
    hud_pedals_sample_mode = str(getattr(ctx.settings, "pedals_sample_mode", "time") or "time").strip().lower()
    if hud_pedals_sample_mode not in ("time", "legacy"):
        hud_pedals_sample_mode = "time"
    try:
        hud_pedals_abs_debounce_ms = int(getattr(ctx.settings, "pedals_abs_debounce_ms", 60))
    except Exception:
        hud_pedals_abs_debounce_ms = 60
    if hud_pedals_abs_debounce_ms < 0:
        hud_pedals_abs_debounce_ms = 0
    if hud_pedals_abs_debounce_ms > 500:
        hud_pedals_abs_debounce_ms = 500

    log_file = ctx.log_file
    _ = hud_name

    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        try:
            import sys
            _log_print(f"[hudpy] python_exe={sys.executable}", log_file)
            _log_print(f"[hudpy] python_ver={sys.version.replace(chr(10), ' ')}", log_file)
        except Exception:
            pass
        _log_print(f"[hudpy] PIL fehlt -> installiere pillow (pip install pillow). Fehler: {e}", log_file)
        _log_print(f"[hudpy] Zielordner wÃ¤re: {out_dir}", log_file)
        return None

    if geom.hud <= 0:
        _log_print("[hudpy] geom.hud <= 0 -> kein HUD mÃ¶glich", log_file)
        return None

    # Alle aktiven HUD-Boxen (absolut im Output)
    boxes_abs = _enabled_hud_boxes_abs(geom=geom, hud_enabled=hud_enabled, hud_boxes=hud_boxes)
    if not boxes_abs:
        _log_print("[hudpy] keine aktive HUD-Box gefunden", log_file)
        return None

    # Story 5: Speed (Units + Update-Rate clamp)
    fps0 = float(fps) if float(fps) > 0.1 else 30.0
    fps_i = int(round(fps0))
    if fps_i <= 0:
        fps_i = 30

    u = (hud_speed_units or "kmh").strip().lower()
    if u not in ("kmh", "mph"):
        u = "kmh"

    if u == "mph":
        speed_factor = 2.2369362920544
        unit_label = "mph"
    else:
        speed_factor = 3.6
        unit_label = "km/h"

    hz = int(hud_speed_update_hz) if int(hud_speed_update_hz) > 0 else 60
    if hz < 1:
        hz = 1
    if hz > 60:
        hz = 60
    if hz > fps_i:
        hz = fps_i

    # alle N Frames aktualisieren (sonst Wert halten)
    every_n = int(round(fps0 / float(hz))) if hz > 0 else 1
    if every_n < 1:
        every_n = 1

    def _speed_to_units(frames_ms: list[float] | None) -> list[float]:
        if not frames_ms:
            return []
        out: list[float] = []
        for v in frames_ms:
            try:
                out.append(float(v) * float(speed_factor))
            except Exception:
                out.append(0.0)
        return out

    def _hold_every_n(frames_u: list[float] | None) -> list[float]:
        if not frames_u:
            return []
        out: list[float] = []
        last = float(frames_u[0])
        for idx, v in enumerate(frames_u):
            if idx % every_n == 0:
                try:
                    last = float(v)
                except Exception:
                    pass
            out.append(float(last))
        return out

    slow_speed_u = _hold_every_n(_speed_to_units(slow_speed_frames))
    fast_speed_u = _hold_every_n(_speed_to_units(fast_speed_frames))
    slow_min_u = _hold_every_n(_speed_to_units(slow_min_speed_frames))
    fast_min_u = _hold_every_n(_speed_to_units(fast_min_speed_frames))
    speed_max_peak_threshold_u = 5.0
    slow_max_u = build_confirmed_max_speed_display(slow_speed_u, threshold=float(speed_max_peak_threshold_u))
    fast_max_u = build_confirmed_max_speed_display(fast_speed_u, threshold=float(speed_max_peak_threshold_u))
    speed_axis_min_u = 0.0
    speed_axis_max_u = 1.0
    try:
        vmax = 0.0
        for arr in (slow_speed_u, fast_speed_u, slow_min_u, fast_min_u, slow_max_u, fast_max_u):
            if not arr:
                continue
            for vv in arr:
                try:
                    fv = float(vv)
                except Exception:
                    continue
                if not math.isfinite(fv):
                    continue
                if fv > vmax:
                    vmax = fv
        speed_axis_max_u = max(1.0, float(vmax))
    except Exception:
        speed_axis_max_u = 1.0

    # Story 6: Gear & RPM (Update-Rate clamp + Werte halten)
    gr_hz = int(hud_gear_rpm_update_hz) if int(hud_gear_rpm_update_hz) > 0 else 60
    if gr_hz < 1:
        gr_hz = 1
    if gr_hz > 60:
        gr_hz = 60
    if gr_hz > fps_i:
        gr_hz = fps_i

    gr_every_n = int(round(fps0 / float(gr_hz))) if gr_hz > 0 else 1
    if gr_every_n < 1:
        gr_every_n = 1

    def _hold_every_n_int(frames_any: list[Any] | None, every_n_local: int) -> list[int]:
        if not frames_any:
            return []
        out_i: list[int] = []
        last_i = 0
        for idx, v in enumerate(frames_any):
            if idx % every_n_local == 0:
                try:
                    last_i = int(v)
                except Exception:
                    try:
                        last_i = int(round(float(v)))
                    except Exception:
                        pass
            out_i.append(int(last_i))
        return out_i

    slow_gear_h = _hold_every_n_int(slow_gear_frames, gr_every_n)
    fast_gear_h = _hold_every_n_int(fast_gear_frames, gr_every_n)
    slow_rpm_h = _hold_every_n_int(slow_rpm_frames, gr_every_n)
    fast_rpm_h = _hold_every_n_int(fast_rpm_frames, gr_every_n)

    # Scroll-HUDs + Table-HUDs getrennt behandeln
    scroll_boxes_abs = [(n, b) for (n, b) in boxes_abs if n in _SCROLL_HUD_NAMES]
    table_boxes_abs = [(n, b) for (n, b) in boxes_abs if n in _TABLE_HUD_NAMES]

    if (not scroll_boxes_abs) and (not table_boxes_abs):
        _log_print("[hudpy] keine aktive HUD-Box (Scroll/Table) gefunden", log_file)
        return None

    # Table-HUD Boxen (Text), Koordinaten relativ zur HUD-Spalte
    table_items: list[tuple[str, int, int, int, int]] = []
    hud_x0 = int(getattr(geom, "left_w", 0))
    for name, box_abs in table_boxes_abs:
        try:
            x_abs, y_abs, w, h = box_abs
            if w <= 0 or h <= 0:
                continue
            x0 = int(x_abs) - int(hud_x0)
            y0 = int(y_abs)
            table_items.append((str(name), int(x0), int(y0), int(w), int(h)))
        except Exception:
            continue

    # Wir zeichnen alles in EIN Bild pro Frame (HUD-Spalte), aber pro HUD mit eigenem Fenster.
    hud_x0 = int(getattr(geom, "left_w", 0))
    hud_items: list[tuple[str, int, int, int, int]] = []
    for name, box_abs in scroll_boxes_abs:
        try:
            x_abs, y_abs, w, h = box_abs
            if w <= 0 or h <= 0:
                continue
            x0 = int(x_abs) - int(hud_x0)  # relativ zur HUD-Spalte
            y0 = int(y_abs)
            hud_items.append((str(name), int(x0), int(y0), int(w), int(h)))
        except Exception:
            continue

    if not hud_items:
        _log_print("[hudpy] keine gÃ¼ltige Scroll-HUD-Box (w/h/x/y) gefunden", log_file)
        return None

    # Parameter (Story 2): Default aus INI + Overrides pro HUD
    default_before_s = max(1e-6, float(before_s))
    default_after_s = max(1e-6, float(after_s))

    # Optional: ENV Ã¼berschreibt (Debug) -> gilt dann fÃ¼r ALLE HUDs
    env_before_s: float | None = None
    env_after_s: float | None = None
    try:
        v = (os.environ.get("IRVC_HUD_WINDOW_BEFORE") or "").strip()
        if v != "":
            env_before_s = max(1e-6, float(v))
    except Exception:
        env_before_s = None

    try:
        v = (os.environ.get("IRVC_HUD_WINDOW_AFTER") or "").strip()
        if v != "":
            env_after_s = max(1e-6, float(v))
    except Exception:
        env_after_s = None

    # pro HUD: before_s/after_s sammeln
    hud_params: dict[str, dict[str, float]] = {}
    ovs = hud_windows if isinstance(hud_windows, dict) else None
    for name, _x0, _y0, _w, _h in hud_items:
        b = float(default_before_s)
        a = float(default_after_s)
        try:
            if ovs and isinstance(ovs.get(name), dict):
                o = ovs.get(name) or {}
                if o.get("before_s") is not None:
                    b = float(o.get("before_s"))
                if o.get("after_s") is not None:
                    a = float(o.get("after_s"))
        except Exception:
            pass

        if env_before_s is not None:
            b = float(env_before_s)
        if env_after_s is not None:
            a = float(env_after_s)

        hud_params[str(name)] = {"before_s": max(1e-6, float(b)), "after_s": max(1e-6, float(a))}

    def _resolve_hud_window_seconds(hud_name_local: str) -> tuple[float, float]:
        b2 = float(default_before_s)
        a2 = float(default_after_s)
        try:
            if isinstance(hud_windows, dict) and isinstance(hud_windows.get(hud_name_local), dict):
                o2 = hud_windows.get(hud_name_local) or {}
                if o2.get("before_s") is not None:
                    b2 = float(o2.get("before_s"))
                if o2.get("after_s") is not None:
                    a2 = float(o2.get("after_s"))
        except Exception:
            pass
        try:
            p2 = hud_params.get(hud_name_local) or {}
            if p2.get("before_s") is not None:
                b2 = float(p2.get("before_s"))
            if p2.get("after_s") is not None:
                a2 = float(p2.get("after_s"))
        except Exception:
            pass
        if env_before_s is not None:
            b2 = float(env_before_s)
        if env_after_s is not None:
            a2 = float(env_after_s)
        return max(1e-6, float(b2)), max(1e-6, float(a2))

    try:
        step = float((os.environ.get("IRVC_HUD_TICK_STEP") or "").strip() or "0.01")
    except Exception:
        step = 0.01
    step = max(1e-6, float(step))

    try:
        max_ticks = int((os.environ.get("IRVC_HUD_MAX_TICKS") or "").strip() or "21")
    except Exception:
        max_ticks = 21
    if max_ticks < 3:
        max_ticks = 3
    if max_ticks > 99:
        max_ticks = 99


    out_dir.mkdir(parents=True, exist_ok=True)

    # FÃ¼r sauberen Test: alte Frames lÃ¶schen
    try:
        for p in out_dir.glob("hud_*.png"):
            p.unlink()
    except Exception:
        pass
        
    # Extra: Sample-Ordner (1 Bild pro Sekunde) zum schnellen PrÃ¼fen
    samples_dir = out_dir / "_samples"
    try:
        samples_dir.mkdir(parents=True, exist_ok=True)
        for p in samples_dir.glob("hud_sample_*.png"):
            p.unlink()
    except Exception:
        pass

    r = max(1.0, float(fps))
    tick_w = 2
    half = max(1, (max_ticks - 1) // 2)

    frames = max(0, int(cut_i1) - int(cut_i0))
    if frames <= 0:
        _log_print("[hudpy] frames <= 0", log_file)
        return None

    fast_frame_count = 0
    try:
        fast_candidates = (
            fast_speed_frames,
            fast_min_speed_frames,
            fast_gear_frames,
            fast_rpm_frames,
            fast_steer_frames,
            fast_throttle_frames,
            fast_brake_frames,
            fast_abs_frames,
            under_oversteer_fast_frames,
        )
        for arr in fast_candidates:
            if arr:
                fast_frame_count = max(int(fast_frame_count), int(len(arr)))
        if slow_to_fast_frame:
            for v_f in slow_to_fast_frame:
                try:
                    fi = int(v_f)
                except Exception:
                    continue
                if fi >= 0:
                    fast_frame_count = max(int(fast_frame_count), int(fi) + 1)
    except Exception:
        fast_frame_count = int(max(0, int(fast_frame_count)))

    hud_dbg = (os.environ.get("IRVC_HUD_DEBUG") or "0").strip() in ("1", "true", "yes", "on")
    if hud_dbg:
        try:
            names = ",".join([n for (n, _x0, _y0, _w, _h) in hud_items])
        except Exception:
            names = ""
        _log_print(
            f"[hudpy] render PNGs: dir={out_dir} fps={r:.3f} frames={frames} huds=[{names}] default_before_s={default_before_s:.3f} default_after_s={default_after_s:.3f} step={step:.6f} max_ticks={max_ticks}",
            log_file,
        )
        
    # Story 3: Steering Skalierung (Y = -1..+1) Ã¼ber max Ausschlag (beide CSVs)
    def _clamp(v: float, lo: float, hi: float) -> float:
        if v < lo:
            return lo
        if v > hi:
            return hi
        return v

    steer_abs_max = 1.0
    steer_abs_raw = 0.0
    try:
        m = 0.0
        if slow_steer_frames:
            for x in slow_steer_frames:
                try:
                    v = float(x)
                    if v != v:  # NaN
                        continue
                    ax = abs(v)
                    if ax > m:
                        m = ax
                except Exception:
                    pass
        if fast_steer_frames:
            for x in fast_steer_frames:
                try:
                    v = float(x)
                    if v != v:  # NaN
                        continue
                    ax = abs(v)
                    if ax > m:
                        m = ax
                except Exception:
                    pass

        steer_abs_raw = float(m)

        # Sicherheitsgrenze: wenn ein Ausreisser drin ist, wird sonst alles flach.
        # 720Â° ist absichtlich gross, aber verhindert kaputte Extremwerte.
        steer_abs_max = float(_clamp(float(m), 1e-6, 720.0))

        if steer_abs_raw > 720.0:
            _log_print(f"[hudpy] SteeringWheelAngle: raw_abs_max={steer_abs_raw:.3f} capped_to={steer_abs_max:.3f}", log_file)
        else:
            _log_print(f"[hudpy] SteeringWheelAngle: abs_max={steer_abs_max:.3f}", log_file)

    except Exception:
        steer_abs_max = 1.0
        
    # Steering: CSV (60 Hz) auf Video-Frame-Index abbilden
    
    # Story 7: Delta (Time delta) â€“ globale Y-Skalierung: min/max Delta Ã¼ber alle Frames (Cut-Bereich)
    delta_min_s = 0.0
    delta_max_s = 0.0
    try:
        fps_safe = float(fps) if float(fps) > 0.1 else 30.0
        i0 = max(0, int(cut_i0))
        i1 = min(len(slow_frame_to_lapdist), int(cut_i1))

        # bevorzugt: glatte Fast-Zeit (interp aus Sync-Map)
        if slow_frame_to_fast_time_s:
            nmap = len(slow_frame_to_fast_time_s)
            if i1 > nmap:
                i1 = nmap

            first = True
            for ii in range(i0, i1):
                slow_t = float(ii) / fps_safe
                fast_t = float(slow_frame_to_fast_time_s[ii])
                d = float(slow_t - fast_t)  # >0: slow ist langsamer, <0: fast ist langsamer
                if first:
                    delta_min_s = d
                    delta_max_s = d
                    first = False
                else:
                    if d < delta_min_s:
                        delta_min_s = d
                    if d > delta_max_s:
                        delta_max_s = d

        # fallback: Frame-Mapping (treppiger)
        elif slow_to_fast_frame:
            nmap = len(slow_to_fast_frame)
            if i1 > nmap:
                i1 = nmap

            first = True
            for ii in range(i0, i1):
                fi = int(slow_to_fast_frame[ii])
                if fi < 0:
                    continue
                slow_t = float(ii) / fps_safe
                fast_t = float(fi) / fps_safe
                d = float(slow_t - fast_t)
                if first:
                    delta_min_s = d
                    delta_max_s = d
                    first = False
                else:
                    if d < delta_min_s:
                        delta_min_s = d
                    if d > delta_max_s:
                        delta_max_s = d
    except Exception:
        delta_min_s = 0.0
        delta_max_s = 0.0

    # robust: nie 0 Range
    if float(delta_max_s - delta_min_s) < 1e-6:
        delta_max_s = float(delta_min_s) + 1e-6

    try:
        _log_print(f"[hudpy] Delta: min={delta_min_s:.6f} s max={delta_max_s:.6f} s", log_file)
    except Exception:
        pass
    
    steer_slow_scale = 1.0
    steer_fast_scale = 1.0
    try:
        n_frames = float(max(1, len(slow_frame_to_lapdist) - 1))
        if slow_steer_frames and len(slow_steer_frames) >= 2:
            steer_slow_scale = float(len(slow_steer_frames) - 1) / n_frames
        if fast_steer_frames and len(fast_steer_frames) >= 2:
            steer_fast_scale = float(len(fast_steer_frames) - 1) / n_frames
    except Exception:
        steer_slow_scale = 1.0
        steer_fast_scale = 1.0

    # Story 7: Delta (Time delta) â€“ Y-Skalierung vorberechnen im Cut-Bereich
    # Delta = slow_time - fast_time, basierend auf der Sync-Map slow_frame_to_fast_time_s
    delta_pos_max = 0.0   # grÃ¶ÃŸtes positives Delta (slow langsamer)
    delta_neg_min = 0.0   # kleinstes negatives Delta (fast langsamer, also < 0)
    delta_has_neg = False
    try:
        fps_safe = float(r) if float(r) > 0.1 else 30.0
        if slow_frame_to_fast_time_s:
            i0 = max(0, int(cut_i0))
            i1 = min(len(slow_frame_to_fast_time_s), int(cut_i1))
            for ii in range(i0, i1):
                slow_t = float(ii) / fps_safe
                fast_t = float(slow_frame_to_fast_time_s[ii])
                d = float(slow_t - fast_t)
                if d >= 0.0:
                    if d > delta_pos_max:
                        delta_pos_max = d
                else:
                    if d < delta_neg_min:
                        delta_neg_min = d
    except Exception as e:
        try:
            _log_print(
                f"[hudpy][Delta][EXC][prepare] {type(e).__name__}: {e}",
                log_file,
            )
        except Exception:
            pass
        delta_pos_max = 0.0
        delta_neg_min = 0.0

    if delta_neg_min < -1e-9:
        delta_has_neg = True

    # Schutz gegen 0-Division
    if delta_pos_max < 1e-6:
        delta_pos_max = 1e-6
    if delta_has_neg and abs(delta_neg_min) < 1e-6:
        delta_neg_min = -1e-6

    def _active_hud_items_for_frame(
        items: list[tuple[str, int, int, int, int]]
    ) -> list[tuple[str, int, int, int, int]]:
        if not items:
            return []

        items_by_name: dict[str, list[tuple[str, int, int, int, int]]] = {}
        for item in items:
            nm = str(item[0])
            items_by_name.setdefault(nm, []).append(item)

        ordered_names: list[str] = []
        try:
            if isinstance(hud_boxes, dict):
                for k, v in hud_boxes.items():
                    if isinstance(v, dict):
                        nm = str(v.get("name") or k)
                    else:
                        nm = str(k)
                    ordered_names.append(nm)
            elif isinstance(hud_boxes, list):
                for b in hud_boxes:
                    if not isinstance(b, dict):
                        continue
                    nm = str(b.get("name") or b.get("id") or "")
                    if nm:
                        ordered_names.append(nm)
        except Exception:
            ordered_names = []

        if not ordered_names:
            return list(items)

        enabled_names: set[str] = set()
        try:
            if isinstance(hud_enabled, dict):
                for k, v in hud_enabled.items():
                    if bool(v):
                        enabled_names.add(str(k))
            elif isinstance(hud_enabled, (list, tuple, set)):
                for k in hud_enabled:
                    enabled_names.add(str(k))
        except Exception:
            enabled_names = set()

        filter_active = len(enabled_names) > 0

        active: list[tuple[str, int, int, int, int]] = []
        for nm in ordered_names:
            if filter_active and (nm not in enabled_names):
                continue
            bucket = items_by_name.get(nm)
            if bucket:
                active.append(bucket.pop(0))

        if active:
            return active
        if filter_active:
            return []
        return list(items)

    # Story 2.2: Persistenter Subpixel-Scrollzustand pro Scroll-HUD-Instanz.
    # Key basiert auf HUD-Key + Box-Geometrie innerhalb der HUD-Spalte.
    scroll_state_by_hud: dict[str, dict[str, Any]] = {}

    for j in range(frames):

        
        i = int(cut_i0) + j
        if i < 0 or i >= len(slow_frame_to_lapdist):
            continue

        img = Image.new("RGBA", (int(geom.hud), int(geom.H)), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)

        ld = float(slow_frame_to_lapdist[i]) % 1.0
        ld_mod = ld % step
        active_table_items = _active_hud_items_for_frame(table_items)
        active_scroll_items = _active_hud_items_for_frame(hud_items)
        global_before_f = 1
        global_after_f = 1
        if active_scroll_items:
            for hud_name_local, _x0, _y0, _w, _h in active_scroll_items:
                b_s_h, a_s_h = _resolve_hud_window_seconds(str(hud_name_local))
                bf_h = max(1, int(round(float(b_s_h) * float(r))))
                af_h = max(1, int(round(float(a_s_h) * float(r))))
                if bf_h > global_before_f:
                    global_before_f = int(bf_h)
                if af_h > global_after_f:
                    global_after_f = int(af_h)
        frame_window_mapping = _build_frame_window_mapping(
            i=int(i),
            before_f=int(global_before_f),
            after_f=int(global_after_f),
            fps=float(r),
            slow_frame_count=len(slow_frame_to_lapdist),
            fast_frame_count=int(fast_frame_count),
            slow_to_fast_frame=slow_to_fast_frame,
            slow_frame_to_fast_time_s=slow_frame_to_fast_time_s,
        )


        # Table-HUDs (Speed, Gear & RPM) als Text
        if table_items and slow_to_fast_frame and i < len(slow_to_fast_frame):
            fi = int(slow_to_fast_frame[i])
            if fi < 0:
                fi = 0

            for hud_key, x0, y0, w, h in active_table_items:
                try:
                    dr.rectangle(
                        [int(x0), int(y0), int(x0 + w - 1), int(y0 + h - 1)],
                        fill=COL_HUD_BG,
                    )
                    def _hud_table_speed() -> None:
                        speed_ctx = {
                            "fps": fps,
                            "i": i,
                            "fi": fi,
                            "slow_speed_frames": slow_speed_frames,
                            "fast_speed_frames": fast_speed_frames,
                            "slow_min_speed_frames": slow_min_speed_frames,
                            "fast_min_speed_frames": fast_min_speed_frames,
                            "hud_speed_units": hud_speed_units,
                            "hud_speed_update_hz": hud_speed_update_hz,
                            "slow_speed_u": slow_speed_u,
                            "fast_speed_u": fast_speed_u,
                            "slow_min_u": slow_min_u,
                            "fast_min_u": fast_min_u,
                            "slow_max_u": slow_max_u,
                            "fast_max_u": fast_max_u,
                            "speed_axis_min_u": speed_axis_min_u,
                            "speed_axis_max_u": speed_axis_max_u,
                            "unit_label": unit_label,
                            "hud_key": hud_key,
                            "COL_SLOW_DARKRED": COL_SLOW_DARKRED,
                            "COL_FAST_DARKBLUE": COL_FAST_DARKBLUE,
                        }
                        render_speed(speed_ctx, (x0, y0, w, h), dr)

                    def _hud_table_gear_rpm() -> None:
                        gear_rpm_ctx = {
                            "hud_key": hud_key,
                            "i": i,
                            "fi": fi,
                            "slow_gear_h": slow_gear_h,
                            "fast_gear_h": fast_gear_h,
                            "slow_rpm_h": slow_rpm_h,
                            "fast_rpm_h": fast_rpm_h,
                            "COL_SLOW_DARKRED": COL_SLOW_DARKRED,
                            "COL_FAST_DARKBLUE": COL_FAST_DARKBLUE,
                        }
                        render_gear_rpm(gear_rpm_ctx, (x0, y0, w, h), dr)

                    table_renderers = {
                        "Speed": _hud_table_speed,
                        "Gear & RPM": _hud_table_gear_rpm,
                    }
                    fn_tbl = table_renderers.get(hud_key)
                    if fn_tbl is not None:
                        fn_tbl()
                except Exception:
                    continue


        # Wir zeichnen alle Scroll-HUDs in dieses eine Bild.
        for hud_key, x0, y0, w, h in active_scroll_items:
            try:
                before_s_h, after_s_h = _resolve_hud_window_seconds(str(hud_key))
                before_f = max(1, int(round(before_s_h * r)))
                after_f = max(1, int(round(after_s_h * r)))
                # Story 2.2: Scroll-HUDs symmetrisch behandeln (before_f == after_f).
                if before_f != after_f:
                    win_f = max(int(before_f), int(after_f))
                    before_f = int(win_f)
                    after_f = int(win_f)

                # Story 2.2:
                # shift_px_per_frame ist die Pixelverschiebung pro Frame.
                # window_frames ist HUD-lokal (inkl. Zentrum), daher pro HUD unterschiedlich.
                window_frames = max(1, int(before_f) + int(after_f) + 1)
                hud_width_px = max(1, int(w))
                shift_px_per_frame = float(hud_width_px) / float(window_frames)

                # Fenster in Frames (Zeit-Achse): stabiler als LapDist-Spannen
                iL = max(0, i - int(before_f))
                iR = min(len(slow_frame_to_lapdist) - 1, i + int(after_f))

                def _right_edge_sample_idx() -> int:
                    idx_r = int(iR)
                    if idx_r < 0:
                        idx_r = 0
                    if idx_r >= len(slow_frame_to_lapdist):
                        idx_r = len(slow_frame_to_lapdist) - 1
                    return int(idx_r)

                is_throttle_brake = str(hud_key) == "Throttle / Brake"
                is_steering = str(hud_key) == "Steering"
                is_delta = str(hud_key) == "Delta"
                is_line_delta = str(hud_key) == "Line Delta"
                is_under_oversteer = str(hud_key) == "Under-/Oversteer"
                tb_layout: dict[str, Any] = {}
                tb_map_idx_to_t_slow: dict[int, float] = {}
                tb_map_idx_to_t_fast: dict[int, float] = {}
                tb_map_idx_to_fast_idx: dict[int, int] = {}
                tb_seconds_per_col = 0.0
                tb_abs_window_s = 0.0
                tb_fps_safe = 30.0
                tb_t_slow_scale = 1.0
                tb_t_fast_scale = 1.0
                tb_b_slow_scale = 1.0
                tb_b_fast_scale = 1.0
                tb_a_slow_scale = 1.0
                tb_a_fast_scale = 1.0
                tb_slow_abs_prefix: list[int] = [0]
                tb_fast_abs_prefix: list[int] = [0]

                if is_throttle_brake:
                    map_idxs_all_tb = list(getattr(frame_window_mapping, "idxs", []) or [])
                    map_t_slow_all_tb = list(getattr(frame_window_mapping, "t_slow", []) or [])
                    map_t_fast_all_tb = list(getattr(frame_window_mapping, "t_fast", []) or [])
                    map_fast_idx_all_tb = list(getattr(frame_window_mapping, "fast_idx", []) or [])
                    if map_idxs_all_tb and len(map_idxs_all_tb) == len(map_t_slow_all_tb):
                        for idx_m, ts_m in zip(map_idxs_all_tb, map_t_slow_all_tb):
                            tb_map_idx_to_t_slow[int(idx_m)] = float(ts_m)
                    if map_idxs_all_tb and len(map_idxs_all_tb) == len(map_t_fast_all_tb):
                        for idx_m, tf_m in zip(map_idxs_all_tb, map_t_fast_all_tb):
                            tb_map_idx_to_t_fast[int(idx_m)] = float(tf_m)
                    if map_idxs_all_tb and len(map_idxs_all_tb) == len(map_fast_idx_all_tb):
                        for idx_m, fi_m in zip(map_idxs_all_tb, map_fast_idx_all_tb):
                            tb_map_idx_to_fast_idx[int(idx_m)] = int(fi_m)

                    tb_fps_safe = float(fps) if (math.isfinite(float(fps)) and float(fps) > 1e-6) else 30.0
                    tb_abs_window_s = float(max(0, int(hud_pedals_abs_debounce_ms))) / 1000.0
                    tb_seconds_per_col = float(window_frames) / max(1.0, float(w)) / max(1e-6, float(tb_fps_safe))

                    n_frames_tb = float(max(1, len(slow_frame_to_lapdist) - 1))
                    try:
                        if slow_throttle_frames and len(slow_throttle_frames) >= 2:
                            tb_t_slow_scale = float(len(slow_throttle_frames) - 1) / n_frames_tb
                        if fast_throttle_frames and len(fast_throttle_frames) >= 2:
                            tb_t_fast_scale = float(len(fast_throttle_frames) - 1) / n_frames_tb
                        if slow_brake_frames and len(slow_brake_frames) >= 2:
                            tb_b_slow_scale = float(len(slow_brake_frames) - 1) / n_frames_tb
                        if fast_brake_frames and len(fast_brake_frames) >= 2:
                            tb_b_fast_scale = float(len(fast_brake_frames) - 1) / n_frames_tb
                        if slow_abs_frames and len(slow_abs_frames) >= 2:
                            tb_a_slow_scale = float(len(slow_abs_frames) - 1) / n_frames_tb
                        if fast_abs_frames and len(fast_abs_frames) >= 2:
                            tb_a_fast_scale = float(len(fast_abs_frames) - 1) / n_frames_tb
                    except Exception:
                        pass

                    def _tb_load_font(sz: int):
                        try:
                            from PIL import ImageFont
                        except Exception:
                            ImageFont = None  # type: ignore
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

                    try:
                        tb_headroom = float((os.environ.get("IRVC_PEDAL_HEADROOM") or "").strip() or "1.12")
                    except Exception:
                        tb_headroom = 1.12
                    if tb_headroom < 1.00:
                        tb_headroom = 1.00
                    if tb_headroom > 2.00:
                        tb_headroom = 2.00

                    tb_font_sz = int(round(max(10.0, min(18.0, float(h) * 0.13))))
                    tb_font_val_sz = int(round(max(11.0, min(20.0, float(h) * 0.15))))
                    tb_font_title = _tb_load_font(tb_font_sz)
                    tb_font_val = _tb_load_font(tb_font_val_sz)
                    tb_font_axis = _tb_load_font(max(8, int(tb_font_sz - 2)))
                    tb_font_axis_small = _tb_load_font(max(7, int(tb_font_sz - 3)))

                    tb_y_txt = int(2)
                    tb_abs_h = int(max(10, min(15, round(float(h) * 0.085))))
                    tb_abs_gap_y = 2
                    tb_y_abs0 = int(tb_font_val_sz + 5)
                    tb_y_abs_s = tb_y_abs0
                    tb_y_abs_f = tb_y_abs0 + tb_abs_h + tb_abs_gap_y
                    tb_plot_top = tb_y_abs_f + tb_abs_h + 4
                    tb_plot_bottom = int(h - 2)
                    if tb_plot_bottom <= tb_plot_top + 5:
                        tb_plot_top = int(max(0, int(float(h) * 0.30)))
                    tb_plot_h = max(10, tb_plot_bottom - tb_plot_top)
                    tb_mx = int(w // 2)
                    tb_half_w = float(w) / 2.0
                    tb_marker_xf = float(tb_mx)

                    def _tb_y_from_01(v01: float) -> int:
                        v01_c = _clamp(float(v01), 0.0, 1.0)
                        v_scaled = float(v01_c) / max(1.0, float(tb_headroom))
                        yy = float(tb_plot_top) + float(tb_plot_h) - (v_scaled * float(tb_plot_h))
                        return int(round(yy))

                    tb_layout = {
                        "font_title": tb_font_title,
                        "font_val": tb_font_val,
                        "font_axis": tb_font_axis,
                        "font_axis_small": tb_font_axis_small,
                        "y_txt": int(tb_y_txt),
                        "abs_h": int(tb_abs_h),
                        "y_abs_s": int(tb_y_abs_s),
                        "y_abs_f": int(tb_y_abs_f),
                        "plot_top": int(tb_plot_top),
                        "plot_bottom": int(tb_plot_bottom),
                        "mx": int(tb_mx),
                        "marker_xf": float(tb_marker_xf),
                        "half_w": float(tb_half_w),
                        "y_from_01": _tb_y_from_01,
                    }

                    def _tb_sample_linear_time(vals: list[float] | None, t_s: float) -> float:
                        if not vals:
                            return 0.0
                        n = len(vals)
                        if n <= 1:
                            return float(vals[0])
                        pos = _clamp(float(t_s) * float(tb_fps_safe), 0.0, float(n - 1))
                        i0_t = int(math.floor(pos))
                        i1_t = min(i0_t + 1, n - 1)
                        a_t = float(pos - float(i0_t))
                        v0_t = float(vals[i0_t])
                        v1_t = float(vals[i1_t])
                        return float(v0_t + ((v1_t - v0_t) * a_t))

                    def _tb_fast_time_from_slow_idx(idx0: int) -> float:
                        ii = int(idx0)
                        if ii < 0:
                            ii = 0
                        if slow_frame_to_fast_time_s:
                            if ii >= len(slow_frame_to_fast_time_s):
                                ii = len(slow_frame_to_fast_time_s) - 1
                            return float(slow_frame_to_fast_time_s[ii])
                        fi_t = int(ii)
                        if slow_to_fast_frame and fi_t < len(slow_to_fast_frame):
                            fi_t = int(slow_to_fast_frame[fi_t])
                            if fi_t < 0:
                                fi_t = 0
                        return float(fi_t) / float(tb_fps_safe)

                    def _tb_build_abs_prefix(vals: list[float] | None) -> list[int]:
                        if not vals:
                            return [0]
                        out_pref = [0]
                        acc_pref = 0
                        for vv_pref in vals:
                            acc_pref += 1 if float(vv_pref) >= 0.5 else 0
                            out_pref.append(acc_pref)
                        return out_pref

                    tb_slow_abs_prefix = _tb_build_abs_prefix(slow_abs_frames)
                    tb_fast_abs_prefix = _tb_build_abs_prefix(fast_abs_frames)

                    def _tb_abs_on_majority_time(vals: list[float] | None, pref: list[int], t_s: float) -> float:
                        if not vals:
                            return 0.0
                        n = len(vals)
                        if n <= 1:
                            return 1.0 if float(vals[0]) >= 0.5 else 0.0
                        if tb_abs_window_s <= 1e-9:
                            p = int(round(_clamp(float(t_s) * float(tb_fps_safe), 0.0, float(n - 1))))
                            return 1.0 if float(vals[p]) >= 0.5 else 0.0
                        half_abs = 0.5 * float(tb_abs_window_s)
                        p0 = int(math.floor((float(t_s) - half_abs) * float(tb_fps_safe)))
                        p1 = int(math.ceil((float(t_s) + half_abs) * float(tb_fps_safe)))
                        if p0 < 0:
                            p0 = 0
                        if p1 >= n:
                            p1 = n - 1
                        if p1 < p0:
                            p1 = p0
                        total = int(p1 - p0 + 1)
                        on_cnt = int(pref[p1 + 1] - pref[p0])
                        return 1.0 if (on_cnt * 2) > total else 0.0

                    def _tb_sample_legacy(vals: list[float] | None, idx_base: int, scale: float) -> float:
                        if not vals:
                            return 0.0
                        i_legacy = int(round(float(idx_base) * float(scale)))
                        if i_legacy < 0:
                            i_legacy = 0
                        if i_legacy >= len(vals):
                            i_legacy = len(vals) - 1
                        try:
                            return float(vals[i_legacy])
                        except Exception:
                            return 0.0

                    def _tb_sample_column(x_col: int) -> dict[str, Any]:
                        xi = int(x_col)
                        if xi < 0:
                            xi = 0
                        if xi >= int(w):
                            xi = int(w) - 1

                        frac = (float(xi) - float(tb_layout["marker_xf"])) / max(1.0, float(tb_layout["half_w"]))
                        frac = _clamp(float(frac), -1.0, 1.0)
                        if frac <= 0.0:
                            off_f = float(frac) * float(before_f)
                        else:
                            off_f = float(frac) * float(after_f)

                        idx_slow = int(round(float(i) + float(off_f)))
                        if idx_slow < int(iL):
                            idx_slow = int(iL)
                        if idx_slow > int(iR):
                            idx_slow = int(iR)
                        if idx_slow < 0:
                            idx_slow = 0
                        if idx_slow >= len(slow_frame_to_lapdist):
                            idx_slow = len(slow_frame_to_lapdist) - 1

                        t_slow = float(tb_map_idx_to_t_slow.get(int(idx_slow), float(idx_slow) / float(tb_fps_safe)))
                        t_fast = float(tb_map_idx_to_t_fast.get(int(idx_slow), _tb_fast_time_from_slow_idx(int(idx_slow))))
                        fi_map = int(tb_map_idx_to_fast_idx.get(int(idx_slow), int(idx_slow)))
                        if fi_map < 0:
                            fi_map = 0
                        if slow_to_fast_frame and int(idx_slow) < len(slow_to_fast_frame):
                            try:
                                fi_map = int(slow_to_fast_frame[int(idx_slow)])
                                if fi_map < 0:
                                    fi_map = 0
                            except Exception:
                                pass

                        if hud_pedals_sample_mode == "time":
                            s_t = _tb_sample_linear_time(slow_throttle_frames, float(t_slow))
                            s_b = _tb_sample_linear_time(slow_brake_frames, float(t_slow))
                            f_t = _tb_sample_linear_time(fast_throttle_frames, float(t_fast))
                            f_b = _tb_sample_linear_time(fast_brake_frames, float(t_fast))
                            abs_s_raw = _tb_abs_on_majority_time(slow_abs_frames, tb_slow_abs_prefix, float(t_slow))
                            abs_f_raw = _tb_abs_on_majority_time(fast_abs_frames, tb_fast_abs_prefix, float(t_fast))
                        else:
                            s_t = _tb_sample_legacy(slow_throttle_frames, int(idx_slow), float(tb_t_slow_scale))
                            s_b = _tb_sample_legacy(slow_brake_frames, int(idx_slow), float(tb_b_slow_scale))
                            f_t = _tb_sample_legacy(fast_throttle_frames, int(fi_map), float(tb_t_fast_scale))
                            f_b = _tb_sample_legacy(fast_brake_frames, int(fi_map), float(tb_b_fast_scale))
                            abs_s_raw = _tb_sample_legacy(slow_abs_frames, int(idx_slow), float(tb_a_slow_scale))
                            abs_f_raw = _tb_sample_legacy(fast_abs_frames, int(fi_map), float(tb_a_fast_scale))

                        y_s_t = int(tb_layout["y_from_01"](float(s_t)))
                        y_s_b = int(tb_layout["y_from_01"](float(s_b)))
                        y_f_t = int(tb_layout["y_from_01"](float(f_t)))
                        y_f_b = int(tb_layout["y_from_01"](float(f_b)))
                        return {
                            "x": int(xi),
                            "slow_idx": int(idx_slow),
                            "s_t": float(s_t),
                            "s_b": float(s_b),
                            "f_t": float(f_t),
                            "f_b": float(f_b),
                            "y_s_t": int(y_s_t),
                            "y_s_b": int(y_s_b),
                            "y_f_t": int(y_f_t),
                            "y_f_b": int(y_f_b),
                            "abs_s_raw_on": bool(float(abs_s_raw) >= 0.5),
                            "abs_f_raw_on": bool(float(abs_f_raw) >= 0.5),
                        }

                    def _tb_apply_abs_debounce(tb_state_local: dict[str, Any], raw_s_on: bool, raw_f_on: bool) -> tuple[bool, bool]:
                        if float(tb_abs_window_s) <= 1e-9:
                            tb_state_local["tb_abs_s_on"] = bool(raw_s_on)
                            tb_state_local["tb_abs_f_on"] = bool(raw_f_on)
                            tb_state_local["tb_abs_s_on_count"] = 0
                            tb_state_local["tb_abs_s_off_count"] = 0
                            tb_state_local["tb_abs_f_on_count"] = 0
                            tb_state_local["tb_abs_f_off_count"] = 0
                            return bool(raw_s_on), bool(raw_f_on)

                        debounce_cols = int(round(float(tb_abs_window_s) / max(1e-6, float(tb_seconds_per_col))))
                        if debounce_cols < 1:
                            debounce_cols = 1
                        if debounce_cols > 64:
                            debounce_cols = 64

                        s_on_now = bool(tb_state_local.get("tb_abs_s_on", False))
                        s_on_count = int(tb_state_local.get("tb_abs_s_on_count", 0))
                        s_off_count = int(tb_state_local.get("tb_abs_s_off_count", 0))
                        if raw_s_on:
                            s_on_count += 1
                            s_off_count = 0
                        else:
                            s_off_count += 1
                            s_on_count = 0
                        if (not s_on_now) and s_on_count >= debounce_cols:
                            s_on_now = True
                        if s_on_now and s_off_count >= debounce_cols:
                            s_on_now = False
                        tb_state_local["tb_abs_s_on"] = bool(s_on_now)
                        tb_state_local["tb_abs_s_on_count"] = int(s_on_count)
                        tb_state_local["tb_abs_s_off_count"] = int(s_off_count)

                        f_on_now = bool(tb_state_local.get("tb_abs_f_on", False))
                        f_on_count = int(tb_state_local.get("tb_abs_f_on_count", 0))
                        f_off_count = int(tb_state_local.get("tb_abs_f_off_count", 0))
                        if raw_f_on:
                            f_on_count += 1
                            f_off_count = 0
                        else:
                            f_off_count += 1
                            f_on_count = 0
                        if (not f_on_now) and f_on_count >= debounce_cols:
                            f_on_now = True
                        if f_on_now and f_off_count >= debounce_cols:
                            f_on_now = False
                        tb_state_local["tb_abs_f_on"] = bool(f_on_now)
                        tb_state_local["tb_abs_f_on_count"] = int(f_on_count)
                        tb_state_local["tb_abs_f_off_count"] = int(f_off_count)
                        return bool(s_on_now), bool(f_on_now)

                    def _tb_text_w(dr_any: Any, text: str, font_obj: Any) -> int:
                        try:
                            bb = dr_any.textbbox((0, 0), str(text), font=font_obj)
                            return int(bb[2] - bb[0])
                        except Exception:
                            try:
                                return int(dr_any.textlength(str(text), font=font_obj))
                            except Exception:
                                return int(len(str(text)) * 8)

                    def _tb_render_static_layer() -> Any:
                        static_img_local = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
                        static_dr_local = ImageDraw.Draw(static_img_local)
                        static_dr_local.rectangle([0, 0, int(w) - 1, int(h) - 1], fill=COL_HUD_BG)

                        y_grid_top = int(tb_layout["y_from_01"](1.0))
                        y_grid_bot = int(tb_layout["y_from_01"](0.0))
                        y_bounds = sorted(
                            list(
                                {
                                    int(tb_layout["y_from_01"](0.0)),
                                    int(tb_layout["y_from_01"](0.2)),
                                    int(tb_layout["y_from_01"](0.4)),
                                    int(tb_layout["y_from_01"](0.6)),
                                    int(tb_layout["y_from_01"](0.8)),
                                    int(tb_layout["y_from_01"](1.0)),
                                }
                            )
                        )
                        x0s = 0
                        x1s = int(w) - 1
                        y0s = int(min(y_grid_top, y_grid_bot))
                        y1s = int(max(y_grid_top, y_grid_bot))
                        for stripe_i in range(len(y_bounds) - 1):
                            y_a = int(y_bounds[stripe_i])
                            y_b = int(y_bounds[stripe_i + 1]) - 1
                            if y_b < y_a:
                                continue
                            if (stripe_i % 2) == 1:
                                static_dr_local.rectangle(
                                    [x0s, y_a, x1s, y_b],
                                    fill=(max(0, COL_HUD_BG[0] - 12), max(0, COL_HUD_BG[1] - 12), max(0, COL_HUD_BG[2] - 12), COL_HUD_BG[3]),
                                )

                        axis_labels = [
                            (int(tb_layout["y_from_01"](0.2)), "20"),
                            (int(tb_layout["y_from_01"](0.4)), "40"),
                            (int(tb_layout["y_from_01"](0.6)), "60"),
                            (int(tb_layout["y_from_01"](0.8)), "80"),
                        ]
                        for y_lab, txt_lab in axis_labels:
                            try:
                                static_dr_local.text((6, int(y_lab) - 6), str(txt_lab), fill=COL_WHITE, font=tb_layout.get("font_axis"))
                            except Exception:
                                pass

                        try:
                            static_dr_local.text((4, int(tb_layout["y_txt"])), "Throttle / Brake", fill=COL_WHITE, font=tb_layout.get("font_title"))
                        except Exception:
                            pass
                        mx_s = int(tb_layout["mx"])
                        static_dr_local.rectangle([mx_s, 0, mx_s + 1, int(h)], fill=(255, 255, 255, 230))
                        return static_img_local

                    def _tb_render_dynamic_full() -> tuple[Any, list[dict[str, Any]], dict[str, Any]]:
                        dyn_img_local = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
                        dyn_dr_local = ImageDraw.Draw(dyn_img_local)
                        tb_cols_local: list[dict[str, Any]] = []
                        tb_abs_state_local: dict[str, Any] = {
                            "tb_abs_s_on": False,
                            "tb_abs_f_on": False,
                            "tb_abs_s_on_count": 0,
                            "tb_abs_s_off_count": 0,
                            "tb_abs_f_on_count": 0,
                            "tb_abs_f_off_count": 0,
                        }
                        prev_col: dict[str, Any] | None = None
                        for xi_full in range(int(w)):
                            col_now = _tb_sample_column(int(xi_full))
                            abs_s_on, abs_f_on = _tb_apply_abs_debounce(
                                tb_abs_state_local,
                                bool(col_now["abs_s_raw_on"]),
                                bool(col_now["abs_f_raw_on"]),
                            )
                            col_now["abs_s_on"] = bool(abs_s_on)
                            col_now["abs_f_on"] = bool(abs_f_on)
                            if prev_col is not None:
                                x_prev = int(prev_col["x"])
                                x_cur = int(col_now["x"])
                                dyn_dr_local.line([(x_prev, int(prev_col["y_s_b"])), (x_cur, int(col_now["y_s_b"]))], fill=COL_SLOW_DARKRED, width=2)
                                dyn_dr_local.line([(x_prev, int(prev_col["y_s_t"])), (x_cur, int(col_now["y_s_t"]))], fill=COL_SLOW_BRIGHTRED, width=2)
                                dyn_dr_local.line([(x_prev, int(prev_col["y_f_b"])), (x_cur, int(col_now["y_f_b"]))], fill=COL_FAST_DARKBLUE, width=2)
                                dyn_dr_local.line([(x_prev, int(prev_col["y_f_t"])), (x_cur, int(col_now["y_f_t"]))], fill=COL_FAST_BRIGHTBLUE, width=2)
                            if bool(col_now["abs_s_on"]):
                                y0_abs_s = int(tb_layout["y_abs_s"])
                                y1_abs_s = int(tb_layout["y_abs_s"]) + int(tb_layout["abs_h"]) - 1
                                dyn_dr_local.line([(int(col_now["x"]), y0_abs_s), (int(col_now["x"]), y1_abs_s)], fill=COL_SLOW_DARKRED, width=1)
                            if bool(col_now["abs_f_on"]):
                                y0_abs_f = int(tb_layout["y_abs_f"])
                                y1_abs_f = int(tb_layout["y_abs_f"]) + int(tb_layout["abs_h"]) - 1
                                dyn_dr_local.line([(int(col_now["x"]), y0_abs_f), (int(col_now["x"]), y1_abs_f)], fill=COL_FAST_DARKBLUE, width=1)
                            tb_cols_local.append(col_now)
                            prev_col = col_now
                        return dyn_img_local, tb_cols_local, tb_abs_state_local

                    def _tb_draw_values_overlay(main_dr_local: Any, base_x: int, base_y: int) -> None:
                        cur_col = _tb_sample_column(int(tb_layout["mx"]))
                        s_txt = f"T{int(round(_clamp(float(cur_col['s_t']), 0.0, 1.0) * 100.0)):03d}% B{int(round(_clamp(float(cur_col['s_b']), 0.0, 1.0) * 100.0)):03d}%"
                        f_txt = f"T{int(round(_clamp(float(cur_col['f_t']), 0.0, 1.0) * 100.0)):03d}% B{int(round(_clamp(float(cur_col['f_b']), 0.0, 1.0) * 100.0)):03d}%"
                        gap_txt = 12
                        mx_txt = int(base_x) + int(tb_layout["mx"])
                        f_w_txt = _tb_text_w(main_dr_local, f_txt, tb_layout.get("font_val"))
                        f_x_txt = int(mx_txt - gap_txt - f_w_txt)
                        s_x_txt = int(mx_txt + gap_txt)
                        if f_x_txt < int(base_x + 2):
                            f_x_txt = int(base_x + 2)
                        if s_x_txt > int(base_x + int(w) - 2):
                            s_x_txt = int(base_x + int(w) - 2)
                        y_txt_abs = int(base_y) + int(tb_layout["y_txt"])
                        try:
                            main_dr_local.text((int(f_x_txt), int(y_txt_abs)), f_txt, fill=COL_SLOW_DARKRED, font=tb_layout.get("font_val"))
                        except Exception:
                            pass
                        try:
                            main_dr_local.text((int(s_x_txt), int(y_txt_abs)), s_txt, fill=COL_FAST_DARKBLUE, font=tb_layout.get("font_val"))
                        except Exception:
                            pass

                if is_steering:
                    st_map_idx_to_fast_idx: dict[int, int] = {}
                    map_idxs_all_st = list(getattr(frame_window_mapping, "idxs", []) or [])
                    map_fast_idx_all_st = list(getattr(frame_window_mapping, "fast_idx", []) or [])
                    if map_idxs_all_st and len(map_idxs_all_st) == len(map_fast_idx_all_st):
                        for idx_m_st, fi_m_st in zip(map_idxs_all_st, map_fast_idx_all_st):
                            st_map_idx_to_fast_idx[int(idx_m_st)] = int(fi_m_st)

                    try:
                        st_headroom = float((os.environ.get("IRVC_STEER_HEADROOM") or "").strip() or "1.20")
                    except Exception:
                        st_headroom = 1.20
                    if st_headroom < 1.00:
                        st_headroom = 1.00
                    if st_headroom > 2.00:
                        st_headroom = 2.00

                    st_mid_y = float(h) / 2.0
                    st_amp_base = max(2.0, (float(h) / 2.0) - 2.0)
                    st_amp_neg = st_amp_base
                    st_amp_pos = st_amp_base / max(1.0, float(st_headroom))
                    st_mx = int(w // 2)
                    st_marker_xf = float(st_mx)
                    st_half_w = float(max(1, int(w) - 1)) / 2.0

                    def _st_load_font(sz: int):
                        try:
                            from PIL import ImageFont
                        except Exception:
                            ImageFont = None  # type: ignore
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

                    st_font_sz = int(round(max(10.0, min(18.0, float(h) * 0.13))))
                    st_font_val_sz = int(round(max(11.0, min(20.0, float(h) * 0.15))))
                    st_layout = {
                        "font_title": _st_load_font(st_font_sz),
                        "font_val": _st_load_font(st_font_val_sz),
                        "y_txt": int(4),
                        "mx": int(st_mx),
                        "y_mid": int(round(st_mid_y)),
                        "marker_xf": float(st_marker_xf),
                        "half_w": float(st_half_w),
                    }

                    def _st_y_from_norm(sn: float) -> int:
                        sn_c = _clamp(float(sn), -1.0, 1.0)
                        if sn_c >= 0.0:
                            yy = float(st_mid_y) - (float(sn_c) * float(st_amp_pos))
                        else:
                            yy = float(st_mid_y) - (float(sn_c) * float(st_amp_neg))
                        return int(round(yy))

                    def _st_fast_idx_from_slow_idx(idx0: int) -> int:
                        ii = int(idx0)
                        if ii < 0:
                            ii = 0
                        has_map = int(ii) in st_map_idx_to_fast_idx
                        fi_map = int(st_map_idx_to_fast_idx.get(int(ii), int(ii)))
                        if fi_map < 0:
                            fi_map = 0
                        if (not has_map) and slow_to_fast_frame and int(ii) < len(slow_to_fast_frame):
                            try:
                                fi_map = int(slow_to_fast_frame[int(ii)])
                                if fi_map < 0:
                                    fi_map = 0
                            except Exception:
                                pass
                        return int(fi_map)

                    def _st_sample_legacy(vals: list[float] | None, idx_base: int, scale: float) -> float:
                        if not vals:
                            return 0.0
                        i_legacy = int(round(float(idx_base) * float(scale)))
                        if i_legacy < 0:
                            i_legacy = 0
                        if i_legacy >= len(vals):
                            i_legacy = len(vals) - 1
                        try:
                            return float(vals[i_legacy])
                        except Exception:
                            return 0.0

                    def _st_sample_column(x_col: int) -> dict[str, Any]:
                        xi = int(x_col)
                        if xi < 0:
                            xi = 0
                        if xi >= int(w):
                            xi = int(w) - 1

                        frac = (float(xi) - float(st_layout["marker_xf"])) / max(1.0, float(st_layout["half_w"]))
                        frac = _clamp(float(frac), -1.0, 1.0)
                        if frac <= 0.0:
                            off_f = float(frac) * float(before_f)
                        else:
                            off_f = float(frac) * float(after_f)

                        idx_slow = int(round(float(i) + float(off_f)))
                        if idx_slow < int(iL):
                            idx_slow = int(iL)
                        if idx_slow > int(iR):
                            idx_slow = int(iR)
                        if idx_slow < 0:
                            idx_slow = 0
                        if idx_slow >= len(slow_frame_to_lapdist):
                            idx_slow = len(slow_frame_to_lapdist) - 1

                        fi_map = _st_fast_idx_from_slow_idx(int(idx_slow))
                        sv = _st_sample_legacy(slow_steer_frames, int(idx_slow), float(steer_slow_scale))
                        fv = _st_sample_legacy(fast_steer_frames, int(fi_map), float(steer_fast_scale))

                        sn = _clamp(float(sv) / max(1e-6, float(steer_abs_max)), -1.0, 1.0)
                        fn = _clamp(float(fv) / max(1e-6, float(steer_abs_max)), -1.0, 1.0)
                        y_s = _st_y_from_norm(float(sn))
                        y_f = _st_y_from_norm(float(fn))
                        return {
                            "x": int(xi),
                            "slow_idx": int(idx_slow),
                            "fast_idx": int(fi_map),
                            "s": float(sv),
                            "f": float(fv),
                            "y_s": int(y_s),
                            "y_f": int(y_f),
                        }

                    def _st_text_w(dr_any: Any, text: str, font_obj: Any) -> int:
                        try:
                            bb = dr_any.textbbox((0, 0), str(text), font=font_obj)
                            return int(bb[2] - bb[0])
                        except Exception:
                            try:
                                return int(dr_any.textlength(str(text), font=font_obj))
                            except Exception:
                                return int(len(str(text)) * 8)

                    def _st_render_static_layer() -> Any:
                        static_img_local = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
                        static_dr_local = ImageDraw.Draw(static_img_local)
                        static_dr_local.rectangle([0, 0, int(w) - 1, int(h) - 1], fill=COL_HUD_BG)
                        try:
                            static_dr_local.line(
                                [(0, int(st_layout["y_mid"])), (int(w) - 1, int(st_layout["y_mid"]))],
                                fill=(COL_WHITE[0], COL_WHITE[1], COL_WHITE[2], 180),
                                width=1,
                            )
                        except Exception:
                            pass
                        mx_s = int(st_layout["mx"])
                        static_dr_local.rectangle([mx_s, 0, mx_s + 1, int(h)], fill=(255, 255, 255, 230))
                        try:
                            static_dr_local.text((4, int(st_layout["y_txt"])), "Steering wheel angle", fill=COL_WHITE, font=st_layout.get("font_title"))
                        except Exception:
                            pass
                        return static_img_local

                    def _st_render_dynamic_full() -> tuple[Any, list[dict[str, Any]]]:
                        dyn_img_local = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
                        dyn_dr_local = ImageDraw.Draw(dyn_img_local)
                        st_cols_local: list[dict[str, Any]] = []
                        prev_col: dict[str, Any] | None = None
                        for xi_full in range(int(w)):
                            col_now = _st_sample_column(int(xi_full))
                            if prev_col is not None:
                                x_prev = int(prev_col["x"])
                                x_cur = int(col_now["x"])
                                dyn_dr_local.line([(x_prev, int(prev_col["y_s"])), (x_cur, int(col_now["y_s"]))], fill=COL_SLOW_DARKRED, width=2)
                                dyn_dr_local.line([(x_prev, int(prev_col["y_f"])), (x_cur, int(col_now["y_f"]))], fill=COL_FAST_DARKBLUE, width=2)
                            st_cols_local.append(col_now)
                            prev_col = col_now
                        return dyn_img_local, st_cols_local

                    def _st_draw_values_overlay(main_dr_local: Any, base_x: int, base_y: int) -> None:
                        sv_cur = _st_sample_legacy(slow_steer_frames, int(i), float(steer_slow_scale))
                        fi_cur = _st_fast_idx_from_slow_idx(int(i))
                        fv_cur = _st_sample_legacy(fast_steer_frames, int(fi_cur), float(steer_fast_scale))

                        sdeg = int(round(float(sv_cur) * 180.0 / math.pi))
                        fdeg = int(round(float(fv_cur) * 180.0 / math.pi))
                        s_txt = f"{sdeg:+04d} deg"
                        f_txt = f"{fdeg:+04d} deg"

                        gap_txt = 12
                        mx_txt = int(base_x) + int(st_layout["mx"])
                        f_w_txt = _st_text_w(main_dr_local, f_txt, st_layout.get("font_val"))
                        f_x_txt = int(mx_txt - gap_txt - f_w_txt)
                        s_x_txt = int(mx_txt + gap_txt)
                        if f_x_txt < int(base_x + 2):
                            f_x_txt = int(base_x + 2)
                        if s_x_txt > int(base_x + int(w) - 2):
                            s_x_txt = int(base_x + int(w) - 2)
                        y_txt_abs = int(base_y) + int(st_layout["y_txt"])
                        try:
                            main_dr_local.text((int(f_x_txt), int(y_txt_abs)), f_txt, fill=(255, 0, 0, 255), font=st_layout.get("font_val"))
                        except Exception:
                            pass
                        try:
                            main_dr_local.text((int(s_x_txt), int(y_txt_abs)), s_txt, fill=(0, 120, 255, 255), font=st_layout.get("font_val"))
                        except Exception:
                            pass

                if is_delta:
                    d_map_idx_to_t_slow: dict[int, float] = {}
                    d_map_idx_to_t_fast: dict[int, float] = {}
                    map_idxs_all_d = list(getattr(frame_window_mapping, "idxs", []) or [])
                    map_t_slow_all_d = list(getattr(frame_window_mapping, "t_slow", []) or [])
                    map_t_fast_all_d = list(getattr(frame_window_mapping, "t_fast", []) or [])
                    if map_idxs_all_d and len(map_idxs_all_d) == len(map_t_slow_all_d):
                        for idx_m_d, ts_m_d in zip(map_idxs_all_d, map_t_slow_all_d):
                            d_map_idx_to_t_slow[int(idx_m_d)] = float(ts_m_d)
                    if map_idxs_all_d and len(map_idxs_all_d) == len(map_t_fast_all_d):
                        for idx_m_d, tf_m_d in zip(map_idxs_all_d, map_t_fast_all_d):
                            d_map_idx_to_t_fast[int(idx_m_d)] = float(tf_m_d)

                    def _d_load_font(sz: int):
                        try:
                            from PIL import ImageFont
                        except Exception:
                            ImageFont = None  # type: ignore
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

                    d_font_sz = int(round(max(10.0, min(18.0, float(h) * 0.13))))
                    d_font_val_sz = int(round(max(11.0, min(20.0, float(h) * 0.15))))
                    d_top_pad = int(round(max(14.0, float(d_font_sz) + 8.0)))
                    d_plot_y0 = int(d_top_pad)
                    d_plot_y1 = int(h - 2)
                    if d_plot_y1 <= d_plot_y0 + 4:
                        d_plot_y0 = int(2)
                        d_plot_y1 = int(h - 2)

                    d_mx = int(w // 2)
                    d_marker_xf = float(d_mx)
                    d_half_w = float(max(1, int(w) - 1)) / 2.0
                    d_layout = {
                        "font_title": _d_load_font(d_font_sz),
                        "font_val": _d_load_font(d_font_val_sz),
                        "y_txt": int(2),
                        "mx": int(d_mx),
                        "marker_xf": float(d_marker_xf),
                        "half_w": float(d_half_w),
                        "plot_y0": int(d_plot_y0),
                        "plot_y1": int(d_plot_y1),
                    }

                    d_range_pos = float(delta_pos_max)
                    d_range_neg = float(abs(delta_neg_min))
                    d_y_top = float(d_layout["plot_y0"])
                    d_y_bot = float(d_layout["plot_y1"])
                    d_span = max(10.0, (d_y_bot - d_y_top))

                    if not delta_has_neg:
                        d_y_zero_f = float(d_y_bot)
                        d_pos_span = max(4.0, (d_y_zero_f - d_y_top))

                        def _d_y_from_delta(dsec: float) -> int:
                            d = float(dsec)
                            if d < 0.0:
                                d = 0.0
                            if d > float(delta_pos_max):
                                d = float(delta_pos_max)
                            yy = d_y_zero_f - (d / float(delta_pos_max)) * d_pos_span
                            return int(round(yy))
                    else:
                        d_total = max(1e-6, (d_range_neg + d_range_pos))
                        d_y_zero_f = d_y_top + (d_range_pos / d_total) * d_span
                        d_y_zero_f = max(d_y_top + 2.0, min(d_y_bot - 2.0, d_y_zero_f))
                        d_pos_span = max(4.0, (d_y_zero_f - d_y_top))
                        d_neg_span = max(4.0, (d_y_bot - d_y_zero_f))

                        def _d_y_from_delta(dsec: float) -> int:
                            d = float(dsec)
                            if d >= 0.0:
                                if d > d_range_pos:
                                    d = d_range_pos
                                yy = d_y_zero_f - (d / d_range_pos) * d_pos_span
                            else:
                                ad = abs(d)
                                if ad > d_range_neg:
                                    ad = d_range_neg
                                yy = d_y_zero_f + (ad / d_range_neg) * d_neg_span
                            return int(round(yy))

                    d_y_zero = int(round(d_y_zero_f))
                    d_fps_safe = float(fps) if float(fps) > 0.1 else 30.0

                    def _d_delta_at_slow_frame(idx0: int) -> float:
                        ii = int(idx0)
                        if ii in d_map_idx_to_t_slow and ii in d_map_idx_to_t_fast:
                            return float(d_map_idx_to_t_slow[ii] - d_map_idx_to_t_fast[ii])
                        if not slow_frame_to_fast_time_s:
                            return 0.0
                        if ii < 0:
                            ii = 0
                        if ii >= len(slow_frame_to_fast_time_s):
                            ii = len(slow_frame_to_fast_time_s) - 1
                        slow_t = float(ii) / float(d_fps_safe)
                        fast_t = float(slow_frame_to_fast_time_s[ii])
                        return float(slow_t - fast_t)

                    def _d_sign_from_delta(dsec: float) -> int:
                        return 1 if float(dsec) >= 0.0 else -1

                    def _d_sample_column(x_col: int) -> dict[str, Any]:
                        xi = int(x_col)
                        if xi < 0:
                            xi = 0
                        if xi >= int(w):
                            xi = int(w) - 1

                        frac = (float(xi) - float(d_layout["marker_xf"])) / max(1.0, float(d_layout["half_w"]))
                        frac = _clamp(float(frac), -1.0, 1.0)
                        if frac <= 0.0:
                            off_f = float(frac) * float(before_f)
                        else:
                            off_f = float(frac) * float(after_f)

                        idx_slow = int(round(float(i) + float(off_f)))
                        if idx_slow < int(iL):
                            idx_slow = int(iL)
                        if idx_slow > int(iR):
                            idx_slow = int(iR)
                        if idx_slow < 0:
                            idx_slow = 0
                        if idx_slow >= len(slow_frame_to_lapdist):
                            idx_slow = len(slow_frame_to_lapdist) - 1

                        d_val = float(_d_delta_at_slow_frame(int(idx_slow)))
                        y_val = int(_d_y_from_delta(float(d_val)))
                        return {
                            "x": int(xi),
                            "slow_idx": int(idx_slow),
                            "delta": float(d_val),
                            "y": int(y_val),
                        }

                    def _d_draw_segment(
                        dr_local: Any,
                        x0_seg: int,
                        y0_seg: int,
                        d0_seg: float,
                        x1_seg: int,
                        y1_seg: int,
                        d1_seg: float,
                    ) -> None:
                        sign0 = _d_sign_from_delta(float(d0_seg))
                        sign1 = _d_sign_from_delta(float(d1_seg))
                        col0 = COL_FAST_DARKBLUE if sign0 >= 0 else COL_SLOW_DARKRED
                        col1 = COL_FAST_DARKBLUE if sign1 >= 0 else COL_SLOW_DARKRED
                        if sign0 == sign1:
                            try:
                                dr_local.line(
                                    [(int(x0_seg), int(y0_seg)), (int(x1_seg), int(y1_seg))],
                                    fill=col1,
                                    width=2,
                                )
                            except Exception:
                                pass
                            return

                        denom = float(d1_seg) - float(d0_seg)
                        if abs(denom) <= 1e-12:
                            try:
                                dr_local.line(
                                    [(int(x0_seg), int(y0_seg)), (int(x1_seg), int(y1_seg))],
                                    fill=col1,
                                    width=2,
                                )
                            except Exception:
                                pass
                            return

                        t_cross = (0.0 - float(d0_seg)) / float(denom)
                        t_cross = _clamp(float(t_cross), 0.0, 1.0)
                        x_cross = int(round(float(x0_seg) + (float(x1_seg - x0_seg) * float(t_cross))))
                        y_cross = int(d_y_zero)
                        try:
                            dr_local.line(
                                [(int(x0_seg), int(y0_seg)), (int(x_cross), int(y_cross))],
                                fill=col0,
                                width=2,
                            )
                        except Exception:
                            pass
                        try:
                            dr_local.line(
                                [(int(x_cross), int(y_cross)), (int(x1_seg), int(y1_seg))],
                                fill=col1,
                                width=2,
                            )
                        except Exception:
                            pass

                    def _d_render_static_layer() -> Any:
                        static_img_local = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
                        static_dr_local = ImageDraw.Draw(static_img_local)
                        static_dr_local.rectangle([0, 0, int(w) - 1, int(h) - 1], fill=COL_HUD_BG)
                        try:
                            static_dr_local.line(
                                [(0, int(d_y_zero)), (int(w) - 1, int(d_y_zero))],
                                fill=(COL_SLOW_DARKRED[0], COL_SLOW_DARKRED[1], COL_SLOW_DARKRED[2], 200),
                                width=1,
                            )
                        except Exception:
                            pass
                        return static_img_local

                    def _d_render_dynamic_full() -> tuple[Any, list[dict[str, Any]]]:
                        dyn_img_local = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
                        dyn_dr_local = ImageDraw.Draw(dyn_img_local)
                        d_cols_local: list[dict[str, Any]] = []
                        prev_col: dict[str, Any] | None = None
                        for xi_full in range(int(w)):
                            col_now = _d_sample_column(int(xi_full))
                            if prev_col is not None:
                                _d_draw_segment(
                                    dyn_dr_local,
                                    int(prev_col["x"]),
                                    int(prev_col["y"]),
                                    float(prev_col["delta"]),
                                    int(col_now["x"]),
                                    int(col_now["y"]),
                                    float(col_now["delta"]),
                                )
                            d_cols_local.append(col_now)
                            prev_col = col_now
                        return dyn_img_local, d_cols_local

                    def _d_draw_values_overlay(main_dr_local: Any, base_x: int, base_y: int) -> None:
                        d_cur = float(_d_delta_at_slow_frame(int(i)))
                        col_cur = COL_FAST_DARKBLUE if d_cur >= 0.0 else COL_SLOW_DARKRED
                        placeholder = "+999.999s"
                        try:
                            bb = main_dr_local.textbbox((0, 0), placeholder, font=d_layout.get("font_val"))
                            w_fix = int(bb[2] - bb[0])
                        except Exception:
                            w_fix = int(len(placeholder) * max(6, int(d_font_val_sz * 0.6)))

                        x_val = int(base_x) + int(d_layout["mx"]) - 6 - int(w_fix)
                        y_val = int(base_y) + int(d_layout["y_txt"])
                        txt = f"{d_cur:+.3f}s"
                        if len(txt) < len(placeholder):
                            txt = txt.rjust(len(placeholder), " ")
                        try:
                            main_dr_local.text((int(x_val), int(y_val)), txt, fill=col_cur, font=d_layout.get("font_val"))
                        except Exception:
                            pass

                if is_line_delta:
                    def _ld_load_font(sz: int):
                        try:
                            from PIL import ImageFont
                        except Exception:
                            ImageFont = None  # type: ignore
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

                    ld_font_sz = int(round(max(10.0, min(18.0, float(h) * 0.13))))
                    ld_font_val_sz = int(round(max(11.0, min(20.0, float(h) * 0.15))))
                    ld_font_axis_sz = max(8, int(ld_font_sz - 2))
                    ld_font_axis_small_sz = max(7, int(ld_font_sz - 3))
                    ld_top_pad = int(round(max(14.0, float(ld_font_sz) + 8.0)))
                    ld_plot_y0 = int(ld_top_pad)
                    ld_plot_y1 = int(h - 2)
                    if ld_plot_y1 <= ld_plot_y0 + 4:
                        ld_plot_y0 = int(2)
                        ld_plot_y1 = int(h - 2)

                    ld_mx = int(w // 2)
                    ld_marker_xf = float(ld_mx)
                    ld_half_w = float(max(1, int(w) - 1)) / 2.0
                    ld_layout = {
                        "font_title": _ld_load_font(ld_font_sz),
                        "font_val": _ld_load_font(ld_font_val_sz),
                        "font_axis": _ld_load_font(ld_font_axis_sz),
                        "font_axis_small": _ld_load_font(ld_font_axis_small_sz),
                        "y_txt": int(2),
                        "mx": int(ld_mx),
                        "marker_xf": float(ld_marker_xf),
                        "half_w": float(ld_half_w),
                        "plot_y0": int(ld_plot_y0),
                        "plot_y1": int(ld_plot_y1),
                    }

                    ld_vals = line_delta_m_frames if isinstance(line_delta_m_frames, list) else []
                    ld_n_vals = len(ld_vals)

                    ld_y_abs = 0.0
                    try:
                        ld_y_abs = float(line_delta_y_abs_m)
                    except Exception:
                        ld_y_abs = 0.0
                    if not math.isfinite(ld_y_abs) or ld_y_abs < 0.0:
                        ld_y_abs = 0.0
                    ld_y_min_m = -float(ld_y_abs)
                    ld_y_max_m = float(ld_y_abs)
                    if ld_y_max_m <= ld_y_min_m:
                        ld_y_max_m = ld_y_min_m + 1e-6

                    def _ld_y_from_m(v_m: float) -> int:
                        vv = float(v_m)
                        if vv < ld_y_min_m:
                            vv = ld_y_min_m
                        if vv > ld_y_max_m:
                            vv = ld_y_max_m
                        den = float(ld_y_max_m - ld_y_min_m)
                        if den <= 1e-12:
                            return int(round((int(ld_layout["plot_y0"]) + int(ld_layout["plot_y1"])) / 2.0))
                        frac = (vv - ld_y_min_m) / den
                        yy = float(ld_layout["plot_y1"]) - (frac * float(int(ld_layout["plot_y1"]) - int(ld_layout["plot_y0"])))
                        return int(round(yy))

                    def _ld_value_at_slow_idx(idx0: int) -> float:
                        if not ld_vals:
                            return 0.0
                        ii = int(idx0)
                        if ii < 0:
                            ii = 0
                        if ii >= ld_n_vals:
                            ii = ld_n_vals - 1
                        try:
                            vv = float(ld_vals[ii])
                        except Exception:
                            vv = 0.0
                        if not math.isfinite(vv):
                            vv = 0.0
                        return float(vv)

                    def _ld_slow_idx_for_column(x_col: int) -> int:
                        xi = int(x_col)
                        if xi < 0:
                            xi = 0
                        if xi >= int(w):
                            xi = int(w) - 1

                        frac = (float(xi) - float(ld_layout["marker_xf"])) / max(1.0, float(ld_layout["half_w"]))
                        frac = _clamp(float(frac), -1.0, 1.0)
                        if frac <= 0.0:
                            off_f = float(frac) * float(before_f)
                        else:
                            off_f = float(frac) * float(after_f)

                        idx_slow = int(round(float(i) + float(off_f)))
                        if idx_slow < int(iL):
                            idx_slow = int(iL)
                        if idx_slow > int(iR):
                            idx_slow = int(iR)
                        if idx_slow < 0:
                            idx_slow = 0
                        if idx_slow >= len(slow_frame_to_lapdist):
                            idx_slow = len(slow_frame_to_lapdist) - 1
                        return int(idx_slow)

                    def _ld_sample_column(x_col: int) -> dict[str, Any]:
                        xi = int(x_col)
                        if xi < 0:
                            xi = 0
                        if xi >= int(w):
                            xi = int(w) - 1
                        idx_slow = _ld_slow_idx_for_column(int(xi))
                        v_val = float(_ld_value_at_slow_idx(int(idx_slow)))
                        y_val = int(_ld_y_from_m(float(v_val)))
                        return {
                            "x": int(xi),
                            "slow_idx": int(idx_slow),
                            "delta_m": float(v_val),
                            "y": int(y_val),
                        }

                    def _ld_text_w(dr_any: Any, text: str, font_obj: Any) -> int:
                        try:
                            bb = dr_any.textbbox((0, 0), str(text), font=font_obj)
                            return int(bb[2] - bb[0])
                        except Exception:
                            try:
                                return int(dr_any.textlength(str(text), font=font_obj))
                            except Exception:
                                return int(len(str(text)) * 8)

                    def _ld_render_static_layer() -> Any:
                        static_img_local = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
                        static_dr_local = ImageDraw.Draw(static_img_local)
                        static_dr_local.rectangle([0, 0, int(w) - 1, int(h) - 1], fill=COL_HUD_BG)

                        axis_labels: list[tuple[int, str]] = []
                        try:
                            tick_ref_max = max(abs(float(ld_y_min_m)), abs(float(ld_y_max_m)))
                            step = choose_tick_step(0.0, tick_ref_max, min_segments=2, max_segments=5, target_segments=5)
                            if step is not None:
                                val_bounds = build_value_boundaries(ld_y_min_m, ld_y_max_m, float(step), anchor="top")
                                y_bounds = value_boundaries_to_y(
                                    val_bounds,
                                    _ld_y_from_m,
                                    int(ld_layout["plot_y0"]),
                                    int(ld_layout["plot_y1"]),
                                )
                                draw_stripe_grid(
                                    static_dr_local,
                                    int(0),
                                    int(w),
                                    int(ld_layout["plot_y0"]),
                                    int(ld_layout["plot_y1"]),
                                    y_bounds,
                                    col_bg=COL_HUD_BG,
                                    darken_delta=6,
                                )
                                for vv in val_bounds:
                                    if should_suppress_boundary_label(float(vv), ld_y_min_m, ld_y_max_m, suppress_zero=True):
                                        continue
                                    axis_labels.append(
                                        (
                                            int(_ld_y_from_m(float(vv))),
                                            format_value_for_step(float(vv), float(step), min_decimals=0),
                                        )
                                    )
                        except Exception:
                            pass

                        y_zero = int(_ld_y_from_m(0.0))
                        axis_labels = filter_axis_labels_by_position(
                            axis_labels,
                            int(ld_layout["plot_y0"]),
                            int(ld_layout["plot_y1"]),
                            zero_y=int(y_zero),
                            pad_px=2,
                        )
                        draw_left_axis_labels(
                            static_dr_local,
                            int(0),
                            int(w),
                            int(ld_layout["plot_y0"]),
                            int(ld_layout["plot_y1"]),
                            axis_labels,
                            ld_layout.get("font_axis"),
                            col_text=COL_WHITE,
                            x_pad=6,
                            fallback_font_obj=ld_layout.get("font_axis_small"),
                        )
                        try:
                            static_dr_local.text((4, int(ld_layout["y_txt"])), "Line delta", fill=COL_WHITE, font=ld_layout.get("font_title"))
                        except Exception:
                            pass
                        try:
                            static_dr_local.line([(0, int(y_zero)), (int(w) - 1, int(y_zero))], fill=COL_WHITE, width=1)
                        except Exception:
                            pass
                        try:
                            mx_s = int(ld_layout["mx"])
                            static_dr_local.rectangle([mx_s, 0, mx_s + 1, int(h)], fill=(255, 255, 255, 230))
                        except Exception:
                            pass
                        return static_img_local

                    def _ld_render_dynamic_full() -> tuple[Any, list[dict[str, Any]]]:
                        dyn_img_local = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
                        dyn_dr_local = ImageDraw.Draw(dyn_img_local)
                        ld_cols_local: list[dict[str, Any]] = []
                        prev_col: dict[str, Any] | None = None
                        for xi_full in range(int(w)):
                            col_now = _ld_sample_column(int(xi_full))
                            if prev_col is not None:
                                try:
                                    dyn_dr_local.line(
                                        [(int(prev_col["x"]), int(prev_col["y"])), (int(col_now["x"]), int(col_now["y"]))],
                                        fill=COL_FAST_DARKBLUE,
                                        width=2,
                                    )
                                except Exception:
                                    pass
                            ld_cols_local.append(col_now)
                            prev_col = col_now
                        return dyn_img_local, ld_cols_local

                    def _ld_draw_values_overlay(main_dr_local: Any, base_x: int, base_y: int) -> None:
                        cur_delta = float(_ld_value_at_slow_idx(int(i)))
                        if abs(cur_delta) < 0.005:
                            cur_delta = 0.0
                        prefix = "L" if cur_delta >= 0.0 else "R"
                        txt = f"{prefix} {abs(cur_delta):.2f} m"
                        placeholder = "R 999.99 m"
                        w_fix = _ld_text_w(main_dr_local, placeholder, ld_layout.get("font_val"))
                        if len(txt) < len(placeholder):
                            txt = txt.rjust(len(placeholder), " ")
                        x_val = int(base_x) + int(ld_layout["mx"]) - 6 - int(w_fix)
                        if x_val < int(base_x + 4):
                            x_val = int(base_x + 4)
                        y_val = int(base_y) + int(ld_layout["y_txt"])
                        try:
                            main_dr_local.text((int(x_val), int(y_val)), txt, fill=COL_WHITE, font=ld_layout.get("font_val"))
                        except Exception:
                            pass

                if is_under_oversteer:
                    def _uo_load_font(sz: int):
                        try:
                            from PIL import ImageFont
                        except Exception:
                            ImageFont = None  # type: ignore
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

                    uo_font_sz = int(round(max(10.0, min(18.0, float(h) * 0.13))))
                    uo_font_axis_sz = max(8, int(uo_font_sz - 2))
                    uo_font_axis_small_sz = max(7, int(uo_font_sz - 3))
                    uo_top_pad = int(round(max(14.0, float(uo_font_sz) + 8.0)))
                    uo_plot_y0 = int(uo_top_pad)
                    uo_plot_y1 = int(h - 2)
                    if uo_plot_y1 <= uo_plot_y0 + 4:
                        uo_plot_y0 = int(2)
                        uo_plot_y1 = int(h - 2)

                    uo_mx = int(w // 2)
                    uo_marker_xf = float(uo_mx)
                    uo_half_w = float(max(1, int(w) - 1)) / 2.0
                    uo_layout = {
                        "font_title": _uo_load_font(uo_font_sz),
                        "font_axis": _uo_load_font(uo_font_axis_sz),
                        "font_axis_small": _uo_load_font(uo_font_axis_small_sz),
                        "label_x": int(4),
                        "label_top_y": int(2),
                        "label_bottom_y": int(h - uo_font_sz - 2),
                        "mx": int(uo_mx),
                        "marker_xf": float(uo_marker_xf),
                        "half_w": float(uo_half_w),
                        "plot_y0": int(uo_plot_y0),
                        "plot_y1": int(uo_plot_y1),
                    }

                    uo_slow_vals = under_oversteer_slow_frames if isinstance(under_oversteer_slow_frames, list) else []
                    uo_fast_vals = under_oversteer_fast_frames if isinstance(under_oversteer_fast_frames, list) else []
                    uo_n_slow = len(uo_slow_vals)
                    uo_n_fast = len(uo_fast_vals)

                    uo_y_abs = 0.0
                    try:
                        uo_y_abs = abs(float(under_oversteer_y_abs))
                    except Exception:
                        uo_y_abs = 0.0
                    if (not math.isfinite(uo_y_abs)) or uo_y_abs < 1e-6:
                        uo_y_abs = 1.0
                    uo_y_min = -float(uo_y_abs)
                    uo_y_max = float(uo_y_abs)
                    uo_y_den = float(uo_y_max - uo_y_min)
                    if uo_y_den <= 1e-12:
                        uo_y_den = 1.0

                    def _uo_y_from_val(v: float) -> int:
                        vv = float(v)
                        if vv < uo_y_min:
                            vv = uo_y_min
                        if vv > uo_y_max:
                            vv = uo_y_max
                        frac = (vv - uo_y_min) / float(uo_y_den)
                        yy = float(uo_layout["plot_y1"]) - (frac * float(int(uo_layout["plot_y1"]) - int(uo_layout["plot_y0"])))
                        return int(round(yy))

                    def _uo_slow_idx_for_column(x_col: int) -> int:
                        xi = int(x_col)
                        if xi < 0:
                            xi = 0
                        if xi >= int(w):
                            xi = int(w) - 1

                        frac = (float(xi) - float(uo_layout["marker_xf"])) / max(1.0, float(uo_layout["half_w"]))
                        frac = _clamp(float(frac), -1.0, 1.0)
                        if frac <= 0.0:
                            off_f = float(frac) * float(before_f)
                        else:
                            off_f = float(frac) * float(after_f)

                        idx_slow = int(round(float(i) + float(off_f)))
                        if idx_slow < int(iL):
                            idx_slow = int(iL)
                        if idx_slow > int(iR):
                            idx_slow = int(iR)
                        if idx_slow < 0:
                            idx_slow = 0
                        if idx_slow >= len(slow_frame_to_lapdist):
                            idx_slow = len(slow_frame_to_lapdist) - 1
                        return int(idx_slow)

                    def _uo_fast_idx_for_slow_idx(idx0: int) -> int:
                        if uo_n_fast <= 0:
                            return 0
                        idx_fast = int(idx0)
                        if idx_fast < 0:
                            idx_fast = 0
                        if idx_fast >= uo_n_fast:
                            idx_fast = uo_n_fast - 1
                        return int(idx_fast)

                    def _uo_sample_slow_value(idx0: int) -> float:
                        if uo_n_slow <= 0:
                            return 0.0
                        ii = int(idx0)
                        if ii < 0:
                            ii = 0
                        if ii >= uo_n_slow:
                            ii = uo_n_slow - 1
                        try:
                            vv = float(uo_slow_vals[ii])
                        except Exception:
                            vv = 0.0
                        if not math.isfinite(vv):
                            vv = 0.0
                        return float(vv)

                    def _uo_sample_fast_value(idx0: int) -> float:
                        if uo_n_fast <= 0:
                            return 0.0
                        ii = int(idx0)
                        if ii < 0:
                            ii = 0
                        if ii >= uo_n_fast:
                            ii = uo_n_fast - 1
                        try:
                            vv = float(uo_fast_vals[ii])
                        except Exception:
                            vv = 0.0
                        if not math.isfinite(vv):
                            vv = 0.0
                        return float(vv)

                    def _uo_sample_column(x_col: int) -> dict[str, Any]:
                        xi = int(x_col)
                        if xi < 0:
                            xi = 0
                        if xi >= int(w):
                            xi = int(w) - 1

                        idx_slow = _uo_slow_idx_for_column(int(xi))
                        idx_fast = _uo_fast_idx_for_slow_idx(int(idx_slow))
                        val_slow = float(_uo_sample_slow_value(int(idx_slow)))
                        val_fast = float(_uo_sample_fast_value(int(idx_fast)))
                        y_slow = int(_uo_y_from_val(float(val_slow)))
                        y_fast = int(_uo_y_from_val(float(val_fast)))
                        return {
                            "x": int(xi),
                            "slow_idx": int(idx_slow),
                            "fast_idx": int(idx_fast),
                            "slow_val": float(val_slow),
                            "fast_val": float(val_fast),
                            "y_s": int(y_slow),
                            "y_f": int(y_fast),
                        }

                    def _uo_render_static_layer() -> Any:
                        static_img_local = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
                        static_dr_local = ImageDraw.Draw(static_img_local)
                        static_dr_local.rectangle([0, 0, int(w) - 1, int(h) - 1], fill=COL_HUD_BG)

                        axis_labels: list[tuple[int, str]] = []
                        try:
                            tick_ref_max = max(abs(float(uo_y_min)), abs(float(uo_y_max)))
                            step = choose_tick_step(0.0, tick_ref_max, min_segments=2, max_segments=5, target_segments=5)
                            if step is not None:
                                val_bounds = build_value_boundaries(uo_y_min, uo_y_max, float(step), anchor="top")
                                y_bounds = value_boundaries_to_y(
                                    val_bounds,
                                    _uo_y_from_val,
                                    int(uo_layout["plot_y0"]),
                                    int(uo_layout["plot_y1"]),
                                )
                                draw_stripe_grid(
                                    static_dr_local,
                                    int(0),
                                    int(w),
                                    int(uo_layout["plot_y0"]),
                                    int(uo_layout["plot_y1"]),
                                    y_bounds,
                                    col_bg=COL_HUD_BG,
                                    darken_delta=6,
                                )
                                for vv in val_bounds:
                                    if should_suppress_boundary_label(float(vv), uo_y_min, uo_y_max, suppress_zero=True):
                                        continue
                                    axis_labels.append(
                                        (
                                            int(_uo_y_from_val(float(vv))),
                                            format_value_for_step(float(vv), float(step), min_decimals=0),
                                        )
                                    )
                        except Exception:
                            pass

                        y_zero = int(_uo_y_from_val(0.0))
                        axis_labels = filter_axis_labels_by_position(
                            axis_labels,
                            int(uo_layout["plot_y0"]),
                            int(uo_layout["plot_y1"]),
                            zero_y=int(y_zero),
                            pad_px=2,
                        )
                        draw_left_axis_labels(
                            static_dr_local,
                            int(0),
                            int(w),
                            int(uo_layout["plot_y0"]),
                            int(uo_layout["plot_y1"]),
                            axis_labels,
                            uo_layout.get("font_axis"),
                            col_text=COL_WHITE,
                            x_pad=6,
                            fallback_font_obj=uo_layout.get("font_axis_small"),
                        )
                        try:
                            static_dr_local.text(
                                (int(uo_layout["label_x"]), int(uo_layout["label_top_y"])),
                                "Oversteer",
                                fill=COL_WHITE,
                                font=uo_layout.get("font_title"),
                            )
                        except Exception:
                            pass
                        try:
                            static_dr_local.text(
                                (int(uo_layout["label_x"]), int(uo_layout["label_bottom_y"])),
                                "Understeer",
                                fill=COL_WHITE,
                                font=uo_layout.get("font_title"),
                            )
                        except Exception:
                            pass
                        try:
                            static_dr_local.line([(0, int(y_zero)), (int(w) - 1, int(y_zero))], fill=COL_WHITE, width=1)
                        except Exception:
                            pass
                        try:
                            mx_s = int(uo_layout["mx"])
                            static_dr_local.rectangle([mx_s, 0, mx_s + 1, int(h)], fill=(255, 255, 255, 230))
                        except Exception:
                            pass
                        return static_img_local

                    def _uo_render_dynamic_full() -> tuple[Any, list[dict[str, Any]]]:
                        dyn_img_local = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
                        dyn_dr_local = ImageDraw.Draw(dyn_img_local)
                        uo_cols_local: list[dict[str, Any]] = []
                        prev_col: dict[str, Any] | None = None
                        for xi_full in range(int(w)):
                            col_now = _uo_sample_column(int(xi_full))
                            if prev_col is not None:
                                try:
                                    dyn_dr_local.line(
                                        [(int(prev_col["x"]), int(prev_col["y_s"])), (int(col_now["x"]), int(col_now["y_s"]))],
                                        fill=COL_SLOW_DARKRED,
                                        width=2,
                                    )
                                except Exception:
                                    pass
                                try:
                                    dyn_dr_local.line(
                                        [(int(prev_col["x"]), int(prev_col["y_f"])), (int(col_now["x"]), int(col_now["y_f"]))],
                                        fill=COL_FAST_DARKBLUE,
                                        width=2,
                                    )
                                except Exception:
                                    pass
                            uo_cols_local.append(col_now)
                            prev_col = col_now
                        return dyn_img_local, uo_cols_local

                def _render_scroll_hud_full(
                    scroll_pos_px_local: float,
                    shift_int_local: int,
                    right_edge_cols_local: int,
                ):
                    hud_img = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
                    hud_dr = ImageDraw.Draw(hud_img)
                    x0_local = 0
                    y0_local = 0
                    center_x_local = int(x0_local + (int(w) // 2))
                    half_w_local = float(int(w) - 1) / 2.0

                    def _idx_to_x_local(idx0: int) -> int:
                        di = int(idx0) - int(i)
                        if di < 0:
                            denom = max(1, int(before_f))
                            frac = float(di) / float(denom)
                        else:
                            denom = max(1, int(after_f))
                            frac = float(di) / float(denom)
                        x = int(round(float(center_x_local) + (frac * half_w_local)))
                        if x < int(x0_local):
                            x = int(x0_local)
                        if x > int(x0_local + int(w) - tick_w):
                            x = int(x0_local + int(w) - tick_w)
                        return x

                    hud_dr.rectangle(
                        [int(x0_local), int(y0_local), int(x0_local + int(w) - 1), int(y0_local + int(h) - 1)],
                        fill=COL_HUD_BG,
                    )
                    mx_local = int(center_x_local)
                    hud_dr.rectangle([mx_local, y0_local, mx_local + 1, y0_local + int(h)], fill=(255, 255, 255, 230))

                    def _hud_throttle_brake() -> None:
                        throttle_brake_ctx = {
                            "hud_key": hud_key,
                            "i": i,
                            "iL": iL,
                            "iR": iR,
                            "window_frames": window_frames,
                            "shift_px_per_frame": shift_px_per_frame,
                            "scroll_pos_px": scroll_pos_px_local,
                            "scroll_shift_int": shift_int_local,
                            "right_edge_cols": right_edge_cols_local,
                            "frame_window_mapping": frame_window_mapping,
                            "fps": fps,
                            "_idx_to_x": _idx_to_x_local,
                            "_clamp": _clamp,
                            "slow_frame_to_lapdist": slow_frame_to_lapdist,
                            "slow_to_fast_frame": slow_to_fast_frame,
                            "slow_frame_to_fast_time_s": slow_frame_to_fast_time_s,
                            "slow_throttle_frames": slow_throttle_frames,
                            "fast_throttle_frames": fast_throttle_frames,
                            "slow_brake_frames": slow_brake_frames,
                            "fast_brake_frames": fast_brake_frames,
                            "slow_abs_frames": slow_abs_frames,
                            "fast_abs_frames": fast_abs_frames,
                            "hud_pedals_sample_mode": hud_pedals_sample_mode,
                            "hud_pedals_abs_debounce_ms": hud_pedals_abs_debounce_ms,
                            "hud_curve_points_default": hud_curve_points_default,
                            "hud_curve_points_overrides": hud_curve_points_overrides,
                            "COL_SLOW_DARKRED": COL_SLOW_DARKRED,
                            "COL_SLOW_BRIGHTRED": COL_SLOW_BRIGHTRED,
                            "COL_FAST_DARKBLUE": COL_FAST_DARKBLUE,
                            "COL_FAST_BRIGHTBLUE": COL_FAST_BRIGHTBLUE,
                            "COL_WHITE": COL_WHITE,
                        }
                        render_throttle_brake(throttle_brake_ctx, (x0_local, y0_local, int(w), int(h)), hud_dr)

                    def _hud_delta() -> None:
                        delta_ctx = {
                            "hud_key": hud_key,
                            "fps": fps,
                            "i": i,
                            "iL": iL,
                            "iR": iR,
                            "window_frames": window_frames,
                            "shift_px_per_frame": shift_px_per_frame,
                            "scroll_pos_px": scroll_pos_px_local,
                            "scroll_shift_int": shift_int_local,
                            "right_edge_cols": right_edge_cols_local,
                            "frame_window_mapping": frame_window_mapping,
                            "mx": mx_local,
                            "_idx_to_x": _idx_to_x_local,
                            "slow_frame_to_fast_time_s": slow_frame_to_fast_time_s,
                            "delta_has_neg": delta_has_neg,
                            "delta_pos_max": delta_pos_max,
                            "delta_neg_min": delta_neg_min,
                            "hud_curve_points_default": hud_curve_points_default,
                            "hud_curve_points_overrides": hud_curve_points_overrides,
                            "hud_dbg": hud_dbg,
                            "_log_print": _log_print,
                            "log_file": log_file,
                            "COL_WHITE": COL_WHITE,
                            "COL_SLOW_DARKRED": COL_SLOW_DARKRED,
                            "COL_FAST_DARKBLUE": COL_FAST_DARKBLUE,
                        }
                        render_delta(delta_ctx, (x0_local, y0_local, int(w), int(h)), hud_dr)

                    def _hud_steering() -> None:
                        steering_ctx = {
                            "hud_key": hud_key,
                            "i": i,
                            "iL": iL,
                            "iR": iR,
                            "window_frames": window_frames,
                            "shift_px_per_frame": shift_px_per_frame,
                            "scroll_pos_px": scroll_pos_px_local,
                            "scroll_shift_int": shift_int_local,
                            "right_edge_cols": right_edge_cols_local,
                            "frame_window_mapping": frame_window_mapping,
                            "slow_to_fast_frame": slow_to_fast_frame,
                            "slow_steer_frames": slow_steer_frames,
                            "fast_steer_frames": fast_steer_frames,
                            "steer_slow_scale": steer_slow_scale,
                            "steer_fast_scale": steer_fast_scale,
                            "steer_abs_max": steer_abs_max,
                            "hud_curve_points_default": hud_curve_points_default,
                            "hud_curve_points_overrides": hud_curve_points_overrides,
                            "hud_windows": hud_windows,
                            "before_s_h": before_s_h,
                            "after_s_h": after_s_h,
                            "default_before_s": default_before_s,
                            "default_after_s": default_after_s,
                            "hud_dbg": hud_dbg,
                            "_clamp": _clamp,
                            "_idx_to_x": _idx_to_x_local,
                            "_log_print": _log_print,
                            "_wrap_delta_05": _wrap_delta_05,
                            "slow_frame_to_lapdist": slow_frame_to_lapdist,
                            "log_file": log_file,
                            "COL_WHITE": COL_WHITE,
                            "COL_SLOW_DARKRED": COL_SLOW_DARKRED,
                            "COL_FAST_DARKBLUE": COL_FAST_DARKBLUE,
                        }
                        render_steering(steering_ctx, (x0_local, y0_local, int(w), int(h)), hud_dr)

                    def _hud_speed() -> None:
                        pass

                    def _hud_gear_rpm() -> None:
                        pass

                    def _hud_line_delta() -> None:
                        line_delta_ctx = {
                            "hud_key": hud_key,
                            "i": i,
                            "before_f": before_f,
                            "after_f": after_f,
                            "window_frames": window_frames,
                            "shift_px_per_frame": shift_px_per_frame,
                            "scroll_pos_px": scroll_pos_px_local,
                            "scroll_shift_int": shift_int_local,
                            "right_edge_cols": right_edge_cols_local,
                            "frame_window_mapping": frame_window_mapping,
                            "line_delta_m_frames": line_delta_m_frames,
                            "line_delta_y_abs_m": line_delta_y_abs_m,
                            "COL_WHITE": COL_WHITE,
                            "COL_FAST_DARKBLUE": COL_FAST_DARKBLUE,
                        }
                        render_line_delta(line_delta_ctx, (x0_local, y0_local, int(w), int(h)), hud_dr)

                    def _hud_under_oversteer() -> None:
                        under_oversteer_ctx = {
                            "hud_key": hud_key,
                            "i": i,
                            "before_f": before_f,
                            "after_f": after_f,
                            "window_frames": window_frames,
                            "shift_px_per_frame": shift_px_per_frame,
                            "scroll_pos_px": scroll_pos_px_local,
                            "scroll_shift_int": shift_int_local,
                            "right_edge_cols": right_edge_cols_local,
                            "frame_window_mapping": frame_window_mapping,
                            "under_oversteer_slow_frames": under_oversteer_slow_frames,
                            "under_oversteer_fast_frames": under_oversteer_fast_frames,
                            "under_oversteer_y_abs": under_oversteer_y_abs,
                            "COL_WHITE": COL_WHITE,
                            "COL_SLOW_DARKRED": COL_SLOW_DARKRED,
                            "COL_FAST_DARKBLUE": COL_FAST_DARKBLUE,
                        }
                        render_under_oversteer(under_oversteer_ctx, (x0_local, y0_local, int(w), int(h)), hud_dr)

                    hud_renderers_local = {
                        "Speed": _hud_speed,
                        "Throttle / Brake": _hud_throttle_brake,
                        "Steering": _hud_steering,
                        "Delta": _hud_delta,
                        "Gear & RPM": _hud_gear_rpm,
                        "Line Delta": _hud_line_delta,
                        "Under-/Oversteer": _hud_under_oversteer,
                    }
                    fn_hud_local = hud_renderers_local.get(hud_key)
                    if fn_hud_local is not None:
                        fn_hud_local()
                    return hud_img

                hud_state_key = f"{str(hud_key)}|{int(x0)}|{int(y0)}|{int(w)}|{int(h)}"
                state = scroll_state_by_hud.get(hud_state_key)
                first_frame = (
                    state is None
                    or state.get("static_layer") is None
                    or state.get("dynamic_layer") is None
                )
                reset_now = False
                if state is not None:
                    try:
                        last_i_state = state.get("last_i")
                        if last_i_state is not None and int(i) != (int(last_i_state) + 1):
                            reset_now = True
                    except Exception:
                        reset_now = True
                    try:
                        last_window_frames = state.get("window_frames")
                        if last_window_frames is not None and int(last_window_frames) != int(window_frames):
                            reset_now = True
                    except Exception:
                        reset_now = True

                def _compose_hud_layers_local(
                    static_layer_local: Any | None,
                    dynamic_layer_local: Any | None,
                    value_layer_local: Any | None,
                ) -> Any:
                    composed_local = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
                    for layer_local in (static_layer_local, dynamic_layer_local, value_layer_local):
                        if layer_local is None:
                            continue
                        layer_rgba = layer_local if getattr(layer_local, "mode", "") == "RGBA" else layer_local.convert("RGBA")
                        if layer_rgba.size != composed_local.size:
                            fixed_layer = Image.new("RGBA", composed_local.size, (0, 0, 0, 0))
                            try:
                                fixed_layer.paste(layer_rgba, (0, 0), layer_rgba)
                            except Exception:
                                fixed_layer.paste(layer_rgba, (0, 0))
                            layer_rgba = fixed_layer
                        composed_local = Image.alpha_composite(composed_local, layer_rgba)
                    return composed_local

                def _render_value_layer_local(draw_values_fn: Any | None) -> Any:
                    value_img_local = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
                    if draw_values_fn is None:
                        return value_img_local
                    try:
                        value_dr_local = ImageDraw.Draw(value_img_local)
                        draw_values_fn(value_dr_local, 0, 0)
                    except Exception:
                        pass
                    return value_img_local

                def _composite_hud_into_frame_local(
                    frame_img_local: Any,
                    hud_layer_local: Any,
                    dst_x_local: int,
                    dst_y_local: int,
                ) -> None:
                    hud_rgba = hud_layer_local if getattr(hud_layer_local, "mode", "") == "RGBA" else hud_layer_local.convert("RGBA")
                    fx0 = int(dst_x_local)
                    fy0 = int(dst_y_local)
                    fx1 = int(fx0 + int(hud_rgba.size[0]))
                    fy1 = int(fy0 + int(hud_rgba.size[1]))
                    frame_w, frame_h = frame_img_local.size
                    cx0 = max(0, int(fx0))
                    cy0 = max(0, int(fy0))
                    cx1 = min(int(frame_w), int(fx1))
                    cy1 = min(int(frame_h), int(fy1))
                    if cx1 <= cx0 or cy1 <= cy0:
                        return
                    sx0 = int(cx0 - fx0)
                    sy0 = int(cy0 - fy0)
                    sx1 = int(sx0 + (cx1 - cx0))
                    sy1 = int(sy0 + (cy1 - cy0))
                    dst_region = frame_img_local.crop((int(cx0), int(cy0), int(cx1), int(cy1)))
                    src_region = hud_rgba.crop((int(sx0), int(sy0), int(sx1), int(sy1)))
                    composed_region = Image.alpha_composite(dst_region, src_region)
                    frame_img_local.paste(composed_region, (int(cx0), int(cy0)))

                if first_frame or reset_now:
                    if is_throttle_brake:
                        static_layer = _tb_render_static_layer()
                        dynamic_layer, tb_cols_fill, tb_abs_state_fill = _tb_render_dynamic_full()
                        right_sample_now = int(tb_cols_fill[-1]["slow_idx"]) if tb_cols_fill else _right_edge_sample_idx()
                        scroll_state_by_hud[hud_state_key] = {
                            "static_layer": static_layer,
                            "dynamic_layer": dynamic_layer,
                            "scroll_pos_px": 0.0,
                            "last_i": int(i),
                            "last_right_sample": int(right_sample_now),
                            "window_frames": int(window_frames),
                            "tb_cols": tb_cols_fill,
                            "tb_abs_s_on": bool(tb_abs_state_fill.get("tb_abs_s_on", False)),
                            "tb_abs_f_on": bool(tb_abs_state_fill.get("tb_abs_f_on", False)),
                            "tb_abs_s_on_count": int(tb_abs_state_fill.get("tb_abs_s_on_count", 0)),
                            "tb_abs_s_off_count": int(tb_abs_state_fill.get("tb_abs_s_off_count", 0)),
                            "tb_abs_f_on_count": int(tb_abs_state_fill.get("tb_abs_f_on_count", 0)),
                            "tb_abs_f_off_count": int(tb_abs_state_fill.get("tb_abs_f_off_count", 0)),
                        }
                        value_layer = _render_value_layer_local(_tb_draw_values_overlay)
                        hud_layer = _compose_hud_layers_local(static_layer, dynamic_layer, value_layer)
                        _composite_hud_into_frame_local(img, hud_layer, int(x0), int(y0))
                    elif is_steering:
                        static_layer = _st_render_static_layer()
                        dynamic_layer, st_cols_fill = _st_render_dynamic_full()
                        st_last_col_fill = st_cols_fill[-1] if st_cols_fill else _st_sample_column(int(w) - 1)
                        right_sample_now = int(st_last_col_fill["slow_idx"]) if st_last_col_fill is not None else _right_edge_sample_idx()
                        scroll_state_by_hud[hud_state_key] = {
                            "static_layer": static_layer,
                            "dynamic_layer": dynamic_layer,
                            "scroll_pos_px": 0.0,
                            "last_i": int(i),
                            "last_right_sample": int(right_sample_now),
                            "window_frames": int(window_frames),
                            "last_y": (int(st_last_col_fill["y_s"]), int(st_last_col_fill["y_f"])),
                            "st_last_fast_idx": int(st_last_col_fill["fast_idx"]),
                        }
                        value_layer = _render_value_layer_local(_st_draw_values_overlay)
                        hud_layer = _compose_hud_layers_local(static_layer, dynamic_layer, value_layer)
                        _composite_hud_into_frame_local(img, hud_layer, int(x0), int(y0))
                    elif is_delta:
                        static_layer = _d_render_static_layer()
                        dynamic_layer, d_cols_fill = _d_render_dynamic_full()
                        d_last_col_fill = d_cols_fill[-1] if d_cols_fill else _d_sample_column(int(w) - 1)
                        right_sample_now = int(d_last_col_fill["slow_idx"]) if d_last_col_fill is not None else _right_edge_sample_idx()
                        d_last_delta = float(d_last_col_fill["delta"]) if d_last_col_fill is not None else 0.0
                        scroll_state_by_hud[hud_state_key] = {
                            "static_layer": static_layer,
                            "dynamic_layer": dynamic_layer,
                            "scroll_pos_px": 0.0,
                            "last_i": int(i),
                            "last_right_sample": int(right_sample_now),
                            "window_frames": int(window_frames),
                            "last_y": int(d_last_col_fill["y"]),
                            "last_delta_value": float(d_last_delta),
                            "last_delta_sign": int(_d_sign_from_delta(float(d_last_delta))),
                        }
                        value_layer = _render_value_layer_local(_d_draw_values_overlay)
                        hud_layer = _compose_hud_layers_local(static_layer, dynamic_layer, value_layer)
                        _composite_hud_into_frame_local(img, hud_layer, int(x0), int(y0))
                    elif is_line_delta:
                        static_layer = _ld_render_static_layer()
                        dynamic_layer, ld_cols_fill = _ld_render_dynamic_full()
                        ld_last_col_fill = ld_cols_fill[-1] if ld_cols_fill else _ld_sample_column(int(w) - 1)
                        right_sample_now = int(ld_last_col_fill["slow_idx"]) if ld_last_col_fill is not None else _right_edge_sample_idx()
                        scroll_state_by_hud[hud_state_key] = {
                            "static_layer": static_layer,
                            "dynamic_layer": dynamic_layer,
                            "scroll_pos_px": 0.0,
                            "last_i": int(i),
                            "last_right_sample": int(right_sample_now),
                            "window_frames": int(window_frames),
                            "last_y": float(ld_last_col_fill["y"]),
                        }
                        value_layer = _render_value_layer_local(_ld_draw_values_overlay)
                        hud_layer = _compose_hud_layers_local(static_layer, dynamic_layer, value_layer)
                        _composite_hud_into_frame_local(img, hud_layer, int(x0), int(y0))
                    elif is_under_oversteer:
                        static_layer = _uo_render_static_layer()
                        dynamic_layer, uo_cols_fill = _uo_render_dynamic_full()
                        uo_last_col_fill = uo_cols_fill[-1] if uo_cols_fill else _uo_sample_column(int(w) - 1)
                        right_sample_now = int(uo_last_col_fill["slow_idx"]) if uo_last_col_fill is not None else _right_edge_sample_idx()
                        scroll_state_by_hud[hud_state_key] = {
                            "static_layer": static_layer,
                            "dynamic_layer": dynamic_layer,
                            "scroll_pos_px": 0.0,
                            "last_i": int(i),
                            "last_right_sample": int(right_sample_now),
                            "window_frames": int(window_frames),
                            "last_y": (int(uo_last_col_fill["y_s"]), int(uo_last_col_fill["y_f"])),
                            "uo_last_fast_idx": int(uo_last_col_fill["fast_idx"]),
                        }
                        value_layer = _render_value_layer_local(None)
                        hud_layer = _compose_hud_layers_local(static_layer, dynamic_layer, value_layer)
                        _composite_hud_into_frame_local(img, hud_layer, int(x0), int(y0))
                    else:
                        static_layer = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
                        dynamic_layer = _render_scroll_hud_full(
                            scroll_pos_px_local=0.0,
                            shift_int_local=0,
                            right_edge_cols_local=max(1, int(w)),
                        )
                        right_sample_now = _right_edge_sample_idx()
                        scroll_state_by_hud[hud_state_key] = {
                            "static_layer": static_layer,
                            "dynamic_layer": dynamic_layer,
                            "scroll_pos_px": 0.0,
                            "last_i": int(i),
                            "last_right_sample": int(right_sample_now),
                            "window_frames": int(window_frames),
                        }
                        value_layer = _render_value_layer_local(None)
                        hud_layer = _compose_hud_layers_local(static_layer, dynamic_layer, value_layer)
                        _composite_hud_into_frame_local(img, hud_layer, int(x0), int(y0))
                    continue

                scroll_pos_px = float(state.get("scroll_pos_px", 0.0))
                scroll_pos_px += float(shift_px_per_frame)
                shift_int = 0
                if scroll_pos_px >= 1.0:
                    shift_int = int(math.floor(scroll_pos_px))
                    scroll_pos_px -= float(shift_int)

                # Deterministische Regel fuer rechte Randspalten:
                # - mit Shift: genau shift_int neue Spalten
                # - ohne Shift: 1 Spalte in-place am rechten Rand aktualisieren
                right_edge_cols = int(shift_int) if int(shift_int) > 0 else 1
                if right_edge_cols > int(w):
                    right_edge_cols = int(w)

                dynamic_prev = state.get("dynamic_layer")
                if dynamic_prev is None:
                    dynamic_prev = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
                dynamic_next = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
                shift_px = int(shift_int)
                if shift_px < int(w):
                    try:
                        preserved = dynamic_prev.crop((int(shift_px), 0, int(w), int(h)))
                        dynamic_next.paste(preserved, (0, 0))
                    except Exception:
                        pass
                if is_throttle_brake:
                    tb_cols_next = list(state.get("tb_cols") or [])
                    if shift_px > 0:
                        tb_cols_next = tb_cols_next[int(shift_px):]
                    if len(tb_cols_next) > int(w):
                        tb_cols_next = tb_cols_next[-int(w):]

                    tb_abs_state_inc: dict[str, Any] = {
                        "tb_abs_s_on": bool(state.get("tb_abs_s_on", False)),
                        "tb_abs_f_on": bool(state.get("tb_abs_f_on", False)),
                        "tb_abs_s_on_count": int(state.get("tb_abs_s_on_count", 0)),
                        "tb_abs_s_off_count": int(state.get("tb_abs_s_off_count", 0)),
                        "tb_abs_f_on_count": int(state.get("tb_abs_f_on_count", 0)),
                        "tb_abs_f_off_count": int(state.get("tb_abs_f_off_count", 0)),
                    }
                    tb_dr_next = ImageDraw.Draw(dynamic_next)
                    prev_col_inc = tb_cols_next[-1] if tb_cols_next else None

                    for c_inc in range(int(right_edge_cols)):
                        dest_x = int(w) - int(right_edge_cols) + int(c_inc)
                        if dest_x < 0:
                            dest_x = 0
                        if dest_x >= int(w):
                            dest_x = int(w) - 1

                        dynamic_next.paste((0, 0, 0, 0), (int(dest_x), 0, int(dest_x) + 1, int(h)))
                        col_now_inc = _tb_sample_column(int(dest_x))
                        abs_s_on_inc, abs_f_on_inc = _tb_apply_abs_debounce(
                            tb_abs_state_inc,
                            bool(col_now_inc["abs_s_raw_on"]),
                            bool(col_now_inc["abs_f_raw_on"]),
                        )
                        col_now_inc["abs_s_on"] = bool(abs_s_on_inc)
                        col_now_inc["abs_f_on"] = bool(abs_f_on_inc)

                        if prev_col_inc is not None:
                            x_prev_inc = int(prev_col_inc["x"])
                            x_cur_inc = int(col_now_inc["x"])
                            tb_dr_next.line([(x_prev_inc, int(prev_col_inc["y_s_b"])), (x_cur_inc, int(col_now_inc["y_s_b"]))], fill=COL_SLOW_DARKRED, width=2)
                            tb_dr_next.line([(x_prev_inc, int(prev_col_inc["y_s_t"])), (x_cur_inc, int(col_now_inc["y_s_t"]))], fill=COL_SLOW_BRIGHTRED, width=2)
                            tb_dr_next.line([(x_prev_inc, int(prev_col_inc["y_f_b"])), (x_cur_inc, int(col_now_inc["y_f_b"]))], fill=COL_FAST_DARKBLUE, width=2)
                            tb_dr_next.line([(x_prev_inc, int(prev_col_inc["y_f_t"])), (x_cur_inc, int(col_now_inc["y_f_t"]))], fill=COL_FAST_BRIGHTBLUE, width=2)

                        if bool(col_now_inc["abs_s_on"]):
                            y0_abs_s_inc = int(tb_layout["y_abs_s"])
                            y1_abs_s_inc = int(tb_layout["y_abs_s"]) + int(tb_layout["abs_h"]) - 1
                            tb_dr_next.line([(int(dest_x), y0_abs_s_inc), (int(dest_x), y1_abs_s_inc)], fill=COL_SLOW_DARKRED, width=1)
                        if bool(col_now_inc["abs_f_on"]):
                            y0_abs_f_inc = int(tb_layout["y_abs_f"])
                            y1_abs_f_inc = int(tb_layout["y_abs_f"]) + int(tb_layout["abs_h"]) - 1
                            tb_dr_next.line([(int(dest_x), y0_abs_f_inc), (int(dest_x), y1_abs_f_inc)], fill=COL_FAST_DARKBLUE, width=1)

                        tb_cols_next.append(col_now_inc)
                        if len(tb_cols_next) > int(w):
                            tb_cols_next = tb_cols_next[-int(w):]
                        prev_col_inc = col_now_inc

                    right_sample_now = int(tb_cols_next[-1]["slow_idx"]) if tb_cols_next else _right_edge_sample_idx()
                    state["dynamic_layer"] = dynamic_next
                    state["scroll_pos_px"] = float(scroll_pos_px)
                    state["last_i"] = int(i)
                    state["last_right_sample"] = int(right_sample_now)
                    state["window_frames"] = int(window_frames)
                    state["tb_cols"] = tb_cols_next
                    state["tb_abs_s_on"] = bool(tb_abs_state_inc.get("tb_abs_s_on", False))
                    state["tb_abs_f_on"] = bool(tb_abs_state_inc.get("tb_abs_f_on", False))
                    state["tb_abs_s_on_count"] = int(tb_abs_state_inc.get("tb_abs_s_on_count", 0))
                    state["tb_abs_s_off_count"] = int(tb_abs_state_inc.get("tb_abs_s_off_count", 0))
                    state["tb_abs_f_on_count"] = int(tb_abs_state_inc.get("tb_abs_f_on_count", 0))
                    state["tb_abs_f_off_count"] = int(tb_abs_state_inc.get("tb_abs_f_off_count", 0))

                    static_now = state.get("static_layer")
                    value_layer = _render_value_layer_local(_tb_draw_values_overlay)
                    hud_layer = _compose_hud_layers_local(static_now, dynamic_next, value_layer)
                    _composite_hud_into_frame_local(img, hud_layer, int(x0), int(y0))
                elif is_steering:
                    st_dr_next = ImageDraw.Draw(dynamic_next)

                    prev_x_inc: int | None = None
                    prev_y_s_inc: int | None = None
                    prev_y_f_inc: int | None = None
                    if int(right_edge_cols) > 0:
                        first_dest_x = int(w) - int(right_edge_cols)
                        if first_dest_x < 0:
                            first_dest_x = 0
                        if first_dest_x > 0:
                            prev_x_inc = int(first_dest_x) - 1
                            if shift_px > 0:
                                last_y_state = state.get("last_y")
                                if isinstance(last_y_state, (list, tuple)) and len(last_y_state) >= 2:
                                    try:
                                        prev_y_s_inc = int(last_y_state[0])
                                        prev_y_f_inc = int(last_y_state[1])
                                    except Exception:
                                        prev_y_s_inc = None
                                        prev_y_f_inc = None
                            if prev_y_s_inc is None or prev_y_f_inc is None:
                                col_prev_left = _st_sample_column(int(prev_x_inc))
                                prev_y_s_inc = int(col_prev_left["y_s"])
                                prev_y_f_inc = int(col_prev_left["y_f"])

                    last_col_inc: dict[str, Any] | None = None
                    for c_inc in range(int(right_edge_cols)):
                        dest_x = int(w) - int(right_edge_cols) + int(c_inc)
                        if dest_x < 0:
                            dest_x = 0
                        if dest_x >= int(w):
                            dest_x = int(w) - 1

                        dynamic_next.paste((0, 0, 0, 0), (int(dest_x), 0, int(dest_x) + 1, int(h)))
                        col_now_inc = _st_sample_column(int(dest_x))

                        if prev_x_inc is not None and prev_y_s_inc is not None:
                            st_dr_next.line(
                                [(int(prev_x_inc), int(prev_y_s_inc)), (int(dest_x), int(col_now_inc["y_s"]))],
                                fill=COL_SLOW_DARKRED,
                                width=2,
                            )
                        if prev_x_inc is not None and prev_y_f_inc is not None:
                            st_dr_next.line(
                                [(int(prev_x_inc), int(prev_y_f_inc)), (int(dest_x), int(col_now_inc["y_f"]))],
                                fill=COL_FAST_DARKBLUE,
                                width=2,
                            )

                        prev_x_inc = int(dest_x)
                        prev_y_s_inc = int(col_now_inc["y_s"])
                        prev_y_f_inc = int(col_now_inc["y_f"])
                        last_col_inc = col_now_inc

                    if last_col_inc is None:
                        last_col_inc = _st_sample_column(int(w) - 1)
                    right_sample_now = int(last_col_inc["slow_idx"]) if last_col_inc is not None else _right_edge_sample_idx()
                    state["dynamic_layer"] = dynamic_next
                    state["scroll_pos_px"] = float(scroll_pos_px)
                    state["last_i"] = int(i)
                    state["last_right_sample"] = int(right_sample_now)
                    state["window_frames"] = int(window_frames)
                    state["last_y"] = (int(last_col_inc["y_s"]), int(last_col_inc["y_f"]))
                    state["st_last_fast_idx"] = int(last_col_inc["fast_idx"])

                    static_now = state.get("static_layer")
                    value_layer = _render_value_layer_local(_st_draw_values_overlay)
                    hud_layer = _compose_hud_layers_local(static_now, dynamic_next, value_layer)
                    _composite_hud_into_frame_local(img, hud_layer, int(x0), int(y0))
                elif is_delta:
                    d_dr_next = ImageDraw.Draw(dynamic_next)

                    prev_x_inc: int | None = None
                    prev_y_inc: int | None = None
                    prev_d_inc: float | None = None
                    if int(right_edge_cols) > 0:
                        first_dest_x = int(w) - int(right_edge_cols)
                        if first_dest_x < 0:
                            first_dest_x = 0
                        if first_dest_x > 0:
                            prev_x_inc = int(first_dest_x) - 1
                            if shift_px > 0:
                                last_y_state = state.get("last_y")
                                last_d_state = state.get("last_delta_value")
                                if last_y_state is not None:
                                    try:
                                        prev_y_inc = int(last_y_state)
                                    except Exception:
                                        prev_y_inc = None
                                if last_d_state is not None:
                                    try:
                                        prev_d_inc = float(last_d_state)
                                    except Exception:
                                        prev_d_inc = None
                            if prev_y_inc is None or prev_d_inc is None:
                                col_prev_left = _d_sample_column(int(prev_x_inc))
                                prev_y_inc = int(col_prev_left["y"])
                                prev_d_inc = float(col_prev_left["delta"])

                    last_col_inc: dict[str, Any] | None = None
                    for c_inc in range(int(right_edge_cols)):
                        dest_x = int(w) - int(right_edge_cols) + int(c_inc)
                        if dest_x < 0:
                            dest_x = 0
                        if dest_x >= int(w):
                            dest_x = int(w) - 1

                        dynamic_next.paste((0, 0, 0, 0), (int(dest_x), 0, int(dest_x) + 1, int(h)))
                        col_now_inc = _d_sample_column(int(dest_x))

                        if prev_x_inc is not None and prev_y_inc is not None and prev_d_inc is not None:
                            _d_draw_segment(
                                d_dr_next,
                                int(prev_x_inc),
                                int(prev_y_inc),
                                float(prev_d_inc),
                                int(dest_x),
                                int(col_now_inc["y"]),
                                float(col_now_inc["delta"]),
                            )

                        prev_x_inc = int(dest_x)
                        prev_y_inc = int(col_now_inc["y"])
                        prev_d_inc = float(col_now_inc["delta"])
                        last_col_inc = col_now_inc

                    if last_col_inc is None:
                        last_col_inc = _d_sample_column(int(w) - 1)
                    right_sample_now = int(last_col_inc["slow_idx"]) if last_col_inc is not None else _right_edge_sample_idx()
                    last_delta_now = float(last_col_inc["delta"]) if last_col_inc is not None else 0.0
                    state["dynamic_layer"] = dynamic_next
                    state["scroll_pos_px"] = float(scroll_pos_px)
                    state["last_i"] = int(i)
                    state["last_right_sample"] = int(right_sample_now)
                    state["window_frames"] = int(window_frames)
                    state["last_y"] = int(last_col_inc["y"])
                    state["last_delta_value"] = float(last_delta_now)
                    state["last_delta_sign"] = int(_d_sign_from_delta(float(last_delta_now)))

                    static_now = state.get("static_layer")
                    value_layer = _render_value_layer_local(_d_draw_values_overlay)
                    hud_layer = _compose_hud_layers_local(static_now, dynamic_next, value_layer)
                    _composite_hud_into_frame_local(img, hud_layer, int(x0), int(y0))
                elif is_line_delta:
                    ld_dr_next = ImageDraw.Draw(dynamic_next)

                    prev_x_inc: int | None = None
                    prev_y_inc: int | None = None
                    if int(right_edge_cols) > 0:
                        first_dest_x = int(w) - int(right_edge_cols)
                        if first_dest_x < 0:
                            first_dest_x = 0
                        if first_dest_x > 0:
                            prev_x_inc = int(first_dest_x) - 1
                            if shift_px > 0:
                                last_y_state = state.get("last_y")
                                if last_y_state is not None:
                                    try:
                                        prev_y_inc = int(round(float(last_y_state)))
                                    except Exception:
                                        prev_y_inc = None
                            if prev_y_inc is None:
                                col_prev_left = _ld_sample_column(int(prev_x_inc))
                                prev_y_inc = int(col_prev_left["y"])

                    last_col_inc: dict[str, Any] | None = None
                    for c_inc in range(int(right_edge_cols)):
                        dest_x = int(w) - int(right_edge_cols) + int(c_inc)
                        if dest_x < 0:
                            dest_x = 0
                        if dest_x >= int(w):
                            dest_x = int(w) - 1

                        dynamic_next.paste((0, 0, 0, 0), (int(dest_x), 0, int(dest_x) + 1, int(h)))
                        col_now_inc = _ld_sample_column(int(dest_x))

                        if prev_x_inc is not None and prev_y_inc is not None:
                            try:
                                ld_dr_next.line(
                                    [(int(prev_x_inc), int(prev_y_inc)), (int(dest_x), int(col_now_inc["y"]))],
                                    fill=COL_FAST_DARKBLUE,
                                    width=2,
                                )
                            except Exception:
                                pass

                        prev_x_inc = int(dest_x)
                        prev_y_inc = int(col_now_inc["y"])
                        last_col_inc = col_now_inc

                    if last_col_inc is None:
                        last_col_inc = _ld_sample_column(int(w) - 1)
                    right_sample_now = int(last_col_inc["slow_idx"]) if last_col_inc is not None else _right_edge_sample_idx()
                    state["dynamic_layer"] = dynamic_next
                    state["scroll_pos_px"] = float(scroll_pos_px)
                    state["last_i"] = int(i)
                    state["last_right_sample"] = int(right_sample_now)
                    state["window_frames"] = int(window_frames)
                    state["last_y"] = float(last_col_inc["y"])

                    static_now = state.get("static_layer")
                    value_layer = _render_value_layer_local(_ld_draw_values_overlay)
                    hud_layer = _compose_hud_layers_local(static_now, dynamic_next, value_layer)
                    _composite_hud_into_frame_local(img, hud_layer, int(x0), int(y0))
                elif is_under_oversteer:
                    uo_dr_next = ImageDraw.Draw(dynamic_next)

                    prev_x_inc: int | None = None
                    prev_y_s_inc: int | None = None
                    prev_y_f_inc: int | None = None
                    if int(right_edge_cols) > 0:
                        first_dest_x = int(w) - int(right_edge_cols)
                        if first_dest_x < 0:
                            first_dest_x = 0
                        if first_dest_x > 0:
                            prev_x_inc = int(first_dest_x) - 1
                            if shift_px > 0:
                                last_y_state = state.get("last_y")
                                if isinstance(last_y_state, (list, tuple)) and len(last_y_state) >= 2:
                                    try:
                                        prev_y_s_inc = int(last_y_state[0])
                                        prev_y_f_inc = int(last_y_state[1])
                                    except Exception:
                                        prev_y_s_inc = None
                                        prev_y_f_inc = None
                            if prev_y_s_inc is None or prev_y_f_inc is None:
                                col_prev_left = _uo_sample_column(int(prev_x_inc))
                                prev_y_s_inc = int(col_prev_left["y_s"])
                                prev_y_f_inc = int(col_prev_left["y_f"])

                    last_col_inc: dict[str, Any] | None = None
                    for c_inc in range(int(right_edge_cols)):
                        dest_x = int(w) - int(right_edge_cols) + int(c_inc)
                        if dest_x < 0:
                            dest_x = 0
                        if dest_x >= int(w):
                            dest_x = int(w) - 1

                        dynamic_next.paste((0, 0, 0, 0), (int(dest_x), 0, int(dest_x) + 1, int(h)))
                        col_now_inc = _uo_sample_column(int(dest_x))

                        if prev_x_inc is not None and prev_y_s_inc is not None:
                            try:
                                uo_dr_next.line(
                                    [(int(prev_x_inc), int(prev_y_s_inc)), (int(dest_x), int(col_now_inc["y_s"]))],
                                    fill=COL_SLOW_DARKRED,
                                    width=2,
                                )
                            except Exception:
                                pass
                        if prev_x_inc is not None and prev_y_f_inc is not None:
                            try:
                                uo_dr_next.line(
                                    [(int(prev_x_inc), int(prev_y_f_inc)), (int(dest_x), int(col_now_inc["y_f"]))],
                                    fill=COL_FAST_DARKBLUE,
                                    width=2,
                                )
                            except Exception:
                                pass

                        prev_x_inc = int(dest_x)
                        prev_y_s_inc = int(col_now_inc["y_s"])
                        prev_y_f_inc = int(col_now_inc["y_f"])
                        last_col_inc = col_now_inc

                    if last_col_inc is None:
                        last_col_inc = _uo_sample_column(int(w) - 1)
                    right_sample_now = int(last_col_inc["slow_idx"]) if last_col_inc is not None else _right_edge_sample_idx()
                    state["dynamic_layer"] = dynamic_next
                    state["scroll_pos_px"] = float(scroll_pos_px)
                    state["last_i"] = int(i)
                    state["last_right_sample"] = int(right_sample_now)
                    state["window_frames"] = int(window_frames)
                    state["last_y"] = (int(last_col_inc["y_s"]), int(last_col_inc["y_f"]))
                    state["uo_last_fast_idx"] = int(last_col_inc["fast_idx"])

                    static_now = state.get("static_layer")
                    value_layer = _render_value_layer_local(None)
                    hud_layer = _compose_hud_layers_local(static_now, dynamic_next, value_layer)
                    _composite_hud_into_frame_local(img, hud_layer, int(x0), int(y0))
                else:
                    hud_full_now = _render_scroll_hud_full(
                        scroll_pos_px_local=scroll_pos_px,
                        shift_int_local=int(shift_int),
                        right_edge_cols_local=int(right_edge_cols),
                    )
                    try:
                        edge_strip = hud_full_now.crop((int(w) - int(right_edge_cols), 0, int(w), int(h)))
                        dynamic_next.paste(edge_strip, (int(w) - int(right_edge_cols), 0))
                    except Exception:
                        dynamic_next = hud_full_now

                    right_sample_now = _right_edge_sample_idx()
                    state["dynamic_layer"] = dynamic_next
                    state["scroll_pos_px"] = float(scroll_pos_px)
                    state["last_i"] = int(i)
                    state["last_right_sample"] = int(right_sample_now)
                    state["window_frames"] = int(window_frames)
                    static_now = state.get("static_layer")
                    value_layer = _render_value_layer_local(None)
                    hud_layer = _compose_hud_layers_local(static_now, dynamic_next, value_layer)
                    _composite_hud_into_frame_local(img, hud_layer, int(x0), int(y0))
            except Exception:
                continue


        fn = out_dir / f"hud_{j:06d}.png"
        img.save(fn)

        # ZusÃ¤tzlich: 1 Sample pro Sekunde kopieren
        try:
            one_sec = max(1, int(round(r)))
            if (j % one_sec) == 0:
                sec = int(j // one_sec)
                sfn = samples_dir / f"hud_sample_{sec:04d}.png"
                shutil.copyfile(fn, sfn)
        except Exception:
            pass

        if hud_dbg and j < 2:
            _log_print(f"[hudpy] sample j={j} ld={ld:.6f} ld_mod={ld_mod:.6f} -> {fn.name}", log_file)
    try:
        n_written = len(list(out_dir.glob("hud_*.png")))
    except Exception:
        n_written = -1
    _log_print(f"[hudpy] geschrieben: {n_written} frames -> {out_dir}", log_file)
    _log_print(f"[hudpy] samples: {samples_dir}", log_file)
    return str(out_dir / "hud_%06d.png")
    
def _wrap_delta_05(a: float, b: float) -> float:
    # kleinste Differenz in [-0.5 .. +0.5]
    # delta = a - b
    d = (float(a) - float(b) + 0.5) % 1.0 - 0.5
    return float(d)

def _enabled_hud_boxes_abs(
    geom: OutputGeometry,
    hud_enabled: Any | None,
    hud_boxes: Any | None,
) -> list[tuple[str, tuple[int, int, int, int]]]:
    """
    Gibt ALLE aktiven HUD-Boxen zurÃ¼ck (Name + Box absolut im Output).
    """
    enabled_names: set[str] = set()
    try:
        if isinstance(hud_enabled, dict):
            for k, v in hud_enabled.items():
                if v:
                    enabled_names.add(str(k))
        elif isinstance(hud_enabled, (list, tuple, set)):
            for k in hud_enabled:
                enabled_names.add(str(k))
    except Exception:
        enabled_names = set()

    norm_boxes: list[dict[str, Any]] = []
    try:
        if isinstance(hud_boxes, dict):
            for k, v in hud_boxes.items():
                if isinstance(v, dict):
                    norm_boxes.append(
                        {
                            "name": v.get("name") or str(k),
                            "x": v.get("x", 0),
                            "y": v.get("y", 0),
                            "w": v.get("w", 0),
                            "h": v.get("h", 0),
                        }
                    )
        elif isinstance(hud_boxes, list):
            for b in hud_boxes:
                if isinstance(b, dict):
                    norm_boxes.append(b)
    except Exception:
        norm_boxes = []

    hud_x0 = int(getattr(geom, "left_w", 0))

    # Auto-Detect absolut vs relativ
    max_x = 0
    try:
        for b in norm_boxes:
            max_x = max(max_x, int(round(float(b.get("x", 0) or 0))))
    except Exception:
        max_x = 0

    boxes_are_absolute = False
    try:
        boxes_are_absolute = max_x > (int(getattr(geom, "hud")) + 10)
    except Exception:
        boxes_are_absolute = False

    out: list[tuple[str, tuple[int, int, int, int]]] = []
    for b in norm_boxes:
        try:
            name = str(b.get("name") or b.get("id") or "")
            if not name:
                continue
            if enabled_names and (name not in enabled_names):
                continue

            x = int(round(float(b.get("x", 0))))
            y = int(round(float(b.get("y", 0))))
            w = int(round(float(b.get("w", 0))))
            h = int(round(float(b.get("h", 0))))

            if w <= 0 or h <= 0:
                continue

            if boxes_are_absolute:
                x_abs = max(0, x)
            else:
                x_abs = hud_x0 + max(0, x)
            y_abs = max(0, y)

            out.append((name, (x_abs, y_abs, w, h)))
        except Exception:
            continue

    return out


def render_split_screen(
    slow: Path,
    fast: Path,
    outp: Path,
    start_s: float,
    duration_s: float,
    preset_w: int,
    preset_h: int,
    hud_width_px: int,
    view_L: dict[str, Any] | None = None,
    view_R: dict[str, Any] | None = None,
    audio_source: str = "none",
    hud_enabled: Any | None = None,
    hud_boxes: Any | None = None,
    hud_speed_units: str = "kmh",
    hud_speed_update_hz: int = 60,
    hud_gear_rpm_update_hz: int = 60,
    hud_pedals_sample_mode: str = "time",
    hud_pedals_abs_debounce_ms: int = 60,
    log_file: "Path | None" = None,
) -> None:
    # 1) Config reading
    slow = Path(slow).resolve()
    fast = Path(fast).resolve()
    outp = Path(outp).resolve()
    outp.parent.mkdir(parents=True, exist_ok=True)

    ms = probe_video_meta(slow)
    mf = probe_video_meta(fast)

    if abs(ms.fps - mf.fps) > 0.01:
        raise RuntimeError("slow/fast haben nicht die gleiche FPS.")

    fps_int = int(round(ms.fps))
    if fps_int <= 0:
        fps_int = 30

    if audio_source not in ("slow", "fast", "none"):
        audio_source = "slow"

    preset = f"{ms.width}x{ms.height}"
    if preset_w > 0 and preset_h > 0:
        preset = f"{int(preset_w)}x{int(preset_h)}"

    # 3) Layout
    geom = build_output_geometry(preset, hud_width_px=hud_width_px)

    filt = build_split_filter_from_geometry(
        geom=geom,
        fps=float(fps_int),
        view_L=view_L,
        view_R=view_R,
        hud_enabled=hud_enabled,
        hud_boxes=hud_boxes,
    )

    available_encoders = detect_available_encoders("ffmpeg")
    encode_candidates = build_encode_specs(
        W=geom.W,
        fps=float(fps_int),
        available=available_encoders,
    )

    has_nvenc = ("h264_nvenc" in available_encoders) or ("hevc_nvenc" in available_encoders)
    has_qsv = ("h264_qsv" in available_encoders) or ("hevc_qsv" in available_encoders)
    has_amf = ("h264_amf" in available_encoders) or ("hevc_amf" in available_encoders)
    print(f"[gpu] nvenc={has_nvenc} qsv={has_qsv} amf={has_amf} (cpu=libx264 immer)")

    # 5) FFmpeg Run
    specs_by_vcodec = {enc.vcodec: enc for enc in encode_candidates}

    def _run_one_encoder(vcodec: str) -> tuple[int, bool]:
        enc = specs_by_vcodec[vcodec]
        # Debug: nur die ersten N Sekunden rendern
        try:
            dbg_max_s = float((os.environ.get("IRVC_DEBUG_MAX_S") or "").strip() or "0")
        except Exception:
            dbg_max_s = 0.0
        if dbg_max_s > 0.0:
            print(f"[debug] IRVC_DEBUG_MAX_S={dbg_max_s} -> input limited")

        plan = build_plan(
            decode=DecodeSpec(slow=slow, fast=fast),
            flt=FilterSpec(filter_complex=filt, video_map="[vout]", audio_map=None),
            enc=enc,
            audio_source=audio_source,
            outp=outp,
            debug_max_s=dbg_max_s,
        )

        live = (os.environ.get("IRVC_FFMPEG_LIVE") or "").strip() == "1"
        rc = run_ffmpeg(plan, tail_n=20, log_file=log_file, live_stdout=live)
        return rc, (rc == 0 and outp.exists())

    selected_vcodec, last_rc = run_encode_with_fallback(
        build_cmd_fn=_run_one_encoder,
        encoder_order=[enc.vcodec for enc in encode_candidates],
        log_fn=print,
    )
    if selected_vcodec != "":
        return

    raise RuntimeError(f"ffmpeg failed (rc={last_rc})")


def render_split_screen_sync(
    slow: Path,
    fast: Path,
    slow_csv: Path,
    fast_csv: Path,
    outp: Path,
    start_s: float,
    duration_s: float,
    preset_w: int,
    preset_h: int,
    hud_width_px: int,
    view_L: dict[str, Any] | None = None,
    view_R: dict[str, Any] | None = None,
    audio_source: str = "none",
    hud_enabled: Any | None = None,
    hud_boxes: Any | None = None,
    hud_window_default_before_s: float = 10.0,
    hud_window_default_after_s: float = 10.0,
    hud_window_overrides: Any | None = None,
    hud_curve_points_default: int = 180,
    hud_curve_points_overrides: Any | None = None,
    hud_speed_units: str = "kmh",
    hud_speed_update_hz: int = 60,
    hud_gear_rpm_update_hz: int = 60,
    hud_pedals_sample_mode: str = "time",
    hud_pedals_abs_debounce_ms: int = 60,
    under_oversteer_curve_center: float = 0.0,
    log_file: "Path | None" = None,
) -> None:
    # Story 6: Stream-Sync (ohne PNG, ohne fast_sync.mp4, ein ffmpeg-Run)
    # 1) Config reading
    slow = Path(slow).resolve()
    fast = Path(fast).resolve()
    scsv = Path(slow_csv).resolve()
    fcsv = Path(fast_csv).resolve()
    outp = Path(outp).resolve()
    outp.parent.mkdir(parents=True, exist_ok=True)

    ms = probe_video_meta(slow)
    mf = probe_video_meta(fast)

    if abs(ms.fps - mf.fps) > 0.01:
        raise RuntimeError("slow/fast haben nicht die gleiche FPS.")

    fps_int = int(round(ms.fps))
    if fps_int <= 0:
        fps_int = 30

    if audio_source not in ("slow", "fast", "none"):
        audio_source = "slow"

    try:
        under_oversteer_curve_center = float(under_oversteer_curve_center)
    except Exception:
        under_oversteer_curve_center = 0.0
    under_oversteer_curve_center = float(_clamp(float(under_oversteer_curve_center), -50.0, 50.0))

    hud_pedals_sample_mode = str(hud_pedals_sample_mode or "time").strip().lower()
    if hud_pedals_sample_mode not in ("time", "legacy"):
        hud_pedals_sample_mode = "time"
    try:
        hud_pedals_abs_debounce_ms = int(hud_pedals_abs_debounce_ms)
    except Exception:
        hud_pedals_abs_debounce_ms = 60
    if hud_pedals_abs_debounce_ms < 0:
        hud_pedals_abs_debounce_ms = 0
    if hud_pedals_abs_debounce_ms > 500:
        hud_pedals_abs_debounce_ms = 500

    preset = f"{ms.width}x{ms.height}"
    if preset_w > 0 and preset_h > 0:
        preset = f"{int(preset_w)}x{int(preset_h)}"

    # 2) Sync/Mapping
    frame_map, slow_frame_to_lapdist, slow_frame_to_fast_time_s, slow_frame_speed_diff = _build_sync_cache_maps_from_csv(
        slow_csv=scsv,
        fast_csv=fcsv,
        fps=float(fps_int),
        slow_duration_s=ms.duration_s,
        fast_duration_s=mf.duration_s,
    )
    
    # Debug: Sync-Map / Delta-Grundlage prÃ¼fen (warum Delta ggf. ~0 ist)
    try:
        n_map = int(len(slow_frame_to_fast_time_s)) if slow_frame_to_fast_time_s else 0
        if n_map <= 0:
            _log_print("[sync6][dbg] slow_frame_to_fast_time_s EMPTY -> Delta basiert nicht auf Zeitmap.", log_file)
        else:
            fps_safe = float(fps_int) if float(fps_int) > 0.1 else 30.0

            def _delta_at(ii: int) -> float:
                if ii < 0:
                    ii = 0
                if ii >= n_map:
                    ii = n_map - 1
                slow_t = float(ii) / fps_safe
                fast_t = float(slow_frame_to_fast_time_s[ii])
                return float(slow_t - fast_t)

            d0 = _delta_at(0)
            d_last = _delta_at(n_map - 1)

            idxs = sorted(set([0, 1, 2, max(0, n_map - 3), max(0, n_map - 2), n_map - 1]))
            samples: list[str] = []
            for ii in idxs:
                try:
                    ft = float(slow_frame_to_fast_time_s[ii])
                    dd = _delta_at(ii)
                    samples.append(f"{ii}:d={dd:+.6f}s ft={ft:.6f}s")
                except Exception:
                    samples.append(f"{ii}:<err>")

            _log_print(f"[sync6][dbg] slow_frame_to_fast_time_s n={n_map} delta0={d0:+.6f}s delta_last={d_last:+.6f}s", log_file)
            _log_print("[sync6][dbg] slow_frame_to_fast_time_s samples: " + ", ".join(samples), log_file)
    except Exception:
        pass
    
    
    # Story 5/6: Table-HUD Daten pro Frame (ohne Fenster)
    slow_speed_frames = _sample_csv_col_to_frames_float(scsv, ms.duration_s, float(fps_int), "Speed")
    fast_speed_frames = _sample_csv_col_to_frames_float(fcsv, mf.duration_s, float(fps_int), "Speed")
    slow_gear_frames = _sample_csv_col_to_frames_int_nearest(scsv, ms.duration_s, float(fps_int), "Gear")
    fast_gear_frames = _sample_csv_col_to_frames_int_nearest(fcsv, mf.duration_s, float(fps_int), "Gear")
    slow_rpm_frames = _sample_csv_col_to_frames_float(scsv, ms.duration_s, float(fps_int), "RPM")
    fast_rpm_frames = _sample_csv_col_to_frames_float(fcsv, mf.duration_s, float(fps_int), "RPM")
    # Story 3: Steering pro Frame (Scroll-HUD)
    slow_steer_frames = _sample_csv_col_to_frames_float(scsv, ms.duration_s, float(fps_int), "SteeringWheelAngle")
    fast_steer_frames = _sample_csv_col_to_frames_float(fcsv, mf.duration_s, float(fps_int), "SteeringWheelAngle")
    # Story 4: Throttle / Brake / ABS pro Frame (Scroll-HUD)
    slow_throttle_frames = _sample_csv_col_to_frames_float(scsv, ms.duration_s, float(fps_int), "Throttle")
    fast_throttle_frames = _sample_csv_col_to_frames_float(fcsv, mf.duration_s, float(fps_int), "Throttle")
    slow_brake_frames = _sample_csv_col_to_frames_float(scsv, ms.duration_s, float(fps_int), "Brake")
    fast_brake_frames = _sample_csv_col_to_frames_float(fcsv, mf.duration_s, float(fps_int), "Brake")
    slow_abs_frames = _sample_csv_col_to_frames_float(scsv, ms.duration_s, float(fps_int), "ABSActive")
    fast_abs_frames = _sample_csv_col_to_frames_float(fcsv, mf.duration_s, float(fps_int), "ABSActive")
    line_delta_m_frames: list[float] = []
    line_delta_y_abs_m = 0.0
    under_oversteer_slow_frames: list[float] = []
    under_oversteer_fast_frames: list[float] = []
    under_oversteer_y_abs = 1.0


    slow_min_speed_frames = _compute_min_speed_display(slow_speed_frames, float(fps_int), str(hud_speed_units)) if slow_speed_frames else []
    fast_min_speed_frames = _compute_min_speed_display(fast_speed_frames, float(fps_int), str(hud_speed_units)) if fast_speed_frames else []


    cut_i0, cut_i1 = _compute_common_cut_by_fast_time(
        fast_time_s=slow_frame_to_fast_time_s,
        fast_duration_s=mf.duration_s,
        fps=float(fps_int),
    )

    # 3) Layout
    geom = build_output_geometry(preset, hud_width_px=hud_width_px)
    
    # Debug: Cut-Bereich auf die ersten N Sekunden begrenzen
    try:
        dbg_max_s = float((os.environ.get("IRVC_DEBUG_MAX_S") or "").strip() or "0")
    except Exception:
        dbg_max_s = 0.0
    if dbg_max_s > 0.0:
        max_frames = int(round(dbg_max_s * float(fps_int)))
        cut_i1 = min(cut_i1, cut_i0 + max(1, max_frames))
        print(f"[debug] IRVC_DEBUG_MAX_S={dbg_max_s} -> cut_i1 limited to {cut_i1}")    

    dbg_dir = outp.parent.parent / "debug"
    dbg_dir.mkdir(parents=True, exist_ok=True)
    sync_cache_path = dbg_dir / "sync_cache.json"

    try:
        k_frames = int((os.environ.get("SYNC6_K_FRAMES") or "").strip() or "30")
    except Exception:
        k_frames = 30

    sync_cache = {
        "mode": "stream",
        "fps": fps_int,
        "frame_count": len(frame_map),
        "cut_i0": int(cut_i0),
        "cut_i1": int(cut_i1),
        "k_frames": int(k_frames),
        "slow_frame_to_lapdist": slow_frame_to_lapdist,
        "slow_frame_to_fast_frame": frame_map,
        "slow_frame_to_fast_time_s": slow_frame_to_fast_time_s,
        "paths": {
            "fast_frames_dir": "",
            "fast_sync_mp4": "",
        },
    }
    sync_cache_path.write_text(json.dumps(sync_cache, indent=2), encoding="utf-8")

    print(f"[sync6] mode=stream fps={fps_int}")
    print(f"[sync6] cut_i0={cut_i0} cut_i1={cut_i1} (output wird gekuerzt)")
    print(f"[sync6] k_frames={k_frames} (Segment-Schritt)")

    if slow_frame_speed_diff is None:
        print("[sync6] speed_diff: nicht verfÃ¼gbar (Speed fehlt in CSV)")

    has_audio_slow = probe_has_audio(slow)
    has_audio_fast = probe_has_audio(fast)

    if audio_source == "slow" and not has_audio_slow:
        print("[sync6] audio: slow hat keinen Audio-Stream -> audio=none")
        audio_source = "none"
    elif audio_source == "fast" and not has_audio_fast:
        print("[sync6] audio: fast hat keinen Audio-Stream -> audio=none")
        audio_source = "none"

    # 4) HUD Render
    # Story 2: HUD pro Frame in Python rendern (PNG Sequenz), dann als 3. ffmpeg Input Ã¼berlagern
    hud_seq_pattern = None
    hud_scroll_on = (os.environ.get("IRVC_HUD_SCROLL") or "").strip() == "1"
    if hud_scroll_on:
        hud_frames_dir = dbg_dir / "hud_frames"

        # Story 2: Zeitfenster pro HUD (nicht nur erstes HUD)
        before_default_s = float(hud_window_default_before_s or 10.0)
        after_default_s = float(hud_window_default_after_s or 10.0)

        # aktive HUDs bestimmen (nur Namen)
        boxes_abs = _enabled_hud_boxes_abs(geom=geom, hud_enabled=hud_enabled, hud_boxes=hud_boxes)
        active_names = [n for (n, _b) in boxes_abs]
        if "Line Delta" in active_names:
            line_delta_m_frames = _build_line_delta_frames_from_csv(
                slow_csv=scsv,
                fast_csv=fcsv,
                slow_duration_s=ms.duration_s,
                fast_duration_s=mf.duration_s,
                fps=float(fps_int),
                slow_frame_to_fast_time_s=slow_frame_to_fast_time_s,
                frame_count_hint=len(slow_frame_to_lapdist),
            )
            abs_global_max = 0.0
            for dv in line_delta_m_frames:
                try:
                    av = abs(float(dv))
                    if math.isfinite(av) and av > abs_global_max:
                        abs_global_max = av
                except Exception:
                    pass
            line_delta_y_abs_m = float(abs_global_max) * 2.0
        if "Under-/Oversteer" in active_names:
            (
                under_oversteer_slow_frames,
                under_oversteer_fast_frames,
                under_oversteer_y_abs,
            ) = _build_under_oversteer_proxy_frames_from_csv(
                slow_csv=scsv,
                fast_csv=fcsv,
                slow_duration_s=ms.duration_s,
                fast_duration_s=mf.duration_s,
                fps=float(fps_int),
                slow_frame_to_fast_time_s=slow_frame_to_fast_time_s,
                frame_count_hint=len(slow_frame_to_lapdist),
                under_oversteer_curve_center=float(under_oversteer_curve_center),
                log_file=log_file,
            )

        # Overrides normalisieren
        ovs = hud_window_overrides if isinstance(hud_window_overrides, dict) else None

        # Fenster-Dict fÃ¼r Renderer: {hud_name: {"before_s": x, "after_s": y}}
        hud_windows: dict[str, dict[str, float]] = {}

        # Logging pro HUD (nur Scroll-HUDs)
        try:
            r = max(1.0, float(fps_int))
        except Exception:
            r = 30.0

        for hud_name in active_names:
            if hud_name not in _SCROLL_HUD_NAMES:
                continue

            b = float(before_default_s)
            a = float(after_default_s)
            
            # DEBUG: Zeigt, welche Sekundenwerte pro HUD wirklich verwendet werden
            # (damit wir sehen, warum Steering bei dir auf 0.1/0.1 steht)
            try:
                if (os.environ.get("RVA_HUD_STEER_DEBUG_FRAME") or "").strip() != "":
                    _log_print(
                        f"[hudpy][dbg-win] hud={hud_name} base_before={before_default_s} base_after={after_default_s} ovs_keys={(list(ovs.keys()) if isinstance(ovs, dict) else [])}",
                        log_file,
                    )
            except Exception:
                pass

            try:
                if ovs and isinstance(ovs.get(hud_name), dict):
                    o = ovs.get(hud_name) or {}
                    if o.get("before_s") is not None:
                        b = float(o.get("before_s"))
                    if o.get("after_s") is not None:
                        a = float(o.get("after_s"))
            except Exception:
                pass

            # Story 2.2: Scroll-HUD Fenster intern symmetrisch halten.
            # Damit ist die Pixel-Scrollrate links/rechts eindeutig.
            b = max(1e-6, float(b))
            a = max(1e-6, float(a))
            sym_s = max(float(b), float(a))
            b = float(sym_s)
            a = float(sym_s)
                 
            # DEBUG: finaler Wert, der gleich in hud_windows geschrieben wird
            try:
                if (os.environ.get("RVA_HUD_STEER_DEBUG_FRAME") or "").strip() != "":
                    if str(hud_name) == "Steering":
                        _log_print(f"[hudpy][dbg-win] FINAL hud=Steering b={b} a={a}", log_file)
            except Exception:
                pass

            hud_windows[str(hud_name)] = {"before_s": float(b), "after_s": float(a)}

            bf = max(1, int(round(float(b) * r)))
            af = max(1, int(round(float(a) * r)))
            _log_print(f"[hudpy] hud={hud_name} before_s={b} after_s={a} frames={bf+af+1}", log_file)

        hud_ctx = _build_hud_context(
            fps=float(fps_int),
            cut_i0=int(cut_i0),
            cut_i1=int(cut_i1),
            geom=geom,
            hud_enabled=hud_enabled,
            hud_boxes=hud_boxes,
            sync=HudSyncMapping(
                slow_frame_to_lapdist=slow_frame_to_lapdist,
                slow_to_fast_frame=frame_map,
                slow_frame_to_fast_time_s=slow_frame_to_fast_time_s,
            ),
            signals=HudSignals(
                slow_speed_frames=slow_speed_frames,
                fast_speed_frames=fast_speed_frames,
                slow_min_speed_frames=slow_min_speed_frames,
                fast_min_speed_frames=fast_min_speed_frames,
                slow_gear_frames=slow_gear_frames,
                fast_gear_frames=fast_gear_frames,
                slow_rpm_frames=slow_rpm_frames,
                fast_rpm_frames=fast_rpm_frames,
                slow_steer_frames=slow_steer_frames,
                fast_steer_frames=fast_steer_frames,
                slow_throttle_frames=slow_throttle_frames,
                fast_throttle_frames=fast_throttle_frames,
                slow_brake_frames=slow_brake_frames,
                fast_brake_frames=fast_brake_frames,
                slow_abs_frames=slow_abs_frames,
                fast_abs_frames=fast_abs_frames,
                line_delta_m_frames=line_delta_m_frames,
                line_delta_y_abs_m=line_delta_y_abs_m,
                under_oversteer_slow_frames=under_oversteer_slow_frames,
                under_oversteer_fast_frames=under_oversteer_fast_frames,
                under_oversteer_y_abs=under_oversteer_y_abs,
            ),
            window=HudWindowParams(
                before_s=float(before_default_s),
                after_s=float(after_default_s),
                hud_name=None,
                hud_windows=hud_windows,
            ),
            settings=HudRenderSettings(
                speed_units=str(hud_speed_units),
                speed_update_hz=int(hud_speed_update_hz),
                gear_rpm_update_hz=int(hud_gear_rpm_update_hz),
                curve_points_default=int(hud_curve_points_default),
                curve_points_overrides=hud_curve_points_overrides,
                pedals_sample_mode=str(hud_pedals_sample_mode),
                pedals_abs_debounce_ms=int(hud_pedals_abs_debounce_ms),
            ),
            log_file=log_file,
        )
        hud_seq_pattern = _render_hud_scroll_frames_png(hud_frames_dir, hud_ctx)
        
        if hud_seq_pattern:
            _log_print(f"[hudpy] ON -> {hud_seq_pattern}", log_file)
        else:
            _log_print("[hudpy] OFF (keine Sequenz erzeugt)", log_file)


    # sendcmd-demo komplett aus (war instabil / unwirksam)
    hud_cmd_file = None

    # Wenn wir HUD-Frames als 3. Input laden, ist das Input-Label in ffmpeg dann [0:v].
    # ABER: wir haben die Reihenfolge der Inputs im build_cmd geÃ¤ndert:
    #   optional HUD = Input 0
    #   slow = Input 1
    #   fast = Input 2
    #
    # Deshalb mÃ¼ssen wir hier merken: wenn hud_seq_pattern aktiv ist, dann ist hud_label="[0:v]"
    hud_label = "[0:v]" if hud_seq_pattern else None

    filt, audio_map = build_stream_sync_filter(
        geom=geom,
        fps=float(fps_int),
        view_L=view_L,
        view_R=view_R,
        fast_time_s=slow_frame_to_fast_time_s,
        speed_diff=slow_frame_speed_diff,
        cut_i0=cut_i0,
        cut_i1=cut_i1,
        audio_source=audio_source,
        hud_enabled=hud_enabled,
        hud_boxes=hud_boxes,
        hud_cmd_file=hud_cmd_file,
        log_file=log_file,
        hud_input_label=hud_label,
    )

    available_encoders = detect_available_encoders("ffmpeg")
    encode_candidates = build_encode_specs(
        W=geom.W,
        fps=float(fps_int),
        available=available_encoders,
    )

    has_nvenc = ("h264_nvenc" in available_encoders) or ("hevc_nvenc" in available_encoders)
    has_qsv = ("h264_qsv" in available_encoders) or ("hevc_qsv" in available_encoders)
    has_amf = ("h264_amf" in available_encoders) or ("hevc_amf" in available_encoders)
    print(f"[gpu] nvenc={has_nvenc} qsv={has_qsv} amf={has_amf} (cpu=libx264 immer)")

    # 5) FFmpeg Run
    specs_by_vcodec = {enc.vcodec: enc for enc in encode_candidates}

    def _run_one_encoder(vcodec: str) -> tuple[int, bool]:
        enc = specs_by_vcodec[vcodec]
        # Debug: nur die ersten N Sekunden rendern (spart Zeit)
        try:
            dbg_max_s = float((os.environ.get("IRVC_DEBUG_MAX_S") or "").strip() or "0")
        except Exception:
            dbg_max_s = 0.0
        if dbg_max_s > 0.0:
            print(f"[debug] IRVC_DEBUG_MAX_S={dbg_max_s} -> input limited")

        plan = build_plan(
            decode=DecodeSpec(
                slow=slow,
                fast=fast,
                hud_seq=hud_seq_pattern,
                hud_fps=float(fps_int),
            ),
            flt=FilterSpec(filter_complex=filt, video_map="[vout]", audio_map=audio_map),
            enc=enc,
            audio_source="none",
            outp=outp,
            debug_max_s=dbg_max_s,
        )

        live = (os.environ.get("IRVC_FFMPEG_LIVE") or "").strip() == "1"
        rc = run_ffmpeg(plan, tail_n=20, log_file=log_file, live_stdout=live)
        return rc, (rc == 0 and outp.exists())

    selected_vcodec, last_rc = run_encode_with_fallback(
        build_cmd_fn=_run_one_encoder,
        encoder_order=[enc.vcodec for enc in encode_candidates],
        log_fn=print,
    )
    if selected_vcodec != "":
        print(f"[sync6] sync_cache_json={sync_cache_path}")
        return

    raise RuntimeError(f"ffmpeg failed (rc={last_rc})")
