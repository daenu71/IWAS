from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, TYPE_CHECKING

from core.models import AppModel

if TYPE_CHECKING:
    from ui.preview.layout_preview import LayoutPreviewController
    from ui.preview.png_preview import PngPreviewController
    from ui.preview.video_preview import VideoPreviewController


@dataclass
class UIContext:
    get_input_video_dir: Callable[[], Path]
    get_input_csv_dir: Callable[[], Path]
    get_current_output_preset: Callable[[], str]
    get_hud_width_px: Callable[[], int]
    get_output_format: Callable[[], dict[str, str]]
    get_hud_layout_data: Callable[[], dict[str, Any]]
    get_png_view_data: Callable[[], dict[str, Any]]
    get_startframes: Callable[[], dict[str, int]]
    get_endframes: Callable[[], dict[str, int]]
    get_selected_files: Callable[[], tuple[list[Path], list[Path]]]
    set_selected_files: Callable[[list[Path], list[Path]], None]
    set_status: Callable[[str], None]
    set_progress: Callable[[float, str], None] | None = None
    set_busy: Callable[[bool], None] | None = None
    set_app_model: Callable[[AppModel], None] | None = None
    set_output_preset: Callable[[str], None] | None = None
    set_hud_width_px: Callable[[int], None] | None = None
    apply_profile_side_effects: Callable[..., None] | None = None
    open_file_dialog: Callable[..., Any] | None = None
    save_file_dialog: Callable[..., Any] | None = None
    show_error: Callable[[str], None] | None = None
    show_info: Callable[[str], None] | None = None
    schedule_after: Callable[[int, Callable[[], None]], Any] | None = None
    save_output_format: Callable[[dict[str, str]], None] | None = None
    get_presets_for_aspect: Callable[[str], list[str]] | None = None
    set_output_preset_values: Callable[[list[str]], None] | None = None
    get_preview_mode: Callable[[], str] | None = None
    refresh_layout_preview: Callable[[], None] | None = None
    render_png_preview: Callable[..., None] | None = None
    png_load_state_for_current: Callable[[], None] | None = None
    png_save_state_for_current: Callable[[], None] | None = None
    close_preview_video: Callable[[], None] | None = None
    refresh_display: Callable[[], None] | None = None
    set_fast_text: Callable[[str], None] | None = None
    set_slow_text: Callable[[str], None] | None = None
    get_profiles_dir: Callable[[], Path] | None = None
    build_profile_dict: Callable[[], dict[str, Any]] | None = None
    apply_profile_dict: Callable[[dict[str, Any]], None] | None = None
    choose_slow_fast_paths: Callable[[], tuple[Path | None, Path | None]] | None = None
    parse_preset: Callable[[str], tuple[int, int]] | None = None
    get_output_video_dir: Callable[[], Path] | None = None
    get_project_root: Callable[[], Path] | None = None
    get_hud_enabled: Callable[[], dict[str, bool]] | None = None
    model_from_ui_state: Callable[[], AppModel] | None = None
    get_hud_boxes_for_current: Callable[[], list[dict]] | None = None
    png_view_key: Callable[[], str] | None = None
    get_png_state: Callable[[], dict[str, dict[str, Any]]] | None = None
    show_progress_with_cancel: (
        Callable[[str, str], tuple[object, Callable[[], None], Callable[[str], None], Callable[[float], None], Callable[[], bool]]] | None
    ) = None
    update_ui: Callable[[], None] | None = None
    show_preview_controls: Callable[[bool], None] | None = None


