from __future__ import annotations

import json
import math
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from core.models import LayoutConfig
from core.output_geometry import (
    OutputGeometry,
    build_output_geometry_for_size,
    format_output_geometry_dump,
    geometry_signature,
)
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
from huds.gear_rpm import (
    build_gear_rpm_table_state,
    extract_gear_rpm_table_values,
    render_gear_rpm,
    render_gear_rpm_table_dynamic,
    render_gear_rpm_table_static,
)
from huds.line_delta import render_line_delta
from huds.speed import (
    build_confirmed_max_speed_display,
    build_speed_table_state,
    extract_speed_table_values,
    render_speed,
    render_speed_table_dynamic,
    render_speed_table_static,
)
from huds.steering import render_steering
from huds.throttle_brake import render_throttle_brake
from huds.under_oversteer import render_under_oversteer

# Orchestrator flow used by render_split_screen_sync:
# 1) Config reading
# 2) Sync/Mapping
# 3) Layout
# 4) HUD Render
# 5) FFmpeg Run

_GEOM_DEBUG_LAST_SIG: tuple[Any, ...] | None = None


def _debug_dump_geometry_once(geom: OutputGeometry, log_file: Path | None = None) -> None:
    dbg = str(os.environ.get("IRVC_DEBUG_SWALLOWED") or "").strip().lower() in ("1", "true", "yes", "on")
    if not dbg:
        return
    global _GEOM_DEBUG_LAST_SIG
    sig = geometry_signature(geom)
    if _GEOM_DEBUG_LAST_SIG == sig:
        return
    _GEOM_DEBUG_LAST_SIG = sig
    _log_print(format_output_geometry_dump(geom), log_file)



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
    fast_lapdist_frames: list[float] | None = None
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
    max_brake_delay_distance: float = 0.003
    max_brake_delay_pressure: float = 35.0


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


@dataclass
class HudRendererState:
    hud_key: str
    x0: int
    y0: int
    w: int
    h: int
    geometry_signature: tuple[Any, ...]
    first_frame: bool = True
    fonts: dict[str, Any] = field(default_factory=dict)
    layout: dict[str, Any] = field(default_factory=dict)
    static_primitives: dict[str, Any] = field(default_factory=dict)
    helpers: dict[str, Any] = field(default_factory=dict)


def _load_hud_font(sz: int) -> Any:
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


def build_output_geometry(
    preset: str,
    hud_width_px: int,
    layout_config: LayoutConfig | None = None,
) -> OutputGeometry:
    W, H = parse_output_preset(preset)
    return build_output_geometry_for_size(
        out_w=W,
        out_h=H,
        hud_width_px=hud_width_px,
        layout_config=layout_config,
    )


def _geom_hud_x0(geom: OutputGeometry) -> int:
    try:
        hud_rects = tuple(getattr(geom, "hud_rects", ()) or ())
        if len(hud_rects) >= 1:
            return int(getattr(hud_rects[0], "x"))
    except Exception:
        pass
    return int(getattr(geom, "left_w", 0))


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

def _sample_csv_col_to_frames_float(run: Any, duration_s: float, fps: float, col: str) -> list[float]:
    from csv_g61 import has_col, sample_float_cols_to_frames
    if run is None:
        return []
    if not has_col(run, col):
        return []
    t = _force_strictly_increasing(_csv_time_axis_or_fallback(run, duration_s))
    if not t:
        return []
    sampled = sample_float_cols_to_frames(
        run,
        time_axis_s=t,
        duration_s=duration_s,
        fps=fps,
        cols=[col],
    )
    arr = sampled.get(str(col))
    if arr is None:
        return []
    try:
        n = int(arr.size)
    except Exception:
        n = len(arr) if isinstance(arr, list) else 0
    if n <= 0:
        return []
    try:
        return [float(v) for v in arr.tolist()]
    except Exception:
        return [float(v) for v in arr]

