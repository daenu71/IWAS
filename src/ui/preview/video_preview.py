from __future__ import annotations

import json
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from typing import Callable
import tkinter as tk
from tkinter import ttk

import cv2
from PIL import Image, ImageTk
from core.encoders import detect_available_encoders
from core.ffmpeg_tools import ffmpeg_exists as _ffmpeg_exists_bundled, resolve_ffmpeg_bin, resolve_ffprobe_bin
from core.subprocess_utils import windows_no_window_subprocess_kwargs


class VideoPreviewController:
    def __init__(
        self,
        *,
        root: tk.Tk,
        preview_area: tk.Widget,
        preview_label: tk.Widget,
        lbl_frame: tk.Widget,
        lbl_end: tk.Widget,
        lbl_loaded: tk.Widget,
        btn_play: tk.Widget,
        scrub: tk.Widget,
        spn_end: tk.Widget,
        end_var: tk.IntVar,
        input_video_dir: Path,
        proxy_dir: Path,
        startframes_by_name: dict[str, int],
        endframes_by_name: dict[str, int],
        save_startframes: Callable[[dict[str, int]], None],
        save_endframes: Callable[[dict[str, int]], None],
        extract_time_ms: Callable[[Path], int | None],
        show_preview_controls: Callable[[bool], None],
        sync_from_folders_if_needed_ui: Callable[..., None],
        show_progress: Callable[[str, str], tuple[object, Callable[[], None]]],
    ) -> None:
        self.root = root
        self.preview_area = preview_area
        self.preview_label = preview_label
        self.lbl_frame = lbl_frame
        self.lbl_end = lbl_end
        self.lbl_loaded = lbl_loaded
        self.btn_play = btn_play
        self.scrub = scrub
        self.spn_end = spn_end
        self.end_var = end_var

        self.input_video_dir = input_video_dir
        self.proxy_dir = proxy_dir
        self.startframes_by_name = startframes_by_name
        self.endframes_by_name = endframes_by_name
        self._save_startframes = save_startframes
        self._save_endframes = save_endframes
        self._extract_time_ms = extract_time_ms
        self._show_preview_controls = show_preview_controls
        self._sync_from_folders_if_needed_ui = sync_from_folders_if_needed_ui
        self._show_progress = show_progress

        self.current_video_original: Path | None = None
        self.current_video_opened: Path | None = None
        self.cap: cv2.VideoCapture | None = None

        self.current_frame_idx: int = 0
        self.total_frames: int = 0
        self.fps: float = 30.0

        self.end_frame_idx: int = 0

        self.is_playing: bool = False
        self.speed_factor: float = 1.0
        self.tk_img = None

        self.scrub_is_dragging: bool = False
        self.last_render_ts: float = 0.0
        self._video_encode_candidates_cache: list[tuple[str, list[str]]] | None = None

    def ffmpeg_exists(self) -> bool:
        try:
            return bool(_ffmpeg_exists_bundled())
        except Exception:
            return False

    def _video_encode_candidates(self) -> list[tuple[str, list[str]]]:
        if self._video_encode_candidates_cache is not None:
            return list(self._video_encode_candidates_cache)

        ffmpeg_bin = resolve_ffmpeg_bin()
        available = detect_available_encoders(ffmpeg_bin, cache=True)

        candidates: list[tuple[str, list[str]]] = []
        if ("libx264" in available) or (not available):
            candidates.append(
                (
                    "libx264",
                    ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "20"],
                )
            )
        if "libopenh264" in available:
            candidates.append(
                (
                    "libopenh264",
                    ["-c:v", "libopenh264", "-pix_fmt", "yuv420p", "-b:v", "6M"],
                )
            )
        for hw_name, hw_args in (
            ("h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p5", "-cq:v", "19"]),
            ("h264_qsv", ["-c:v", "h264_qsv", "-global_quality", "23"]),
            ("h264_amf", ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp", "-qp_i", "20", "-qp_p", "20", "-qp_b", "22"]),
        ):
            if hw_name in available:
                candidates.append((hw_name, [*hw_args, "-pix_fmt", "yuv420p"]))

        # Native MPEG-4 is available in LGPL builds and keeps Cut/Proxy functional
        # when libx264 is not bundled (e.g. LGPL FFmpeg package).
        candidates.append(
            (
                "mpeg4",
                ["-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-q:v", "2"],
            )
        )

        deduped: list[tuple[str, list[str]]] = []
        seen: set[str] = set()
        for name, args in candidates:
            if name in seen:
                continue
            seen.add(name)
            deduped.append((name, args))

        self._video_encode_candidates_cache = list(deduped)
        return list(deduped)

    def _exact_cut_encode_candidates(self) -> list[tuple[str, list[str]]]:
        ffmpeg_bin = resolve_ffmpeg_bin()
        available = detect_available_encoders(ffmpeg_bin, cache=True)

        candidates: list[tuple[str, list[str]]] = []

        if ("libx264" in available) or (not available):
            candidates.append(
                (
                    "libx264",
                    ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium", "-crf", "18"],
                )
            )

        for hw_name, hw_args in (
            ("h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p6", "-cq:v", "16"]),
            ("h264_qsv", ["-c:v", "h264_qsv", "-global_quality", "18"]),
            (
                "h264_amf",
                [
                    "-c:v",
                    "h264_amf",
                    "-quality",
                    "quality",
                    "-rc",
                    "cqp",
                    "-qp_i",
                    "16",
                    "-qp_p",
                    "16",
                    "-qp_b",
                    "18",
                ],
            ),
            (
                "h264_mf",
                [
                    "-c:v",
                    "h264_mf",
                    "-rate_control",
                    "quality",
                    "-quality",
                    "100",
                    "-scenario",
                    "archive",
                ],
            ),
        ):
            if hw_name in available:
                candidates.append((hw_name, [*hw_args, "-pix_fmt", "yuv420p"]))

        if "libopenh264" in available:
            candidates.append(
                (
                    "libopenh264",
                    [
                        "-c:v",
                        "libopenh264",
                        "-pix_fmt",
                        "yuv420p",
                        "-rc_mode",
                        "quality",
                        "-profile",
                        "high",
                        "-coder",
                        "cabac",
                        "-allow_skip_frames",
                        "0",
                    ],
                )
            )

        deduped: list[tuple[str, list[str]]] = []
        seen: set[str] = set()
        for name, args in candidates:
            if name in seen:
                continue
            seen.add(name)
            deduped.append((name, args))
        return deduped

    def _run_ffmpeg_with_video_encode_fallback(
        self,
        *,
        args_before_video_codec: list[str],
        out_path: Path,
        encoder_candidates: list[tuple[str, list[str]]] | None = None,
        progress_total_sec: float | None = None,
        progress_cb: Callable[[float], None] | None = None,
    ) -> tuple[bool, str, str]:
        ffmpeg_bin = resolve_ffmpeg_bin()
        last_error = ""

        candidates = list(encoder_candidates) if encoder_candidates is not None else self._video_encode_candidates()
        for encoder_name, encoder_args in candidates:
            self.safe_unlink(out_path)
            progress_line_cb = None
            if (progress_cb is not None) and (progress_total_sec is not None) and (progress_total_sec > 0):
                try:
                    progress_cb(0.0)
                except Exception:
                    pass

                def _on_progress_line(line: str, total_s: float = float(progress_total_sec), cb: Callable[[float], None] = progress_cb) -> None:
                    pct = self._progress_pct_from_ffmpeg_line(line, total_s)
                    if pct is None:
                        return
                    try:
                        cb(pct)
                    except Exception:
                        pass

                progress_line_cb = _on_progress_line

            cmd = [
                ffmpeg_bin,
                "-hide_banner",
                "-nostats",
                "-loglevel",
                "error",
                *([] if progress_line_cb is None else ["-progress", "pipe:1"]),
                *args_before_video_codec,
                *encoder_args,
                str(out_path),
            ]
            try:
                p = self._run_process_with_ui_pump(cmd, stdout_line_cb=progress_line_cb)
            except Exception as e:
                last_error = str(e)
                continue

            if p.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
                return True, encoder_name, ""

            err_txt = (p.stderr or p.stdout or "").strip()
            if err_txt:
                lines = [ln.strip() for ln in err_txt.splitlines() if ln.strip()]
                if lines:
                    last_error = lines[-1]
            if not last_error:
                last_error = f"ffmpeg rc={p.returncode}"

        self.safe_unlink(out_path)
        return False, "", last_error

    def _run_ffmpeg_once(
        self,
        *,
        args: list[str],
        out_path: Path,
    ) -> tuple[bool, str]:
        self.safe_unlink(out_path)
        try:
            p = self._run_process_with_ui_pump(
                [resolve_ffmpeg_bin(), "-hide_banner", "-nostats", "-loglevel", "error", *args, str(out_path)]
            )
        except Exception as e:
            return False, str(e)

        if p.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
            return True, ""

        err_txt = (p.stderr or p.stdout or "").strip()
        if err_txt:
            lines = [ln.strip() for ln in err_txt.splitlines() if ln.strip()]
            if lines:
                return False, lines[-1]
        return False, f"ffmpeg rc={p.returncode}"

    def _probe_video_frames_for_hybrid_cut(self, src: Path) -> tuple[list[float], list[int]]:
        try:
            p = subprocess.run(
                [
                    resolve_ffprobe_bin(),
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_frames",
                    "-show_entries",
                    "frame=key_frame,best_effort_timestamp_time,pkt_dts_time,pkt_pts_time,pkt_duration_time",
                    "-of",
                    "json",
                    str(src),
                ],
                capture_output=True,
                text=True,
                **windows_no_window_subprocess_kwargs(),
            )
        except Exception as e:
            raise RuntimeError(f"ffprobe start failed: {e}") from e

        if p.returncode != 0:
            err = (p.stderr or p.stdout or "").strip()
            if err:
                lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
                if lines:
                    raise RuntimeError(lines[-1])
            raise RuntimeError(f"ffprobe rc={p.returncode}")

        try:
            payload = json.loads(p.stdout or "{}")
        except Exception as e:
            raise RuntimeError(f"ffprobe JSON parse failed: {e}") from e

        raw_frames = payload.get("frames")
        if not isinstance(raw_frames, list) or len(raw_frames) == 0:
            raise RuntimeError("ffprobe returned no video frames")

        frame_times: list[float] = []
        keyframes: list[int] = []
        last_ts = -1.0
        nominal_step = 1.0 / max(1.0, float(self.fps))
        for item in raw_frames:
            if not isinstance(item, dict):
                continue
            ts = None
            for k in ("best_effort_timestamp_time", "pkt_pts_time", "pkt_dts_time"):
                raw = item.get(k)
                if raw is None or raw == "N/A":
                    continue
                try:
                    ts = float(raw)
                    break
                except Exception:
                    continue
            if ts is None:
                dur = None
                raw_dur = item.get("pkt_duration_time")
                if raw_dur not in (None, "N/A"):
                    try:
                        dur = float(raw_dur)
                    except Exception:
                        dur = None
                if frame_times:
                    step = dur if (dur is not None and dur > 0.0) else nominal_step
                    ts = max(last_ts + max(1e-6, step), 0.0)
                else:
                    ts = 0.0
            if ts <= last_ts:
                ts = last_ts + max(1e-6, nominal_step)
            frame_times.append(float(ts))
            last_ts = float(ts)
            try:
                if int(item.get("key_frame", 0)) == 1:
                    keyframes.append(len(frame_times) - 1)
            except Exception:
                pass

        if not frame_times:
            raise RuntimeError("ffprobe returned no usable frame timestamps")
        if 0 not in keyframes:
            keyframes.insert(0, 0)
        return frame_times, sorted(set(int(i) for i in keyframes if 0 <= int(i) < len(frame_times)))

    def _hybrid_cut_copy_partition(self, *, s: int, e: int, keyframes: list[int]) -> tuple[int, int] | None:
        if e <= s or not keyframes:
            return None

        copy_start = None
        for kf in keyframes:
            if kf > s:
                copy_start = int(kf)
                break
        if copy_start is None:
            return None

        copy_end = None
        for kf in reversed(keyframes):
            if kf > copy_start and kf <= e:
                copy_end = int(kf)
                break
        if copy_end is None or copy_end <= copy_start:
            return None
        return int(copy_start), int(copy_end)

    def _run_ffmpeg_with_single_video_encoder(
        self,
        *,
        args_before_video_codec: list[str],
        encoder_args: list[str],
        out_path: Path,
        progress_total_sec: float | None = None,
        progress_cb: Callable[[float], None] | None = None,
    ) -> tuple[bool, str]:
        self.safe_unlink(out_path)
        progress_line_cb = None
        if (progress_cb is not None) and (progress_total_sec is not None) and (progress_total_sec > 0):
            try:
                progress_cb(0.0)
            except Exception:
                pass

            def _on_progress_line(line: str, total_s: float = float(progress_total_sec), cb: Callable[[float], None] = progress_cb) -> None:
                pct = self._progress_pct_from_ffmpeg_line(line, total_s)
                if pct is None:
                    return
                try:
                    cb(pct)
                except Exception:
                    pass

            progress_line_cb = _on_progress_line

        cmd = [
            resolve_ffmpeg_bin(),
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "error",
            *([] if progress_line_cb is None else ["-progress", "pipe:1"]),
            *args_before_video_codec,
            *encoder_args,
            str(out_path),
        ]
        try:
            p = self._run_process_with_ui_pump(cmd, stdout_line_cb=progress_line_cb)
        except Exception as e:
            return False, str(e)

        if p.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
            return True, ""

        err_txt = (p.stderr or p.stdout or "").strip()
        if err_txt:
            lines = [ln.strip() for ln in err_txt.splitlines() if ln.strip()]
            if lines:
                return False, lines[-1]
        return False, f"ffmpeg rc={p.returncode}"

    def _run_hybrid_exact_cut(
        self,
        *,
        src: Path,
        s: int,
        e: int,
        frame_times: list[float],
        keyframes: list[int],
        out_path: Path,
        progress_cb: Callable[[float], None] | None = None,
    ) -> tuple[bool, str, str]:
        part = self._hybrid_cut_copy_partition(s=int(s), e=int(e), keyframes=keyframes)
        if part is None:
            return False, "", "hybrid_copy_window_unavailable"
        copy_start_idx, copy_end_idx = part

        n_frames = len(frame_times)
        if not (0 <= s < n_frames and 0 <= e < n_frames and s <= e):
            return False, "", "hybrid_frame_range_out_of_probe"
        if not (s < copy_start_idx < copy_end_idx <= e):
            return False, "", "hybrid_invalid_partition"

        left_count = max(0, copy_start_idx - s)
        mid_count = max(0, copy_end_idx - copy_start_idx)
        right_count = max(0, e - copy_end_idx + 1)
        if left_count <= 0 or mid_count <= 0 or right_count <= 0:
            return False, "", "hybrid_no_meaningful_middle"

        nominal_step = 1.0 / max(1.0, float(self.fps))

        def _frame_ts(idx: int) -> float:
            return float(frame_times[max(0, min(idx, n_frames - 1))])

        def _frame_end_ts(idx_inclusive: int) -> float:
            nxt = idx_inclusive + 1
            if 0 <= nxt < n_frames:
                t0 = _frame_ts(idx_inclusive)
                t1 = _frame_ts(nxt)
                if t1 > t0:
                    return t1
            if idx_inclusive > 0:
                prev = _frame_ts(idx_inclusive - 1)
                cur = _frame_ts(idx_inclusive)
                if cur > prev:
                    return cur + (cur - prev)
            return _frame_ts(idx_inclusive) + nominal_step

        left_start_ts = _frame_ts(s)
        copy_start_ts = _frame_ts(copy_start_idx)
        copy_end_ts = _frame_ts(copy_end_idx)
        right_start_ts = copy_end_ts
        right_dur_ts = max(0.0, _frame_end_ts(e) - right_start_ts)
        left_dur_ts = max(0.0, copy_start_ts - left_start_ts)
        mid_dur_ts = max(0.0, copy_end_ts - copy_start_ts)

        if left_dur_ts <= 0.0 or mid_dur_ts <= 0.0 or right_dur_ts <= 0.0:
            return False, "", "hybrid_nonpositive_duration"

        work_dir = self.input_video_dir / f"{src.stem}__cut_hybrid_tmp"
        concat_txt = work_dir / "concat.txt"
        seg_left = work_dir / "seg_left.ts"
        seg_mid = work_dir / "seg_mid.ts"
        seg_right = work_dir / "seg_right.ts"
        mux_out = work_dir / "mux_out.mp4"

        try:
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)
            work_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return False, "", f"hybrid_tmp_dir_failed: {e}"

        total_duration = max(0.001, float(left_dur_ts + mid_dur_ts + right_dur_ts))

        def _stage_progress(start_pct: float, span_pct: float, local_pct: float) -> None:
            if progress_cb is None:
                return
            try:
                progress_cb(max(0.0, min(100.0, start_pct + (span_pct * (max(0.0, min(100.0, local_pct)) / 100.0)))))
            except Exception:
                pass

        def _set_abs_progress(pct: float) -> None:
            if progress_cb is None:
                return
            try:
                progress_cb(max(0.0, min(100.0, pct)))
            except Exception:
                pass

        encoder_candidates = self._exact_cut_encode_candidates()
        if not encoder_candidates:
            return False, "", "no exact H.264 encoder available"

        last_error = ""
        self.safe_unlink(out_path)

        try:
            for encoder_name, encoder_args in encoder_candidates:
                for p in (seg_left, seg_mid, seg_right, concat_txt, mux_out):
                    self.safe_unlink(p)

                left_weight = (left_dur_ts / total_duration) * 90.0
                mid_weight = (mid_dur_ts / total_duration) * 90.0
                right_weight = (right_dur_ts / total_duration) * 90.0

                ok_left, err_left = self._run_ffmpeg_with_single_video_encoder(
                    args_before_video_codec=[
                        "-y",
                        "-i",
                        str(src),
                        "-ss",
                        f"{left_start_ts:.6f}",
                        "-map",
                        "0:v:0",
                        "-an",
                        "-frames:v",
                        str(int(left_count)),
                        "-f",
                        "mpegts",
                    ],
                    encoder_args=list(encoder_args),
                    out_path=seg_left,
                    progress_total_sec=float(left_dur_ts),
                    progress_cb=(None if progress_cb is None else (lambda p, sw=left_weight: _stage_progress(0.0, sw, p))),
                )
                if not ok_left:
                    last_error = err_left or f"{encoder_name}: left segment failed"
                    continue
                _set_abs_progress(left_weight)

                ok_mid, err_mid = self._run_ffmpeg_once(
                    args=[
                        "-y",
                        "-ss",
                        f"{copy_start_ts:.6f}",
                        "-i",
                        str(src),
                        "-t",
                        f"{mid_dur_ts:.6f}",
                        "-map",
                        "0:v:0",
                        "-an",
                        "-c:v",
                        "copy",
                        "-bsf:v",
                        "h264_mp4toannexb",
                        "-f",
                        "mpegts",
                    ],
                    out_path=seg_mid,
                )
                if not ok_mid:
                    last_error = err_mid or f"{encoder_name}: middle copy segment failed"
                    continue
                _set_abs_progress(left_weight + mid_weight)

                ok_right, err_right = self._run_ffmpeg_with_single_video_encoder(
                    args_before_video_codec=[
                        "-y",
                        "-i",
                        str(src),
                        "-ss",
                        f"{right_start_ts:.6f}",
                        "-map",
                        "0:v:0",
                        "-an",
                        "-frames:v",
                        str(int(right_count)),
                        "-f",
                        "mpegts",
                    ],
                    encoder_args=list(encoder_args),
                    out_path=seg_right,
                    progress_total_sec=float(right_dur_ts),
                    progress_cb=(
                        None
                        if progress_cb is None
                        else (lambda p, sp=(left_weight + mid_weight), sw=right_weight: _stage_progress(sp, sw, p))
                    ),
                )
                if not ok_right:
                    last_error = err_right or f"{encoder_name}: right segment failed"
                    continue
                _set_abs_progress(left_weight + mid_weight + right_weight)

                try:
                    concat_txt.write_text(
                        "\n".join(
                            [
                                "ffconcat version 1.0",
                                f"file '{seg_left.as_posix()}'",
                                f"file '{seg_mid.as_posix()}'",
                                f"file '{seg_right.as_posix()}'",
                                "",
                            ]
                        ),
                        encoding="utf-8",
                    )
                except Exception as e:
                    last_error = f"hybrid concat list failed: {e}"
                    continue

                ok_mux, err_mux = self._run_ffmpeg_once(
                    args=[
                        "-y",
                        "-fflags",
                        "+genpts",
                        "-safe",
                        "0",
                        "-f",
                        "concat",
                        "-i",
                        str(concat_txt),
                        "-map",
                        "0:v:0",
                        "-an",
                        "-c:v",
                        "copy",
                        "-movflags",
                        "+faststart",
                    ],
                    out_path=mux_out,
                )
                if not ok_mux:
                    last_error = err_mux or f"{encoder_name}: concat failed"
                    continue

                self.safe_unlink(out_path)
                mux_out.replace(out_path)
                _set_abs_progress(100.0)
                return True, encoder_name, ""

            if not last_error:
                last_error = "hybrid failed"
            return False, "", last_error
        finally:
            for p in (seg_left, seg_mid, seg_right, concat_txt, mux_out):
                self.safe_unlink(p)
            try:
                if work_dir.exists():
                    work_dir.rmdir()
            except Exception:
                pass

    @staticmethod
    def _parse_ffmpeg_clock_to_sec(s: str) -> float:
        try:
            txt = str(s or "").strip()
            if txt.count(":") != 2:
                return 0.0
            hh, mm, ss = txt.split(":", 2)
            return (int(hh) * 3600.0) + (int(mm) * 60.0) + float(ss)
        except Exception:
            return 0.0

    def _progress_pct_from_ffmpeg_line(self, line: str, total_s: float) -> float | None:
        txt = str(line or "").strip()
        if txt == "" or "=" not in txt or total_s <= 0:
            return None
        k, v = txt.split("=", 1)
        key = k.strip()
        val = v.strip()
        if key == "progress" and val == "end":
            return 100.0
        if key == "out_time_ms":
            try:
                sec = float(val) / 1_000_000.0
            except Exception:
                return None
        elif key == "out_time_us":
            try:
                sec = float(val) / 1_000_000.0
            except Exception:
                return None
        elif key == "out_time":
            sec = self._parse_ffmpeg_clock_to_sec(val)
        else:
            return None
        pct = (max(0.0, sec) / max(0.001, float(total_s))) * 100.0
        if pct < 0.0:
            return 0.0
        if pct > 100.0:
            return 100.0
        return pct

    def _run_process_with_ui_pump(
        self,
        cmd: list[str],
        *,
        stdout_line_cb: Callable[[str], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            **windows_no_window_subprocess_kwargs(),
        )

        q: queue.Queue[tuple[str, str | None]] = queue.Queue()

        def _reader(tag: str, stream) -> None:
            try:
                if stream is None:
                    return
                for raw in iter(stream.readline, ""):
                    q.put((tag, raw))
            finally:
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
                q.put((tag, None))

        t_out = threading.Thread(target=_reader, args=("stdout", proc.stdout), daemon=True)
        t_err = threading.Thread(target=_reader, args=("stderr", proc.stderr), daemon=True)
        t_out.start()
        t_err.start()

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        out_done = False
        err_done = False

        while True:
            drained_any = False
            while True:
                try:
                    tag, payload = q.get_nowait()
                except queue.Empty:
                    break
                drained_any = True
                if payload is None:
                    if tag == "stdout":
                        out_done = True
                    else:
                        err_done = True
                    continue
                if tag == "stdout":
                    stdout_parts.append(payload)
                    if stdout_line_cb is not None:
                        try:
                            stdout_line_cb(payload)
                        except Exception:
                            pass
                else:
                    stderr_parts.append(payload)

            try:
                self.root.update()
            except Exception:
                pass

            if (proc.poll() is not None) and out_done and err_done:
                break
            if not drained_any:
                time.sleep(0.03)

        try:
            proc.wait(timeout=1.0)
        except Exception:
            pass
        t_out.join(timeout=0.2)
        t_err.join(timeout=0.2)
        stdout_txt = "".join(stdout_parts)
        stderr_txt = "".join(stderr_parts)
        return subprocess.CompletedProcess(cmd, int(proc.returncode or 0), stdout_txt or "", stderr_txt or "")

    @staticmethod
    def _find_progressbar_widget(progress_win: object) -> ttk.Progressbar | None:
        try:
            if not isinstance(progress_win, tk.Misc):
                return None
            for child in progress_win.winfo_children():
                for grand in child.winfo_children():
                    if isinstance(grand, ttk.Progressbar):
                        return grand
        except Exception:
            return None
        return None

    def _make_progress_setter(self, progress_win: object, *, label_prefix: str) -> Callable[[float], None]:
        bar = self._find_progressbar_widget(progress_win)
        if bar is not None:
            try:
                bar.stop()
            except Exception:
                pass
            try:
                bar.configure(mode="determinate", maximum=100.0, value=0.0)
            except Exception:
                pass

        last_pct = {"v": -1}

        def _set_progress(pct: float) -> None:
            p = max(0.0, min(100.0, float(pct)))
            pi = int(p)
            if pi == last_pct["v"] and p < 100.0:
                return
            last_pct["v"] = pi
            if bar is not None:
                try:
                    bar["value"] = p
                except Exception:
                    pass
            try:
                self.lbl_loaded.config(text=f"{label_prefix} {pi}%")
            except Exception:
                pass
            try:
                self.root.update_idletasks()
            except Exception:
                pass

        return _set_progress

    def make_proxy_h264(self, src: Path) -> Path | None:
        if not self.ffmpeg_exists():
            return None

        safe_name = src.stem + "__proxy_h264.mp4"
        dst = self.proxy_dir / safe_name

        if dst.exists() and dst.stat().st_size > 0:
            return dst

        try:
            self.lbl_loaded.config(text=f"Video: Creating proxy… ({src.name})")
            self.root.update_idletasks()

            ok, _, _ = self._run_ffmpeg_with_video_encode_fallback(
                args_before_video_codec=[
                    "-y",
                    "-i", str(src),
                    "-an",
                ],
                out_path=dst,
            )
            if ok and dst.exists() and dst.stat().st_size > 0:
                return dst
        except Exception:
            pass
        return None

    def try_open_for_png(self, p: Path) -> cv2.VideoCapture | None:
        c = cv2.VideoCapture(str(p))
        if c is not None and c.isOpened():
            return c
        try:
            if c is not None:
                c.release()
        except Exception:
            pass

        proxy = self.make_proxy_h264(p)
        if proxy is not None:
            c2 = cv2.VideoCapture(str(proxy))
            if c2 is not None and c2.isOpened():
                return c2
            try:
                if c2 is not None:
                    c2.release()
            except Exception:
                pass
        return None

    def read_frame_as_pil(self, p: Path, frame_idx: int) -> Image.Image | None:
        c = self.try_open_for_png(p)
        if c is None:
            return None
        try:
            total = int(c.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            if total > 0:
                frame_idx = max(0, min(int(frame_idx), total - 1))
            c.set(cv2.CAP_PROP_POS_FRAMES, float(frame_idx))
            ok, frame = c.read()
            if not ok or frame is None:
                return None
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            return Image.fromarray(rgb)
        finally:
            try:
                c.release()
            except Exception:
                pass

    def clamp_frame(self, idx: int) -> int:
        if self.total_frames <= 0:
            return max(0, idx)
        if idx < 0:
            return 0
        if idx > self.total_frames - 1:
            return self.total_frames - 1
        return idx

    def set_endframe(self, idx: int, save: bool = True) -> None:
        idx = self.clamp_frame(int(idx))
        self.end_frame_idx = idx

        try:
            self.spn_end.configure(from_=0, to=max(0, self.total_frames - 1))
        except Exception:
            pass

        try:
            self.end_var.set(int(self.end_frame_idx))
        except Exception:
            pass

        self.lbl_end.config(text=f"End: {self.end_frame_idx}")

        if save and self.current_video_original is not None:
            self.endframes_by_name[self.current_video_original.name] = int(self.end_frame_idx)
            self._save_endframes(self.endframes_by_name)

    def auto_end_from_start(self, start_idx: int) -> None:
        lap_ms = self._extract_time_ms(self.current_video_original) if self.current_video_original is not None else None
        if lap_ms is None:
            self.set_endframe(self.total_frames - 1, save=True)
            return
        dur_frames = int(round((lap_ms / 1000.0) * max(1.0, self.fps)))
        self.set_endframe(int(start_idx) + int(dur_frames), save=True)

    def save_endframe_from_ui(self) -> None:
        try:
            self.set_endframe(int(self.end_var.get()), save=True)
        except Exception:
            pass

    @staticmethod
    def safe_unlink(p: Path) -> None:
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass

    def close_preview_video(self) -> None:
        self.is_playing = False
        self.current_frame_idx = 0
        self.current_video_original = None
        self.current_video_opened = None
        self.tk_img = None
        self.total_frames = 0
        self.speed_factor = 1.0
        self.btn_play.config(text="▶")

        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
        self.cap = None

        self.preview_label.config(image="", text="")
        self.lbl_frame.config(text="Frame: –")
        self.lbl_loaded.config(text="Video: –")
        self.scrub.configure(from_=0, to=0)
        self.scrub.set(0)
        self._show_preview_controls(False)

    @staticmethod
    def try_open_video(path: Path) -> tuple[cv2.VideoCapture | None, str]:
        c = cv2.VideoCapture(str(path))
        if c is None or not c.isOpened():
            return None, "open_failed"
        ok, frame = c.read()
        if not ok or frame is None:
            try:
                c.release()
            except Exception:
                pass
            return None, "read_failed"
        c.set(cv2.CAP_PROP_POS_FRAMES, 0.0)
        return c, "ok"

    def render_image_from_frame(self, frame) -> None:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)

        area_w = max(200, self.preview_area.winfo_width())
        area_h = max(200, self.preview_area.winfo_height())
        img.thumbnail((area_w, area_h), Image.LANCZOS)

        self.tk_img = ImageTk.PhotoImage(img)
        self.preview_label.config(image=self.tk_img, text="")

    def seek_and_read(self, idx: int) -> bool:
        if self.cap is None:
            return False

        if self.total_frames > 0:
            if idx < 0:
                idx = 0
            if idx > self.total_frames - 1:
                idx = self.total_frames - 1

        self.cap.set(cv2.CAP_PROP_POS_FRAMES, float(idx))
        ok, frame = self.cap.read()
        if not ok or frame is None:
            return False

        self.current_frame_idx = idx

        fps_val = self.cap.get(cv2.CAP_PROP_FPS)
        if fps_val and fps_val > 0.1:
            self.fps = float(fps_val)

        self.render_image_from_frame(frame)
        self.lbl_frame.config(text=f"Frame: {self.current_frame_idx}")

        if (not self.scrub_is_dragging) and self.total_frames > 0:
            self.scrub.set(self.current_frame_idx)

        return True

    def read_next_frame(self) -> bool:
        if self.cap is None:
            return False

        ok, frame = self.cap.read()
        if not ok or frame is None:
            return False

        pos = int(self.cap.get(cv2.CAP_PROP_POS_FRAMES) or 0)
        self.current_frame_idx = max(0, pos - 1)

        fps_val = self.cap.get(cv2.CAP_PROP_FPS)
        if fps_val and fps_val > 0.1:
            self.fps = float(fps_val)

        self.render_image_from_frame(frame)
        self.lbl_frame.config(text=f"Frame: {self.current_frame_idx}")

        if (not self.scrub_is_dragging) and self.total_frames > 0:
            self.scrub.set(self.current_frame_idx)

        return True

    def render_frame(self, idx: int, force: bool = False) -> None:
        now = time.time()
        if (not force) and (now - self.last_render_ts) < 0.02:
            return
        self.last_render_ts = now
        self.seek_and_read(idx)

    def play_tick(self) -> None:
        if not self.is_playing:
            return

        ok = self.read_next_frame()
        if not ok:
            self.is_playing = False
            self.btn_play.config(text="▶")
            return

        base = 1000.0 / max(1.0, self.fps)
        delay = int(base / max(0.25, self.speed_factor))
        delay = max(1, delay)
        self.root.after(delay, self.play_tick)

    def on_play_pause(self) -> None:
        if self.cap is None:
            return
        self.is_playing = not self.is_playing
        self.btn_play.config(text="⏸" if self.is_playing else "▶")
        if self.is_playing:
            self.play_tick()

    def on_prev_frame(self) -> None:
        if self.cap is None:
            return
        self.is_playing = False
        self.btn_play.config(text="▶")
        self.render_frame(self.current_frame_idx - 1, force=True)

    def on_next_frame(self) -> None:
        if self.cap is None:
            return
        self.is_playing = False
        self.btn_play.config(text="▶")
        self.render_frame(self.current_frame_idx + 1, force=True)

    def on_scrub_press(self, _event=None) -> None:
        self.scrub_is_dragging = True

    def on_scrub_release(self, _event=None) -> None:
        self.scrub_is_dragging = False
        if self.cap is None:
            return
        self.is_playing = False
        self.btn_play.config(text="▶")
        self.render_frame(int(self.scrub.get()), force=True)

    def on_scrub_move(self, _event=None) -> None:
        if self.cap is None:
            return
        if self.scrub_is_dragging:
            self.render_frame(int(self.scrub.get()), force=False)

    def set_start_here(self) -> None:
        if self.current_video_original is None:
            return
        self.startframes_by_name[self.current_video_original.name] = int(self.current_frame_idx)
        self._save_startframes(self.startframes_by_name)
        self.auto_end_from_start(int(self.current_frame_idx))

    def cut_current_video(self) -> None:
        if self.current_video_original is None:
            return
        if not self.ffmpeg_exists():
            self.lbl_loaded.config(text="Video: ffmpeg missing (cut not possible)")
            return

        s = self.clamp_frame(int(self.startframes_by_name.get(self.current_video_original.name, 0)))
        e = self.clamp_frame(int(self.end_frame_idx))

        if e <= s:
            self.lbl_loaded.config(text="Video: End frame must be > start frame")
            return

        self.is_playing = False
        self.btn_play.config(text="▶")

        start_sec = s / max(1.0, self.fps)
        dur_sec = (max(0, (e - s) + 1)) / max(1.0, self.fps)

        src = self.current_video_original
        dst_final = self.input_video_dir / src.name
        tmp = self.input_video_dir / (src.stem + "__cut_tmp.mp4")

        self.close_preview_video()

        if src is not None:
            proxy_path = self.proxy_dir / (src.stem + "__proxy_h264.mp4")
            self.safe_unlink(proxy_path)

        progress_win, progress_close = self._show_progress("Cutting", "Video is being cut… Please wait.")
        self.root.update()
        set_cut_progress = self._make_progress_setter(progress_win, label_prefix="Video: Cutting…")

        cut_ok = False
        cut_success_msg = ""
        cut_fail_msg = "Video: Cut failed"
        try:
            self.lbl_loaded.config(text="Video: Cutting…")
            self.root.update_idletasks()
            try:
                set_cut_progress(0.0)
            except Exception:
                pass

            exact_frame_count = max(1, (e - s) + 1)
            used_mode = "exact"
            ok = False
            used_encoder = ""
            err = ""

            try:
                frame_times, keyframes = self._probe_video_frames_for_hybrid_cut(dst_final)
            except Exception as probe_err:
                frame_times = []
                keyframes = []
                err = f"hybrid probe failed: {probe_err}"

            if frame_times and keyframes:
                ok, used_encoder, hybrid_err = self._run_hybrid_exact_cut(
                    src=dst_final,
                    s=int(s),
                    e=int(e),
                    frame_times=frame_times,
                    keyframes=keyframes,
                    out_path=tmp,
                    progress_cb=set_cut_progress,
                )
                if ok:
                    used_mode = "hybrid"
                    err = ""
                elif hybrid_err:
                    err = hybrid_err

            if not ok:
                ok, used_encoder, exact_err = self._run_ffmpeg_with_video_encode_fallback(
                    args_before_video_codec=[
                        "-y",
                        "-i", str(dst_final),
                        # Output-side seek is slower, but prioritizes exactness.
                        "-ss", f"{start_sec:.6f}",
                        "-map", "0:v:0",
                        "-an",
                        "-frames:v", str(int(exact_frame_count)),
                        "-movflags", "+faststart",
                    ],
                    out_path=tmp,
                    encoder_candidates=self._exact_cut_encode_candidates(),
                    progress_total_sec=float(dur_sec),
                    progress_cb=set_cut_progress,
                )
                if ok:
                    used_mode = "exact"
                    err = ""
                elif exact_err:
                    err = exact_err

            if not ok:
                if err:
                    cut_fail_msg = f"Video: Cut failed ({err})"
                else:
                    cut_fail_msg = "Video: Cut failed (no exact H.264 encoder available)"
                self.lbl_loaded.config(text=cut_fail_msg)
                self.safe_unlink(tmp)
                self._sync_from_folders_if_needed_ui(force=True)
                return

            self.safe_unlink(dst_final)
            tmp.replace(dst_final)

            self.startframes_by_name[dst_final.name] = 0
            self._save_startframes(self.startframes_by_name)

            self.endframes_by_name[dst_final.name] = self.clamp_frame(int(dur_sec * max(1.0, self.fps)))
            self._save_endframes(self.endframes_by_name)

            cut_success_msg = f"Video: Cut and replaced ({used_mode}:{used_encoder})"
            try:
                set_cut_progress(100.0)
            except Exception:
                pass
            self.lbl_loaded.config(text=cut_success_msg)
            cut_ok = True
        except Exception as e:
            cut_fail_msg = f"Video: Cut failed ({e})"
            self.lbl_loaded.config(text=cut_fail_msg)
            self.safe_unlink(tmp)
        finally:
            progress_close()

        self._sync_from_folders_if_needed_ui(force=True)
        self.start_crop_for_video(dst_final)
        if cut_ok and cut_success_msg:
            self.lbl_loaded.config(text=cut_success_msg)
        if not cut_ok:
            self.lbl_loaded.config(text=cut_fail_msg)

    def start_crop_for_video(self, video_path: Path) -> None:
        self.close_preview_video()

        self.current_video_original = video_path
        self.current_video_opened = video_path

        c, status = self.try_open_video(video_path)
        if c is None and status in ("open_failed", "read_failed"):
            proxy = self.make_proxy_h264(video_path)
            if proxy is not None:
                self.current_video_opened = proxy
                c, status = self.try_open_video(proxy)

        if c is None:
            self.lbl_loaded.config(text="Video: Cannot be read (codec?)")
            self.preview_label.config(text="This video cannot be read here.\nPlease convert it to H.264 first.\nOr install ffmpeg.")
            self._show_preview_controls(False)
            return

        self.cap = c

        fps_val = self.cap.get(cv2.CAP_PROP_FPS)
        self.fps = float(fps_val) if (fps_val and fps_val > 0.1) else 30.0

        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if self.total_frames < 1:
            self.total_frames = 1

        self.scrub.configure(from_=0, to=max(0, self.total_frames - 1))
        self.scrub.set(0)

        self.speed_factor = 1.0

        start_idx = int(self.startframes_by_name.get(video_path.name, 0))
        self.current_frame_idx = start_idx

        saved_end = self.endframes_by_name.get(video_path.name)
        if saved_end is not None:
            self.set_endframe(int(saved_end), save=False)
        else:
            self.auto_end_from_start(int(start_idx))

        self.lbl_loaded.config(text=f"Video: {video_path.name}")

        self._show_preview_controls(True)

        def _late_render() -> None:
            self.render_frame(self.current_frame_idx, force=True)

        self.root.after(60, _late_render)
