from __future__ import annotations

import math
import os
import subprocess
import io
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DecodeSpec:
    slow: Path
    fast: Path
    hud_fps: float = 0.0
    hud_stdin_raw: bool = False
    hud_size: tuple[int, int] | None = None
    hud_pix_fmt: str = "rgba"


@dataclass(frozen=True)
class FilterSpec:
    filter_complex: str
    video_map: str = "[vout]"
    audio_map: str | None = None


@dataclass(frozen=True)
class EncodeSpec:
    vcodec: str
    extra: list[str]
    pix_fmt: str = "yuv420p"
    fps: float = 0.0


@dataclass(frozen=True)
class Plan:
    cmd: list[str]
    filter_complex: str
    filter_script_path: Path | None = None


def _filter_args_for_ffmpeg(filter_complex: str, out_dir: Path) -> tuple[list[str], Path | None]:
    # Windows: CreateProcess/Commandline kann hart limitiert sein.
    # Deshalb: Filter relativ frueh in Datei auslagern.
    s = filter_complex or ""

    # Sehr konservativ auf Windows, damit WinError 206 nicht mehr vorkommt.
    if os.name == "nt":
        if len(s) < 2000:
            return ["-filter_complex", s], None
    else:
        if len(s) < 8000:
            return ["-filter_complex", s], None

    out_dir.mkdir(parents=True, exist_ok=True)
    fn = f"fc_{os.getpid()}.txt"
    p = out_dir / fn
    p.write_text(s, encoding="utf-8")
    return ["-filter_complex_script", str(p)], p


def build_plan(
    *,
    decode: DecodeSpec,
    flt: FilterSpec,
    enc: EncodeSpec,
    audio_source: str,
    outp: Path,
    debug_max_s: float = 0.0,
) -> Plan:
    filter_dir = outp.parent / "_tmp_filters"

    cmd: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-nostats",
        "-progress",
        "pipe:1",
    ]

    has_hud_input = bool(decode.hud_stdin_raw)

    # Optional: HUD als Input 0
    if decode.hud_stdin_raw:
        hud_r = decode.hud_fps if decode.hud_fps and decode.hud_fps > 0.1 else 0.0
        if hud_r > 0.1:
            cmd += ["-r", f"{hud_r}"]
        hud_size = decode.hud_size if isinstance(decode.hud_size, tuple) else None
        if not hud_size or int(hud_size[0]) <= 0 or int(hud_size[1]) <= 0:
            raise RuntimeError("HUD raw stdin input requires valid hud_size=(W,H).")
        cmd += [
            "-f",
            "rawvideo",
            "-pix_fmt",
            str(decode.hud_pix_fmt or "rgba"),
            "-s",
            f"{int(hud_size[0])}x{int(hud_size[1])}",
            "-i",
            "-",
        ]

    # Inputs 0/1 bleiben slow/fast
    cmd += [
        "-i",
        str(decode.slow),
        "-i",
        str(decode.fast),
    ]

    filter_args, filter_script_path = _filter_args_for_ffmpeg(flt.filter_complex, filter_dir)
    cmd += filter_args

    cmd += [
        "-map",
        flt.video_map,
    ]

    # Audio: entweder ueber Filtergraph (audio_map) oder direktes Mapping (alt)
    if flt.audio_map:
        cmd += ["-map", flt.audio_map]
    else:
        # Wenn HUD-Stream aktiv ist, ist Input 0 = HUD, Input 1 = slow, Input 2 = fast
        slow_ai = "1:a?" if has_hud_input else "0:a?"
        fast_ai = "2:a?" if has_hud_input else "1:a?"

        if audio_source == "slow":
            cmd += ["-map", slow_ai]
        elif audio_source == "fast":
            cmd += ["-map", fast_ai]

    cmd += [
        "-c:v",
        enc.vcodec,
        "-pix_fmt",
        enc.pix_fmt,
    ]

    cmd += list(enc.extra)

    if enc.fps and enc.fps > 0.1:
        cmd += ["-r", f"{enc.fps}"]

    cmd += [str(outp)]

    if debug_max_s > 0.0:
        # sicher: -t muss VOR den Inputs stehen (Input-Trim)
        # wir haengen es direkt nach "ffmpeg -hide_banner -y -nostats -progress pipe:1"
        try:
            i_idx = cmd.index("-i")
        except ValueError:
            i_idx = 0
        ins = max(0, i_idx)
        cmd[ins:ins] = ["-t", f"{debug_max_s}"]

    return Plan(cmd=cmd, filter_complex=flt.filter_complex, filter_script_path=filter_script_path)