def _sample_csv_col_to_frames_int_nearest(run: Any, duration_s: float, fps: float, col: str) -> list[int]:
    ys = _sample_csv_col_to_frames_float(run, duration_s, fps, col)
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
    run_s: Any | None = None,
    run_f: Any | None = None,
) -> list[float]:
    from csv_g61 import has_col, load_g61_csv, sample_float_cols_to_frames

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

        run_s = run_s if run_s is not None else load_g61_csv(slow_csv)
        run_f = run_f if run_f is not None else load_g61_csv(fast_csv)
        for req in ("Lat", "Lon", "LapDistPct"):
            if not has_col(run_s, req):
                return []
        for req in ("Lat", "Lon"):
            if not has_col(run_f, req):
                return []

        t_s_raw = _force_strictly_increasing(_csv_time_axis_or_fallback(run_s, slow_duration_s))
        t_f_raw = _force_strictly_increasing(_csv_time_axis_or_fallback(run_f, fast_duration_s))
        sampled_s = sample_float_cols_to_frames(
            run_s,
            time_axis_s=t_s_raw,
            duration_s=slow_duration_s,
            fps=fps_safe,
            cols=["Lat", "Lon", "LapDistPct"],
            target_times_s=t_s_raw,
        )
        sampled_f = sample_float_cols_to_frames(
            run_f,
            time_axis_s=t_f_raw,
            duration_s=fast_duration_s,
            fps=fps_safe,
            cols=["Lat", "Lon"],
            target_times_s=t_f_raw,
        )
        lat_s_raw = [float(v) for v in sampled_s.get("Lat", []).tolist()] if "Lat" in sampled_s else []
        lon_s_raw = [float(v) for v in sampled_s.get("Lon", []).tolist()] if "Lon" in sampled_s else []
        lat_f_raw = [float(v) for v in sampled_f.get("Lat", []).tolist()] if "Lat" in sampled_f else []
        lon_f_raw = [float(v) for v in sampled_f.get("Lon", []).tolist()] if "Lon" in sampled_f else []
        ld_s_raw = [float(v) for v in sampled_s.get("LapDistPct", []).tolist()] if "LapDistPct" in sampled_s else []

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
    run_s: Any | None = None,
    run_f: Any | None = None,
) -> tuple[list[float], list[float], float]:
    from csv_g61 import has_col, load_g61_csv, sample_float_cols_to_frames

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

        run_s = run_s if run_s is not None else load_g61_csv(slow_csv)
        run_f = run_f if run_f is not None else load_g61_csv(fast_csv)

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
        sampled_s = sample_float_cols_to_frames(
            run_s,
            time_axis_s=t_s_raw,
            duration_s=slow_duration_s,
            fps=fps_safe,
            cols=["Lat", "Lon", "Yaw"],
            target_times_s=t_s_raw,
        )
        sampled_f = sample_float_cols_to_frames(
            run_f,
            time_axis_s=t_f_raw,
            duration_s=fast_duration_s,
            fps=fps_safe,
            cols=["Lat", "Lon", "Yaw"],
            target_times_s=t_f_raw,
        )
        lat_s_raw = [float(v) for v in sampled_s.get("Lat", []).tolist()] if "Lat" in sampled_s else []
        lon_s_raw = [float(v) for v in sampled_s.get("Lon", []).tolist()] if "Lon" in sampled_s else []
        lat_f_raw = [float(v) for v in sampled_f.get("Lat", []).tolist()] if "Lat" in sampled_f else []
        lon_f_raw = [float(v) for v in sampled_f.get("Lon", []).tolist()] if "Lon" in sampled_f else []
        yaw_s_raw = [float(v) for v in sampled_s.get("Yaw", []).tolist()] if "Yaw" in sampled_s else []
        yaw_f_raw = [float(v) for v in sampled_f.get("Yaw", []).tolist()] if "Yaw" in sampled_f else []

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

        slow_max_abs_before_clamp = 0.0
        fast_max_abs_before_clamp = 0.0
        abs_vals: list[float] = []
        for v in slow_err:
            av = abs(float(v))
            if math.isfinite(av):
                abs_vals.append(float(av))
                if av > slow_max_abs_before_clamp:
                    slow_max_abs_before_clamp = float(av)
        for v in fast_err:
            av = abs(float(v))
            if math.isfinite(av):
                abs_vals.append(float(av))
                if av > fast_max_abs_before_clamp:
                    fast_max_abs_before_clamp = float(av)
        abs_vals.sort()
        y_abs_base = _percentile_sorted(abs_vals, 99.0)
        if y_abs_base < 1e-6:
            y_abs_base = 0.05
        max_abs_for_scale = max(y_abs_base, slow_max_abs_before_clamp, fast_max_abs_before_clamp)

        slow_outside_base = 0
        fast_outside_base = 0
        if dbg_enabled:
            for v in slow_err:
                if abs(float(v)) > float(y_abs_base):
                    slow_outside_base += 1
            for v in fast_err:
                if abs(float(v)) > float(y_abs_base):
                    fast_outside_base += 1

        headroom_ratio = 0.15
        y_abs = float(max_abs_for_scale) * (1.0 + headroom_ratio)

        curve_center_pct = 0.0
        try:
            curve_center_pct = float(under_oversteer_curve_center)
        except Exception:
            curve_center_pct = 0.0
        curve_center_pct = float(_clamp(curve_center_pct, -50.0, 50.0))
        offset_units = (float(curve_center_pct) / 100.0) * (2.0 * float(y_abs_base))
        if abs(float(offset_units)) > 0.0:
            slow_err = [float(_clamp(float(v) + float(offset_units), -y_abs, y_abs)) for v in slow_err]
            fast_err = [float(_clamp(float(v) + float(offset_units), -y_abs, y_abs)) for v in fast_err]
        if hud_dbg:
            _log_print(
                f"[uo] curve_center_pct={curve_center_pct:+.6f} offset_units={offset_units:+.6f} y_abs_base={float(y_abs_base):+.6f}",
                log_file,
            )

        slow_clamped_count = 0
        fast_clamped_count = 0
        slow_err_clamped: list[float] = []
        fast_err_clamped: list[float] = []
        for v in slow_err:
            fv = float(v)
            if fv < -y_abs or fv > y_abs:
                slow_clamped_count += 1
            slow_err_clamped.append(float(_clamp(fv, -y_abs, y_abs)))
        for v in fast_err:
            fv = float(v)
            if fv < -y_abs or fv > y_abs:
                fast_clamped_count += 1
            fast_err_clamped.append(float(_clamp(fv, -y_abs, y_abs)))
        slow_err = slow_err_clamped
        fast_err = fast_err_clamped

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

        if dbg_enabled:
            _uo_log(
                f"[scale] y_abs_base_p99={y_abs_base:+.6f} max_abs_for_scale={max_abs_for_scale:+.6f} y_abs={y_abs:+.6f} headroom_ratio={headroom_ratio:.3f}"
            )
            _uo_log(
                f"[slow] max_abs_before_clamp={slow_max_abs_before_clamp:+.6f} max_abs_after_clamp={slow_max_abs_after_clamp:+.6f} "
                + f"clamped_points={slow_clamped_count}/{len(slow_err)} outliers_above_base={slow_outside_base}"
            )
            _uo_log(
                f"[fast] max_abs_before_clamp={fast_max_abs_before_clamp:+.6f} max_abs_after_clamp={fast_max_abs_after_clamp:+.6f} "
                + f"clamped_points={fast_clamped_count}/{len(fast_err)} outliers_above_base={fast_outside_base}"
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
    run_s: Any | None = None,
    run_f: Any | None = None,
) -> tuple[list[int], list[float], list[float], list[float] | None]:
    # Wie bisher: Slow->Fast Mapping + LapDist pro Slow-Frame
    # Neu: zusÃ¤tzlich Fast-Zeit pro Slow-Frame (fÃ¼r Stream-Sync / Segment-Warp)
    # Neu: optional Speed-Differenz pro Slow-Frame (fÃ¼r dynamische Segmentierung)
    from csv_g61 import get_float_col, has_col, load_g61_csv

    run_s = run_s if run_s is not None else load_g61_csv(slow_csv)
    run_f = run_f if run_f is not None else load_g61_csv(fast_csv)

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
    ctx: HudContext,
    *,
    frame_writer: Any,
    frame_written_cb: Any | None = None,
) -> None:
    """
    Rendert pro Frame die HUD-Spalte und streamt RGBA-Frames nach ffmpeg stdin.
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
    fast_lapdist_frames = ctx.signals.fast_lapdist_frames
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
    try:
        hud_max_brake_delay_distance = float(getattr(ctx.settings, "max_brake_delay_distance", 0.003))
    except Exception:
        hud_max_brake_delay_distance = 0.003
    if (not math.isfinite(float(hud_max_brake_delay_distance))) or float(hud_max_brake_delay_distance) < 0.0:
        hud_max_brake_delay_distance = 0.0
    if float(hud_max_brake_delay_distance) > 1.0:
        hud_max_brake_delay_distance = 1.0

    try:
        hud_max_brake_delay_pressure = float(getattr(ctx.settings, "max_brake_delay_pressure", 35.0))
    except Exception:
        hud_max_brake_delay_pressure = 35.0
    if not math.isfinite(float(hud_max_brake_delay_pressure)):
        hud_max_brake_delay_pressure = 35.0
    if float(hud_max_brake_delay_pressure) < 0.0:
        hud_max_brake_delay_pressure = 0.0
    if float(hud_max_brake_delay_pressure) > 100.0:
        hud_max_brake_delay_pressure = 100.0
    hud_max_brake_delay_pressure_scale = float(hud_max_brake_delay_pressure) / 100.0

    log_file = ctx.log_file
    table_cache_dbg = (os.environ.get("IRVC_DEBUG_TABLE_CACHE") or "0").strip().lower() in ("1", "true", "yes", "on")
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
    hud_x0 = _geom_hud_x0(geom)
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
    hud_x0 = _geom_hud_x0(geom)
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

    # Story 4.2: Scroll-HUD Fenster kommen nur aus globalen Defaults.
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

    effective_before_s = float(default_before_s)
    effective_after_s = float(default_after_s)
    if env_before_s is not None:
        effective_before_s = float(env_before_s)
    if env_after_s is not None:
        effective_after_s = float(env_after_s)

    def _resolve_hud_window_seconds(_hud_name_local: str) -> tuple[float, float]:
        return max(1e-6, float(effective_before_s)), max(1e-6, float(effective_after_s))

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


    if frame_writer is None:
        raise RuntimeError("HUD streaming requires an active ffmpeg stdin frame writer.")

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
            fast_lapdist_frames,
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
    debug_composite = (os.environ.get("IRVC_DEBUG_COMPOSITE") or "0").strip() in ("1", "true", "yes", "on")
    debug_composite_state = {"bbox_logged": False, "scratch_logged": False}
    if hud_dbg:
        try:
            names = ",".join([n for (n, _x0, _y0, _w, _h) in hud_items])
        except Exception:
            names = ""
        _log_print(
            f"[hudpy] render stream: fps={r:.3f} frames={frames} huds=[{names}] default_before_s={default_before_s:.3f} default_after_s={default_after_s:.3f} step={step:.6f} max_ticks={max_ticks}",
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
    renderer_state_by_hud: dict[str, HudRendererState] = {}
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

    for hud_key_s, x0_s, y0_s, w_s, h_s in active_table_items:
        hud_state_key_s = f"{str(hud_key_s)}|{int(x0_s)}|{int(y0_s)}|{int(w_s)}|{int(h_s)}"
        renderer_state_by_hud[hud_state_key_s] = HudRendererState(
            hud_key=str(hud_key_s),
            x0=int(x0_s),
            y0=int(y0_s),
            w=int(w_s),
            h=int(h_s),
            geometry_signature=(int(w_s), int(h_s), float(r), 0, 0),
            first_frame=True,
        )

    for hud_key_s, x0_s, y0_s, w_s, h_s in active_scroll_items:
        before_s_h_s, after_s_h_s = _resolve_hud_window_seconds(str(hud_key_s))
        before_f_s = max(1, int(round(float(before_s_h_s) * float(r))))
        after_f_s = max(1, int(round(float(after_s_h_s) * float(r))))
        if before_f_s != after_f_s:
            win_f_s = max(int(before_f_s), int(after_f_s))
            before_f_s = int(win_f_s)
            after_f_s = int(win_f_s)
        hud_state_key_s = f"{str(hud_key_s)}|{int(x0_s)}|{int(y0_s)}|{int(w_s)}|{int(h_s)}"
        renderer_state_by_hud[hud_state_key_s] = HudRendererState(
            hud_key=str(hud_key_s),
            x0=int(x0_s),
            y0=int(y0_s),
            w=int(w_s),
            h=int(h_s),
            geometry_signature=(int(w_s), int(h_s), float(r), int(before_f_s), int(after_f_s)),
            first_frame=True,
        )

    def _compose_hud_layers_local(
        w_local: int,
        h_local: int,
        static_layer_local: Any | None,
        dynamic_layer_local: Any | None,
        draw_values_fn_local: Any | None,
    ) -> Any:
        # PERF: alloc (per-HUD composite target; values are drawn directly here)
        composed_local = Image.new("RGBA", (int(w_local), int(h_local)), (0, 0, 0, 0))
        for layer_local in (static_layer_local, dynamic_layer_local):
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
            # PERF: composite (alpha chain over static/dynamic/value layers)
            try:
                composed_local.alpha_composite(layer_rgba)
            except Exception:
                composed_local = Image.alpha_composite(composed_local, layer_rgba)
        if draw_values_fn_local is not None:
            try:
                # PERF: composite (no separate value_layer allocation)
                value_dr_local = ImageDraw.Draw(composed_local)
                draw_values_fn_local(value_dr_local, 0, 0)
            except Exception:
                pass
        return composed_local

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
        # PERF: composite (crop -> alpha_composite -> paste chain)
        try:
            frame_img_local.alpha_composite(
                hud_rgba,
                dest=(int(cx0), int(cy0)),
                source=(int(sx0), int(sy0), int(sx1), int(sy1)),
            )
            if debug_composite and (not bool(debug_composite_state.get("bbox_logged", False))):
                _log_print(
                    f"[perf][composite] region dst=({int(cx0)},{int(cy0)},{int(cx1)},{int(cy1)}) src=({int(sx0)},{int(sy0)},{int(sx1)},{int(sy1)})",
                    log_file,
                )
                debug_composite_state["bbox_logged"] = True
        except Exception:
            dst_region = frame_img_local.crop((int(cx0), int(cy0), int(cx1), int(cy1)))
            src_region = hud_rgba.crop((int(sx0), int(sy0), int(sx1), int(sy1)))
            composed_region = Image.alpha_composite(dst_region, src_region)
            frame_img_local.paste(composed_region, (int(cx0), int(cy0)))
            if debug_composite and (not bool(debug_composite_state.get("bbox_logged", False))):
                _log_print(
                    f"[perf][composite] fallback region dst=({int(cx0)},{int(cy0)},{int(cx1)},{int(cy1)}) src=({int(sx0)},{int(sy0)},{int(sx1)},{int(sy1)})",
                    log_file,
                )
                debug_composite_state["bbox_logged"] = True

    fps_mapping_safe = float(r) if float(r) > 1e-6 else 30.0
    slow_frame_count_total = max(0, int(len(slow_frame_to_lapdist)))
    fast_frame_hi = int(fast_frame_count) - 1 if int(fast_frame_count) > 0 else None
    slow_to_fast_len = int(len(slow_to_fast_frame)) if slow_to_fast_frame else 0
    slow_to_fast_time_len = int(len(slow_frame_to_fast_time_s)) if slow_frame_to_fast_time_s else 0

    def _clamp_idx_int(v: int, lo: int, hi: int) -> int:
        if v < int(lo):
            return int(lo)
        if v > int(hi):
            return int(hi)
        return int(v)

    def _mapped_fast_idx_for_slow_idx(idx0: int) -> int:
        ii = int(idx0)
        fi = int(ii)
        if slow_to_fast_frame and 0 <= int(ii) < int(slow_to_fast_len):
            try:
                fi = int(slow_to_fast_frame[ii])
            except Exception:
                fi = int(ii)
        if fi < 0:
            fi = 0
        if fast_frame_hi is not None and fi > int(fast_frame_hi):
            fi = int(fast_frame_hi)
        return int(fi)

    def _mapped_t_slow_for_slow_idx(idx0: int) -> float:
        return float(int(idx0)) / float(fps_mapping_safe)

    def _mapped_t_fast_for_slow_idx(idx0: int) -> float:
        ii = int(idx0)
        fi = _mapped_fast_idx_for_slow_idx(int(ii))
        if slow_to_fast_time_len > 0 and 0 <= int(ii) < int(slow_to_fast_time_len):
            try:
                return float(slow_frame_to_fast_time_s[ii])
            except Exception:
                return float(fi) / float(fps_mapping_safe)
        return float(fi) / float(fps_mapping_safe)

    def _tb_fast_idx_for_slow_idx(idx0: int) -> int:
        ii = int(idx0)
        fi = _mapped_fast_idx_for_slow_idx(int(ii))
        if slow_to_fast_frame and 0 <= int(ii) < int(slow_to_fast_len):
            try:
                fi = int(slow_to_fast_frame[ii])
                if fi < 0:
                    fi = 0
            except Exception:
                pass
        return int(fi)

    verify_frame_map = (os.environ.get("IRVC_VERIFY_FRAME_MAP") or "0").strip().lower() in ("1", "true", "yes", "on")
    verify_js: set[int] = set()
    if verify_frame_map and int(frames) > 0:
        verify_js.add(0)
        if int(frames) > 1:
            verify_js.add(1)
        if int(frames) > 2:
            verify_js.add(2)
        verify_js.add(int(frames) // 2)
        verify_js.add(int(frames) - 1)

    for j in range(frames):

        
        i = int(cut_i0) + j
        if i < 0 or i >= len(slow_frame_to_lapdist):
            continue

        img = Image.new("RGBA", (int(geom.hud), int(geom.H)), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)

        ld = float(slow_frame_to_lapdist[i]) % 1.0
        ld_mod = ld % step
        if verify_frame_map and int(j) in verify_js:
            old_map_dbg = _build_frame_window_mapping(
                i=int(i),
                before_f=int(global_before_f),
                after_f=int(global_after_f),
                fps=float(r),
                slow_frame_count=len(slow_frame_to_lapdist),
                fast_frame_count=int(fast_frame_count),
                slow_to_fast_frame=slow_to_fast_frame,
                slow_frame_to_fast_time_s=slow_frame_to_fast_time_s,
            )
            idxs_dbg = list(getattr(old_map_dbg, "idxs", []) or [])
            ts_dbg = list(getattr(old_map_dbg, "t_slow", []) or [])
            fi_dbg = list(getattr(old_map_dbg, "fast_idx", []) or [])
            tf_dbg = list(getattr(old_map_dbg, "t_fast", []) or [])
            n_dbg = int(len(idxs_dbg))
            if not (int(len(ts_dbg)) == n_dbg and int(len(fi_dbg)) == n_dbg and int(len(tf_dbg)) == n_dbg):
                raise AssertionError(f"[verify-map] inconsistent old mapping lengths at j={int(j)} i={int(i)}")
            for k_dbg in range(n_dbg):
                idx_dbg = int(idxs_dbg[k_dbg])
                fi_old = int(fi_dbg[k_dbg])
                ts_old = float(ts_dbg[k_dbg])
                tf_old = float(tf_dbg[k_dbg])
                fi_new = int(_mapped_fast_idx_for_slow_idx(int(idx_dbg)))
                ts_new = float(_mapped_t_slow_for_slow_idx(int(idx_dbg)))
                tf_new = float(_mapped_t_fast_for_slow_idx(int(idx_dbg)))
                if (
                    int(fi_new) != int(fi_old)
                    or abs(float(ts_new) - float(ts_old)) > 1e-12
                    or abs(float(tf_new) - float(tf_old)) > 1e-12
                ):
                    raise AssertionError(
                        f"[verify-map] mismatch j={int(j)} i={int(i)} idx={int(idx_dbg)} "
                        f"fi_old={int(fi_old)} fi_new={int(fi_new)} "
                        f"ts_old={float(ts_old):+.12f} ts_new={float(ts_new):+.12f} "
                        f"tf_old={float(tf_old):+.12f} tf_new={float(tf_new):+.12f}"
                    )

        # Table-HUDs (Speed, Gear & RPM) als Text
        if table_items and slow_to_fast_frame and i < len(slow_to_fast_frame):
            fi = int(slow_to_fast_frame[i])
            if fi < 0:
                fi = 0

            for hud_key, x0, y0, w, h in active_table_items:
                try:
                    hud_state_key = f"{str(hud_key)}|{int(x0)}|{int(y0)}|{int(w)}|{int(h)}"
                    renderer_state = renderer_state_by_hud.get(hud_state_key)
                    if renderer_state is None:
                        renderer_state = HudRendererState(
                            hud_key=str(hud_key),
                            x0=int(x0),
                            y0=int(y0),
                            w=int(w),
                            h=int(h),
                            geometry_signature=(int(w), int(h), float(r), 0, 0),
                            first_frame=True,
                        )
                        renderer_state_by_hud[hud_state_key] = renderer_state

                    table_cache = renderer_state.helpers.get("table_cache")
                    if not isinstance(table_cache, dict):
                        table_cache = {}
                        renderer_state.helpers["table_cache"] = table_cache

                    table_cache_key = (
                        str(hud_key),
                        int(w),
                        int(h),
                        tuple(COL_HUD_BG),
                        tuple(COL_SLOW_DARKRED),
                        tuple(COL_FAST_DARKBLUE),
                        str(hud_speed_units),
                    )
                    if tuple(table_cache.get("cache_key") or ()) != tuple(table_cache_key):
                        table_cache.clear()
                        table_cache["cache_key"] = tuple(table_cache_key)
                        table_cache["invalidated"] = True
                    else:
                        table_cache["invalidated"] = False

                    if str(hud_key) == "Speed":
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
                        speed_vals = extract_speed_table_values(speed_ctx)
                        if speed_vals is None:
                            dr.rectangle(
                                [int(x0), int(y0), int(x0 + w - 1), int(y0 + h - 1)],
                                fill=COL_HUD_BG,
                            )
                            continue

                        speed_state = table_cache.get("table_state")
                        speed_static = table_cache.get("static_image")
                        if bool(table_cache.get("invalidated")) or speed_state is None or speed_static is None:
                            try:
                                speed_state = build_speed_table_state((int(w), int(h)), probe_values=speed_vals)
                            except Exception:
                                speed_state = None
                            speed_static = None
                            if speed_state is not None:
                                speed_static = render_speed_table_static(speed_state, COL_SLOW_DARKRED, COL_FAST_DARKBLUE)
                                table_cache["table_state"] = speed_state
                                table_cache["static_image"] = speed_static
                                if table_cache_dbg:
                                    _log_print(f"[hudpy][table-cache] static rebuilt hud=Speed key={hud_state_key}", log_file)

                        if speed_state is None or speed_static is None:
                            dr.rectangle(
                                [int(x0), int(y0), int(x0 + w - 1), int(y0 + h - 1)],
                                fill=COL_HUD_BG,
                            )
                            render_speed(speed_ctx, (x0, y0, w, h), dr)
                            continue

                        speed_dynamic = render_speed_table_dynamic(
                            speed_state,
                            speed_vals[0],
                            speed_vals[1],
                            COL_SLOW_DARKRED,
                            COL_FAST_DARKBLUE,
                        )
                        # PERF: composite (avoid per-frame copy/merge temp; preserve static->dynamic order)
                        _composite_hud_into_frame_local(img, speed_static, int(x0), int(y0))
                        _composite_hud_into_frame_local(img, speed_dynamic, int(x0), int(y0))
                    elif str(hud_key) == "Gear & RPM":
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
                        gear_vals = extract_gear_rpm_table_values(gear_rpm_ctx)
                        if gear_vals is None:
                            dr.rectangle(
                                [int(x0), int(y0), int(x0 + w - 1), int(y0 + h - 1)],
                                fill=COL_HUD_BG,
                            )
                            continue

                        gear_state = table_cache.get("table_state")
                        gear_static = table_cache.get("static_image")
                        if bool(table_cache.get("invalidated")) or gear_state is None or gear_static is None:
                            try:
                                gear_state = build_gear_rpm_table_state((int(w), int(h)), probe_values=gear_vals)
                            except Exception:
                                gear_state = None
                            gear_static = None
                            if gear_state is not None:
                                gear_static = render_gear_rpm_table_static(gear_state, COL_SLOW_DARKRED, COL_FAST_DARKBLUE)
                                table_cache["table_state"] = gear_state
                                table_cache["static_image"] = gear_static
                                if table_cache_dbg:
                                    _log_print(f"[hudpy][table-cache] static rebuilt hud=Gear & RPM key={hud_state_key}", log_file)

                        if gear_state is None or gear_static is None:
                            dr.rectangle(
                                [int(x0), int(y0), int(x0 + w - 1), int(y0 + h - 1)],
                                fill=COL_HUD_BG,
                            )
                            render_gear_rpm(gear_rpm_ctx, (x0, y0, w, h), dr)
                            continue

                        gear_dynamic = render_gear_rpm_table_dynamic(
                            gear_state,
                            gear_vals[0],
                            gear_vals[1],
                            COL_SLOW_DARKRED,
                            COL_FAST_DARKBLUE,
                        )
                        # PERF: composite (avoid per-frame copy/merge temp; preserve static->dynamic order)
                        _composite_hud_into_frame_local(img, gear_static, int(x0), int(y0))
                        _composite_hud_into_frame_local(img, gear_dynamic, int(x0), int(y0))
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
                hud_state_key = f"{str(hud_key)}|{int(x0)}|{int(y0)}|{int(w)}|{int(h)}"
                state = scroll_state_by_hud.get(hud_state_key)
                if state is None:
                    state = {}
                    scroll_state_by_hud[hud_state_key] = state
                renderer_state = renderer_state_by_hud.get(hud_state_key)
                if renderer_state is None:
                    renderer_state = HudRendererState(
                        hud_key=str(hud_key),
                        x0=int(x0),
                        y0=int(y0),
                        w=int(w),
                        h=int(h),
                        geometry_signature=(int(w), int(h), float(r), int(before_f), int(after_f)),
                        first_frame=True,
                    )
                    renderer_state_by_hud[hud_state_key] = renderer_state

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
                use_cached_tb = False
                if is_throttle_brake:
                    tb_cached = renderer_state.helpers.get("tb_fns")
                    if isinstance(tb_cached, dict):
                        tb_layout = dict(tb_cached.get("layout") or {})
                        _tb_sample_column = tb_cached.get("sample_column")
                        _tb_apply_abs_debounce = tb_cached.get("apply_abs_debounce")
                        _tb_render_static_layer = tb_cached.get("render_static_layer")
                        _tb_render_dynamic_full = tb_cached.get("render_dynamic_full")
                        _tb_draw_values_overlay = tb_cached.get("draw_values_overlay")
                        if (
                            callable(_tb_sample_column)
                            and callable(_tb_apply_abs_debounce)
                            and callable(_tb_render_static_layer)
                            and callable(_tb_render_dynamic_full)
                            and callable(_tb_draw_values_overlay)
                            and tb_layout
                        ):
                            use_cached_tb = True
                if is_throttle_brake and use_cached_tb:
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

                    tb_slow_abs_prefix = [0]
                    if slow_abs_frames:
                        acc_s_pref = 0
                        for vv_pref in slow_abs_frames:
                            acc_s_pref += 1 if float(vv_pref) >= 0.5 else 0
                            tb_slow_abs_prefix.append(acc_s_pref)
                    tb_fast_abs_prefix = [0]
                    if fast_abs_frames:
                        acc_f_pref = 0
                        for vv_pref in fast_abs_frames:
                            acc_f_pref += 1 if float(vv_pref) >= 0.5 else 0
                            tb_fast_abs_prefix.append(acc_f_pref)

                if is_throttle_brake and (not use_cached_tb):
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

                    tb_layout_sig = (
                        int(w),
                        int(h),
                        int(window_frames),
                        float(tb_fps_safe),
                        int(hud_pedals_abs_debounce_ms),
                    )
                    if renderer_state.helpers.get("tb_layout_sig") != tb_layout_sig or ("tb" not in renderer_state.layout):
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
                        tb_font_title = _load_hud_font(tb_font_sz)
                        tb_font_val = _load_hud_font(tb_font_val_sz)
                        tb_font_axis = _load_hud_font(max(8, int(tb_font_sz - 2)))
                        tb_font_axis_small = _load_hud_font(max(7, int(tb_font_sz - 3)))

                        tb_y_txt = int(2)
                        tb_title_h = int(max(8, min(24, round(float(h) * 0.13))))

                        tb_table_top = int(tb_y_txt + tb_title_h + 2)
                        tb_table_h = int(max(22, min(44, round(float(h) * 0.24))))
                        tb_table_header_h = int(max(10, min(tb_table_h - 8, round(float(tb_table_h) * 0.45))))
                        tb_table_value_h = int(max(8, tb_table_h - tb_table_header_h))
                        tb_table_bottom = int(tb_table_top + tb_table_h - 1)

                        tb_table_outer_pad_x = int(max(2, min(10, round(float(w) * 0.015))))
                        tb_table_gap_x = int(max(4, min(16, round(float(w) * 0.03))))
                        tb_table_side_w = int((int(w) - (2 * tb_table_outer_pad_x) - tb_table_gap_x) // 2)
                        if tb_table_side_w < 16:
                            tb_table_side_w = max(16, int((int(w) - 2) // 2))
                            tb_table_outer_pad_x = 1
                            tb_table_gap_x = max(0, int(w) - (2 * tb_table_side_w) - (2 * tb_table_outer_pad_x))
                        tb_table_left_x = int(tb_table_outer_pad_x)
                        tb_table_right_x = int(tb_table_left_x + tb_table_side_w + tb_table_gap_x)
                        tb_table_cols = ("Throttle", "Brake", "ABS", "Max. Brake")
                        tb_table_cols_per_side = int(len(tb_table_cols))
                        tb_table_col_w = float(tb_table_side_w) / max(1.0, float(tb_table_cols_per_side))
                        tb_table_cell_pad_x = int(max(1, min(6, round(tb_table_col_w * 0.08))))
                        tb_table_header_fit_w = int(max(6, round(tb_table_col_w) - (2 * tb_table_cell_pad_x)))
                        tb_table_header_fit_h = int(max(6, tb_table_header_h - 2))
                        tb_table_value_fit_h = int(max(6, tb_table_value_h - 2))

                        probe_img_tb = Image.new("RGBA", (max(1, int(w)), max(1, int(h))), (0, 0, 0, 0))
                        probe_dr_tb = ImageDraw.Draw(probe_img_tb)

                        def _tb_probe_wh(text_probe: str, font_probe: Any) -> tuple[int, int]:
                            try:
                                bb = probe_dr_tb.textbbox((0, 0), str(text_probe), font=font_probe)
                                return int(bb[2] - bb[0]), int(bb[3] - bb[1])
                            except Exception:
                                return int(max(1, len(str(text_probe))) * 7), 12

                        def _tb_fit_font(max_sz: int, min_sz: int, labels: tuple[str, ...], fit_w: int, fit_h: int) -> Any:
                            f_best = _load_hud_font(int(min_sz))
                            for sz in range(int(max_sz), int(min_sz) - 1, -1):
                                f_try = _load_hud_font(int(sz))
                                if f_try is None:
                                    continue
                                ok = True
                                for lbl in labels:
                                    tw, th = _tb_probe_wh(str(lbl), f_try)
                                    if tw > int(fit_w) or th > int(fit_h):
                                        ok = False
                                        break
                                if ok:
                                    f_best = f_try
                                    break
                            return f_best

                        tb_font_tbl_head = _tb_fit_font(
                            max_sz=int(max(6, min(22, tb_table_header_fit_h))),
                            min_sz=6,
                            labels=tuple(tb_table_cols),
                            fit_w=int(tb_table_header_fit_w),
                            fit_h=int(tb_table_header_fit_h),
                        )
                        tb_font_tbl_val = _tb_fit_font(
                            max_sz=int(max(8, min(26, tb_table_value_fit_h))),
                            min_sz=8,
                            labels=("100%",),
                            fit_w=int(tb_table_header_fit_w),
                            fit_h=int(tb_table_value_fit_h),
                        )

                        tb_abs_h = int(max(10, min(15, round(float(h) * 0.085))))
                        tb_abs_gap_y = 2
                        tb_y_abs0 = int(tb_table_bottom + 3)
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
                            "font_table_header": tb_font_tbl_head,
                            "font_table_value": tb_font_tbl_val,
                            "y_txt": int(tb_y_txt),
                            "table_top": int(tb_table_top),
                            "table_bottom": int(tb_table_bottom),
                            "table_header_h": int(tb_table_header_h),
                            "table_value_h": int(tb_table_value_h),
                            "table_left_x": int(tb_table_left_x),
                            "table_right_x": int(tb_table_right_x),
                            "table_side_w": int(tb_table_side_w),
                            "table_col_w": float(tb_table_col_w),
                            "table_cols": tuple(tb_table_cols),
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
                        renderer_state.layout["tb"] = tb_layout
                        renderer_state.fonts["tb"] = {
                            "title": tb_font_title,
                            "value": tb_font_val,
                            "axis": tb_font_axis,
                            "axis_small": tb_font_axis_small,
                            "table_header": tb_font_tbl_head,
                            "table_value": tb_font_tbl_val,
                        }
                        renderer_state.helpers["tb_layout_sig"] = tb_layout_sig
                    else:
                        tb_layout = dict(renderer_state.layout.get("tb") or {})

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

                        t_slow = float(_mapped_t_slow_for_slow_idx(int(idx_slow)))
                        t_fast = float(_mapped_t_fast_for_slow_idx(int(idx_slow)))
                        fi_map = int(_tb_fast_idx_for_slow_idx(int(idx_slow)))

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

                        s_ld = 0.0
                        try:
                            if slow_frame_to_lapdist and 0 <= int(idx_slow) < len(slow_frame_to_lapdist):
                                s_ld = float(slow_frame_to_lapdist[int(idx_slow)])
                        except Exception:
                            s_ld = 0.0
                        s_ld = float(s_ld) % 1.0

                        f_ld = float(s_ld)
                        try:
                            if fast_lapdist_frames:
                                if hud_pedals_sample_mode == "time":
                                    f_ld = float(_tb_sample_linear_time(fast_lapdist_frames, float(t_fast)))
                                else:
                                    f_ld = float(_tb_sample_legacy(fast_lapdist_frames, int(fi_map), 1.0))
                        except Exception:
                            f_ld = float(s_ld)
                        f_ld = float(f_ld) % 1.0

                        y_s_t = int(tb_layout["y_from_01"](float(s_t)))
                        y_s_b = int(tb_layout["y_from_01"](float(s_b)))
                        y_f_t = int(tb_layout["y_from_01"](float(f_t)))
                        y_f_b = int(tb_layout["y_from_01"](float(f_b)))
                        return {
                            "x": int(xi),
                            "slow_idx": int(idx_slow),
                            "fast_idx": int(fi_map),
                            "s_ld": float(s_ld),
                            "f_ld": float(f_ld),
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

                    def _tb_max_brake_new_state() -> dict[str, Any]:
                        return {
                            "armed": False,
                            "in_phase": False,
                            "phase_peak": 0.0,
                            "last_max_brake_percent": 0.0,
                            "last_zero_lapdist": None,
                            "was_zero": False,
                        }

                    def _tb_max_brake_forward_delta(ld_from: float, ld_to: float) -> float:
                        return float((float(ld_to) - float(ld_from)) % 1.0)

                    def _tb_update_max_brake_state(
                        st_local: dict[str, Any],
                        brake_now: float,
                        lapdist_now: float,
                        side_name: str,
                    ) -> None:
                        b_now = float(_clamp(float(brake_now), 0.0, 1.0))
                        ld_now = float(lapdist_now) % 1.0
                        if not math.isfinite(float(b_now)):
                            b_now = 0.0
                        if not math.isfinite(float(ld_now)):
                            ld_now = 0.0
                        in_phase = bool(st_local.get("in_phase", False))
                        was_zero = bool(st_local.get("was_zero", False))
                        is_zero_now = float(b_now) == 0.0

                        # Strict rule: phase end/rearm is only on exact zero.
                        if is_zero_now:
                            if in_phase:
                                peak = float(_clamp(float(st_local.get("phase_peak", 0.0)), 0.0, 1.0))
                                peak_pct = float(_clamp(float(round(float(peak) * 100.0)), 0.0, 100.0))
                                st_local["last_max_brake_percent"] = float(peak_pct)
                                st_local["in_phase"] = False
                                st_local["phase_peak"] = 0.0
                                if hud_dbg:
                                    _log_print(
                                        f"[tb-max-brake] side={side_name} event=commit lapdist={ld_now:.6f} peak_pct={peak_pct:.0f}",
                                        log_file,
                                    )
                                st_local["armed"] = True
                                st_local["last_zero_lapdist"] = float(ld_now)
                            elif not was_zero:
                                st_local["armed"] = True
                                st_local["last_zero_lapdist"] = float(ld_now)
                            st_local["was_zero"] = True
                            return

                        st_local["was_zero"] = False

                        if in_phase:
                            st_local["phase_peak"] = float(max(float(st_local.get("phase_peak", 0.0)), float(b_now)))
                            return

                        if not bool(st_local.get("armed", False)):
                            return

                        allow_start = True
                        zero_ld = st_local.get("last_zero_lapdist")
                        if (
                            float(hud_max_brake_delay_distance) > 0.0
                            and zero_ld is not None
                        ):
                            dist_since_zero = _tb_max_brake_forward_delta(float(zero_ld), float(ld_now))
                            if float(dist_since_zero) < float(hud_max_brake_delay_distance):
                                allow_start = False
                                if float(b_now) >= float(hud_max_brake_delay_pressure_scale):
                                    allow_start = True
                                    if hud_dbg:
                                        _log_print(
                                            f"[tb-max-brake] side={side_name} event=override lapdist={ld_now:.6f} brake_pct={float(b_now) * 100.0:.1f} threshold_pct={float(hud_max_brake_delay_pressure):.1f}",
                                            log_file,
                                        )

                        if allow_start:
                            st_local["in_phase"] = True
                            st_local["phase_peak"] = float(b_now)
                            if hud_dbg:
                                _log_print(
                                    f"[tb-max-brake] side={side_name} event=start lapdist={ld_now:.6f}",
                                    log_file,
                                )

                    def _tb_pct_text(v_01: float) -> str:
                        pct = int(round(float(_clamp(float(v_01), 0.0, 1.0) * 100.0)))
                        return f"{pct:03d}%"

                    def _tb_cell_center_text(
                        dr_any: Any,
                        x0_cell: int,
                        x1_cell: int,
                        y0_cell: int,
                        y1_cell: int,
                        text_val: str,
                        col_val: Any,
                        font_val_any: Any,
                    ) -> None:
                        txt = str(text_val)
                        try:
                            bb = dr_any.textbbox((0, 0), txt, font=font_val_any)
                            tw = int(bb[2] - bb[0])
                            th = int(bb[3] - bb[1])
                            bx0 = float(bb[0])
                            by0 = float(bb[1])
                        except Exception:
                            tw = int(max(1, len(txt)) * 7)
                            th = 12
                            bx0 = 0.0
                            by0 = 0.0
                        tx = ((float(x0_cell) + float(x1_cell)) - float(tw)) / 2.0 - bx0
                        ty = ((float(y0_cell) + float(y1_cell)) - float(th)) / 2.0 - by0
                        try:
                            dr_any.text((int(round(tx)), int(round(ty))), txt, fill=col_val, font=font_val_any)
                        except Exception:
                            pass

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
                        stripe_color = None
                        if len(y_bounds) > 1:
                            stripe_alpha = max(int(COL_HUD_BG[3]), min(210, int(COL_HUD_BG[3]) + 70))
                            stripe_lift = 10
                            stripe_rgb = (
                                min(255, int(COL_HUD_BG[0]) + stripe_lift),
                                min(255, int(COL_HUD_BG[1]) + stripe_lift),
                                min(255, int(COL_HUD_BG[2]) + stripe_lift),
                            )
                            stripe_color = (*stripe_rgb, int(stripe_alpha))
                            for stripe_i in range(len(y_bounds) - 1):
                                y_a = int(y_bounds[stripe_i])
                                y_b = int(y_bounds[stripe_i + 1]) - 1
                                if y_b < y_a:
                                    continue
                                if (stripe_i % 2) == 1:
                                    static_dr_local.rectangle([x0s, y_a, x1s, y_b], fill=stripe_color)
                        if stripe_color is not None:
                            try:
                                for y_sep in y_bounds[1:-1]:
                                    static_dr_local.line([(x0s, int(y_sep)), (x1s, int(y_sep))], fill=stripe_color, width=1)
                            except Exception:
                                pass

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

                        table_top = int(tb_layout.get("table_top", 0))
                        table_bottom = int(tb_layout.get("table_bottom", table_top))
                        table_header_h = int(tb_layout.get("table_header_h", 1))
                        table_side_w = int(tb_layout.get("table_side_w", max(1, int(w // 2))))
                        table_col_w = float(tb_layout.get("table_col_w", max(1.0, float(table_side_w) / 4.0)))
                        table_cols = tuple(tb_layout.get("table_cols") or ("Throttle", "Brake", "ABS", "Max. Brake"))
                        table_hdr_font = tb_layout.get("font_table_header")
                        row_sep = int(table_top + table_header_h - 1)
                        if row_sep > table_bottom:
                            row_sep = int(table_bottom)
                        header_y0 = int(table_top)
                        header_y1 = int(max(header_y0, row_sep - 1))
                        value_y0 = int(min(table_bottom, row_sep + 1))
                        value_y1 = int(table_bottom)
                        table_grid_col = (
                            int(min(255, int(COL_HUD_BG[0]) + 20)),
                            int(min(255, int(COL_HUD_BG[1]) + 20)),
                            int(min(255, int(COL_HUD_BG[2]) + 20)),
                            int(min(255, max(int(COL_HUD_BG[3]), 150))),
                        )

                        def _tb_table_edges(side_x_local: int) -> list[int]:
                            edges_local = [int(side_x_local + int(round(float(ci) * float(table_col_w)))) for ci in range(int(len(table_cols)) + 1)]
                            edges_local[0] = int(side_x_local)
                            edges_local[-1] = int(side_x_local + table_side_w)
                            for ei in range(1, len(edges_local)):
                                if edges_local[ei] <= edges_local[ei - 1]:
                                    edges_local[ei] = edges_local[ei - 1] + 1
                            return edges_local

                        def _tb_draw_table_static(side_x_local: int, col_side: Any) -> None:
                            edges_local = _tb_table_edges(int(side_x_local))
                            left_local = int(edges_local[0])
                            right_local = int(edges_local[-1] - 1)
                            if right_local <= left_local:
                                return
                            try:
                                static_dr_local.rectangle([left_local, table_top, right_local, table_bottom], outline=table_grid_col, width=1)
                            except Exception:
                                pass
                            try:
                                static_dr_local.line([(left_local, row_sep), (right_local, row_sep)], fill=table_grid_col, width=1)
                            except Exception:
                                pass
                            for x_sep in edges_local[1:-1]:
                                try:
                                    static_dr_local.line([(int(x_sep), table_top), (int(x_sep), table_bottom)], fill=table_grid_col, width=1)
                                except Exception:
                                    pass
                            for c_idx, lbl in enumerate(table_cols):
                                c0 = int(edges_local[c_idx])
                                c1 = int(edges_local[c_idx + 1] - 1)
                                _tb_cell_center_text(
                                    static_dr_local,
                                    int(c0),
                                    int(c1),
                                    int(header_y0),
                                    int(header_y1),
                                    str(lbl),
                                    col_side,
                                    table_hdr_font,
                                )

                        _tb_draw_table_static(int(tb_layout.get("table_left_x", 0)), COL_SLOW_DARKRED)
                        _tb_draw_table_static(int(tb_layout.get("table_right_x", 0)), COL_FAST_DARKBLUE)

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
                        tb_max_states = renderer_state.helpers.get("tb_max_brake_states")
                        if not isinstance(tb_max_states, dict):
                            tb_max_states = {
                                "slow": _tb_max_brake_new_state(),
                                "fast": _tb_max_brake_new_state(),
                            }
                            renderer_state.helpers["tb_max_brake_states"] = tb_max_states
                        if not isinstance(tb_max_states.get("slow"), dict):
                            tb_max_states["slow"] = _tb_max_brake_new_state()
                        if not isinstance(tb_max_states.get("fast"), dict):
                            tb_max_states["fast"] = _tb_max_brake_new_state()

                        idx_cur = int(cur_col.get("slow_idx", -1))
                        last_idx = renderer_state.helpers.get("tb_max_brake_last_idx")
                        if last_idx is None or int(last_idx) != int(idx_cur):
                            _tb_update_max_brake_state(
                                tb_max_states["slow"],
                                float(cur_col.get("s_b", 0.0)),
                                float(cur_col.get("s_ld", 0.0)),
                                "slow",
                            )
                            _tb_update_max_brake_state(
                                tb_max_states["fast"],
                                float(cur_col.get("f_b", 0.0)),
                                float(cur_col.get("f_ld", 0.0)),
                                "fast",
                            )
                            renderer_state.helpers["tb_max_brake_last_idx"] = int(idx_cur)

                        s_max_pct = int(round(float(_clamp(float(tb_max_states["slow"].get("last_max_brake_percent", 0.0)), 0.0, 100.0))))
                        f_max_pct = int(round(float(_clamp(float(tb_max_states["fast"].get("last_max_brake_percent", 0.0)), 0.0, 100.0))))
                        s_abs_pct = 100 if bool(cur_col.get("abs_s_raw_on", False)) else 0
                        f_abs_pct = 100 if bool(cur_col.get("abs_f_raw_on", False)) else 0

                        slow_vals = (
                            _tb_pct_text(float(cur_col.get("s_t", 0.0))),
                            _tb_pct_text(float(cur_col.get("s_b", 0.0))),
                            f"{int(s_abs_pct):03d}%",
                            f"{int(s_max_pct):03d}%",
                        )
                        fast_vals = (
                            _tb_pct_text(float(cur_col.get("f_t", 0.0))),
                            _tb_pct_text(float(cur_col.get("f_b", 0.0))),
                            f"{int(f_abs_pct):03d}%",
                            f"{int(f_max_pct):03d}%",
                        )

                        table_top = int(tb_layout.get("table_top", 0))
                        table_bottom = int(tb_layout.get("table_bottom", table_top))
                        table_header_h = int(tb_layout.get("table_header_h", 1))
                        table_side_w = int(tb_layout.get("table_side_w", max(1, int(w // 2))))
                        table_col_w = float(tb_layout.get("table_col_w", max(1.0, float(table_side_w) / 4.0)))
                        table_cols = tuple(tb_layout.get("table_cols") or ("Throttle", "Brake", "ABS", "Max. Brake"))
                        row_sep = int(table_top + table_header_h - 1)
                        if row_sep > table_bottom:
                            row_sep = int(table_bottom)
                        value_y0 = int(min(table_bottom, row_sep + 1))
                        value_y1 = int(table_bottom)
                        font_tbl_val = tb_layout.get("font_table_value") or tb_layout.get("font_val")

                        def _tb_table_edges(side_x_local: int) -> list[int]:
                            edges_local = [int(side_x_local + int(round(float(ci) * float(table_col_w)))) for ci in range(int(len(table_cols)) + 1)]
                            edges_local[0] = int(side_x_local)
                            edges_local[-1] = int(side_x_local + table_side_w)
                            for ei in range(1, len(edges_local)):
                                if edges_local[ei] <= edges_local[ei - 1]:
                                    edges_local[ei] = edges_local[ei - 1] + 1
                            return edges_local

                        def _tb_draw_values_row(side_x_local: int, vals_local: tuple[str, str, str, str], col_side: Any) -> None:
                            edges_local = _tb_table_edges(int(side_x_local))
                            for c_idx, txt_val in enumerate(vals_local):
                                if c_idx + 1 >= len(edges_local):
                                    break
                                c0 = int(base_x) + int(edges_local[c_idx])
                                c1 = int(base_x) + int(edges_local[c_idx + 1] - 1)
                                _tb_cell_center_text(
                                    main_dr_local,
                                    int(c0),
                                    int(c1),
                                    int(base_y + value_y0),
                                    int(base_y + value_y1),
                                    str(txt_val),
                                    col_side,
                                    font_tbl_val,
                                )

                        # Side mapping must follow curve colors: left/red=slow, right/blue=fast.
                        _tb_draw_values_row(int(tb_layout.get("table_left_x", 0)), slow_vals, COL_SLOW_DARKRED)
                        _tb_draw_values_row(int(tb_layout.get("table_right_x", 0)), fast_vals, COL_FAST_DARKBLUE)
                    renderer_state.helpers["tb_fns"] = {
                        "layout": tb_layout,
                        "sample_column": _tb_sample_column,
                        "apply_abs_debounce": _tb_apply_abs_debounce,
                        "render_static_layer": _tb_render_static_layer,
                        "render_dynamic_full": _tb_render_dynamic_full,
                        "draw_values_overlay": _tb_draw_values_overlay,
                    }

                use_cached_st = False
                if is_steering:
                    st_cached = renderer_state.helpers.get("st_fns")
                    if isinstance(st_cached, dict):
                        st_layout = dict(st_cached.get("layout") or {})
                        _st_sample_column = st_cached.get("sample_column")
                        _st_render_static_layer = st_cached.get("render_static_layer")
                        _st_render_dynamic_full = st_cached.get("render_dynamic_full")
                        _st_draw_values_overlay = st_cached.get("draw_values_overlay")
                        _st_fast_idx_from_slow_idx = st_cached.get("fast_idx_from_slow_idx")
                        _st_sample_legacy = st_cached.get("sample_legacy")
                        if (
                            callable(_st_sample_column)
                            and callable(_st_render_static_layer)
                            and callable(_st_render_dynamic_full)
                            and callable(_st_draw_values_overlay)
                            and callable(_st_fast_idx_from_slow_idx)
                            and callable(_st_sample_legacy)
                            and st_layout
                        ):
                            use_cached_st = True
                if is_steering and (not use_cached_st):
                    st_layout_sig = (int(w), int(h))
                    if renderer_state.helpers.get("st_layout_sig") != st_layout_sig or ("st" not in renderer_state.layout):
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

                        st_font_sz = int(round(max(10.0, min(18.0, float(h) * 0.13))))
                        st_font_val_sz = int(round(max(11.0, min(20.0, float(h) * 0.15))))
                        st_font_axis_sz = max(8, int(st_font_sz - 2))
                        st_font_axis_small_sz = max(7, int(st_font_sz - 3))
                        st_layout = {
                            "font_title": _load_hud_font(st_font_sz),
                            "font_val": _load_hud_font(st_font_val_sz),
                            "font_axis": _load_hud_font(st_font_axis_sz),
                            "font_axis_small": _load_hud_font(st_font_axis_small_sz),
                            "y_txt": int(4),
                            "mx": int(st_mx),
                            "y_mid": int(round(st_mid_y)),
                            "marker_xf": float(st_marker_xf),
                            "half_w": float(st_half_w),
                        }
                        renderer_state.layout["st"] = st_layout
                        renderer_state.fonts["st"] = {
                            "title": st_layout.get("font_title"),
                            "value": st_layout.get("font_val"),
                            "axis": st_layout.get("font_axis"),
                            "axis_small": st_layout.get("font_axis_small"),
                        }
                        renderer_state.helpers["st_layout_sig"] = st_layout_sig
                        renderer_state.helpers["st_mid_y"] = float(st_mid_y)
                        renderer_state.helpers["st_amp_neg"] = float(st_amp_neg)
                        renderer_state.helpers["st_amp_pos"] = float(st_amp_pos)
                    else:
                        st_layout = dict(renderer_state.layout.get("st") or {})
                        st_mid_y = float(renderer_state.helpers.get("st_mid_y", float(h) / 2.0))
                        st_amp_neg = float(renderer_state.helpers.get("st_amp_neg", max(2.0, (float(h) / 2.0) - 2.0)))
                        st_amp_pos = float(renderer_state.helpers.get("st_amp_pos", max(2.0, (float(h) / 2.0) - 2.0)))

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
                        return int(_mapped_fast_idx_for_slow_idx(int(ii)))

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
                        axis_labels_st: list[tuple[int, str]] = []
                        try:
                            st_abs_deg = abs(float(steer_abs_max) * 180.0 / math.pi)
                            if (not math.isfinite(st_abs_deg)) or st_abs_deg < 1e-6:
                                st_abs_deg = 1.0

                            def _st_y_from_deg(v_deg: float) -> int:
                                return int(_st_y_from_norm(float(v_deg) / float(st_abs_deg)))

                            step_st = choose_tick_step(-st_abs_deg, st_abs_deg, min_segments=2, max_segments=5, target_segments=5)
                            if step_st is not None:
                                val_bounds_st = build_value_boundaries(-st_abs_deg, st_abs_deg, float(step_st), anchor="top")
                                y_bounds_st = value_boundaries_to_y(
                                    val_bounds_st,
                                    _st_y_from_deg,
                                    int(0),
                                    int(h) - 1,
                                )
                                draw_stripe_grid(
                                    static_dr_local,
                                    int(0),
                                    int(w),
                                    int(0),
                                    int(h) - 1,
                                    y_bounds_st,
                                    col_bg=COL_HUD_BG,
                                    darken_delta=6,
                                )
                                for vv in val_bounds_st:
                                    if should_suppress_boundary_label(float(vv), -st_abs_deg, st_abs_deg, suppress_zero=True):
                                        continue
                                    axis_labels_st.append(
                                        (
                                            int(_st_y_from_deg(float(vv))),
                                            format_value_for_step(float(vv), float(step_st), min_decimals=0),
                                        )
                                    )
                        except Exception:
                            pass
                        axis_labels_st = filter_axis_labels_by_position(
                            axis_labels_st,
                            int(0),
                            int(h) - 1,
                            zero_y=int(st_layout["y_mid"]),
                            pad_px=2,
                        )
                        draw_left_axis_labels(
                            static_dr_local,
                            int(0),
                            int(w),
                            int(0),
                            int(h) - 1,
                            axis_labels_st,
                            st_layout.get("font_axis"),
                            col_text=COL_WHITE,
                            x_pad=6,
                            fallback_font_obj=st_layout.get("font_axis_small"),
                        )
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
                        s_txt = f"{sdeg:+04d}°"
                        f_txt = f"{fdeg:+04d}°"

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

                    renderer_state.helpers["st_fns"] = {
                        "layout": st_layout,
                        "sample_column": _st_sample_column,
                        "render_static_layer": _st_render_static_layer,
                        "render_dynamic_full": _st_render_dynamic_full,
                        "draw_values_overlay": _st_draw_values_overlay,
                        "fast_idx_from_slow_idx": _st_fast_idx_from_slow_idx,
                        "sample_legacy": _st_sample_legacy,
                    }

                use_cached_d = False
                if is_delta:
                    d_cached = renderer_state.helpers.get("d_fns")
                    if isinstance(d_cached, dict):
                        d_layout = dict(d_cached.get("layout") or {})
                        _d_sample_column = d_cached.get("sample_column")
                        _d_draw_segment = d_cached.get("draw_segment")
                        _d_render_static_layer = d_cached.get("render_static_layer")
                        _d_render_dynamic_full = d_cached.get("render_dynamic_full")
                        _d_draw_values_overlay = d_cached.get("draw_values_overlay")
                        _d_sign_from_delta = d_cached.get("sign_from_delta")
                        if (
                            callable(_d_sample_column)
                            and callable(_d_draw_segment)
                            and callable(_d_render_static_layer)
                            and callable(_d_render_dynamic_full)
                            and callable(_d_draw_values_overlay)
                            and callable(_d_sign_from_delta)
                            and d_layout
                        ):
                            use_cached_d = True
                if is_delta and (not use_cached_d):
                    d_layout_sig = (int(w), int(h))
                    if renderer_state.helpers.get("d_layout_sig") != d_layout_sig or ("d" not in renderer_state.layout):
                        d_font_sz = int(round(max(10.0, min(18.0, float(h) * 0.13))))
                        d_font_val_sz = int(round(max(11.0, min(20.0, float(h) * 0.15))))
                        d_font_axis_sz = max(8, int(d_font_sz - 2))
                        d_font_axis_small_sz = max(7, int(d_font_sz - 3))
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
                            "font_title": _load_hud_font(d_font_sz),
                            "font_val": _load_hud_font(d_font_val_sz),
                            "font_axis": _load_hud_font(d_font_axis_sz),
                            "font_axis_small": _load_hud_font(d_font_axis_small_sz),
                            "y_txt": int(2),
                            "mx": int(d_mx),
                            "marker_xf": float(d_marker_xf),
                            "half_w": float(d_half_w),
                            "plot_y0": int(d_plot_y0),
                            "plot_y1": int(d_plot_y1),
                        }
                        renderer_state.layout["d"] = d_layout
                        renderer_state.fonts["d"] = {
                            "title": d_layout.get("font_title"),
                            "value": d_layout.get("font_val"),
                            "axis": d_layout.get("font_axis"),
                            "axis_small": d_layout.get("font_axis_small"),
                        }
                        renderer_state.helpers["d_layout_sig"] = d_layout_sig
                        renderer_state.helpers["d_font_val_sz"] = int(d_font_val_sz)
                    else:
                        d_layout = dict(renderer_state.layout.get("d") or {})
                        d_font_val_sz = int(renderer_state.helpers.get("d_font_val_sz", max(11, int(round(float(h) * 0.15)))))

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
                        if int(slow_frame_count_total) <= 0:
                            return 0.0
                        ii = int(idx0)
                        ii = _clamp_idx_int(int(ii), 0, int(slow_frame_count_total) - 1)
                        slow_t = float(_mapped_t_slow_for_slow_idx(int(ii)))
                        fast_t = float(_mapped_t_fast_for_slow_idx(int(ii)))
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
                        axis_labels_d: list[tuple[int, str]] = []
                        try:
                            d_val_min = -float(d_range_neg) if bool(delta_has_neg) else 0.0
                            d_val_max = float(d_range_pos)
                            if d_val_max <= d_val_min + 1e-9:
                                d_val_max = d_val_min + 1e-6
                            step_d = choose_tick_step(d_val_min, d_val_max, min_segments=2, max_segments=5, target_segments=5)
                            if step_d is not None:
                                val_bounds_d = build_value_boundaries(d_val_min, d_val_max, float(step_d), anchor="top")
                                y_bounds_d = value_boundaries_to_y(
                                    val_bounds_d,
                                    _d_y_from_delta,
                                    int(d_layout["plot_y0"]),
                                    int(d_layout["plot_y1"]),
                                )
                                draw_stripe_grid(
                                    static_dr_local,
                                    int(0),
                                    int(w),
                                    int(d_layout["plot_y0"]),
                                    int(d_layout["plot_y1"]),
                                    y_bounds_d,
                                    col_bg=COL_HUD_BG,
                                    darken_delta=6,
                                )
                                for vv in val_bounds_d:
                                    if should_suppress_boundary_label(float(vv), d_val_min, d_val_max, suppress_zero=True):
                                        continue
                                    axis_labels_d.append(
                                        (
                                            int(_d_y_from_delta(float(vv))),
                                            format_value_for_step(float(vv), float(step_d), min_decimals=0),
                                        )
                                    )
                        except Exception:
                            pass
                        axis_labels_d = filter_axis_labels_by_position(
                            axis_labels_d,
                            int(d_layout["plot_y0"]),
                            int(d_layout["plot_y1"]),
                            zero_y=int(d_y_zero),
                            pad_px=2,
                        )
                        draw_left_axis_labels(
                            static_dr_local,
                            int(0),
                            int(w),
                            int(d_layout["plot_y0"]),
                            int(d_layout["plot_y1"]),
                            axis_labels_d,
                            d_layout.get("font_axis"),
                            col_text=COL_WHITE,
                            x_pad=6,
                            fallback_font_obj=d_layout.get("font_axis_small"),
                        )
                        try:
                            # Visible title is "Time Delta"; HUD key remains "Delta" (API contract).
                            static_dr_local.text(
                                (4, int(d_layout["y_txt"])),
                                "Time Delta",
                                fill=COL_WHITE,
                                font=d_layout.get("font_title"),
                            )
                        except Exception:
                            pass
                        try:
                            static_dr_local.line(
                                [(0, int(d_y_zero)), (int(w) - 1, int(d_y_zero))],
                                fill=(COL_SLOW_DARKRED[0], COL_SLOW_DARKRED[1], COL_SLOW_DARKRED[2], 200),
                                width=1,
                            )
                        except Exception:
                            pass
                        try:
                            mx_s = int(d_layout["mx"])
                            static_dr_local.rectangle([mx_s, 0, mx_s + 1, int(h)], fill=(255, 255, 255, 230))
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
                    renderer_state.helpers["d_fns"] = {
                        "layout": d_layout,
                        "sample_column": _d_sample_column,
                        "draw_segment": _d_draw_segment,
                        "render_static_layer": _d_render_static_layer,
                        "render_dynamic_full": _d_render_dynamic_full,
                        "draw_values_overlay": _d_draw_values_overlay,
                        "sign_from_delta": _d_sign_from_delta,
                    }

                use_cached_ld = False
                if is_line_delta:
                    ld_cached = renderer_state.helpers.get("ld_fns")
                    if isinstance(ld_cached, dict):
                        ld_layout = dict(ld_cached.get("layout") or {})
                        _ld_sample_column = ld_cached.get("sample_column")
                        _ld_render_static_layer = ld_cached.get("render_static_layer")
                        _ld_render_dynamic_full = ld_cached.get("render_dynamic_full")
                        _ld_draw_values_overlay = ld_cached.get("draw_values_overlay")
                        _ld_value_at_slow_idx = ld_cached.get("value_at_slow_idx")
                        if (
                            callable(_ld_sample_column)
                            and callable(_ld_render_static_layer)
                            and callable(_ld_render_dynamic_full)
                            and callable(_ld_draw_values_overlay)
                            and callable(_ld_value_at_slow_idx)
                            and ld_layout
                        ):
                            use_cached_ld = True

                if is_line_delta and (not use_cached_ld):
                    ld_layout_sig = (int(w), int(h))
                    if renderer_state.helpers.get("ld_layout_sig") != ld_layout_sig or ("ld" not in renderer_state.layout):
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
                            "font_title": _load_hud_font(ld_font_sz),
                            "font_val": _load_hud_font(ld_font_val_sz),
                            "font_axis": _load_hud_font(ld_font_axis_sz),
                            "font_axis_small": _load_hud_font(ld_font_axis_small_sz),
                            "y_txt": int(2),
                            "mx": int(ld_mx),
                            "marker_xf": float(ld_marker_xf),
                            "half_w": float(ld_half_w),
                            "plot_y0": int(ld_plot_y0),
                            "plot_y1": int(ld_plot_y1),
                        }
                        renderer_state.layout["ld"] = ld_layout
                        renderer_state.fonts["ld"] = {
                            "title": ld_layout.get("font_title"),
                            "value": ld_layout.get("font_val"),
                            "axis": ld_layout.get("font_axis"),
                            "axis_small": ld_layout.get("font_axis_small"),
                        }
                        renderer_state.helpers["ld_layout_sig"] = ld_layout_sig
                    else:
                        ld_layout = dict(renderer_state.layout.get("ld") or {})

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
                    renderer_state.helpers["ld_fns"] = {
                        "layout": ld_layout,
                        "sample_column": _ld_sample_column,
                        "render_static_layer": _ld_render_static_layer,
                        "render_dynamic_full": _ld_render_dynamic_full,
                        "draw_values_overlay": _ld_draw_values_overlay,
                        "value_at_slow_idx": _ld_value_at_slow_idx,
                    }

                use_cached_uo = False
                if is_under_oversteer:
                    uo_cached = renderer_state.helpers.get("uo_fns")
                    if isinstance(uo_cached, dict):
                        uo_layout = dict(uo_cached.get("layout") or {})
                        _uo_sample_column = uo_cached.get("sample_column")
                        _uo_render_static_layer = uo_cached.get("render_static_layer")
                        _uo_render_dynamic_full = uo_cached.get("render_dynamic_full")
                        if (
                            callable(_uo_sample_column)
                            and callable(_uo_render_static_layer)
                            and callable(_uo_render_dynamic_full)
                            and uo_layout
                        ):
                            use_cached_uo = True

                if is_under_oversteer and (not use_cached_uo):
                    uo_layout_sig = (int(w), int(h))
                    if renderer_state.helpers.get("uo_layout_sig") != uo_layout_sig or ("uo" not in renderer_state.layout):
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
                            "font_title": _load_hud_font(uo_font_sz),
                            "font_axis": _load_hud_font(uo_font_axis_sz),
                            "font_axis_small": _load_hud_font(uo_font_axis_small_sz),
                            "label_x": int(4),
                            "label_top_y": int(2),
                            "label_bottom_y": int(h - uo_font_sz - 2),
                            "mx": int(uo_mx),
                            "marker_xf": float(uo_marker_xf),
                            "half_w": float(uo_half_w),
                            "plot_y0": int(uo_plot_y0),
                            "plot_y1": int(uo_plot_y1),
                        }
                        renderer_state.layout["uo"] = uo_layout
                        renderer_state.fonts["uo"] = {
                            "title": uo_layout.get("font_title"),
                            "axis": uo_layout.get("font_axis"),
                            "axis_small": uo_layout.get("font_axis_small"),
                        }
                        renderer_state.helpers["uo_layout_sig"] = uo_layout_sig
                    else:
                        uo_layout = dict(renderer_state.layout.get("uo") or {})

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
                    renderer_state.helpers["uo_fns"] = {
                        "layout": uo_layout,
                        "sample_column": _uo_sample_column,
                        "render_static_layer": _uo_render_static_layer,
                        "render_dynamic_full": _uo_render_dynamic_full,
                    }

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
                            "frame_window_mapping": None,
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
                            "frame_window_mapping": None,
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
                            "frame_window_mapping": None,
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
                            "frame_window_mapping": None,
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
                            "frame_window_mapping": None,
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

                current_geom_sig = (int(w), int(h), float(r), int(before_f), int(after_f))
                geometry_changed = tuple(renderer_state.geometry_signature) != tuple(current_geom_sig)
                if geometry_changed:
                    renderer_state.geometry_signature = tuple(current_geom_sig)
                    renderer_state.layout.clear()
                    renderer_state.fonts.clear()
                    renderer_state.static_primitives.clear()
                    renderer_state.helpers.clear()
                    state["static_layer"] = None
                    state["dynamic_layer"] = None
                    state["tb_cols"] = []
                    state["last_y"] = None
                    state["last_delta_value"] = None
                    state["last_delta_sign"] = None

                first_frame = bool(renderer_state.first_frame) or state.get("static_layer") is None or state.get("dynamic_layer") is None
                reset_now = False
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
                if geometry_changed:
                    reset_now = True

                if first_frame or reset_now:
                    if is_throttle_brake:
                        renderer_state.helpers.pop("tb_max_brake_states", None)
                        renderer_state.helpers.pop("tb_max_brake_last_idx", None)
                        static_layer = _tb_render_static_layer()
                        dynamic_layer, tb_cols_fill, tb_abs_state_fill = _tb_render_dynamic_full()
                        tb_last_col_fill = tb_cols_fill[-1] if tb_cols_fill else _tb_sample_column(int(w) - 1)
                        right_sample_now = int(tb_last_col_fill["slow_idx"]) if tb_last_col_fill is not None else _right_edge_sample_idx()
                        scroll_state_by_hud[hud_state_key] = {
                            "static_layer": static_layer,
                            "dynamic_layer": dynamic_layer,
                            "scroll_pos_px": 0.0,
                            "last_i": int(i),
                            "last_right_sample": int(right_sample_now),
                            "window_frames": int(window_frames),
                            "tb_cols": tb_cols_fill,
                            "last_y": (
                                int(tb_last_col_fill["y_s_b"]),
                                int(tb_last_col_fill["y_s_t"]),
                                int(tb_last_col_fill["y_f_b"]),
                                int(tb_last_col_fill["y_f_t"]),
                            ),
                            "tb_abs_s_on": bool(tb_abs_state_fill.get("tb_abs_s_on", False)),
                            "tb_abs_f_on": bool(tb_abs_state_fill.get("tb_abs_f_on", False)),
                            "tb_abs_s_on_count": int(tb_abs_state_fill.get("tb_abs_s_on_count", 0)),
                            "tb_abs_s_off_count": int(tb_abs_state_fill.get("tb_abs_s_off_count", 0)),
                            "tb_abs_f_on_count": int(tb_abs_state_fill.get("tb_abs_f_on_count", 0)),
                            "tb_abs_f_off_count": int(tb_abs_state_fill.get("tb_abs_f_off_count", 0)),
                        }
                        hud_layer = _compose_hud_layers_local(int(w), int(h), static_layer, dynamic_layer, _tb_draw_values_overlay)
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
                        hud_layer = _compose_hud_layers_local(int(w), int(h), static_layer, dynamic_layer, _st_draw_values_overlay)
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
                        hud_layer = _compose_hud_layers_local(int(w), int(h), static_layer, dynamic_layer, _d_draw_values_overlay)
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
                        hud_layer = _compose_hud_layers_local(int(w), int(h), static_layer, dynamic_layer, _ld_draw_values_overlay)
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
                        hud_layer = _compose_hud_layers_local(int(w), int(h), static_layer, dynamic_layer, None)
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
                        hud_layer = _compose_hud_layers_local(int(w), int(h), static_layer, dynamic_layer, None)
                        _composite_hud_into_frame_local(img, hud_layer, int(x0), int(y0))
                    renderer_state.first_frame = False
                    continue

                renderer_state.first_frame = False
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
                    # PERF: alloc (avoid transparent fallback image; skip copy when previous layer is missing)
                    dynamic_prev = None
                # PERF: alloc (reused dynamic scratch buffers; realloc only on mismatch)
                dynamic_scratch_pair = renderer_state.helpers.get("dynamic_next_scratch_pair")
                need_new_scratch_pair = True
                if isinstance(dynamic_scratch_pair, list) and len(dynamic_scratch_pair) == 2:
                    need_new_scratch_pair = False
                    for scratch_img in dynamic_scratch_pair:
                        if (
                            scratch_img is None
                            or getattr(scratch_img, "mode", "") != "RGBA"
                            or tuple(getattr(scratch_img, "size", (0, 0))) != (int(w), int(h))
                        ):
                            need_new_scratch_pair = True
                            break
                if need_new_scratch_pair:
                    dynamic_scratch_pair = [
                        Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0)),
                        Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0)),
                    ]
                    renderer_state.helpers["dynamic_next_scratch_pair"] = dynamic_scratch_pair
                    renderer_state.helpers["dynamic_next_scratch_idx"] = 0
                last_scratch_idx = int(renderer_state.helpers.get("dynamic_next_scratch_idx", 0))
                dynamic_next_idx = 1 if int(last_scratch_idx) == 0 else 0
                dynamic_next = dynamic_scratch_pair[int(dynamic_next_idx)]
                if dynamic_next is dynamic_prev:
                    dynamic_next_idx = 1 if int(dynamic_next_idx) == 0 else 0
                    dynamic_next = dynamic_scratch_pair[int(dynamic_next_idx)]
                dynamic_next.paste((0, 0, 0, 0), (0, 0, int(w), int(h)))
                renderer_state.helpers["dynamic_next_scratch_idx"] = int(dynamic_next_idx)
                if debug_composite and (not bool(debug_composite_state.get("scratch_logged", False))):
                    _log_print(
                        f"[perf][scratch] dynamic_next reuse={not bool(need_new_scratch_pair)} size={int(w)}x{int(h)}",
                        log_file,
                    )
                    debug_composite_state["scratch_logged"] = True
                shift_px = int(shift_int)
                if dynamic_prev is not None and shift_px < int(w):
                    try:
                        dynamic_next.paste(dynamic_prev, (-int(shift_px), 0))
                    except Exception:
                        pass
                if is_throttle_brake:
                    tb_cols_next = list(state.get("tb_cols") or [])
                    if shift_px > 0:
                        tb_cols_next = tb_cols_next[int(shift_px):]
                    if len(tb_cols_next) > int(w):
                        tb_cols_next = tb_cols_next[-int(w):]
                    if tb_cols_next:
                        for x_rebase, col_rebase in enumerate(tb_cols_next):
                            try:
                                col_rebase["x"] = int(x_rebase)
                            except Exception:
                                pass

                    tb_abs_state_inc: dict[str, Any] = {
                        "tb_abs_s_on": bool(state.get("tb_abs_s_on", False)),
                        "tb_abs_f_on": bool(state.get("tb_abs_f_on", False)),
                        "tb_abs_s_on_count": int(state.get("tb_abs_s_on_count", 0)),
                        "tb_abs_s_off_count": int(state.get("tb_abs_s_off_count", 0)),
                        "tb_abs_f_on_count": int(state.get("tb_abs_f_on_count", 0)),
                        "tb_abs_f_off_count": int(state.get("tb_abs_f_off_count", 0)),
                    }
                    tb_dr_next = ImageDraw.Draw(dynamic_next)
                    prev_x_inc: int | None = None
                    prev_y_s_b_inc: int | None = None
                    prev_y_s_t_inc: int | None = None
                    prev_y_f_b_inc: int | None = None
                    prev_y_f_t_inc: int | None = None
                    if int(right_edge_cols) > 0:
                        first_dest_x = int(w) - int(right_edge_cols)
                        if first_dest_x < 0:
                            first_dest_x = 0
                        if first_dest_x > 0:
                            prev_x_inc = int(first_dest_x) - 1
                            if shift_px > 0:
                                last_y_state = state.get("last_y")
                                if isinstance(last_y_state, (list, tuple)) and len(last_y_state) >= 4:
                                    try:
                                        prev_y_s_b_inc = int(last_y_state[0])
                                        prev_y_s_t_inc = int(last_y_state[1])
                                        prev_y_f_b_inc = int(last_y_state[2])
                                        prev_y_f_t_inc = int(last_y_state[3])
                                    except Exception:
                                        prev_y_s_b_inc = None
                                        prev_y_s_t_inc = None
                                        prev_y_f_b_inc = None
                                        prev_y_f_t_inc = None
                            if (
                                prev_y_s_b_inc is None
                                or prev_y_s_t_inc is None
                                or prev_y_f_b_inc is None
                                or prev_y_f_t_inc is None
                            ):
                                col_prev_left = _tb_sample_column(int(prev_x_inc))
                                prev_y_s_b_inc = int(col_prev_left["y_s_b"])
                                prev_y_s_t_inc = int(col_prev_left["y_s_t"])
                                prev_y_f_b_inc = int(col_prev_left["y_f_b"])
                                prev_y_f_t_inc = int(col_prev_left["y_f_t"])

                    last_col_inc: dict[str, Any] | None = None

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

                        if prev_x_inc is not None and prev_y_s_b_inc is not None:
                            tb_dr_next.line([(int(prev_x_inc), int(prev_y_s_b_inc)), (int(dest_x), int(col_now_inc["y_s_b"]))], fill=COL_SLOW_DARKRED, width=2)
                        if prev_x_inc is not None and prev_y_s_t_inc is not None:
                            tb_dr_next.line([(int(prev_x_inc), int(prev_y_s_t_inc)), (int(dest_x), int(col_now_inc["y_s_t"]))], fill=COL_SLOW_BRIGHTRED, width=2)
                        if prev_x_inc is not None and prev_y_f_b_inc is not None:
                            tb_dr_next.line([(int(prev_x_inc), int(prev_y_f_b_inc)), (int(dest_x), int(col_now_inc["y_f_b"]))], fill=COL_FAST_DARKBLUE, width=2)
                        if prev_x_inc is not None and prev_y_f_t_inc is not None:
                            tb_dr_next.line([(int(prev_x_inc), int(prev_y_f_t_inc)), (int(dest_x), int(col_now_inc["y_f_t"]))], fill=COL_FAST_BRIGHTBLUE, width=2)

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
                        prev_x_inc = int(dest_x)
                        prev_y_s_b_inc = int(col_now_inc["y_s_b"])
                        prev_y_s_t_inc = int(col_now_inc["y_s_t"])
                        prev_y_f_b_inc = int(col_now_inc["y_f_b"])
                        prev_y_f_t_inc = int(col_now_inc["y_f_t"])
                        last_col_inc = col_now_inc

                    if last_col_inc is None:
                        last_col_inc = _tb_sample_column(int(w) - 1)
                    right_sample_now = int(last_col_inc["slow_idx"]) if last_col_inc is not None else _right_edge_sample_idx()
                    state["dynamic_layer"] = dynamic_next
                    state["scroll_pos_px"] = float(scroll_pos_px)
                    state["last_i"] = int(i)
                    state["last_right_sample"] = int(right_sample_now)
                    state["window_frames"] = int(window_frames)
                    state["tb_cols"] = tb_cols_next
                    state["last_y"] = (
                        int(last_col_inc["y_s_b"]),
                        int(last_col_inc["y_s_t"]),
                        int(last_col_inc["y_f_b"]),
                        int(last_col_inc["y_f_t"]),
                    )
                    state["tb_abs_s_on"] = bool(tb_abs_state_inc.get("tb_abs_s_on", False))
                    state["tb_abs_f_on"] = bool(tb_abs_state_inc.get("tb_abs_f_on", False))
                    state["tb_abs_s_on_count"] = int(tb_abs_state_inc.get("tb_abs_s_on_count", 0))
                    state["tb_abs_s_off_count"] = int(tb_abs_state_inc.get("tb_abs_s_off_count", 0))
                    state["tb_abs_f_on_count"] = int(tb_abs_state_inc.get("tb_abs_f_on_count", 0))
                    state["tb_abs_f_off_count"] = int(tb_abs_state_inc.get("tb_abs_f_off_count", 0))

                    static_now = state.get("static_layer")
                    hud_layer = _compose_hud_layers_local(int(w), int(h), static_now, dynamic_next, _tb_draw_values_overlay)
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
                    hud_layer = _compose_hud_layers_local(int(w), int(h), static_now, dynamic_next, _st_draw_values_overlay)
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
                    hud_layer = _compose_hud_layers_local(int(w), int(h), static_now, dynamic_next, _d_draw_values_overlay)
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
                    hud_layer = _compose_hud_layers_local(int(w), int(h), static_now, dynamic_next, _ld_draw_values_overlay)
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
                    hud_layer = _compose_hud_layers_local(int(w), int(h), static_now, dynamic_next, None)
                    _composite_hud_into_frame_local(img, hud_layer, int(x0), int(y0))
                else:
                    hud_full_now = _render_scroll_hud_full(
                        scroll_pos_px_local=scroll_pos_px,
                        shift_int_local=int(shift_int),
                        right_edge_cols_local=int(right_edge_cols),
                    )
                    try:
                        # PERF: composite (crop -> paste strip update on fallback HUD path)
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
                    hud_layer = _compose_hud_layers_local(int(w), int(h), static_now, dynamic_next, None)
                    _composite_hud_into_frame_local(img, hud_layer, int(x0), int(y0))
            except Exception:
                continue


        # Flatten HUD over black to keep visual output identical to the PNG path.
        save_img = Image.new("RGB", (int(geom.hud), int(geom.H)), (0, 0, 0))
        src_rgba = img if getattr(img, "mode", "") == "RGBA" else img.convert("RGBA")
        save_img.paste(src_rgba, (0, 0), src_rgba)

        rgba_bytes = save_img.convert("RGBA").tobytes()
        frame_writer(rgba_bytes)
        if frame_written_cb is not None:
            try:
                frame_written_cb(int(j) + 1, int(frames))
            except Exception:
                pass
        if hud_dbg and j < 2:
            _log_print(f"[hudpy] sample j={j} ld={ld:.6f} ld_mod={ld_mod:.6f} -> stream rgba", log_file)
    _log_print(f"[hudpy] geschrieben: {frames} frames -> ffmpeg stdin (rgba)", log_file)
    return None
    
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

    hud_x0 = _geom_hud_x0(geom)

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
    hud_max_brake_delay_distance: float = 0.003,
    hud_max_brake_delay_pressure: float = 35.0,
    layout_config: LayoutConfig | None = None,
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
    geom = build_output_geometry(preset, hud_width_px=hud_width_px, layout_config=layout_config)
    _debug_dump_geometry_once(geom, log_file=log_file)

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
    hud_max_brake_delay_distance: float = 0.003,
    hud_max_brake_delay_pressure: float = 35.0,
    under_oversteer_curve_center: float = 0.0,
    layout_config: LayoutConfig | None = None,
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

    from csv_g61 import load_g61_csv
    csv_load_debug = (os.environ.get("IRVC_DEBUG_CSV_LOADS") or "").strip().lower() in ("1", "true", "yes", "on")
    csv_load_counts: dict[str, int] = {}

    def _load_run_once(label: str, path: Path):
        run = load_g61_csv(path)
        k = str(path)
        csv_load_counts[k] = int(csv_load_counts.get(k, 0)) + 1
        if csv_load_debug:
            _log_print(f"[csv] load label={label} count={csv_load_counts[k]} path={k}", log_file)
        return run

    run_slow = _load_run_once("slow", scsv)
    run_fast = _load_run_once("fast", fcsv)

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
    try:
        hud_max_brake_delay_distance = float(hud_max_brake_delay_distance)
    except Exception:
        hud_max_brake_delay_distance = 0.003
    if (not math.isfinite(float(hud_max_brake_delay_distance))) or float(hud_max_brake_delay_distance) < 0.0:
        hud_max_brake_delay_distance = 0.0
    if float(hud_max_brake_delay_distance) > 1.0:
        hud_max_brake_delay_distance = 1.0

    try:
        hud_max_brake_delay_pressure = float(hud_max_brake_delay_pressure)
    except Exception:
        hud_max_brake_delay_pressure = 35.0
    if not math.isfinite(float(hud_max_brake_delay_pressure)):
        hud_max_brake_delay_pressure = 35.0
    if float(hud_max_brake_delay_pressure) < 0.0:
        hud_max_brake_delay_pressure = 0.0
    if float(hud_max_brake_delay_pressure) > 100.0:
        hud_max_brake_delay_pressure = 100.0

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
        run_s=run_slow,
        run_f=run_fast,
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
    slow_speed_frames = _sample_csv_col_to_frames_float(run_slow, ms.duration_s, float(fps_int), "Speed")
    fast_speed_frames = _sample_csv_col_to_frames_float(run_fast, mf.duration_s, float(fps_int), "Speed")
    slow_gear_frames = _sample_csv_col_to_frames_int_nearest(run_slow, ms.duration_s, float(fps_int), "Gear")
    fast_gear_frames = _sample_csv_col_to_frames_int_nearest(run_fast, mf.duration_s, float(fps_int), "Gear")
    slow_rpm_frames = _sample_csv_col_to_frames_float(run_slow, ms.duration_s, float(fps_int), "RPM")
    fast_rpm_frames = _sample_csv_col_to_frames_float(run_fast, mf.duration_s, float(fps_int), "RPM")
    # Story 3: Steering pro Frame (Scroll-HUD)
    slow_steer_frames = _sample_csv_col_to_frames_float(run_slow, ms.duration_s, float(fps_int), "SteeringWheelAngle")
    fast_steer_frames = _sample_csv_col_to_frames_float(run_fast, mf.duration_s, float(fps_int), "SteeringWheelAngle")
    # Story 4: Throttle / Brake / ABS pro Frame (Scroll-HUD)
    slow_throttle_frames = _sample_csv_col_to_frames_float(run_slow, ms.duration_s, float(fps_int), "Throttle")
    fast_throttle_frames = _sample_csv_col_to_frames_float(run_fast, mf.duration_s, float(fps_int), "Throttle")
    slow_brake_frames = _sample_csv_col_to_frames_float(run_slow, ms.duration_s, float(fps_int), "Brake")
    fast_brake_frames = _sample_csv_col_to_frames_float(run_fast, mf.duration_s, float(fps_int), "Brake")
    slow_abs_frames = _sample_csv_col_to_frames_float(run_slow, ms.duration_s, float(fps_int), "ABSActive")
    fast_abs_frames = _sample_csv_col_to_frames_float(run_fast, mf.duration_s, float(fps_int), "ABSActive")
    fast_lapdist_frames = _sample_csv_col_to_frames_float(run_fast, mf.duration_s, float(fps_int), "LapDistPct")
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
    geom = build_output_geometry(preset, hud_width_px=hud_width_px, layout_config=layout_config)
    _debug_dump_geometry_once(geom, log_file=log_file)
    
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
    # HUD pro Frame in Python rendern und als rawvideo/rgba an ffmpeg stdin streamen.
    hud_stream_ctx: HudContext | None = None
    hud_scroll_on = (os.environ.get("IRVC_HUD_SCROLL") or "").strip() == "1"
    if hud_scroll_on:
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
                run_s=run_slow,
                run_f=run_fast,
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
                run_s=run_slow,
                run_f=run_fast,
            )

        # Story 4.2: per-HUD Overrides sind inaktiv; alle Scroll-HUDs nutzen globales Fenster.
        global_before_s = max(1e-6, float(before_default_s))
        global_after_s = max(1e-6, float(after_default_s))
        # Story 2.2: Scroll-HUD Fenster intern symmetrisch halten.
        # Damit ist die Pixel-Scrollrate links/rechts eindeutig.
        global_sym_s = max(float(global_before_s), float(global_after_s))
        global_before_s = float(global_sym_s)
        global_after_s = float(global_sym_s)

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

            b = float(global_before_s)
            a = float(global_after_s)
            
            # DEBUG: Zeigt, welche Sekundenwerte pro HUD wirklich verwendet werden
            # (damit wir sehen, warum Steering bei dir auf 0.1/0.1 steht)
            try:
                if (os.environ.get("RVA_HUD_STEER_DEBUG_FRAME") or "").strip() != "":
                    _log_print(
                        f"[hudpy][dbg-win] hud={hud_name} base_before={before_default_s} base_after={after_default_s} effective_before={global_before_s} effective_after={global_after_s}",
                        log_file,
                    )
            except Exception:
                pass

            b = max(1e-6, float(b))
            a = max(1e-6, float(a))
                 
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
                fast_lapdist_frames=fast_lapdist_frames,
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
                max_brake_delay_distance=float(hud_max_brake_delay_distance),
                max_brake_delay_pressure=float(hud_max_brake_delay_pressure),
            ),
            log_file=log_file,
        )
        hud_stream_ctx = hud_ctx
        _log_print("[hudpy] ON -> ffmpeg stdin stream (rgba)", log_file)


    # sendcmd-demo komplett aus (war instabil / unwirksam)
    hud_cmd_file = None

    # Wenn wir HUD-Frames als 3. Input laden, ist das Input-Label in ffmpeg dann [0:v].
    # ABER: wir haben die Reihenfolge der Inputs im build_cmd geÃ¤ndert:
    #   optional HUD = Input 0
    #   slow = Input 1
    #   fast = Input 2
    #
    # Deshalb mÃ¼ssen wir hier merken: wenn HUD-Input aktiv ist, dann ist hud_label="[0:v]"
    hud_input_active = bool(hud_stream_ctx is not None)
    hud_label = "[0:v]" if hud_input_active else None

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
    if csv_load_debug:
        parts: list[str] = []
        for k in sorted(csv_load_counts.keys()):
            parts.append(f"{k}={int(csv_load_counts[k])}")
        summary = ", ".join(parts) if parts else "<none>"
        _log_print(f"[csv] load_counts {summary}", log_file)
        over = [k for k, v in csv_load_counts.items() if int(v) > 1]
        if over:
            _log_print(f"[csv] WARN more_than_once={','.join(over)}", log_file)
        else:
            _log_print("[csv] OK each_source_loaded_once", log_file)

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
                hud_fps=float(fps_int),
                hud_stdin_raw=bool(hud_stream_ctx is not None),
                hud_size=(int(geom.hud), int(geom.H)),
                hud_pix_fmt="rgba",
            ),
            flt=FilterSpec(filter_complex=filt, video_map="[vout]", audio_map=audio_map),
            enc=enc,
            audio_source="none",
            outp=outp,
            debug_max_s=dbg_max_s,
        )

        live = (os.environ.get("IRVC_FFMPEG_LIVE") or "").strip() == "1"
        if hud_stream_ctx is not None:
            expected_bytes = int(geom.hud) * int(geom.H) * 4
            report_every = 5
            report_state = {"last": 0}

            def _stdin_writer(stdin_pipe: Any) -> None:
                def _write_frame_rgba(frame_bytes: bytes) -> None:
                    if len(frame_bytes) != expected_bytes:
                        raise RuntimeError(
                            f"HUD stream frame size mismatch: expected {expected_bytes} bytes, got {len(frame_bytes)}"
                        )
                    try:
                        stdin_pipe.write(frame_bytes)
                    except BrokenPipeError as e:
                        raise RuntimeError("ffmpeg stdin pipe closed while streaming HUD frames.") from e

                def _on_frame_written(written: int, total: int) -> None:
                    if written == total or written == 1 or (written - int(report_state["last"])) >= report_every:
                        print(f"hud_stream_frame={written}/{total}", flush=True)
                        report_state["last"] = int(written)

                _render_hud_scroll_frames_png(
                    hud_stream_ctx,
                    frame_writer=_write_frame_rgba,
                    frame_written_cb=_on_frame_written,
                )
                try:
                    stdin_pipe.flush()
                except Exception:
                    pass

            rc = run_ffmpeg(
                plan,
                tail_n=20,
                log_file=log_file,
                live_stdout=live,
                stdin_write_fn=_stdin_writer,
            )
        else:
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
