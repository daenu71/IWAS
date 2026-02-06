from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from huds.speed import render_speed

# ---------------------------------------------------------------------------
# Global HUD colors (RGBA) – shared by all HUDs
# ---------------------------------------------------------------------------
COL_SLOW_DARKRED = (234, 0, 0, 255)          # dunkel rot
COL_SLOW_BRIGHTRED = (255, 137, 117, 255)    # hell rot
COL_FAST_DARKBLUE = (36, 0, 250, 255)        # dunkel blau
COL_FAST_BRIGHTBLUE = (1, 253, 255, 255)     # hell blau (cyan)
COL_WHITE = (255, 255, 255, 255)             # weiss



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


def _ffmpeg_has_encoder(encoder_name: str) -> bool:
    try:
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
        )
        if p.returncode != 0:
            return False
        return encoder_name in (p.stdout or "")
    except Exception:
        return False


@dataclass(frozen=True)
class DecodeSpec:
    slow: Path
    fast: Path
    hud_seq: str | None = None  # ffmpeg image2 sequence pattern, z.B. "...\hud_%06d.png"
    hud_fps: float = 0.0


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
class FfmpegPipeline:
    decode: DecodeSpec
    flt: FilterSpec
    enc: EncodeSpec
    audio_source: str  # "slow" | "fast" | "none"
    outp: Path

    def build_cmd(self) -> list[str]:
        filter_dir = self.outp.parent / "_tmp_filters"

        cmd: list[str] = [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-nostats",
            "-progress",
            "pipe:1",
        ]

        # Optional: HUD als 3. Input (PNG-Sequenz)
        if self.decode.hud_seq:
            hud_r = self.decode.hud_fps if self.decode.hud_fps and self.decode.hud_fps > 0.1 else 0.0
            if hud_r > 0.1:
                cmd += ["-framerate", f"{hud_r}"]
            cmd += ["-i", str(self.decode.hud_seq)]

        # Inputs 0/1 bleiben slow/fast
        cmd += [
            "-i",
            str(self.decode.slow),
            "-i",
            str(self.decode.fast),
        ]

        cmd += _filter_args_for_ffmpeg(self.flt.filter_complex, filter_dir)

        cmd += [
            "-map",
            self.flt.video_map,
        ]

        # Audio: entweder über Filtergraph (audio_map) oder direktes Mapping (alt)
        if self.flt.audio_map:
            cmd += ["-map", self.flt.audio_map]
        else:
            # Wenn HUD-Seq aktiv ist, ist Input 0 = HUD, Input 1 = slow, Input 2 = fast
            slow_ai = "1:a?" if self.decode.hud_seq else "0:a?"
            fast_ai = "2:a?" if self.decode.hud_seq else "1:a?"

            if self.audio_source == "slow":
                cmd += ["-map", slow_ai]
            elif self.audio_source == "fast":
                cmd += ["-map", fast_ai]

        cmd += [
            "-c:v",
            self.enc.vcodec,
            "-pix_fmt",
            self.enc.pix_fmt,
        ]

        cmd += list(self.enc.extra)

        if self.enc.fps and self.enc.fps > 0.1:
            cmd += ["-r", f"{self.enc.fps}"]

        cmd += [str(self.outp)]
        return cmd


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


def _build_side_chain(input_idx: int, target_w: int, target_h: int, r: int, view: dict[str, Any], label_out: str) -> str:
    zoom = _num(view.get("zoom"), 1.0)
    if zoom < 1.0:
        zoom = 1.0
    off_x = -_int(view.get("off_x"), 0)
    off_y = -_int(view.get("off_y"), 0)
    fit_to_height = _bool(view.get("fit_to_height"), True)

    chain = f"[{input_idx}:v]fps={r},setpts=PTS-STARTPTS"

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


def _build_side_chain_from_label(in_label: str, target_w: int, target_h: int, r: int, view: dict[str, Any], label_out: str) -> str:
    zoom = _num(view.get("zoom"), 1.0)
    if zoom < 1.0:
        zoom = 1.0
    off_x = -_int(view.get("off_x"), 0)
    off_y = -_int(view.get("off_y"), 0)
    fit_to_height = _bool(view.get("fit_to_height"), True)

    chain = f"[{in_label}]fps={r},setpts=PTS-STARTPTS"

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