def run_ffmpeg(
    plan: Plan,
    *,
    tail_n: int = 40,
    log_file: Path | None = None,
    live_stdout: bool = False,
    stdin_write_fn: Any | None = None,
) -> int:
    """
    Runs ffmpeg while:
      - always appending FULL ffmpeg output (stdout+stderr) to log_file (if provided)
      - printing ONLY -progress lines to stdout (so UI progress keeps working)
      - optionally printing full ffmpeg output live to stdout if live_stdout=True
      - on failure: prints last tail_n lines to stdout
    """
    def _append(line: str) -> None:
        if log_file is None:
            return
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line.rstrip("\n") + "\n")
        except Exception:
            pass

    def _is_progress_line(line: str) -> bool:
        s = (line or "").strip()
        return s.startswith(
            (
                "frame=",
                "fps=",
                "bitrate=",
                "total_size=",
                "out_time=",
                "out_time_us=",
                "out_time_ms=",
                "speed=",
                "progress=",
            )
        )

    use_stdin_writer = stdin_write_fn is not None
    tail: list[str] = []
    writer_error: Exception | None = None
    writer_thread: threading.Thread | None = None
    try:
        p = subprocess.Popen(
            plan.cmd,
            stdin=subprocess.PIPE if use_stdin_writer else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=not use_stdin_writer,
            bufsize=1 if not use_stdin_writer else 0,
        )
    except Exception as e:
        _append(f"[python] failed to start ffmpeg: {e}")
        return 1

    def _writer() -> None:
        nonlocal writer_error
        assert p.stdin is not None
        try:
            stdin_write_fn(p.stdin)
        except Exception as e:
            writer_error = e
        finally:
            try:
                p.stdin.close()
            except Exception:
                pass

    if use_stdin_writer:
        writer_thread = threading.Thread(target=_writer, daemon=True)
        writer_thread.start()

    assert p.stdout is not None
    if use_stdin_writer:
        out_stream = io.TextIOWrapper(p.stdout, encoding="utf-8", errors="replace")
        for raw in out_stream:
            line = (raw or "").rstrip("\n")
            _append(line)

            if live_stdout:
                print(line)
                continue

            if _is_progress_line(line):
                print(line, flush=True)

            if tail_n > 0:
                tail.append(line)
                if len(tail) > tail_n:
                    tail = tail[-tail_n:]
    else:
        for raw in p.stdout:
            line = (raw or "").rstrip("\n")
            _append(line)

            if live_stdout:
                print(line)
                continue

            if _is_progress_line(line):
                print(line, flush=True)

            if tail_n > 0:
                tail.append(line)
                if len(tail) > tail_n:
                    tail = tail[-tail_n:]

    rc = p.wait()
    if writer_thread is not None:
        writer_thread.join(timeout=30.0)
    if writer_error is not None:
        msg = f"[python] ffmpeg stdin writer failed: {writer_error}"
        _append(msg)
        print(msg)
        if rc == 0:
            rc = 1
    if rc != 0 and not live_stdout and tail_n > 0:
        print(f"[ffmpeg-tail] last {min(tail_n, len(tail))} lines:")
        for t in tail[-tail_n:]:
            print(t)
    return int(rc)


def _view_defaults() -> dict[str, Any]:
    return {"zoom": 1.0, "off_x": 0, "off_y": 0, "fit_to_height": True}


