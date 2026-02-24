from __future__ import annotations

from pathlib import Path
import subprocess
import time
from typing import Callable
import tkinter as tk

import cv2
from PIL import Image, ImageTk
from core.encoders import detect_available_encoders
from core.ffmpeg_tools import ffmpeg_exists as _ffmpeg_exists_bundled, resolve_ffmpeg_bin
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
    ) -> tuple[bool, str, str]:
        ffmpeg_bin = resolve_ffmpeg_bin()
        last_error = ""

        candidates = list(encoder_candidates) if encoder_candidates is not None else self._video_encode_candidates()
        for encoder_name, encoder_args in candidates:
            self.safe_unlink(out_path)
            cmd = [
                ffmpeg_bin,
                "-hide_banner",
                "-nostats",
                "-loglevel",
                "error",
                *args_before_video_codec,
                *encoder_args,
                str(out_path),
            ]
            try:
                p = self._run_process_with_ui_pump(cmd)
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

    def _run_process_with_ui_pump(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **windows_no_window_subprocess_kwargs(),
        )
        while proc.poll() is None:
            try:
                self.root.update()
            except Exception:
                pass
            time.sleep(0.03)
        stdout_txt, stderr_txt = proc.communicate()
        return subprocess.CompletedProcess(cmd, int(proc.returncode or 0), stdout_txt or "", stderr_txt or "")

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

        cut_ok = False
        cut_success_msg = ""
        cut_fail_msg = "Video: Cut failed"
        try:
            self.lbl_loaded.config(text="Video: Cutting…")
            self.root.update_idletasks()

            exact_frame_count = max(1, (e - s) + 1)
            ok, used_encoder, err = self._run_ffmpeg_with_video_encode_fallback(
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
            )

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

            cut_success_msg = f"Video: Cut and replaced (exact:{used_encoder})"
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