class Controller:
    def __init__(
        self,
        *,
        ui: UIContext,
        render_service: Any,
        profile_service: Any,
        files_service: Any,
        get_layout_preview_ctrl: Callable[[], LayoutPreviewController | None],
        get_png_preview_ctrl: Callable[[], PngPreviewController | None],
        get_video_preview_ctrl: Callable[[], VideoPreviewController | None],
    ) -> None:
        self.ui = ui
        self.render_service = render_service
        self.profile_service = profile_service
        self.files_service = files_service
        self.get_layout_preview_ctrl = get_layout_preview_ctrl
        self.get_png_preview_ctrl = get_png_preview_ctrl
        self.get_video_preview_ctrl = get_video_preview_ctrl
        self._preview_resize_followup_pending = False

    def _schedule(self, ms: int, fn: Callable[[], None]) -> None:
        if self.ui.schedule_after is not None:
            self.ui.schedule_after(ms, fn)
            return
        fn()

    def _is_video_preview_active(self) -> bool:
        ctrl = self.get_video_preview_ctrl()
        return ctrl is not None and getattr(ctrl, "cap", None) is not None

    def _refresh_active_preview(self, *, force_reload: bool = False) -> None:
        try:
            mode = self.ui.get_preview_mode() if self.ui.get_preview_mode is not None else "layout"
            if mode == "png":
                if self.ui.png_load_state_for_current is not None:
                    self.ui.png_load_state_for_current()
                if self.ui.render_png_preview is not None:
                    self.ui.render_png_preview(force_reload=force_reload)
            else:
                if self.ui.refresh_layout_preview is not None:
                    self.ui.refresh_layout_preview()
        except Exception:
            pass

    @staticmethod
    def _extract_time_ms_from_name(path: Path) -> int | None:
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{3})", path.name)
        if not m:
            return None
        mm, ss, ms = m.groups()
        return int(mm) * 60_000 + int(ss) * 1_000 + int(ms)

    @staticmethod
    def _parse_g61_csv_basename(path: Path) -> tuple[str, str, str, str] | None:
        name = str(path.name or "")
        if not name.lower().endswith(".csv"):
            return None

        stem = name[:-4]
        prefix = "Garage 61 - "
        if not stem.startswith(prefix):
            return None

        rest = stem[len(prefix):]
        try:
            left, track, lap_time, _run_id = rest.rsplit(" - ", 3)
            driver, car = left.split(" - ", 1)
        except ValueError:
            return None

        driver = driver.strip()
        car = car.strip()
        track = track.strip()
        lap_time = lap_time.strip()
        if not driver or not car or not track:
            return None
        if re.fullmatch(r"\d{2}\.\d{2}\.\d{3}", lap_time) is None:
            return None
        return driver, car, track, lap_time

    @staticmethod
    def _sanitize_windows_filename_base(name: str) -> str:
        s = re.sub(r'[\\/:*?"<>|]+', "-", str(name or ""))
        s = re.sub(r"\s+", " ", s).strip()
        s = s.rstrip(" .")
        return s

    @staticmethod
    def _unique_output_path(out_dir: Path, base_name: str, ext: str) -> Path:
        candidate = out_dir / f"{base_name}{ext}"
        if not candidate.exists():
            return candidate
        n = 1
        while True:
            candidate_n = out_dir / f"{base_name} ({n}){ext}"
            if not candidate_n.exists():
                return candidate_n
            n += 1

    def _build_output_name_from_csvs(
        self,
        *,
        csvs: list[Path],
        fast_p: Path,
        slow_p: Path,
        fallback_base: str,
    ) -> str:
        if len(csvs) < 2:
            return fallback_base

        parsed: list[tuple[Path, str, str, str, str, int]] = []
        for c in csvs[:2]:
            row = self._parse_g61_csv_basename(c)
            if row is None:
                return fallback_base
            driver, car, track, lap_time = row
            tms = self._extract_time_ms_from_name(c)
            if tms is None:
                return fallback_base
            parsed.append((c, driver, car, track, lap_time, tms))

        fast_ms = self._extract_time_ms_from_name(fast_p)
        slow_ms = self._extract_time_ms_from_name(slow_p)

        fast_row: tuple[Path, str, str, str, str, int] | None = None
        slow_row: tuple[Path, str, str, str, str, int] | None = None

        if fast_ms is not None and slow_ms is not None:
            for row in parsed:
                if row[5] == fast_ms and fast_row is None:
                    fast_row = row
                elif row[5] == slow_ms and slow_row is None:
                    slow_row = row

        if fast_row is None or slow_row is None:
            rows = sorted(parsed, key=lambda x: x[5])
            if len(rows) < 2:
                return fallback_base
            fast_row, slow_row = rows[0], rows[1]

        track = fast_row[3]
        fast_driver, fast_car, fast_time = fast_row[1], fast_row[2], fast_row[4]
        slow_driver, slow_car, slow_time = slow_row[1], slow_row[2], slow_row[4]

        base = f"{track} - {fast_driver} - {fast_car} - {fast_time} - vs - {slow_driver} - {slow_car} - {slow_time}"
        base = self._sanitize_windows_filename_base(base)
        if not base:
            return fallback_base
        return base

    def on_select_files(self) -> None:
        if self.ui.open_file_dialog is None:
            return

        paths = self.ui.open_file_dialog(
            multiple=True,
            title="Select Files (2 videos + optional CSV)",
            filetypes=[
                ("Videos and CSV", "*.mp4 *.csv"),
                ("Video", "*.mp4"),
                ("CSV", "*.csv"),
            ],
        )

        status, selected_videos, selected_csvs = self.files_service.select_files(
            paths=paths,
            input_video_dir=self.ui.get_input_video_dir(),
            input_csv_dir=self.ui.get_input_csv_dir(),
        )
        if status == "empty":
            return

        current_videos, _current_csvs = self.ui.get_selected_files()
        if status == "csv_only":
            self.ui.set_selected_files(list(current_videos), list(selected_csvs[:2]))
            if self.ui.refresh_display is not None:
                self.ui.refresh_display()
            return

        if status == "need_two_videos":
            self.ui.set_selected_files([], [])
            if self.ui.refresh_display is not None:
                self.ui.refresh_display()
            if self.ui.set_fast_text is not None:
                self.ui.set_fast_text("Fast: Please select exactly 2 videos")
            if self.ui.set_slow_text is not None:
                self.ui.set_slow_text("Slow: â€“")
            if self.ui.close_preview_video is not None:
                self.ui.close_preview_video()
            return

        self.ui.set_selected_files(list(selected_videos[:2]), list(selected_csvs[:2]))
        if self.ui.refresh_display is not None:
            self.ui.refresh_display()

    def on_generate(self) -> None:
        videos, csvs = self.ui.get_selected_files()
        if len(videos) != 2:
            self.ui.set_status("Video: Please select exactly 2 videos")
            return

        if self.ui.choose_slow_fast_paths is None:
            return
        slow_p, fast_p = self.ui.choose_slow_fast_paths()
        if slow_p is None or fast_p is None:
            self.ui.set_status("Video: Missing time in filename (Fast/Slow)")
            return

        if self.ui.parse_preset is None:
            return
        out_w, out_h = self.ui.parse_preset(self.ui.get_current_output_preset())
        if out_w <= 0 or out_h <= 0:
            out_w, out_h = 1280, 720

        hud_w = int(self.ui.get_hud_width_px())
        hud_w = max(0, min(hud_w, max(0, out_w - 2)))

        if self.ui.get_output_video_dir is None:
            return
        out_dir = self.ui.get_output_video_dir()
        ts = time.strftime("%Y%m%d-%H%M%S")
        fallback_base = f"compare_{ts}_{out_w}x{out_h}_hud{hud_w}"
        base_name = self._build_output_name_from_csvs(
            csvs=list(csvs),
            fast_p=fast_p,
            slow_p=slow_p,
            fallback_base=fallback_base,
        )
        base_name = self._sanitize_windows_filename_base(base_name) or fallback_base
        out_path = self._unique_output_path(out_dir, base_name, ".mp4")

        hud_enabled: dict[str, bool] = {}
        if self.ui.get_hud_enabled is not None:
            try:
                hud_enabled = self.ui.get_hud_enabled()
            except Exception:
                hud_enabled = {}

        if self.ui.get_project_root is None:
            return
        project_root_local = self.ui.get_project_root()
        main_py = project_root_local / "src" / "main.py"
        if not main_py.exists():
            self.ui.set_status("Video: main.py not found")
            return

        if self.ui.model_from_ui_state is None:
            return
        app_model = self.ui.model_from_ui_state()
        requested_video_mode = str(getattr(app_model, "video_mode", "full") or "full").strip().lower()
        if requested_video_mode not in ("full", "cut"):
            requested_video_mode = "full"
        if self.ui.set_app_model is not None:
            try:
                self.ui.set_app_model(app_model)
            except Exception:
                pass

        if self.ui.show_progress_with_cancel is None:
            return
        _win, close, set_text, set_progress, is_cancelled = self.ui.show_progress_with_cancel(
            "Generate Video",
            "Starting main.pyâ€¦",
        )
        if self.ui.update_ui is not None:
            try:
                self.ui.update_ui()
            except Exception:
                pass

        def on_progress(pct: float, text: str) -> None:
            try:
                set_progress(float(pct))
            except Exception:
                pass
            if str(text):
                try:
                    set_text(str(text))
                except Exception:
                    pass

        def worker() -> None:
            try:
                result = self.render_service.start_render(
                    project_root=project_root_local,
                    videos=list(videos),
                    csvs=list(csvs),
                    slow_p=slow_p,
                    fast_p=fast_p,
                    out_path=out_path,
                    out_aspect=str(self.ui.get_output_format().get("aspect", "")),
                    out_preset=str(self.ui.get_output_format().get("preset", "")),
                    out_quality=str(self.ui.get_output_format().get("quality", "")),
                    hud_w=int(hud_w),
                    hud_enabled=hud_enabled,
                    app_model=app_model,
                    get_hud_boxes_for_current=self.ui.get_hud_boxes_for_current,
                    png_save_state_for_current=self.ui.png_save_state_for_current,
                    png_view_key=self.ui.png_view_key,
                    png_state=self.ui.get_png_state(),
                    is_cancelled=is_cancelled,
                    on_progress=on_progress,
                )

                def finish_ok() -> None:
                    close()
                    cut_zero_segments_fallback = bool(result.get("cut_zero_segments_fallback"))
                    out_ok = out_path.exists() and out_path.stat().st_size > 0
                    if requested_video_mode == "cut" and cut_zero_segments_fallback and out_ok:
                        self.ui.set_status("Cut: 0 segments -> rendered full")
                    elif out_ok:
                        self.ui.set_status(f"Video: Done ({out_path.name})")
                    else:
                        self.ui.set_status("Video: Render failed (0 KB)")

                def finish_cancel() -> None:
                    close()
                    try:
                        if out_path.exists():
                            out_path.unlink()
                    except Exception:
                        pass
                    self.ui.set_status("Video: Cancelled")

                def finish_error() -> None:
                    close()
                    err = str(result.get("error") or "")
                    if err == "ui_json_write_failed":
                        self.ui.set_status("Video: Could not write UI JSON")
                    elif err == "main_py_not_found":
                        self.ui.set_status("Video: main.py not found")
                    else:
                        self.ui.set_status("Video: Render failed")

                if str(result.get("status") or "") == "cancelled":
                    self._schedule(0, finish_cancel)
                elif str(result.get("status") or "") == "ok":
                    self._schedule(0, finish_ok)
                else:
                    self._schedule(0, finish_error)

            except Exception:
                try:
                    self._schedule(0, lambda: self.ui.set_status("Video: Render failed"))
                    self._schedule(0, close)
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def on_profile_save(self) -> None:
        if self.ui.save_file_dialog is None:
            return

        try:
            profiles_dir = self.ui.get_profiles_dir() if self.ui.get_profiles_dir is not None else None
        except Exception:
            profiles_dir = None

        fn = self.ui.save_file_dialog(
            title="Save Profile",
            defaultextension=".json",
            filetypes=[("Profile (*.json)", "*.json"), ("All Files", "*.*")],
            initialdir=str(profiles_dir) if profiles_dir is not None else "",
            initialfile="profile.json",
        )
        if not fn:
            return

        try:
            try:
                if self.ui.png_save_state_for_current is not None:
                    self.ui.png_save_state_for_current()
            except Exception:
                pass

            data = self.ui.build_profile_dict() if self.ui.build_profile_dict is not None else {}
            Path(fn).write_text(json.dumps(data, indent=2), encoding="utf-8")
            self.ui.set_status("Video: Profile saved")
        except Exception:
            self.ui.set_status("Video: Failed to save profile")

    def on_profile_load(self) -> None:
        if self.ui.open_file_dialog is None:
            return

        fn = self.ui.open_file_dialog(
            title="Load Profile",
            defaultextension=".json",
            filetypes=[("Profile (*.json)", "*.json"), ("All Files", "*.*")],
            initialdir=str(self.ui.get_profiles_dir()) if self.ui.get_profiles_dir is not None else "",
        )
        if not fn:
            return

        try:
            data = json.loads(Path(fn).read_text(encoding="utf-8"))
        except Exception:
            self.ui.set_status("Video: Failed to load profile")
            return

        try:
            if self.ui.apply_profile_dict is not None:
                self.ui.apply_profile_dict(data)
            self.ui.set_status("Video: Profile loaded")
        except Exception:
            self.ui.set_status("Video: Failed to load profile")

    def on_output_change(self, _event: Any = None) -> None:
        try:
            if self.ui.get_presets_for_aspect is not None and self.ui.set_output_preset_values is not None:
                out = self.ui.get_output_format()
                presets = self.ui.get_presets_for_aspect(str(out.get("aspect", "")))
                self.ui.set_output_preset_values(presets)
                if self.ui.set_output_preset is not None and str(out.get("preset", "")) not in presets:
                    self.ui.set_output_preset(presets[0])
        except Exception:
            pass

        if self.ui.save_output_format is not None:
            out = self.ui.get_output_format()
            self.ui.save_output_format(
                {
                    "aspect": str(out.get("aspect", "")),
                    "preset": str(out.get("preset", "")),
                    "quality": str(out.get("quality", "")),
                }
            )

        if self._is_video_preview_active():
            return
        self._refresh_active_preview(force_reload=False)

    def on_hud_width_change(self, _event: Any = None) -> None:
        if self.ui.save_output_format is not None:
            self.ui.save_output_format({"hud_width_px": str(self.ui.get_hud_width_px())})
        self._refresh_active_preview(force_reload=False)

    def on_preview_mode_change(self, *_args: Any) -> None:
        if self._is_video_preview_active():
            return
        try:
            if self.ui.show_preview_controls is not None:
                self.ui.show_preview_controls(False)
        except Exception:
            pass

    def on_preview_resize(self, _event: Any = None) -> None:
        def _refresh_after_resize() -> None:
            video_ctrl = self.get_video_preview_ctrl()
            if video_ctrl is not None and getattr(video_ctrl, "cap", None) is not None:
                video_ctrl.render_frame(video_ctrl.current_frame_idx, force=True)
                return
            self._refresh_active_preview(force_reload=False)

        video_ctrl = self.get_video_preview_ctrl()
        if video_ctrl is not None and getattr(video_ctrl, "cap", None) is not None:
            video_ctrl.render_frame(video_ctrl.current_frame_idx, force=True)
        else:
            self._refresh_active_preview(force_reload=False)

        if self._preview_resize_followup_pending:
            return
        self._preview_resize_followup_pending = True

        def _followup() -> None:
            self._preview_resize_followup_pending = False
            _refresh_after_resize()

        self._schedule(16, _followup)

