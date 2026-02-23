from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable

from core.ffmpeg_plan import EncodeSpec
from core.subprocess_utils import windows_no_window_subprocess_kwargs


@dataclass(frozen=True)
class EncoderSelection:
    requested: str | None
    selected: str | None
    available: set[str]


_ENCODER_CACHE: dict[str, set[str]] = {}


def detect_available_encoders(ffmpeg_bin: str, *, cache: bool = True) -> set[str]:
    key = (ffmpeg_bin or "ffmpeg").strip() or "ffmpeg"
    if cache and key in _ENCODER_CACHE:
        return set(_ENCODER_CACHE[key])
    try:
        p = subprocess.run(
            [key, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            **windows_no_window_subprocess_kwargs(),
        )
        if p.returncode != 0:
            out: set[str] = set()
        else:
            out = _parse_encoder_names(p.stdout or "")
    except Exception:
        out = set()
    if cache:
        _ENCODER_CACHE[key] = set(out)
    return out


def validate_encoder_request(
    requested: str | None,
    available: set[str],
    *,
    allow_none: bool = True,
) -> str | None:
    s = (requested or "").strip()
    if s == "":
        return None if allow_none else ""
    return s if s in available else (None if allow_none else "")


def choose_encoder(
    preferred: str | None,
    available: set[str],
    *,
    fallback_order: list[str],
) -> str:
    req = validate_encoder_request(preferred, available, allow_none=True)
    if req:
        return req
    for enc in fallback_order:
        if enc == "libx264" or enc in available:
            return enc
    return "libx264"


def build_encode_args(encoder: str, options: dict) -> list[str]:
    w = int(options.get("W", options.get("width", 0)) or 0)

    if encoder == "h264_nvenc":
        if w > 4096:
            return []
        return ["-preset", "p5", "-cq:v", "19"]
    if encoder == "hevc_nvenc":
        return ["-preset", "p5", "-cq:v", "23", "-tag:v", "hvc1"]

    if encoder == "h264_qsv":
        if w > 4096:
            return []
        return ["-global_quality", "23"]
    if encoder == "hevc_qsv":
        return ["-global_quality", "26", "-tag:v", "hvc1"]

    if encoder == "h264_amf":
        return ["-quality", "balanced", "-rc", "cqp", "-qp_i", "20", "-qp_p", "20", "-qp_b", "22"]
    if encoder == "hevc_amf":
        return [
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
        ]

    return ["-preset", "veryfast", "-crf", "18"]


def build_encode_specs(*, W: int, fps: float, available: set[str]) -> list[EncodeSpec]:
    specs: list[EncodeSpec] = []

    if "h264_nvenc" in available and (W <= 4096):
        specs.append(
            EncodeSpec(
                vcodec="h264_nvenc",
                extra=build_encode_args("h264_nvenc", {"W": W}),
                fps=fps,
            )
        )

    if "hevc_nvenc" in available:
        specs.append(
            EncodeSpec(
                vcodec="hevc_nvenc",
                extra=build_encode_args("hevc_nvenc", {"W": W}),
                fps=fps,
            )
        )

    if "h264_qsv" in available and (W <= 4096):
        specs.append(
            EncodeSpec(
                vcodec="h264_qsv",
                extra=build_encode_args("h264_qsv", {"W": W}),
                fps=fps,
            )
        )

    if "hevc_qsv" in available:
        specs.append(
            EncodeSpec(
                vcodec="hevc_qsv",
                extra=build_encode_args("hevc_qsv", {"W": W}),
                fps=fps,
            )
        )

    if "h264_amf" in available and (W <= 4096):
        specs.append(
            EncodeSpec(
                vcodec="h264_amf",
                extra=build_encode_args("h264_amf", {"W": W}),
                fps=fps,
            )
        )

    if "hevc_amf" in available:
        specs.append(
            EncodeSpec(
                vcodec="hevc_amf",
                extra=build_encode_args("hevc_amf", {"W": W}),
                fps=fps,
            )
        )

    specs.append(
        EncodeSpec(
            vcodec="libx264",
            extra=build_encode_args("libx264", {"W": W}),
            fps=fps,
        )
    )
    return specs


def run_encode_with_fallback(
    *,
    build_cmd_fn: Callable[[str], tuple[int, bool]],
    encoder_order: list[str],
    log_fn: Callable[[str], None],
) -> tuple[str, int]:
    last_rc = 1
    last_encoder = ""
    for encoder in encoder_order:
        last_encoder = encoder
        log_fn(f"[encode] try vcodec={encoder}")
        rc, ok = build_cmd_fn(encoder)
        last_rc = int(rc)
        if ok:
            log_fn(f"[encode] OK vcodec={encoder}")
            return encoder, last_rc
        log_fn(f"[encode] FAIL vcodec={encoder} rc={last_rc}")
    return "", last_rc


def _parse_encoder_names(stdout: str) -> set[str]:
    names: set[str] = set()
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if line == "" or line.startswith("Encoders:") or line.startswith("------"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        flag = parts[0]
        name = parts[1]
        if len(flag) == 6 and all(ch.isalpha() or ch == "." for ch in flag):
            names.add(name)
    return names