def _hud_drawboxes_chain(
    geom: "OutputGeometry",
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

            chain_parts.append(f"drawbox=x={x_abs}:y={y_abs}:w={w}:h={h}:color=white@0.20:t=fill")
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


def _build_split_filter_from_geometry(
    geom: OutputGeometry,
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


def _choose_encoder(W: int, fps: float) -> list[EncodeSpec]:
    # Reihenfolge ist verbindlich:
    # 1) NVIDIA GPU (NVENC), 2) Intel GPU (QSV), 3) AMD GPU (AMF), 4) CPU (libx264)

    has_hevc_nvenc = _ffmpeg_has_encoder("hevc_nvenc")
    has_h264_nvenc = _ffmpeg_has_encoder("h264_nvenc")

    has_hevc_qsv = _ffmpeg_has_encoder("hevc_qsv")
    has_h264_qsv = _ffmpeg_has_encoder("h264_qsv")

    has_hevc_amf = _ffmpeg_has_encoder("hevc_amf")
    has_h264_amf = _ffmpeg_has_encoder("h264_amf")

    specs: list[EncodeSpec] = []

    # --- NVIDIA (NVENC) ---
    if has_h264_nvenc and (W <= 4096):
        specs.append(
            EncodeSpec(
                vcodec="h264_nvenc",
                extra=[
                    "-preset",
                    "p5",
                    "-cq:v",
                    "19",
                ],
                fps=fps,
            )
        )

    if has_hevc_nvenc:
        specs.append(
            EncodeSpec(
                vcodec="hevc_nvenc",
                extra=[
                    "-preset",
                    "p5",
                    "-cq:v",
                    "23",
                    "-tag:v",
                    "hvc1",
                ],
                fps=fps,
            )
        )

    # --- Intel (QSV) ---
    if has_h264_qsv and (W <= 4096):
        specs.append(
            EncodeSpec(
                vcodec="h264_qsv",
                extra=[
                    "-global_quality",
                    "23",
                ],
                fps=fps,
            )
        )

    if has_hevc_qsv:
        specs.append(
            EncodeSpec(
                vcodec="hevc_qsv",
                extra=[
                    "-global_quality",
                    "26",
                    "-tag:v",
                    "hvc1",
                ],
                fps=fps,
            )
        )

    # --- AMD (AMF) ---
    if has_h264_amf and (W <= 4096):
        specs.append(
            EncodeSpec(
                vcodec="h264_amf",
                extra=[
                    "-quality",
                    "balanced",
                    "-rc",
                    "cqp",
                    "-qp_i",
                    "20",
                    "-qp_p",
                    "20",
                    "-qp_b",
                    "22",
                ],
                fps=fps,
            )
        )

    if has_hevc_amf:
        specs.append(
            EncodeSpec(
                vcodec="hevc_amf",
                extra=[
                    "-quality",
                    "balanced",
                    "-rc",
                    "cqp",
                    "-qp_i",
                    "24",
                    "-qp_p",
                    "24",
                    "-qp_b",
                    "26",
                    "-tag:v",
                    "hvc1",
                ],
                fps=fps,
            )
        )

    # --- CPU (Fallback) ---
    specs.append(
        EncodeSpec(
            vcodec="libx264",
            extra=[
                "-preset",
                "veryfast",
                "-crf",
                "18",
            ],
            fps=fps,
        )
    )

    return specs


def _run(cmd: list[str]) -> None:
    p = subprocess.run(cmd)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (rc={p.returncode})")


def _tail_lines(s: str, n: int = 20) -> str:
    lines = (s or "").splitlines()
    tail = lines[-n:] if len(lines) > n else lines
    return "\n".join(tail).strip()


def _filter_args_for_ffmpeg(filter_complex: str, out_dir: Path) -> list[str]:
    # Windows: CreateProcess/Commandline kann hart limitiert sein.
    # Deshalb: Filter relativ früh in Datei auslagern.
    s = filter_complex or ""

    # Sehr konservativ auf Windows, damit WinError 206 nicht mehr vorkommt.
    if os.name == "nt":
        if len(s) < 2000:
            return ["-filter_complex", s]
    else:
        if len(s) < 8000:
            return ["-filter_complex", s]

    out_dir.mkdir(parents=True, exist_ok=True)
    fn = f"fc_{os.getpid()}.txt"
    p = out_dir / fn
    p.write_text(s, encoding="utf-8")
    return ["-filter_complex_script", str(p)]

def _log_print(msg: str, log_file: Path | None) -> None:
    # Immer in die Konsole UND wenn möglich ins Log schreiben.
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

def _run_live_or_tail_on_fail(
    cmd: list[str],
    *,
    tail_n: int = 40,
    log_file: Path | None = None,
    live_stdout: bool = False,
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

    tail: list[str] = []
    try:
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except Exception as e:
        _append(f"[python] failed to start ffmpeg: {e}")
        return 1

    assert p.stdout is not None
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
    if rc != 0 and not live_stdout and tail_n > 0:
        print(f"[ffmpeg-tail] last {min(tail_n, len(tail))} lines:")
        for t in tail[-tail_n:]:
            print(t)
    return int(rc)

    # stdout selten wichtig; stderr ist ffmpeg-Log
    if p.stderr is not None:
        for raw in p.stderr:
            line = (raw or "").rstrip("\n")
            if not line:
                continue
            tail.append(line)
            _append(line)
            if live_stdout:
                print(line)

    rc = p.wait()

    if rc != 0:
        msg = f"[ffmpeg] FAIL rc={rc} -> tail({tail_n})"
        _append(msg)
        if live_stdout:
            print(msg)
        for t in list(tail):
            _append(t)
            if live_stdout:
                print(t)

    return int(rc)
    
    

def _safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


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
    
def _csv_time_axis_or_fallback(run, duration_s: float) -> list[float]:
    from csv_g61 import get_float_col, has_col
    if has_col(run, "Time_s"):
        return get_float_col(run, "Time_s")
    n = int(getattr(run, "row_count", 0) or 0)
    if n < 2:
        raise RuntimeError("CSV hat zu wenig Zeilen für Fallback-Zeitachse.")
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
    # Wir prüfen aber in m/s, weil Speed-Quelle m/s ist.
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
    # Neu: zusätzlich Fast-Zeit pro Slow-Frame (für Stream-Sync / Segment-Warp)
    # Neu: optional Speed-Differenz pro Slow-Frame (für dynamische Segmentierung)
    from csv_g61 import get_float_col, has_col, load_g61_csv

    run_s = load_g61_csv(slow_csv)
    run_f = load_g61_csv(fast_csv)

    ld_s = get_float_col(run_s, "LapDistPct")
    ld_f = get_float_col(run_f, "LapDistPct")

    has_speed_s = has_col(run_s, "Speed")
    has_speed_f = has_col(run_f, "Speed")
    sp_s = get_float_col(run_s, "Speed") if has_speed_s else []
    sp_f = get_float_col(run_f, "Speed") if has_speed_f else []

    def _time_axis_or_fallback(run, duration_s: float) -> list[float]:
        if has_col(run, "Time_s"):
            return get_float_col(run, "Time_s")
        n = int(getattr(run, "row_count", 0) or 0)
        if n < 2:
            raise RuntimeError("CSV hat zu wenig Zeilen für Fallback-Zeitachse.")
        dt = float(duration_s) / float(n - 1)
        out: list[float] = []
        for i in range(n):
            out.append(float(i) * dt)
        return out

    t_s = _time_axis_or_fallback(run_s, slow_duration_s)
    t_f = _time_axis_or_fallback(run_f, fast_duration_s)

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
        raise RuntimeError("CSV hat zu wenige Samples für Sync.")

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
    # Wir schneiden auf den Bereich, wo Fast-Zeit gültig ist.
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

def _ffmpeg_escape_path(p: "Path") -> str:
    # Für Filter-Strings: Backslashes -> '/', Quotes escapen.
    # Wichtig (Windows): der Doppelpunkt muss als "\:" erscheinen -> "C\:/..."
    s = str(p).replace("\\", "/")
    s = s.replace("'", "\\'")
    s = s.replace(":", "\\:")
    return s

def _render_hud_scroll_frames_png(
    out_dir: Path,
    *,
    fps: float,
    cut_i0: int,
    cut_i1: int,
    geom: OutputGeometry,
    hud_enabled: Any | None,
    hud_boxes: Any | None,
    slow_frame_to_lapdist: list[float],
    hud_gear_rpm_update_hz: int = 60,
    slow_frame_to_fast_time_s: list[float] | None = None,
    slow_to_fast_frame: list[int] | None = None,
    hud_curve_points_default: int = 180,
    hud_curve_points_overrides: Any | None = None,
    slow_speed_frames: list[float] | None = None,
    fast_speed_frames: list[float] | None = None,
    slow_min_speed_frames: list[float] | None = None,
    fast_min_speed_frames: list[float] | None = None,
    slow_gear_frames: list[int] | None = None,
    fast_gear_frames: list[int] | None = None,
    slow_rpm_frames: list[float] | None = None,
    fast_rpm_frames: list[float] | None = None,
    slow_steer_frames: list[float] | None = None,
    fast_steer_frames: list[float] | None = None,
    before_s: float = 10.0,
    after_s: float = 10.0,
    slow_throttle_frames: list[float] | None = None,
    fast_throttle_frames: list[float] | None = None,
    slow_brake_frames: list[float] | None = None,
    fast_brake_frames: list[float] | None = None,
    slow_abs_frames: list[int] | None = None,
    fast_abs_frames: list[int] | None = None,
    hud_speed_units: str = "kmh",
    hud_speed_update_hz: int = 60,
    hud_name: str | None = None,
    hud_windows: Any | None = None,
    log_file: Path | None = None,
) -> str | None:
    """
    Rendert pro Frame eine PNG (transparent) für die HUD-Spalte (geom.hud x geom.H).
    Gibt ffmpeg-Pattern zurück: ".../hud_%06d.png" oder None.
    """
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
        _log_print(f"[hudpy] Zielordner wäre: {out_dir}", log_file)
        return None

    if geom.hud <= 0:
        _log_print("[hudpy] geom.hud <= 0 -> kein HUD möglich", log_file)
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
        _log_print("[hudpy] keine gültige Scroll-HUD-Box (w/h/x/y) gefunden", log_file)
        return None

    # Parameter (Story 2): Default aus INI + Overrides pro HUD
    default_before_s = max(1e-6, float(before_s))
    default_after_s = max(1e-6, float(after_s))

    # Optional: ENV überschreibt (Debug) -> gilt dann für ALLE HUDs
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

    # Für sauberen Test: alte Frames löschen
    try:
        for p in out_dir.glob("hud_*.png"):
            p.unlink()
    except Exception:
        pass
        
    # Extra: Sample-Ordner (1 Bild pro Sekunde) zum schnellen Prüfen
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
        
    # Story 3: Steering Skalierung (Y = -1..+1) über max Ausschlag (beide CSVs)
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
        # 720° ist absichtlich gross, aber verhindert kaputte Extremwerte.
        steer_abs_max = float(_clamp(float(m), 1e-6, 720.0))

        if steer_abs_raw > 720.0:
            _log_print(f"[hudpy] SteeringWheelAngle: raw_abs_max={steer_abs_raw:.3f} capped_to={steer_abs_max:.3f}", log_file)
        else:
            _log_print(f"[hudpy] SteeringWheelAngle: abs_max={steer_abs_max:.3f}", log_file)

    except Exception:
        steer_abs_max = 1.0
        
    # Steering: CSV (60 Hz) auf Video-Frame-Index abbilden
    
    # Story 7: Delta (Time delta) – globale Y-Skalierung: min/max Delta über alle Frames (Cut-Bereich)
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

    # Story 7: Delta (Time delta) – Y-Skalierung vorberechnen im Cut-Bereich
    # Delta = slow_time - fast_time, basierend auf der Sync-Map slow_frame_to_fast_time_s
    delta_pos_max = 0.0   # größtes positives Delta (slow langsamer)
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
                    xL = int(x0 + 6)
                    xR = int(x0 + (w // 2) + 6)
                    y1 = int(y0 + 6)
                    y2 = int(y0 + 26)

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
                        if hud_key == "Gear & RPM":
                            if slow_gear_h and i < len(slow_gear_h) and fast_gear_h and fi < len(fast_gear_h):
                                sg = int(slow_gear_h[i])
                                fg = int(fast_gear_h[fi])

                                sr = 0
                                fr = 0
                                if slow_rpm_h and i < len(slow_rpm_h):
                                    sr = int(slow_rpm_h[i])
                                if fast_rpm_h and fi < len(fast_rpm_h):
                                    fr = int(fast_rpm_h[fi])

                                # Fonts (ähnlich wie Throttle / Brake)
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
                                            try:
                                                return ImageFont.truetype("DejaVuSans.ttf", sz)
                                            except Exception:
                                                return None

                                    font_title = _load_font(18)
                                    font_val = _load_font(22)

                                    # Titel
                                    y_title = int(y0 + 6)
                                    dr.text((xL, y_title), "Gear / RPM", fill=COL_SLOW_DARKRED, font=font_title)
                                    dr.text((xR, y_title), "Gear / RPM", fill=COL_FAST_DARKBLUE, font=font_title)

                                    # Werte
                                    y_val = int(y0 + 30)
                                    dr.text((xL, y_val), f"{sg} / {sr} rpm", fill=COL_SLOW_DARKRED, font=font_val)
                                    dr.text((xR, y_val), f"{fg} / {fr} rpm", fill=COL_FAST_DARKBLUE, font=font_val)

                                except Exception:
                                    # Fallback ohne Fonts
                                    dr.text((xL, y1), "Gear / RPM", fill=COL_SLOW_DARKRED)
                                    dr.text((xR, y1), "Gear / RPM", fill=COL_FAST_DARKBLUE)
                                    dr.text((xL, y2), f"{sg} / {sr} rpm", fill=COL_SLOW_DARKRED)
                                    dr.text((xR, y2), f"{fg} / {fr} rpm", fill=COL_FAST_DARKBLUE)

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

                # Optional: ENV überschreibt (Debug) -> gilt dann für ALLE HUDs
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
    
                            # Titel + Werte auf gleicher Höhe (ruhig, kein Springen)
                            y_txt = int(y0 + 2)
                            dr.text((int(x0 + 4), y_txt), "Throttle / Brake", fill=COL_WHITE, font=font_title)
    
                            # Skalen (CSV ~60Hz -> Video-Frames)
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
    
                            # Aktuelle Werte (am Marker)
                            mx0 = int(x0 + (w // 2))
                            gap = 12
    
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
    
                            # Fast links, Slow rechts (wie Steering)
                            dr.text((f_x, y_txt), f_txt, fill=COL_SLOW_BRAKE, font=font_val)
                            dr.text((s_x, y_txt), s_txt, fill=COL_FAST_BRAKE, font=font_val)
    
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
    
                            # Marker-zentriertes Sampling (symmetrisch links/rechts) + Endpunkte erzwingen
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
    
                            # Kurven sammeln
                            pts_s_t: list[tuple[int, int]] = []
                            pts_s_b: list[tuple[int, int]] = []
                            pts_f_t: list[tuple[int, int]] = []
                            pts_f_b: list[tuple[int, int]] = []
    
                            for idx in idxs:
                                if idx < int(iL) or idx > int(iR):
                                    continue
    
                                x = _idx_to_x(int(idx))
    
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
    
                                pts_s_t.append((x, _y_from_01(st)))
                                pts_s_b.append((x, _y_from_01(sb)))
    
                                # Fast: idx -> frame_map -> fast idx
                                fi = int(idx)
                                if slow_to_fast_frame and fi < len(slow_to_fast_frame):
                                    fi = int(slow_to_fast_frame[fi])
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
    
                                pts_f_t.append((x, _y_from_01(ft)))
                                pts_f_b.append((x, _y_from_01(fb)))
    
                            # Linien zeichnen (Bremse erst, dann Gas)
                            if len(pts_s_b) >= 2:
                                dr.line(pts_s_b, fill=COL_SLOW_BRAKE, width=2)
                            if len(pts_s_t) >= 2:
                                dr.line(pts_s_t, fill=COL_SLOW_THROTTLE, width=2)
                            if len(pts_f_b) >= 2:
                                dr.line(pts_f_b, fill=COL_FAST_BRAKE, width=2)
                            if len(pts_f_t) >= 2:
                                dr.line(pts_f_t, fill=COL_FAST_THROTTLE, width=2)
    
                            # ABS-Balken: scrollende Segmente (Länge = Dauer von ABS=1 im Fenster)
                            def _abs_val_s(idx0: int) -> float:
                                if not slow_abs_frames:
                                    return 0.0
                                si = int(round(float(idx0) * float(a_slow_scale)))
                                si = max(0, min(si, len(slow_abs_frames) - 1))
                                return float(slow_abs_frames[si])
    
                            def _abs_val_f(idx0: int) -> float:
                                if not fast_abs_frames:
                                    return 0.0
                                fi = int(idx0)
                                if slow_to_fast_frame and fi < len(slow_to_fast_frame):
                                    fi = int(slow_to_fast_frame[fi])
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
                                    x2 = _x_from_idx(idx2)
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
    
                        except Exception:
                            pass
    
                def _hud_delta() -> None:
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
    
                            # Text oben: reservierter Bereich (Headroom)
                            top_pad = int(round(max(14.0, float(font_sz) + 8.0)))
                            plot_y0 = int(y0) + top_pad
                            plot_y1 = int(y0 + h - 2)
    
                            if plot_y1 <= plot_y0 + 4:
                                plot_y0 = int(y0) + 2
                                plot_y1 = int(y0 + h - 2)
    
                            # Titel
                            try:
                                dr.text((int(x0 + 4), int(y0 + 2)), "Time delta", fill=COL_WHITE, font=font_title)
                            except Exception as e:
                                if hud_dbg:
                                    try:
                                        _log_print(f"[hudpy][Delta][EXC][title] {type(e).__name__}: {e}", log_file)
                                    except Exception:
                                        pass
    
                            # Delta-Funktion (aus Sync-Map)
                            def _delta_at_slow_frame(idx0: int) -> float:
                                fps_safe = float(fps) if float(fps) > 0.1 else 30.0
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
                                range_neg = float(abs(delta_neg_min))
                                range_pos = float(delta_pos_max)
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
    
                            # 0-Linie (rot)
                            try:
                                y_mid = int(round(y_zero))
                                dr.line(
                                    [(int(x0), y_mid), (int(x0 + w - 1), y_mid)],
                                    fill=(COL_SLOW_DARKRED[0], COL_SLOW_DARKRED[1], COL_SLOW_DARKRED[2], 200),
                                    width=1,
                                )
                            except Exception as e:
                                if hud_dbg:
                                    try:
                                        _log_print(f"[hudpy][Delta][EXC][zero_line] {type(e).__name__}: {e}", log_file)
                                    except Exception:
                                        pass
    
                            # Aktueller Wert oberhalb Marker (stabile Breite, Vorzeichen immer)
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
    
                                dr.text((x_val, y_val), txt, fill=col_cur, font=font_val)
                            except Exception as e:
                                if hud_dbg:
                                    try:
                                        _log_print(f"[hudpy][Delta][EXC][value_text] {type(e).__name__}: {e}", log_file)
                                    except Exception:
                                        pass
    
                            # Kurve: Anzahl Punkte aus Override, ohne 600-Cap, bis zur HUD-Breite
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
    
                            idxs = []
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
    
                            for idx in idxs:
                                if idx < int(iL) or idx > int(iR):
                                    continue
    
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
    
                        except Exception as e:
                            try:
                                _log_print(
                                    f"[hudpy][Delta][EXC][draw] {type(e).__name__}: {e}",
                                    log_file,
                                )
                            except Exception:
                                pass
    
                def _hud_steering() -> None:
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
    
                            # Titel oben links + Werte am Marker (größer, gleiche Höhe, stabil formatiert)
                            try:
                                try:
                                    from PIL import ImageFont
                                except Exception:
                                    ImageFont = None  # type: ignore
    
                                # Schriftgrößen: bewusst kleiner und ruhiger
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
    
                                y_txt = int(y0 + 2)  # gleiche Höhe wie Titel
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
                                # Beispiel: +075°, -147°
                                s_txt = f"{sdeg:+04d}°"
                                f_txt = f"{fdeg:+04d}°"
    
                                mx = int(x0 + (w // 2))
                                gap = 12  # ~1 Ziffer Abstand zum Marker
    
                                # Rot näher an den Marker (links), Blau rechts
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
    
                            # --- DEBUG: Welche CSV-Punkte werden für einen bestimmten Output-Frame benutzt? ---
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
    
                                # Probe: ein paar Samples gleichmäßig über das Fenster
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
                            # damit links/rechts gleich „stabil“ wirkt.
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
                            
                            # --- DEBUG: Welche CSV-Punkte werden für einen bestimmten Output-Frame benutzt? ---
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
    
                                # Probe: ein paar Samples gleichmäßig über das Fenster
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
    

                def _hud_speed() -> None:
                    pass

                def _hud_gear_rpm() -> None:
                    pass

                def _hud_line_delta() -> None:
                    pass

                def _hud_under_oversteer() -> None:
                    pass

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

        # Zusätzlich: 1 Sample pro Sekunde kopieren
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

def _first_enabled_hud_name(hud_enabled: Any | None, hud_boxes: Any | None) -> str | None:
    try:
        enabled = hud_enabled if isinstance(hud_enabled, dict) else {}
        boxes = hud_boxes if isinstance(hud_boxes, dict) else {}
        for name, on in enabled.items():
            if bool(on) and isinstance(boxes.get(name), dict):
                return str(name)
    except Exception:
        pass
    return None

def _first_enabled_hud_box_abs(
    geom: OutputGeometry,
    hud_enabled: Any | None,
    hud_boxes: Any | None,
) -> tuple[int, int, int, int] | None:
    # Re-Use Logik wie _hud_drawboxes_chain (nur: wir geben die ERSTE Box zurück)
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
            return (x_abs, y_abs, w, h)
        except Exception:
            continue

    return None

_SCROLL_HUD_NAMES: set[str] = {
    "Throttle / Brake",
    "Steering",
    "Delta",
    "Line Delta",
    "Under-/Oversteer",
}

_TABLE_HUD_NAMES: set[str] = {
    "Speed",
    "Gear & RPM",
}

def _enabled_hud_boxes_abs(
    geom: OutputGeometry,
    hud_enabled: Any | None,
    hud_boxes: Any | None,
) -> list[tuple[str, tuple[int, int, int, int]]]:
    """
    Gibt ALLE aktiven HUD-Boxen zurück (Name + Box absolut im Output).
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


def _write_hud_scroll_cmds(
    cmd_path: "Path",
    fps: float,
    cut_i0: int,
    cut_i1: int,
    slow_frame_to_lapdist: list[float],
    box_abs: tuple[int, int, int, int],
    *,
    before_s: float | None = None,
    after_s: float | None = None,
    log_file: "Path | None" = None
) -> None:
    # Demo: Tick-Raster + fester Marker (Marker ist statisch im Filter)
    # Ticks werden pro Frame gesetzt, basierend auf LapDistPct.
    x0, y0, w, h = box_abs

    try:
        before = float((os.environ.get("IRVC_HUD_WINDOW_BEFORE") or "").strip() or "0.02")
    except Exception:
        before = 0.02
    try:
        after = float((os.environ.get("IRVC_HUD_WINDOW_AFTER") or "").strip() or "0.02")
    except Exception:
        after = 0.02
    before = max(1e-6, float(before))
    after = max(1e-6, float(after))

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

    hud_dbg = (os.environ.get("IRVC_HUD_DEBUG") or "0").strip().lower() in ("1", "true", "yes", "on")
    if hud_dbg:
        _log_print(
            f"[hud] sendcmd-gen: path={cmd_path} fps={float(fps):.3f} cut_i0={int(cut_i0)} cut_i1={int(cut_i1)} box_abs=({x0},{y0},{w},{h}) before={before:.6f} after={after:.6f} step={step:.6f} max_ticks={max_ticks}",
            log_file,
        )

    center_x = x0 + (w // 2)
    tick_w = 2

    def _delta_to_x(delta: float) -> int:
        # Marker ist in der Mitte.
        # Links: -before .. 0, Rechts: 0 .. +after
        if delta < 0.0:
            x = int(round(center_x + (delta * (float(w) / 2.0) / before)))
        else:
            x = int(round(center_x + (delta * (float(w) / 2.0) / after)))
        if x < x0:
            x = x0
        if x > x0 + w - tick_w:
            x = x0 + w - tick_w
        return x

    lines: list[str] = []

    out_frames = max(0, int(cut_i1) - int(cut_i0))
    r = max(1.0, float(fps))

    # Anzahl Ticks links/rechts (ohne Marker). Beispiel: 21 -> 10 links + 10 rechts
    half = max(1, (max_ticks - 1) // 2)

    for j in range(out_frames):
        i = int(cut_i0) + j
        if i < 0 or i >= len(slow_frame_to_lapdist):
            continue

        ld = float(slow_frame_to_lapdist[i]) % 1.0
        t = float(j) / r

        # sendcmd: braucht "start-end" und dann Befehle, getrennt mit ";"
        iv = f"{t:.6f}-{t:.6f}"

        cmds: list[str] = []
        used_xs: list[int] = []

        # Hard-Test: Wenn das NICHT springt, wirkt sendcmd nicht auf drawbox.
        hud_sendcmd_test = (os.environ.get("IRVC_HUD_SENDCMD_TEST") or "").strip() == "1"
        if hud_sendcmd_test:
            x_left = x0
            x_right = x0 + w - tick_w
            xk = x_left if (j % 2 == 0) else x_right

            # Nur Tick0 sichtbar und bewegt
            cmds.append(f"hud_tick0 x {xk}")
            cmds.append(f"hud_tick0 color white@0.95")
            used_xs.append(xk)

            # Alle anderen Ticks ausblenden
            for slot in range(1, max_ticks):
                cmds.append(f"hud_tick{slot} x {x0 - 10}")
                cmds.append(f"hud_tick{slot} color white@0.00")

            # Rest der normalen Logik überspringen
            slot = max_ticks
            ld_mod = 0.0


        slot = 0

        # Rest innerhalb des Tick-Schritts (0..step)
        ld_mod = ld % step

        # Links: -half .. -1
        for k in range(-half, 0):
            # Scroll: Tick-Delta hängt vom aktuellen LapDistPct ab
            delta = (float(k) * step) - ld_mod
            show = (-before <= delta <= after)
            if show and slot < max_ticks:
                xk = _delta_to_x(delta)
                used_xs.append(xk)
                cmds.append(f"hud_tick{slot} x {xk}")
                cmds.append(f"hud_tick{slot} color white@0.80")
            elif slot < max_ticks:
                cmds.append(f"hud_tick{slot} x {x0 - 10}")
                cmds.append(f"hud_tick{slot} color white@0.00")
            slot += 1

        # Rechts: +1 .. +half
        for k in range(1, half + 1):
            delta = float(k) * step
            show = (-before <= delta <= after)
            if show and slot < max_ticks:
                xk = _delta_to_x(delta)
                used_xs.append(xk)
                cmds.append(f"hud_tick{slot} x {xk}")
                cmds.append(f"hud_tick{slot} color white@0.80")
            elif slot < max_ticks:
                cmds.append(f"hud_tick{slot} x {x0 - 10}")
                cmds.append(f"hud_tick{slot} color white@0.00")
            slot += 1

        # Restliche Slots ausblenden
        while slot < max_ticks:
            cmds.append(f"hud_tick{slot} x {x0 - 10}")
            cmds.append(f"hud_tick{slot} color white@0.00")
            slot += 1

        # sendcmd stabil: 1 Command pro Zeile
        # Format: "time [enter] target command arg"
        # Beispiel: 0.008333 [enter] hud_tick0 x 2259
        first_line = ""
        for c in cmds:
            # Wichtig: Jede INTERVAL-Zeile muss mit ";" enden, sonst fehlt der Separator.
            # Wichtig: Wir benutzen "start-end", damit sendcmd das als echtes Intervall behandelt.
            l = f"{iv} [enter] {c};"
            if not first_line:
                first_line = l
            lines.append(l)

        if hud_dbg and j < 3:
            _log_print(
                f"[hud] sendcmd-gen sample j={j} t={t:.6f} ld={ld:.6f} ticks_visible={len(used_xs)} xs={used_xs[:12]}",
                log_file,
            )
            _log_print(f"[hud] sendcmd-gen sample line={first_line[:180]}...", log_file)

    cmd_path.write_text("\n".join(lines) + "\n", encoding="utf-8")




def _build_stream_sync_filter(
    geom: OutputGeometry,
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
        # 1) Krümmung des Mappings: d2 aus fast_time_s
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
        # Gewichtung: Krümmung ist wichtiger als Speed-Diff
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

    # HUD (Python PNG Sequence) als Overlay in die Mitte
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

    # Audio-Input-Index hängt davon ab, ob HUD als Input 0 existiert
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


def _extract_frames(video: Path, fps_int: int, out_dir: Path) -> None:
    # Story 6: wird nicht mehr benutzt, bleibt für Debug/Alt-Code erhalten
    _safe_mkdir(out_dir)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        str(video),
        "-vf",
        f"fps={fps_int}",
        "-start_number",
        "0",
        str(out_dir / "%06d.png"),
    ]
    _run(cmd)


def _remap_frames(src_dir: Path, dst_dir: Path, frame_map: list[int]) -> None:
    # Story 6: wird nicht mehr benutzt, bleibt für Debug/Alt-Code erhalten
    _safe_mkdir(dst_dir)

    for out_idx, src_idx in enumerate(frame_map):
        src = src_dir / f"{src_idx:06d}.png"
        dst = dst_dir / f"{out_idx:06d}.png"
        if not src.exists():
            raise RuntimeError(f"fast frame fehlt: {src}")
        try:
            os.link(src, dst)
        except Exception:
            shutil.copyfile(src, dst)


def _encode_from_frames(frames_dir: Path, fps_int: int, out_mp4: Path, encoders: list[EncodeSpec]) -> None:
    # Story 6: wird nicht mehr benutzt, bleibt für Debug/Alt-Code erhalten
    out_mp4.parent.mkdir(parents=True, exist_ok=True)

    last_rc = 1
    for enc in encoders:
        print(f"[sync-encode] try vcodec={enc.vcodec}")

        cmd: list[str] = [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-framerate",
            str(fps_int),
            "-i",
            str(frames_dir / "%06d.png"),
            "-c:v",
            enc.vcodec,
            "-pix_fmt",
            enc.pix_fmt,
        ]

        cmd += list(enc.extra)

        if enc.vcodec in ("hevc_nvenc", "hevc_qsv", "hevc_amf"):
            if "-tag:v" not in cmd:
                cmd += ["-tag:v", "hvc1"]

        cmd += [str(out_mp4)]

        rc = _run_live_or_tail_on_fail(cmd, tail_n=20)
        last_rc = rc

        if rc == 0 and out_mp4.exists():
            print(f"[sync-encode] OK vcodec={enc.vcodec}")
            return
        else:
            print(f"[sync-encode] FAIL vcodec={enc.vcodec} rc={rc}")

    raise RuntimeError(f"sync encode failed (rc={last_rc})")


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

    geom = build_output_geometry(preset, hud_width_px=hud_width_px)

    filt = _build_split_filter_from_geometry(
        geom=geom,
        fps=float(fps_int),
        view_L=view_L,
        view_R=view_R,
        hud_enabled=hud_enabled,
        hud_boxes=hud_boxes,
    )

    encode_candidates = _choose_encoder(W=geom.W, fps=float(fps_int))

    has_nvenc = _ffmpeg_has_encoder("h264_nvenc") or _ffmpeg_has_encoder("hevc_nvenc")
    has_qsv = _ffmpeg_has_encoder("h264_qsv") or _ffmpeg_has_encoder("hevc_qsv")
    has_amf = _ffmpeg_has_encoder("h264_amf") or _ffmpeg_has_encoder("hevc_amf")
    print(f"[gpu] nvenc={has_nvenc} qsv={has_qsv} amf={has_amf} (cpu=libx264 immer)")

    last_rc = 1
    for enc in encode_candidates:
        print(f"[encode] try vcodec={enc.vcodec}")

        pipeline = FfmpegPipeline(
            decode=DecodeSpec(slow=slow, fast=fast),
            flt=FilterSpec(filter_complex=filt, video_map="[vout]", audio_map=None),
            enc=enc,
            audio_source=audio_source,
            outp=outp,
        )

        cmd = pipeline.build_cmd()

        # Debug: nur die ersten N Sekunden rendern
        try:
            dbg_max_s = float((os.environ.get("IRVC_DEBUG_MAX_S") or "").strip() or "0")
        except Exception:
            dbg_max_s = 0.0
        if dbg_max_s > 0.0:
            cmd = list(cmd)

            # sicher: -t muss VOR den Inputs stehen (Input-Trim)
            # wir hängen es direkt nach "ffmpeg -hide_banner -y -nostats -progress pipe:1"
            try:
                i_idx = cmd.index("-i")
            except ValueError:
                i_idx = 0
            ins = max(0, i_idx)
            cmd[ins:ins] = ["-t", f"{dbg_max_s}"]

            print(f"[debug] IRVC_DEBUG_MAX_S={dbg_max_s} -> input limited")

        live = (os.environ.get("IRVC_FFMPEG_LIVE") or "").strip() == "1"
        rc = _run_live_or_tail_on_fail(cmd, tail_n=20, log_file=log_file, live_stdout=live)
        last_rc = rc

        if rc == 0 and outp.exists():
            print(f"[encode] OK vcodec={enc.vcodec}")
            return
        else:
            print(f"[encode] FAIL vcodec={enc.vcodec} rc={rc}")

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

    geom = build_output_geometry(preset, hud_width_px=hud_width_px)

    frame_map, slow_frame_to_lapdist, slow_frame_to_fast_time_s, slow_frame_speed_diff = _build_sync_cache_maps_from_csv(
        slow_csv=scsv,
        fast_csv=fcsv,
        fps=float(fps_int),
        slow_duration_s=ms.duration_s,
        fast_duration_s=mf.duration_s,
    )
    
    # Debug: Sync-Map / Delta-Grundlage prüfen (warum Delta ggf. ~0 ist)
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
        print("[sync6] speed_diff: nicht verfügbar (Speed fehlt in CSV)")

    has_audio_slow = probe_has_audio(slow)
    has_audio_fast = probe_has_audio(fast)

    if audio_source == "slow" and not has_audio_slow:
        print("[sync6] audio: slow hat keinen Audio-Stream -> audio=none")
        audio_source = "none"
    elif audio_source == "fast" and not has_audio_fast:
        print("[sync6] audio: fast hat keinen Audio-Stream -> audio=none")
        audio_source = "none"

    # Story 2: HUD pro Frame in Python rendern (PNG Sequenz), dann als 3. ffmpeg Input überlagern
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

        # Fenster-Dict für Renderer: {hud_name: {"before_s": x, "after_s": y}}
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

        hud_seq_pattern = _render_hud_scroll_frames_png(
            hud_frames_dir,
            fps=float(fps_int),
            cut_i0=int(cut_i0),
            cut_i1=int(cut_i1),
            geom=geom,
            hud_enabled=hud_enabled,
            hud_boxes=hud_boxes,
            slow_frame_to_lapdist=slow_frame_to_lapdist,
            slow_to_fast_frame=frame_map,
            slow_frame_to_fast_time_s=slow_frame_to_fast_time_s,
            slow_speed_frames=slow_speed_frames,
            fast_speed_frames=fast_speed_frames,
            slow_min_speed_frames=slow_min_speed_frames,
            fast_min_speed_frames=fast_min_speed_frames,
            hud_speed_units=str(hud_speed_units),
            hud_speed_update_hz=int(hud_speed_update_hz),
            slow_gear_frames=slow_gear_frames,
            fast_gear_frames=fast_gear_frames,
            slow_rpm_frames=slow_rpm_frames,
            fast_rpm_frames=fast_rpm_frames,
            slow_steer_frames=slow_steer_frames,
            fast_steer_frames=fast_steer_frames,
            hud_curve_points_default=int(hud_curve_points_default),
            hud_gear_rpm_update_hz=int(hud_gear_rpm_update_hz),
            slow_throttle_frames=slow_throttle_frames,
            fast_throttle_frames=fast_throttle_frames,
            slow_brake_frames=slow_brake_frames,
            fast_brake_frames=fast_brake_frames,
            slow_abs_frames=slow_abs_frames,
            fast_abs_frames=fast_abs_frames,
            hud_curve_points_overrides=hud_curve_points_overrides,
            before_s=float(before_default_s),
            after_s=float(after_default_s),
            hud_name=None,
            hud_windows=hud_windows,
            log_file=log_file,
        )
        
        if hud_seq_pattern:
            _log_print(f"[hudpy] ON -> {hud_seq_pattern}", log_file)
        else:
            _log_print("[hudpy] OFF (keine Sequenz erzeugt)", log_file)


    # sendcmd-demo komplett aus (war instabil / unwirksam)
    hud_cmd_file = None

    # Wenn wir HUD-Frames als 3. Input laden, ist das Input-Label in ffmpeg dann [0:v].
    # ABER: wir haben die Reihenfolge der Inputs im build_cmd geändert:
    #   optional HUD = Input 0
    #   slow = Input 1
    #   fast = Input 2
    #
    # Deshalb müssen wir hier merken: wenn hud_seq_pattern aktiv ist, dann ist hud_label="[0:v]"
    hud_label = "[0:v]" if hud_seq_pattern else None

    filt, audio_map = _build_stream_sync_filter(
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

    encode_candidates = _choose_encoder(W=geom.W, fps=float(fps_int))

    has_nvenc = _ffmpeg_has_encoder("h264_nvenc") or _ffmpeg_has_encoder("hevc_nvenc")
    has_qsv = _ffmpeg_has_encoder("h264_qsv") or _ffmpeg_has_encoder("hevc_qsv")
    has_amf = _ffmpeg_has_encoder("h264_amf") or _ffmpeg_has_encoder("hevc_amf")
    print(f"[gpu] nvenc={has_nvenc} qsv={has_qsv} amf={has_amf} (cpu=libx264 immer)")

    last_rc = 1
    for enc in encode_candidates:
        print(f"[encode] try vcodec={enc.vcodec}")

        pipeline = FfmpegPipeline(
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
        )

        cmd = pipeline.build_cmd()

        # Debug: nur die ersten N Sekunden rendern (spart Zeit)
        try:
            dbg_max_s = float((os.environ.get("IRVC_DEBUG_MAX_S") or "").strip() or "0")
        except Exception:
            dbg_max_s = 0.0
        if dbg_max_s > 0.0:
            cmd = list(cmd)

            # sicher: -t muss VOR den Inputs stehen (Input-Trim)
            try:
                i_idx = cmd.index("-i")
            except ValueError:
                i_idx = 0
            ins = max(0, i_idx)
            cmd[ins:ins] = ["-t", f"{dbg_max_s}"]

            print(f"[debug] IRVC_DEBUG_MAX_S={dbg_max_s} -> input limited")

        live = (os.environ.get("IRVC_FFMPEG_LIVE") or "").strip() == "1"
        rc = _run_live_or_tail_on_fail(cmd, tail_n=20, log_file=log_file, live_stdout=live)
        last_rc = rc

        if rc == 0 and outp.exists():
            print(f"[encode] OK vcodec={enc.vcodec}")
            print(f"[sync6] sync_cache_json={sync_cache_path}")
            return
        else:
            print(f"[encode] FAIL vcodec={enc.vcodec} rc={rc}")

    raise RuntimeError(f"ffmpeg failed (rc={last_rc})")