def _view_get(d: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(d, dict):
        return _view_defaults()
    out = _view_defaults()
    for k in out.keys():
        if k in d:
            out[k] = d.get(k)
    return out


def _num(v: Any, default: float) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _int(v: Any, default: int) -> int:
    try:
        if v is None or str(v).strip() == "":
            return default
        return int(float(v))
    except Exception:
        return default


def _bool(v: Any, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


def _build_side_chain_core(input_ref: str, target_w: int, target_h: int, r: int, view: dict[str, Any], label_out: str) -> str:
    zoom = _num(view.get("zoom"), 1.0)
    if zoom < 1.0:
        zoom = 1.0
    off_x = -_int(view.get("off_x"), 0)
    off_y = -_int(view.get("off_y"), 0)
    fit_to_height = _bool(view.get("fit_to_height"), True)

    chain = f"[{input_ref}]fps={r},setpts=PTS-STARTPTS"

    if fit_to_height:
        chain += f",scale=-2:{target_h}"
    else:
        chain += f",scale=-2:{target_h}"

    if abs(zoom - 1.0) > 1e-6:
        chain += f",scale=iw*{zoom}:ih*{zoom}"

    x_expr = f"max(0\\,min(iw-{target_w}\\,(iw-{target_w})/2+{off_x}))"
    y_expr = f"max(0\\,min(ih-{target_h}\\,(ih-{target_h})/2+{off_y}))"
    chain += f",crop={target_w}:{target_h}:{x_expr}:{y_expr}[{label_out}]"

    return chain


def _build_side_chain(input_idx: int, target_w: int, target_h: int, r: int, view: dict[str, Any], label_out: str) -> str:
    return _build_side_chain_core(f"{input_idx}:v", target_w, target_h, r, view, label_out)


def _build_side_chain_from_label(in_label: str, target_w: int, target_h: int, r: int, view: dict[str, Any], label_out: str) -> str:
    return _build_side_chain_core(in_label, target_w, target_h, r, view, label_out)


def _hud_drawboxes_chain(
    geom: Any,
    hud_enabled: Any | None,
    hud_boxes: Any | None,
) -> str:
    """
    Liefert eine Filter-Chain wie:
      ,drawbox=...,drawbox=...
    oder "" wenn nichts aktiv ist.

    Koordinaten in hud_boxes sind relativ zum HUD-Bereich (0,0 = oben links im HUD).
    """
    hud_dbg = (os.environ.get("IRVC_HUD_DEBUG") or "0").strip().lower() in ("1", "true", "yes", "on")

    if not hud_enabled or not hud_boxes:
        if hud_dbg:
            print(f"[hud] disabled_or_empty: hud_enabled={type(hud_enabled).__name__} hud_boxes={type(hud_boxes).__name__}")
        return ""

    enabled_names: set[str] = set()
    try:
        if isinstance(hud_enabled, dict):
            for k, v in hud_enabled.items():
                if bool(v):
                    enabled_names.add(str(k))
        elif isinstance(hud_enabled, list):
            for k in hud_enabled:
                enabled_names.add(str(k))
    except Exception:
        if hud_dbg:
            print("[hud] hud_enabled parse failed")
        return ""

    if not enabled_names:
        if hud_dbg:
            print("[hud] enabled_names empty")
        return ""

    # HUD beginnt im Output bei x = geom.left_w (Mitte)
    hud_x0 = int(getattr(geom, "left_w"))

    # ui_app.py kann hud_boxes in zwei Varianten liefern:
    # A) relativ zum HUD-Bereich (x beginnt bei 0)
    # B) bereits absolut im Output (x bereits inkl. hud_x0)
    #
    # Wir normalisieren erst die Boxes und entscheiden dann automatisch.
    norm_boxes: list[dict[str, Any]] = []
    try:
        if isinstance(hud_boxes, dict):
            for k, v in hud_boxes.items():
                if not isinstance(v, dict):
                    continue
                norm_boxes.append(
                    {
                        "name": str(k),
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

    # Auto-Detect: wenn x schon "gross" ist, sind es absolute Output-Koordinaten.
    # Heuristik: max_x > (geom.hud + 10) => absolut
    max_x = 0
    try:
        for b in norm_boxes:
            if isinstance(b, dict):
                max_x = max(max_x, int(round(float(b.get("x", 0) or 0))))
    except Exception:
        max_x = 0

    boxes_are_absolute = False
    try:
        boxes_are_absolute = max_x > (int(getattr(geom, "hud")) + 10)
    except Exception:
        boxes_are_absolute = False

    if hud_dbg:
        print(f"[hud] enabled={sorted(enabled_names)}")
        print(f"[hud] boxes_norm_count={len(norm_boxes)} hud_x0={hud_x0} abs={boxes_are_absolute}")

    chain_parts: list[str] = []
    drawn = 0

    try:
        for b in norm_boxes:
            if not isinstance(b, dict):
                continue
            name = str(b.get("name") or b.get("id") or "")
            if not name:
                continue
            if name not in enabled_names:
                continue

            x = int(round(float(b.get("x", 0))))
            y = int(round(float(b.get("y", 0))))
            w = int(round(float(b.get("w", 0))))
            h = int(round(float(b.get("h", 0))))

            if w <= 0 or h <= 0:
                if hud_dbg:
                    print(f"[hud] skip {name}: w/h invalid w={w} h={h}")
                continue

            if boxes_are_absolute:
                x_abs = max(0, x)
            else:
                x_abs = hud_x0 + max(0, x)

            y_abs = max(0, y)

            if hud_dbg:
                print(f"[hud] draw {name}: rel=({x},{y},{w},{h}) abs=({x_abs},{y_abs},{w},{h})")

            # Keep only the border; filled white boxes create an unintended milky veil.
            chain_parts.append(f"drawbox=x={x_abs}:y={y_abs}:w={w}:h={h}:color=white@0.80:t=2")
            drawn += 1
    except Exception:
        if hud_dbg:
            print("[hud] draw loop failed")
        return ""

    if not chain_parts:
        if hud_dbg:
            print("[hud] no boxes drawn -> chain empty")
        return ""

    if hud_dbg:
        print(f"[hud] drawn_boxes={drawn}")

    return "," + ",".join(chain_parts)


def build_split_filter_from_geometry(
    geom: Any,
    fps: float,
    view_L: dict[str, Any] | None,
    view_R: dict[str, Any] | None,
    hud_enabled: Any | None = None,
    hud_boxes: Any | None = None,
) -> str:
    H = geom.H
    r = int(round(fps)) if fps and fps > 0.1 else 30

    vL = _view_get(view_L)
    vR = _view_get(view_R)

    left_chain = _build_side_chain(0, geom.left_w, H, r, vL, "vslow")
    right_chain = _build_side_chain(1, geom.right_w, H, r, vR, "vfast")

    hud_chain = _hud_drawboxes_chain(geom=geom, hud_enabled=hud_enabled, hud_boxes=hud_boxes)

    filt = (
        f"{left_chain};"
        f"{right_chain};"
        f"color=c=black:s={geom.W}x{geom.H}:r={r}[base];"
        f"[base][vslow]overlay=x={geom.left_x}:y={geom.left_y}:shortest=1[tmp];"
        f"[tmp][vfast]overlay=x={geom.fast_out_x}:y={geom.fast_out_y}:shortest=1{hud_chain}, fps={r}[vout]"
    )
    return filt


def _percentile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    ys = sorted(float(v) for v in xs)
    if q <= 0.0:
        return ys[0]
    if q >= 1.0:
        return ys[-1]
    k = (len(ys) - 1) * q
    i = int(math.floor(k))
    j = int(math.ceil(k))
    if j <= i:
        return ys[i]
    a = ys[i]
    b = ys[j]
    t = k - float(i)
    return a * (1.0 - t) + b * t


def _clamp(x: float, lo: float, hi: float) -> float:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def build_stream_sync_filter(
    geom: Any,
    fps: float,
    view_L: dict[str, Any] | None,
    view_R: dict[str, Any] | None,
    fast_time_s: list[float],
    speed_diff: list[float] | None,
    cut_i0: int,
    cut_i1: int,
    audio_source: str,
    hud_enabled: Any | None = None,
    log_file: "Path | None" = None,
    hud_boxes: Any | None = None,
    hud_cmd_file: "Path | None" = None,
    hud_input_label: str | None = None,  # z.B. "[hudin]"
) -> tuple[str, str | None]:
    # Ein ffmpeg-Run:
    # - Slow wird auf [cut] getrimmt
    # - Fast wird in Segmente getrimmt und pro Segment zeitlich gestreckt/gestaucht
    # - Danach normaler Split-Render (crop/overlay)
    W = geom.W
    H = geom.H
    r = int(round(fps)) if fps and fps > 0.1 else 30

    vL = _view_get(view_L)
    vR = _view_get(view_R)

    # Default (wie bisher)
    try:
        k_base = int((os.environ.get("SYNC6_K_FRAMES") or "").strip() or "30")
    except Exception:
        k_base = 30
    if k_base < 5:
        k_base = 5

    # Dynamik (default AN)
    dyn_on = (os.environ.get("SYNC6_DYNAMIC") or "1").strip().lower() not in ("0", "false", "no", "off")

    try:
        k_min = int((os.environ.get("SYNC6_K_MIN") or "").strip() or "15")
    except Exception:
        k_min = 15
    try:
        k_max = int((os.environ.get("SYNC6_K_MAX") or "").strip() or "30")
    except Exception:
        k_max = 30
    if k_min < 5:
        k_min = 5
    if k_max < k_min:
        k_max = k_min

    try:
        score_low = float((os.environ.get("SYNC6_SCORE_LOW") or "").strip() or "0.30")
    except Exception:
        score_low = 0.30
    try:
        score_high = float((os.environ.get("SYNC6_SCORE_HIGH") or "").strip() or "0.70")
    except Exception:
        score_high = 0.70
    score_low = _clamp(score_low, 0.0, 1.0)
    score_high = _clamp(score_high, 0.0, 1.0)
    if score_high < score_low + 0.05:
        score_high = min(1.0, score_low + 0.05)

    try:
        max_segs = int((os.environ.get("SYNC6_MAX_SEGS") or "").strip() or "800")
    except Exception:
        max_segs = 800
    if max_segs < 50:
        max_segs = 50

    ts0 = float(cut_i0) / float(r)
    ts1 = float(cut_i1) / float(r)

    # Keyframes/Indices bauen
    idxs: list[int] = []

    if not dyn_on:
        i = cut_i0
        while i < cut_i1:
            idxs.append(i)
            i += k_base
        idxs.append(cut_i1)
        print(f"[sync6] segments=fixed k_frames={k_base} segs={max(0, len(idxs)-1)}")
    else:
        # 1) Kruemmung des Mappings: d2 aus fast_time_s
        # d1[i] = tf[i] - tf[i-1]
        # d2[i] = abs(d1[i] - d1[i-1])
        n = len(fast_time_s)
        d2: list[float] = [0.0] * n
        if n >= 3:
            prev_d1 = float(fast_time_s[1]) - float(fast_time_s[0])
            for i in range(2, n):
                d1 = float(fast_time_s[i]) - float(fast_time_s[i - 1])
                d2[i] = abs(d1 - prev_d1)
                prev_d1 = d1

        # nur Cut-Bereich
        d2_cut = [d2[i] for i in range(cut_i0, cut_i1 + 1) if 0 <= i < n]
        d2_p95 = _percentile(d2_cut, 0.95)
        if d2_p95 <= 1e-12:
            d2_p95 = 1e-12

        # 2) Speed-Diff normalisieren (optional)
        sp_p95 = 0.0
        if speed_diff:
            sp_cut = [float(speed_diff[i]) for i in range(cut_i0, cut_i1 + 1) if 0 <= i < len(speed_diff)]
            sp_p95 = _percentile(sp_cut, 0.95)
            if sp_p95 <= 1e-12:
                sp_p95 = 1e-12

        # 3) Score je Frame (0..1)
        # Gewichtung: Kruemmung ist wichtiger als Speed-Diff
        w_d2 = 0.65
        w_sp = 0.35

        def _score_at(i: int) -> float:
            s1 = _clamp(float(d2[i]) / float(d2_p95), 0.0, 1.0) if 0 <= i < len(d2) else 0.0
            if speed_diff and 0 <= i < len(speed_diff) and sp_p95 > 0.0:
                s2 = _clamp(float(speed_diff[i]) / float(sp_p95), 0.0, 1.0)
            else:
                s2 = 0.0
            return _clamp(w_d2 * s1 + w_sp * s2, 0.0, 1.0)

        # 4) Schritt aus Score
        def _step_from_score(s: float) -> int:
            if s >= score_high:
                return int(k_min)
            if s <= score_low:
                return int(k_max)
            # linear zwischen k_max (low) und k_min (high)
            t = (s - score_low) / (score_high - score_low)
            val = float(k_max) + (float(k_min) - float(k_max)) * t
            return int(round(val))

        # 5) Indices bauen + Segment-Cap
        def _build_idxs(kmin: int, kmax: int) -> list[int]:
            out: list[int] = []
            i2 = cut_i0
            while i2 < cut_i1:
                out.append(i2)
                st = _step_from_score(_score_at(i2))
                if st < kmin:
                    st = kmin
                if st > kmax:
                    st = kmax
                if st < 1:
                    st = 1
                i2 += st
            out.append(cut_i1)
            return out

        idxs = _build_idxs(k_min, k_max)
        segs = max(0, len(idxs) - 1)

        if segs > max_segs:
            # Wenn zu viele Segmente: Schritte proportional vergroessern (einmalig)
            factor = float(segs) / float(max_segs)
            k_min2 = max(5, int(round(float(k_min) * factor)))
            k_max2 = max(k_min2, int(round(float(k_max) * factor)))
            idxs = _build_idxs(k_min2, k_max2)
            segs2 = max(0, len(idxs) - 1)
            print(
                f"[sync6] segments=dynamic segs={segs} > max_segs={max_segs} -> scaled k_min={k_min2} k_max={k_max2} segs={segs2}"
            )
        else:
            print(
                f"[sync6] segments=dynamic k_base={k_base} k_min={k_min} k_max={k_max} score_low={score_low:.2f} score_high={score_high:.2f} segs={segs}"
            )

    seg_fast_labels: list[str] = []
    parts: list[str] = []

    # Input-Index verschiebt sich um 1, wenn HUD als Input 0 existiert.
    v_slow_in = "1:v" if hud_input_label else "0:v"
    v_fast_in = "2:v" if hud_input_label else "1:v"

    parts.append(f"[{v_slow_in}]trim=start={ts0}:end={ts1},setpts=PTS-STARTPTS[slowcut]")

    # Fast Video als Segmente + Warp
    eps_t = 1e-6
    for si in range(len(idxs) - 1):
        a = idxs[si]
        b = idxs[si + 1]
        if b <= a:
            continue

        seg_ts0 = float(a) / float(r)
        seg_ts1 = float(b) / float(r)
        slow_dur = max(eps_t, seg_ts1 - seg_ts0)

        tf0 = float(fast_time_s[a])
        tf1 = float(fast_time_s[b])

        # Sicherstellen, dass Fast-Zeit steigt
        if tf1 <= tf0 + eps_t:
            tf1 = tf0 + eps_t

        fast_dur = max(eps_t, tf1 - tf0)
        factor = slow_dur / fast_dur

        lab = f"fseg{si}"
        seg_fast_labels.append(f"[{lab}]")

        parts.append(
            f"[{v_fast_in}]trim=start={tf0}:end={tf1},setpts=PTS-STARTPTS,setpts=PTS*{factor}[{lab}]"
        )
    if not seg_fast_labels:
        raise RuntimeError("sync: keine Fast-Segmente gebaut.")

    parts.append(f"{''.join(seg_fast_labels)}concat=n={len(seg_fast_labels)}:v=1:a=0[fastsync]")

    # Side-Chains auf Basis der geschnittenen Streams
    left_chain = _build_side_chain_from_label("slowcut", geom.left_w, H, r, vL, "vslow")
    right_chain = _build_side_chain_from_label("fastsync", geom.right_w, H, r, vR, "vfast")

    parts.append(f"{left_chain}")
    parts.append(f"{right_chain}")

    parts.append(f"color=c=black:s={W}x{H}:r={r}[base]")

    hud_chain = _hud_drawboxes_chain(geom=geom, hud_enabled=hud_enabled, hud_boxes=hud_boxes)

    parts.append(f"[base][vslow]overlay=0:0:shortest=1[tmp0]")

    # HUD (Python rawvideo/rgba Stream) als Overlay in die Mitte
    if hud_input_label:
        # HUD-Input ist geom.hud x geom.H mit Alpha.
        parts.append(f"{hud_input_label}format=rgba[hudrgba]")
        parts.append(f"[tmp0][hudrgba]overlay={geom.left_w}:0:shortest=1[tmp]")
    else:
        parts.append(f"[tmp0]copy[tmp]")

    # Danach Fast overlay + danach die HUD-Rahmen (Story 1) als drawbox
    parts.append(
        f"[tmp][vfast]overlay={geom.fast_out_x}:0:shortest=1{hud_chain}[vpre]"
    )

    parts.append(f"[vpre]fps={r}[vout]")

    audio_map = None

    # Audio-Input-Index haengt davon ab, ob HUD als Input 0 existiert
    a_slow_in = "1:a" if hud_input_label else "0:a"
    a_fast_in = "2:a" if hud_input_label else "1:a"

    if audio_source == "slow":
        parts.append(f"[{a_slow_in}]atrim=start={ts0}:end={ts1},asetpts=PTS-STARTPTS[aout]")
        audio_map = "[aout]"
    elif audio_source == "fast":
        parts.append(
            f"[{a_fast_in}]atrim=start={float(fast_time_s[cut_i0])}:end={float(fast_time_s[cut_i1])},asetpts=PTS-STARTPTS[aout]"
        )
        audio_map = "[aout]"
    else:
        audio_map = None

    return ";".join(parts), audio_map

