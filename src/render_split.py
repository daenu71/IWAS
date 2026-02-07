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
    COL_SLOW_BRIGHTRED,
    COL_SLOW_DARKRED,
    COL_WHITE,
    SCROLL_HUD_NAMES as _SCROLL_HUD_NAMES,
    TABLE_HUD_NAMES as _TABLE_HUD_NAMES,
)
from huds.delta import render_delta
from huds.gear_rpm import render_gear_rpm
from huds.line_delta import render_line_delta
from huds.speed import render_speed
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
    # Immer in die Konsole UND wenn mÃ¶glich ins Log schreiben.
    try:
        print(msg, flush=True)
    except Exception:
        pass
    if log_file is None:
        return
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(str(msg).rstrip("\n") + "\n")
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

    before_s = float(ctx.window.before_s)
    after_s = float(ctx.window.after_s)
    hud_name = ctx.window.hud_name
    hud_windows = ctx.window.hud_windows

    hud_speed_units = str(ctx.settings.speed_units)
    hud_speed_update_hz = int(ctx.settings.speed_update_hz)
    hud_gear_rpm_update_hz = int(ctx.settings.gear_rpm_update_hz)
    hud_curve_points_default = int(ctx.settings.curve_points_default)
    hud_curve_points_overrides = ctx.settings.curve_points_overrides

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


        # Table-HUDs (Speed, Gear & RPM) als Text
        if table_items and slow_to_fast_frame and i < len(slow_to_fast_frame):
            fi = int(slow_to_fast_frame[i])
            if fi < 0:
                fi = 0

            for hud_key, x0, y0, w, h in active_table_items:
                try:
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
                # Sekundenfenster: bevorzugt aus hud_windows (kommt aus render_split_screen_sync),
                # damit wir garantiert die 10/10 Sekunden bekommen.
                b2 = float(default_before_s)
                a2 = float(default_after_s)

                try:
                    if isinstance(hud_windows, dict) and isinstance(hud_windows.get(hud_key), dict):
                        o2 = hud_windows.get(hud_key) or {}
                        if o2.get("before_s") is not None:
                            b2 = float(o2.get("before_s"))
                        if o2.get("after_s") is not None:
                            a2 = float(o2.get("after_s"))
                except Exception:
                    pass

                # Fallback (kompatibel): hud_params, falls vorhanden
                try:
                    p = hud_params.get(hud_key) or {}
                    if p.get("before_s") is not None:
                        b2 = float(p.get("before_s"))
                    if p.get("after_s") is not None:
                        a2 = float(p.get("after_s"))
                except Exception:
                    p = {}

                before_s_h = float(b2)
                after_s_h = float(a2)

                # Optional: ENV Ã¼berschreibt (Debug) -> gilt dann fÃ¼r ALLE HUDs
                if env_before_s is not None:
                    before_s_h = float(env_before_s)
                if env_after_s is not None:
                    after_s_h = float(env_after_s)

                before_f = max(1, int(round(before_s_h * r)))
                after_f = max(1, int(round(after_s_h * r)))

                # Fenster in Frames (Zeit-Achse): stabiler als LapDist-Spannen
                iL = max(0, i - before_f)
                iR = min(len(slow_frame_to_lapdist) - 1, i + after_f)

                center_x = int(x0 + (w // 2))
                half_w = float(w - 1) / 2.0  # Pixel bis zum Rand (links/rechts)

                def _idx_to_x(idx0: int) -> int:
                    # X basiert auf Frame-Offset (Zeit), nicht auf LapDistPct (Strecke)
                    di = int(idx0) - int(i)
                    if di < 0:
                        denom = max(1, int(before_f))
                        frac = float(di) / float(denom)  # -1 .. 0
                    else:
                        denom = max(1, int(after_f))
                        frac = float(di) / float(denom)  # 0 .. +1

                    x = int(round(float(center_x) + (frac * half_w)))

                    if x < x0:
                        x = x0
                    if x > x0 + w - tick_w:
                        x = x0 + w - tick_w
                    return x

                # Marker (pro HUD-Box)
                mx = int(center_x)
                dr.rectangle([mx, y0, mx + 1, y0 + h], fill=(255, 255, 255, 230))

                # Vertikale Tick-/Debug-Striche entfernt.
                # Der Marker (mx) bleibt bestehen und zeigt den aktuellen Zeitpunkt.
                pass

                def _hud_throttle_brake() -> None:
                    throttle_brake_ctx = {
                        "hud_key": hud_key,
                        "i": i,
                        "iL": iL,
                        "iR": iR,
                        "_idx_to_x": _idx_to_x,
                        "_clamp": _clamp,
                        "slow_frame_to_lapdist": slow_frame_to_lapdist,
                        "slow_to_fast_frame": slow_to_fast_frame,
                        "slow_throttle_frames": slow_throttle_frames,
                        "fast_throttle_frames": fast_throttle_frames,
                        "slow_brake_frames": slow_brake_frames,
                        "fast_brake_frames": fast_brake_frames,
                        "slow_abs_frames": slow_abs_frames,
                        "fast_abs_frames": fast_abs_frames,
                        "hud_curve_points_default": hud_curve_points_default,
                        "hud_curve_points_overrides": hud_curve_points_overrides,
                        "COL_SLOW_DARKRED": COL_SLOW_DARKRED,
                        "COL_SLOW_BRIGHTRED": COL_SLOW_BRIGHTRED,
                        "COL_FAST_DARKBLUE": COL_FAST_DARKBLUE,
                        "COL_FAST_BRIGHTBLUE": COL_FAST_BRIGHTBLUE,
                        "COL_WHITE": COL_WHITE,
                    }
                    render_throttle_brake(throttle_brake_ctx, (x0, y0, w, h), dr)

                def _hud_delta() -> None:
                    delta_ctx = {
                        "hud_key": hud_key,
                        "fps": fps,
                        "i": i,
                        "iL": iL,
                        "iR": iR,
                        "mx": mx,
                        "_idx_to_x": _idx_to_x,
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
                    render_delta(delta_ctx, (x0, y0, w, h), dr)

                def _hud_steering() -> None:
                    steering_ctx = {
                        "hud_key": hud_key,
                        "i": i,
                        "iL": iL,
                        "iR": iR,
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
                        "_idx_to_x": _idx_to_x,
                        "_log_print": _log_print,
                        "_wrap_delta_05": _wrap_delta_05,
                        "slow_frame_to_lapdist": slow_frame_to_lapdist,
                        "log_file": log_file,
                        "COL_WHITE": COL_WHITE,
                        "COL_SLOW_DARKRED": COL_SLOW_DARKRED,
                        "COL_FAST_DARKBLUE": COL_FAST_DARKBLUE,
                    }
                    render_steering(steering_ctx, (x0, y0, w, h), dr)

                def _hud_speed() -> None:
                    pass

                def _hud_gear_rpm() -> None:
                    pass

                def _hud_line_delta() -> None:
                    line_delta_ctx = {
                        "hud_key": hud_key,
                    }
                    render_line_delta(line_delta_ctx, (x0, y0, w, h), dr)

                def _hud_under_oversteer() -> None:
                    under_oversteer_ctx = {
                        "hud_key": hud_key,
                    }
                    render_under_oversteer(under_oversteer_ctx, (x0, y0, w, h), dr)

                hud_renderers = {
                    "Speed": _hud_speed,
                    "Throttle / Brake": _hud_throttle_brake,
                    "Steering": _hud_steering,
                    "Delta": _hud_delta,
                    "Gear & RPM": _hud_gear_rpm,
                    "Line Delta": _hud_line_delta,
                    "Under-/Oversteer": _hud_under_oversteer,
                }
                fn_hud = hud_renderers.get(hud_key)
                if fn_hud is not None:
                    fn_hud()
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
