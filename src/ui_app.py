import tkinter as tk
from tkinter import ttk, filedialog
import re
from pathlib import Path
import shutil
import os
import json
import time
import subprocess
import threading
import sys

import cv2
from PIL import Image, ImageTk

from core.models import (
    AppModel,
    HudLayoutState,
    OutputFormat,
    PngViewState,
    Profile,
)
from core import persistence, filesvc
from core.render_service import start_render
from preview.layout_preview import LayoutPreviewController, OutputFormat as LayoutPreviewOutputFormat
from preview.png_preview import PngPreviewController
from preview.video_preview import VideoPreviewController


TIME_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{3})")


def find_project_root(script_path: Path) -> Path:
    p = script_path.resolve()
    for parent in [p.parent] + list(p.parents):
        if (parent / "requirements.txt").exists():
            return parent
    return p.parent


def extract_time_ms(path: Path) -> int | None:
    m = TIME_RE.search(path.name)
    if not m:
        return None
    mm, ss, ms = m.groups()
    return int(mm) * 60_000 + int(ss) * 1_000 + int(ms)


def extract_time_str(path: Path) -> str | None:
    m = TIME_RE.search(path.name)
    if not m:
        return None
    return m.group(0)


def shorten_prefix(text: str, max_len: int) -> str:
    s = text or ""
    if len(s) <= max_len:
        return s
    if max_len <= 1:
        return "…"
    return s[: max_len - 1] + "…"


class HoverTooltip:
    def __init__(self, widget: tk.Widget) -> None:
        self.widget = widget
        self._text = ""
        self._tip: tk.Toplevel | None = None
        self.widget.bind("<Enter>", self._on_enter, add="+")
        self.widget.bind("<Leave>", self._on_leave, add="+")
        self.widget.bind("<Motion>", self._on_motion, add="+")

    def set_text(self, text: str) -> None:
        self._text = text or ""

    def _on_enter(self, _event=None) -> None:
        if self._text.strip() == "":
            return
        self._show()

    def _on_leave(self, _event=None) -> None:
        self._hide()

    def _on_motion(self, event) -> None:
        if self._tip is None:
            return
        x = event.x_root + 12
        y = event.y_root + 12
        self._tip.geometry(f"+{x}+{y}")

    def _show(self) -> None:
        if self._tip is not None:
            return
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.attributes("-topmost", True)
        lbl = tk.Label(
            self._tip,
            text=self._text,
            justify="left",
            relief="solid",
            borderwidth=1,
            padx=6,
            pady=4,
            background="#ffffe0",
        )
        lbl.pack()
        x = self.widget.winfo_pointerx() + 12
        y = self.widget.winfo_pointery() + 12
        self._tip.geometry(f"+{x}+{y}")

    def _hide(self) -> None:
        if self._tip is None:
            return
        try:
            self._tip.destroy()
        except Exception:
            pass
        self._tip = None


def main() -> None:
    root = tk.Tk()
    root.title("iRacing Video Compare")
    root.geometry("1200x800")

    project_root = find_project_root(Path(__file__))

    input_video_dir = project_root / "input" / "video"
    
    # --- ENV Dump (IRVC_*, RVA_*) ---
    try:
        import os

        env_keys = sorted(
            k for k in os.environ.keys()
            if k.startswith("IRVC_") or k.startswith("RVA_")
        )

        debug_dir = project_root / "_logs"
        debug_dir.mkdir(parents=True, exist_ok=True)
        env_log = debug_dir / "ui_app_env.txt"

        lines: list[str] = []
        if env_keys:
            lines.append("[env] aktive IRVC_/RVA_-Variablen:")
            for k in env_keys:
                lines.append(f"[env]   {k}={os.environ.get(k)}")
        else:
            lines.append("[env] keine IRVC_/RVA_-Variablen gesetzt")

        # In Datei schreiben (überschreibt pro Start)
        try:
            env_log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            pass

        # Zusätzlich in die Konsole (hilft beim Debuggen)
        try:
            for ln in lines:
                print(ln)
        except Exception:
            pass

    except Exception:
        pass
    # --- /ENV Dump ---

    input_csv_dir = project_root / "input" / "csv"
    input_video_dir.mkdir(parents=True, exist_ok=True)
    input_csv_dir.mkdir(parents=True, exist_ok=True)

    cache_dir = project_root / "cache"
    proxy_dir = cache_dir / "proxy"
    proxy_dir.mkdir(parents=True, exist_ok=True)
    
    output_dir = project_root / "output"
    output_video_dir = output_dir / "video"
    output_video_dir.mkdir(parents=True, exist_ok=True)

    config_dir = project_root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    startframes_by_name: dict[str, int] = persistence.load_startframes()
    
    profiles_dir = config_dir / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)

    endframes_by_name: dict[str, int] = persistence.load_endframes()

    frame_files = ttk.LabelFrame(root, text="Dateibereich")
    frame_preview = ttk.LabelFrame(root, text="Vorschau")
    frame_settings = ttk.LabelFrame(root, text="Einstellungen")

    frame_files.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
    frame_settings.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

    # Vorschau soll die ganze rechte Seite füllen (ohne "Aktionen")
    frame_preview.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=10, pady=10)
    # Grid-Gewichte: rechte Seite (Vorschau) wächst mit dem Fenster
    root.grid_columnconfigure(0, weight=0)
    root.grid_columnconfigure(1, weight=1)
    root.grid_rowconfigure(0, weight=1)
    root.grid_rowconfigure(1, weight=0)

    # ---- Output-Format (Story 4) ----
    png_view_data: dict = persistence.load_png_view()

    def get_hud_width_px() -> int:
        try:
            v = int(hud_width_var.get())
        except Exception:
            v = 0
        if v < 0:
            v = 0
        return int(v)

    def png_view_key() -> str:
        # Pro Output-Preset + HUD-Breite separat speichern (wie HUD-Layout)
        return f"{out_preset_var.get()}|hud{get_hud_width_px()}"

    HUD_TYPES = [
        "Speed",
        "Throttle / Brake",
        "Steering",
        "Delta",
        "Gear & RPM",
        "Line Delta",
        "Under-/Oversteer",
    ]

    def default_hud_boxes() -> list[dict]:
        # Koordinaten sind in "Output-Pixeln" (bezogen auf das Output-Format)
        # x/y werden später in die HUD-Mitte eingeschränkt
        return [
            {"type": "Speed", "x": 0, "y": 40, "w": 260, "h": 90},
            {"type": "Throttle / Brake", "x": 0, "y": 160, "w": 320, "h": 140},
            {"type": "Steering", "x": 0, "y": 330, "w": 320, "h": 140},
            {"type": "Delta", "x": 0, "y": 500, "w": 320, "h": 110},

            {"type": "Gear & RPM", "x": 0, "y": 630, "w": 260, "h": 90},
            {"type": "Line Delta", "x": 0, "y": 740, "w": 320, "h": 110},
            {"type": "Under-/Oversteer", "x": 0, "y": 870, "w": 320, "h": 110},
        ]

    hud_layout_data: dict = persistence.load_hud_layout()

    def hud_layout_key() -> str:
        # Pro Output-Preset + HUD-Breite separat speichern
        return f"{out_preset_var.get()}|hud{get_hud_width_px()}"

    def get_hud_boxes_for_current() -> list[dict]:
        key = hud_layout_key()
        boxes = hud_layout_data.get(key)
        if isinstance(boxes, list) and len(boxes) > 0:
            out: list[dict] = []
            for b in boxes:
                if not isinstance(b, dict):
                    continue
                t = str(b.get("type") or "").strip()
                if t not in HUD_TYPES:
                    continue
                try:
                    x = int(b.get("x", 0))
                    y = int(b.get("y", 0))
                    w = int(b.get("w", 200))
                    h = int(b.get("h", 100))
                except Exception:
                    continue
                out.append({"type": t, "x": x, "y": y, "w": max(40, w), "h": max(30, h)})

            if len(out) > 0:
                # Fehlende neue Boxen automatisch ergänzen (ohne Reset)
                have = {str(b.get("type") or "") for b in out}
                for d in default_hud_boxes():
                    tt = str(d.get("type") or "")
                    if tt in HUD_TYPES and tt not in have:
                        out.append(d)
                return out

        return default_hud_boxes()

    def set_hud_boxes_for_current(boxes: list[dict]) -> None:
        hud_layout_data[hud_layout_key()] = boxes
        persistence.save_hud_layout(hud_layout_data)

    # Auswahlmöglichkeiten
    ASPECTS = ["32:9", "21:9", "16:9"]

    PRESETS_BY_ASPECT = {
        "32:9": ["5120x1440", "3840x1080", "2560x720"],
        "21:9": ["3440x1440", "2560x1080", "1920x800"],
        "16:9": ["3840x2160", "2560x1440", "1920x1080", "1280x720"],
    }

    def get_presets_for_aspect(a: str) -> list[str]:
        return list(PRESETS_BY_ASPECT.get(a, ["1920x1080"]))

    sel = persistence.load_output_format()
    out_aspect_var = tk.StringVar(value=sel.get("aspect", "32:9"))
    out_preset_var = tk.StringVar(value=sel.get("preset", get_presets_for_aspect(sel.get("aspect", "32:9"))[0]))

    # UI im Einstellungen-Block
    frame_settings.columnconfigure(0, weight=0)
    frame_settings.columnconfigure(1, weight=1)
    frame_settings.columnconfigure(2, weight=0)

    ttk.Label(frame_settings, text="Output-Format", font=("Segoe UI", 10, "bold")).grid(
        row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(10, 6)
    )

    ttk.Label(frame_settings, text="Seitenverhältnis:").grid(row=1, column=0, sticky="w", padx=10, pady=2)
    cmb_aspect = ttk.Combobox(frame_settings, values=ASPECTS, textvariable=out_aspect_var, state="readonly", width=10)
    cmb_aspect.grid(row=1, column=1, sticky="w", padx=10, pady=2)

    QUALITYS = ["Original", "2160p", "1440p", "1080p", "720p", "480p"]
    out_quality_var = tk.StringVar(value=sel.get("quality", "Original"))

    lbl_in_res = ttk.Label(frame_settings, text="Input-Auflösung: –")
    lbl_in_res.grid(row=2, column=0, columnspan=3, sticky="w", padx=10, pady=(2, 2))

    ttk.Label(frame_settings, text="Qualität (Output):").grid(row=3, column=0, sticky="w", padx=10, pady=2)
    cmb_quality = ttk.Combobox(frame_settings, values=QUALITYS, textvariable=out_quality_var, state="readonly", width=10)
    cmb_quality.grid(row=3, column=1, sticky="w", padx=10, pady=2)

    ttk.Label(frame_settings, text="Auflösung (Output):").grid(row=4, column=0, sticky="w", padx=10, pady=2)
    cmb_preset = ttk.Combobox(
        frame_settings, values=get_presets_for_aspect(out_aspect_var.get()), textvariable=out_preset_var, state="readonly", width=12
    )
    cmb_preset.grid(row=4, column=1, sticky="w", padx=10, pady=2)
    

    # HUD-Breite (Mitte)
    try:
        hud_default = int(str(sel.get("hud_width_px", "320")).strip())
    except Exception:
        hud_default = 320
    hud_width_var = tk.IntVar(value=max(0, hud_default))

    # --- Mapping-Layer (Story 2): UI-State <-> zentrale Modelle ---
    app_model = AppModel()

    def model_from_ui_state() -> AppModel:
        return AppModel(
            output=OutputFormat(
                aspect=str(out_aspect_var.get()),
                preset=str(out_preset_var.get()),
                quality=str(out_quality_var.get()),
                hud_width_px=int(get_hud_width_px()),
            ),
            hud_layout=HudLayoutState(hud_layout_data=hud_layout_data),
            png_view=PngViewState(png_view_data=png_view_data),
        )

    def apply_model_to_ui_state(model: AppModel) -> None:
        nonlocal hud_layout_data, png_view_data, app_model
        if not isinstance(model, AppModel):
            return
        app_model = model
        try:
            out_aspect_var.set(str(model.output.aspect))
        except Exception:
            pass
        try:
            out_preset_var.set(str(model.output.preset))
        except Exception:
            pass
        try:
            out_quality_var.set(str(model.output.quality))
        except Exception:
            pass
        try:
            hud_width_var.set(max(0, int(model.output.hud_width_px)))
        except Exception:
            pass
        if isinstance(model.hud_layout.hud_layout_data, dict):
            hud_layout_data = model.hud_layout.hud_layout_data
        if isinstance(model.png_view.png_view_data, dict):
            png_view_data = model.png_view.png_view_data

    def profile_model_from_ui_state(
        videos_names: list[str],
        csv_names: list[str],
        starts: dict[str, int],
        ends: dict[str, int],
    ) -> Profile:
        m = model_from_ui_state()
        return Profile(
            version=1,
            videos=videos_names,
            csvs=csv_names,
            startframes=starts,
            endframes=ends,
            output=m.output,
            hud_layout_data=m.hud_layout.hud_layout_data,
            png_view_data=m.png_view.png_view_data,
        )

    apply_model_to_ui_state(model_from_ui_state())

    ttk.Label(frame_settings, text="HUD-Breite (px):").grid(row=5, column=0, sticky="w", padx=10, pady=2)
    spn_hud = ttk.Spinbox(frame_settings, from_=0, to=10000, width=10, textvariable=hud_width_var)
    spn_hud.grid(row=5, column=1, sticky="w", padx=10, pady=2)

    lbl_out_fps = ttk.Label(frame_settings, text="FPS (vom Video): –")
    lbl_out_fps.grid(row=6, column=0, columnspan=3, sticky="w", padx=10, pady=(6, 6))

    def on_hud_width_change(_event=None) -> None:
        persistence.save_output_format({"hud_width_px": str(get_hud_width_px())})
        try:
            if preview_mode_var.get() == "png":
                png_load_state_for_current()
                render_png_preview(force_reload=False)
            else:
                refresh_layout_preview()
        except Exception:
            pass

    try:
        hud_width_var.trace_add("write", lambda *_: on_hud_width_change())
    except Exception:
        pass
    spn_hud.bind("<Return>", on_hud_width_change)
    spn_hud.bind("<FocusOut>", on_hud_width_change)

    # ---- HUD Platzhalter (Story 6) ----
    ttk.Separator(frame_settings, orient="horizontal").grid(row=7, column=0, columnspan=3, sticky="ew", padx=10, pady=(8, 8))

    ttk.Label(frame_settings, text="HUD Platzhalter", font=("Segoe UI", 10, "bold")).grid(
        row=8, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 6)
    )

    hud_enabled_vars: dict[str, tk.BooleanVar] = {}
    for i, t in enumerate(HUD_TYPES):
        var = tk.BooleanVar(value=True)
        hud_enabled_vars[t] = var
        cb = ttk.Checkbutton(frame_settings, text=t, variable=var)
        cb.grid(row=9 + i, column=0, columnspan=2, sticky="w", padx=10, pady=1)

        def _make_cb_handler():
            def _h(*_args):
                try:
                    if preview_mode_var.get() == "png":
                        render_png_preview(force_reload=False)
                    else:
                        refresh_layout_preview()
                except Exception:
                    pass
            return _h

        try:
            var.trace_add("write", _make_cb_handler())
        except Exception:
            pass

    def reset_hud_layout() -> None:
        boxes = default_hud_boxes()
        set_hud_boxes_for_current(boxes)
        try:
            refresh_layout_preview()
        except Exception:
            pass

    btn_reset_hud = ttk.Button(frame_settings, text="HUD zurücksetzen", command=reset_hud_layout)
    btn_reset_hud.grid(row=9 + len(HUD_TYPES), column=0, sticky="w", padx=10, pady=(6, 2))

    def hud_fit_to_frame_width() -> None:
        """
        Setzt alle AKTIVIERTEN HUD-Boxen (Checkbox) auf:
        - x = linke HUD-Kante (hud_x0)
        - w = hud_w (HUD-Breite, z.B. 800px)
        - y/h bleiben, werden aber in den Output-Bereich geclamped
        """
        nonlocal hud_boxes

        # Aktuellen Zustand holen (damit wir garantiert mit dem aktuellen Layout-Key arbeiten)
        try:
            hud_boxes = get_hud_boxes_for_current()
        except Exception:
            pass

        out_w, out_h = parse_preset(out_preset_var.get())
        if out_w <= 0 or out_h <= 0:
            out_w, out_h = 1280, 720

        hud_w = get_hud_width_px()
        hud_w = max(0, min(int(hud_w), max(0, out_w - 2)))

        side_w = int((out_w - hud_w) / 2)
        if side_w < 0:
            side_w = 0

        hud_x0 = int(side_w)

        en = enabled_types()

        for b in hud_boxes:
            try:
                t = str(b.get("type") or "")
            except Exception:
                continue
            if t not in en:
                continue

            try:
                y = int(b.get("y", 0))
                h = int(b.get("h", 100))
            except Exception:
                y = 0
                h = 100

            h = max(30, int(h))
            y = clamp(int(y), 0, max(0, int(out_h) - h))

            b["x"] = int(hud_x0)
            b["y"] = int(y)
            b["w"] = int(hud_w)
            b["h"] = int(h)

        # Persistieren + neu zeichnen
        try:
            save_current_boxes()
        except Exception:
            pass

        try:
            if preview_mode_var.get() == "png":
                render_png_preview(force_reload=False)
            else:
                refresh_layout_preview()
        except Exception:
            pass

    btn_hud_fit = ttk.Button(frame_settings, text="HUDs auf Rahmenbreite", command=hud_fit_to_frame_width)
    btn_hud_fit.grid(row=9 + len(HUD_TYPES), column=1, sticky="w", padx=(10, 0), pady=(6, 2))


    def parse_preset(preset: str) -> tuple[int, int]:
        s = (preset or "").lower().replace("×", "x").strip()
        if "x" not in s:
            return 0, 0
        a, b = s.split("x", 1)
        try:
            return int(a.strip()), int(b.strip())
        except Exception:
            return 0, 0

    def parse_aspect(aspect: str) -> tuple[int, int]:
        s = (aspect or "").strip()
        if ":" not in s:
            return 0, 0
        a, b = s.split(":", 1)
        try:
            return int(a.strip()), int(b.strip())
        except Exception:
            return 0, 0

    def ffprobe_exists() -> bool:
        try:
            from shutil import which
            return which("ffprobe") is not None
        except Exception:
            return False

    def ffprobe_get_video_info(p: Path) -> tuple[int, int, float]:
        if not ffprobe_exists():
            return 0, 0, 0.0
        try:
            cmd = [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,r_frame_rate",
                "-of", "json",
                str(p),
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=0.8)
            if r.returncode != 0:
                return 0, 0, 0.0

            data = json.loads(r.stdout or "{}")
            streams = data.get("streams") or []
            if not streams:
                return 0, 0, 0.0

            s0 = streams[0] or {}
            w = int(s0.get("width") or 0)
            h = int(s0.get("height") or 0)

            fps = 0.0
            fr = str(s0.get("r_frame_rate") or "").strip()
            if "/" in fr:
                a, b = fr.split("/", 1)
                try:
                    aa = float(a.strip())
                    bb = float(b.strip())
                    if bb > 0:
                        fps = aa / bb
                except Exception:
                    fps = 0.0
            return w, h, fps
        except Exception:
            return 0, 0, 0.0

    def get_video_resolution(p: Path) -> tuple[int, int]:
        w, h, _fps = ffprobe_get_video_info(p)
        return w, h

    # ---- Video-Info Cache (nicht blockierend) ----
    video_info_cache: dict[str, tuple[int, int, float]] = {}
    video_info_inflight: set[str] = set()

    def request_video_info(path: Path) -> None:
        key = str(path)
        if key in video_info_cache:
            return
        if key in video_info_inflight:
            return
        video_info_inflight.add(key)

        def worker() -> None:
            w, h, fp = ffprobe_get_video_info(path)

            def apply() -> None:
                try:
                    video_info_cache[key] = (int(w), int(h), float(fp))
                except Exception:
                    video_info_cache[key] = (0, 0, 0.0)
                try:
                    video_info_inflight.discard(key)
                except Exception:
                    pass
                try:
                    refresh_display()
                except Exception:
                    pass

            try:
                root.after(0, apply)
            except Exception:
                pass

        pass

    def fmt_res(w: int, h: int) -> str:
        if w <= 0 or h <= 0:
            return "–"
        return f"{w}×{h}"

    def quality_to_height(q: str) -> int:
        s = (q or "").strip().lower()
        if s == "original":
            return 0
        if s.endswith("p"):
            s = s[:-1]
        try:
            return int(s)
        except Exception:
            return 0

    def compute_output_preset_from_quality(
        src_w: int, src_h: int, aspect: str, quality: str, fallback_preset: str
    ) -> str:
        aw, ah = parse_aspect(aspect)
        if aw <= 0 or ah <= 0:
            aw, ah = 32, 9

        if src_w <= 0 or src_h <= 0:
            return fallback_preset

        target_h = quality_to_height(quality)
        if target_h <= 0:
            return f"{src_w}x{src_h}"

        target_h = min(target_h, src_h)

        target_w = int(round(target_h * (aw / max(1, ah))))
        if target_w <= 0:
            return fallback_preset

        if target_w > src_w:
            scale = src_w / max(1, target_w)
            target_w = int(round(target_w * scale))
            target_h = int(round(target_h * scale))

        return f"{max(1, target_w)}x{max(1, target_h)}"

    def on_output_change(_event=None) -> None:
        # Presets je Seitenverhältnis aktualisieren
        try:
            presets = get_presets_for_aspect(out_aspect_var.get())
            cmb_preset.config(values=presets)
            if out_preset_var.get() not in presets:
                out_preset_var.set(presets[0])
        except Exception:
            pass

        persistence.save_output_format(
            {"aspect": out_aspect_var.get(), "preset": out_preset_var.get(), "quality": out_quality_var.get()}
        )

        if video_preview_ctrl is not None and video_preview_ctrl.cap is not None:
            return

        try:
            if preview_mode_var.get() == "png":
                png_load_state_for_current()
                render_png_preview(force_reload=False)
            else:
                refresh_layout_preview()
        except Exception:
            pass

    cmb_aspect.bind("<<ComboboxSelected>>", on_output_change)
    cmb_preset.bind("<<ComboboxSelected>>", on_output_change)
    cmb_quality.bind("<<ComboboxSelected>>", on_output_change)

    # ---- Dateibereich ----

    # Nur 2 Spalten (Text + "…"), damit der linke Bereich nicht unnötig breit wird
    frame_files.columnconfigure(0, weight=0)
    frame_files.columnconfigure(1, weight=0)

    # Button-Leiste in Zeile 0, aber ohne zusätzliche Grid-Spalten
    top_buttons = ttk.Frame(frame_files)
    top_buttons.grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=6)

    btn_select = ttk.Button(top_buttons, text="Dateien auswählen")
    btn_select.pack(side="left", padx=(0, 10))

    btn_generate = ttk.Button(top_buttons, text="Video erzeugen")
    btn_generate.pack(side="left", padx=(0, 10))

    btn_profile_save = ttk.Button(top_buttons, text="Profil speichern")
    btn_profile_save.pack(side="left", padx=(0, 10))

    btn_profile_load = ttk.Button(top_buttons, text="Profil laden")
    btn_profile_load.pack(side="left")

    videos: list[Path] = []
    csvs: list[Path] = []

    lbl_v1 = ttk.Label(frame_files, text="Video 1: –")
    btn_v1 = ttk.Button(frame_files, text="...", width=3)
    lbl_v2 = ttk.Label(frame_files, text="Video 2: –")
    btn_v2 = ttk.Button(frame_files, text="...", width=3)

    lbl_c1 = ttk.Label(frame_files, text="CSV 1: –")
    btn_c1 = ttk.Button(frame_files, text="...", width=3)
    lbl_c2 = ttk.Label(frame_files, text="CSV 2: –")
    btn_c2 = ttk.Button(frame_files, text="...", width=3)

    lbl_v1.grid(row=1, column=0, sticky="w", padx=10, pady=2)
    btn_v1.grid(row=1, column=1, sticky="e", padx=10, pady=2)
    lbl_v2.grid(row=2, column=0, sticky="w", padx=10, pady=2)
    btn_v2.grid(row=2, column=1, sticky="e", padx=10, pady=2)

    lbl_c1.grid(row=3, column=0, sticky="w", padx=10, pady=2)
    btn_c1.grid(row=3, column=1, sticky="e", padx=10, pady=2)
    lbl_c2.grid(row=4, column=0, sticky="w", padx=10, pady=2)
    btn_c2.grid(row=4, column=1, sticky="e", padx=10, pady=2)

    lbl_fast = ttk.Label(frame_files, text="Fast: –", font=("Segoe UI", 10, "bold"))
    lbl_slow = ttk.Label(frame_files, text="Slow: –", font=("Segoe UI", 10, "bold"))
    lbl_fast.grid(row=5, column=0, sticky="w", padx=10, pady=(10, 2))
    lbl_slow.grid(row=6, column=0, sticky="w", padx=10, pady=(2, 10))

    tip_v1 = HoverTooltip(lbl_v1)
    tip_v2 = HoverTooltip(lbl_v2)
    tip_c1 = HoverTooltip(lbl_c1)
    tip_c2 = HoverTooltip(lbl_c2)

    def clear_result() -> None:
        lbl_fast.config(text="Fast: –")
        lbl_slow.config(text="Slow: –")

    def set_row(label: ttk.Label, tip: HoverTooltip, prefix: str, path: Path | None) -> None:
        if path is None:
            label.config(text=f"{prefix}: –")
            tip.set_text("")
            return
        short = shorten_prefix(path.name, max_len=55)
        label.config(text=f"{prefix}: {short}")
        tip.set_text(path.name)

    def render_output_preview() -> None:
        # Placeholder: existiert nur wegen alter Logik. Wird bewusst nicht genutzt.
        return
        
    def build_profile_dict() -> dict:
        nonlocal app_model
        vnames: list[str] = []
        cnames: list[str] = []

        for p in videos[:2]:
            try:
                vnames.append(p.name)
            except Exception:
                pass

        for p in csvs[:2]:
            try:
                cnames.append(p.name)
            except Exception:
                pass

        starts: dict[str, int] = {}
        ends: dict[str, int] = {}

        for n in vnames:
            try:
                starts[n] = int(startframes_by_name.get(n, 0))
            except Exception:
                starts[n] = 0
            try:
                ends[n] = int(endframes_by_name.get(n, 0))
            except Exception:
                ends[n] = 0

        profile = profile_model_from_ui_state(vnames, cnames, starts, ends)
        app_model = AppModel(
            output=profile.output,
            hud_layout=HudLayoutState(hud_layout_data=profile.hud_layout_data),
            png_view=PngViewState(png_view_data=profile.png_view_data),
        )
        return profile.to_dict()

    def apply_profile_dict(d: dict) -> None:
        nonlocal videos, csvs, hud_layout_data, png_view_data, last_scan_sig, app_model

        if not isinstance(d, dict):
            return

        # Output / HUD-Breite setzen
        out = d.get("output")
        if isinstance(out, dict):
            a = str(out.get("aspect") or "").strip()
            p = str(out.get("preset") or "").strip()
            q = str(out.get("quality") or "").strip()
            h = str(out.get("hud_width_px") or "").strip()

            if a:
                try:
                    out_aspect_var.set(a)
                except Exception:
                    pass
            if q:
                try:
                    out_quality_var.set(q)
                except Exception:
                    pass
            if p:
                try:
                    out_preset_var.set(p)
                except Exception:
                    pass
            if h != "":
                try:
                    hud_width_var.set(max(0, int(float(h))))
                except Exception:
                    pass

            try:
                persistence.save_output_format(
                    {"aspect": out_aspect_var.get(), "preset": out_preset_var.get(), "quality": out_quality_var.get(), "hud_width_px": str(get_hud_width_px())}
                )
            except Exception:
                pass

        # HUD-Layout + PNG-View komplett übernehmen (damit alles wieder da ist)
        hl = d.get("hud_layout_data")
        if isinstance(hl, dict):
            hud_layout_data = hl
            try:
                persistence.save_hud_layout(hud_layout_data)
            except Exception:
                pass

        pv = d.get("png_view_data")
        if isinstance(pv, dict):
            png_view_data = pv
            try:
                persistence.save_png_view(png_view_data)
            except Exception:
                pass

        # Mapping-Layer: aktuellen UI-Zustand zentral ins Modell spiegeln
        app_model = model_from_ui_state()

        # Start/Endframes mergen und speichern
        sf = d.get("startframes")
        if isinstance(sf, dict):
            for k, v in sf.items():
                try:
                    startframes_by_name[str(k)] = int(v)
                except Exception:
                    pass
            try:
                persistence.save_startframes(startframes_by_name)
            except Exception:
                pass

        ef = d.get("endframes")
        if isinstance(ef, dict):
            for k, v in ef.items():
                try:
                    endframes_by_name[str(k)] = int(v)
                except Exception:
                    pass
            try:
                persistence.save_endframes(endframes_by_name)
            except Exception:
                pass

        # Dateien setzen (nur wenn sie im input-Ordner existieren)
        vlist = d.get("videos")
        clist = d.get("csvs")

        new_videos: list[Path] = []
        new_csvs: list[Path] = []

        if isinstance(vlist, list):
            for n in vlist[:2]:
                try:
                    p = input_video_dir / str(n)
                    if p.exists():
                        new_videos.append(p)
                except Exception:
                    pass

        if isinstance(clist, list):
            for n in clist[:2]:
                try:
                    p = input_csv_dir / str(n)
                    if p.exists():
                        new_csvs.append(p)
                except Exception:
                    pass

        videos = new_videos[:2]
        csvs = new_csvs[:2]

        # Force refresh (auch wenn Ordner-Signatur gleich ist)
        last_scan_sig = None
        try:
            close_preview_video()
        except Exception:
            pass
        try:
            refresh_display()
        except Exception:
            pass
        try:
            if preview_mode_var.get() == "png":
                png_load_state_for_current()
                render_png_preview(force_reload=True)
            else:
                refresh_layout_preview()
        except Exception:
            pass

    def profile_save_dialog() -> None:
        try:
            initial = profiles_dir / "profile.json"
        except Exception:
            initial = None

        fn = filedialog.asksaveasfilename(
            title="Profil speichern",
            defaultextension=".json",
            filetypes=[("Profil (*.json)", "*.json"), ("Alle Dateien", "*.*")],
            initialdir=str(profiles_dir),
            initialfile="profile.json",
        )
        if not fn:
            return

        try:
            try:
                png_save_state_for_current()
            except Exception:
                pass

            data = build_profile_dict()
            Path(fn).write_text(json.dumps(data, indent=2), encoding="utf-8")
            lbl_loaded.config(text="Video: Profil gespeichert")
        except Exception:
            lbl_loaded.config(text="Video: Profil speichern fehlgeschlagen")

    def profile_load_dialog() -> None:
        fn = filedialog.askopenfilename(
            title="Profil laden",
            defaultextension=".json",
            filetypes=[("Profil (*.json)", "*.json"), ("Alle Dateien", "*.*")],
            initialdir=str(profiles_dir),
        )
        if not fn:
            return

        try:
            d = json.loads(Path(fn).read_text(encoding="utf-8"))
        except Exception:
            lbl_loaded.config(text="Video: Profil laden fehlgeschlagen")
            return

        try:
            apply_profile_dict(d)
            lbl_loaded.config(text="Video: Profil geladen")
        except Exception:
            lbl_loaded.config(text="Video: Profil laden fehlgeschlagen")
            

    def refresh_display() -> None:
        v1 = videos[0] if len(videos) >= 1 else None
        v2 = videos[1] if len(videos) >= 2 else None
        c1 = csvs[0] if len(csvs) >= 1 else None
        c2 = csvs[1] if len(csvs) >= 2 else None

        set_row(lbl_v1, tip_v1, "Video 1", v1)
        set_row(lbl_v2, tip_v2, "Video 2", v2)
        set_row(lbl_c1, tip_c1, "CSV 1", c1)
        set_row(lbl_c2, tip_c2, "CSV 2", c2)

        w1 = h1 = 0
        w2 = h2 = 0
        fps1 = 0.0

        if v1 is not None:
            request_video_info(v1)
            info = video_info_cache.get(str(v1))
            if info is not None:
                w1, h1, fps1 = info

        if v2 is not None:
            request_video_info(v2)
            info2 = video_info_cache.get(str(v2))
            if info2 is not None:
                w2, h2, _fps2 = info2

        if v1 is None and v2 is None:
            lbl_in_res.config(text="Input-Auflösung: –")
        elif v2 is None:
            if w1 > 0 and h1 > 0:
                lbl_in_res.config(text=f"Input-Auflösung: V1 {fmt_res(w1, h1)}")
            else:
                lbl_in_res.config(text="Input-Auflösung: V1 … (lädt)")
        else:
            s1 = fmt_res(w1, h1) if (w1 > 0 and h1 > 0) else "… (lädt)"
            s2 = fmt_res(w2, h2) if (w2 > 0 and h2 > 0) else "… (lädt)"
            lbl_in_res.config(text=f"Input-Auflösung: V1 {s1} | V2 {s2}")

        if v1 is None:
            lbl_out_fps.config(text="FPS (vom Video): –")
        else:
            if fps1 > 0.1:
                lbl_out_fps.config(text=f"FPS (vom Video): {fps1:.3f}")
            else:
                lbl_out_fps.config(text="FPS (vom Video): … (lädt)")

        try:
            base_w = w1
            base_h = h1
            if base_w > 0 and base_h > 0:
                new_preset = compute_output_preset_from_quality(
                    base_w,
                    base_h,
                    out_aspect_var.get(),
                    out_quality_var.get(),
                    out_preset_var.get(),
                )
                if out_preset_var.get() != new_preset:
                    out_preset_var.set(new_preset)
                persistence.save_output_format(
                    {"aspect": out_aspect_var.get(), "preset": out_preset_var.get(), "quality": out_quality_var.get()}
                )
                if video_preview_ctrl is None or video_preview_ctrl.cap is None:
                    if preview_mode_var.get() == "png":
                        png_load_state_for_current()
                        render_png_preview(force_reload=False)
                    else:
                        refresh_layout_preview()
        except Exception:
            pass

        if len(videos) == 2:
            t1_ms = extract_time_ms(videos[0])
            t2_ms = extract_time_ms(videos[1])
            t1_str = extract_time_str(videos[0])
            t2_str = extract_time_str(videos[1])

            if t1_ms is None or t2_ms is None or t1_str is None or t2_str is None:
                lbl_fast.config(text="Fast: Zeit im Dateinamen fehlt")
                lbl_slow.config(text="Slow: Zeit im Dateinamen fehlt")
                return

            if t1_ms < t2_ms:
                fast_time = t1_str
                slow_time = t2_str
            else:
                fast_time = t2_str
                slow_time = t1_str

            lbl_fast.config(text=f"Fast: {fast_time}")
            lbl_slow.config(text=f"Slow: {slow_time}")
        else:
            clear_result()

    last_scan_sig: tuple[tuple[str, ...], tuple[str, ...]] | None = None

    def sync_from_folders_if_needed_ui(force: bool = False) -> None:
        nonlocal videos, csvs, last_scan_sig
        videos, csvs, last_scan_sig = filesvc.sync_from_folders_if_needed(
            videos=videos,
            csvs=csvs,
            last_scan_sig=last_scan_sig,
            input_video_dir=input_video_dir,
            input_csv_dir=input_csv_dir,
            refresh_display=refresh_display,
            force=force,
        )

    def run_periodic_folder_watch() -> None:
        filesvc.periodic_folder_watch(
            sync_callback=lambda: sync_from_folders_if_needed_ui(force=False),
            schedule_callback=lambda: root.after(1000, run_periodic_folder_watch),
        )

    run_periodic_folder_watch()

    def on_select_files() -> None:
        paths = filedialog.askopenfilenames(
            title="Dateien auswählen (2 Videos + optional CSV)",
            filetypes=[
                ("Videos und CSV", "*.mp4 *.csv"),
                ("Video", "*.mp4"),
                ("CSV", "*.csv"),
            ],
        )
        status, selected_videos, selected_csvs = filesvc.select_files(
            paths=paths,
            input_video_dir=input_video_dir,
            input_csv_dir=input_csv_dir,
        )
        if status == "empty":
            return

        if status == "csv_only":
            csvs[:] = selected_csvs[:2]
            refresh_display()
            return

        if status == "need_two_videos":
            videos.clear()
            csvs.clear()
            refresh_display()
            lbl_fast.config(text="Fast: Bitte genau 2 Videos wählen")
            lbl_slow.config(text="Slow: –")
            close_preview_video()
            return

        videos[:] = selected_videos[:2]
        csvs[:] = selected_csvs[:2]
        refresh_display()

    btn_select.config(command=on_select_files)
    btn_profile_save.config(command=profile_save_dialog)
    btn_profile_load.config(command=profile_load_dialog)

    # ---- Vorschau ----

    frame_preview.columnconfigure(0, weight=1)
    frame_preview.rowconfigure(0, weight=0)
    frame_preview.rowconfigure(1, weight=1)
    frame_preview.rowconfigure(2, weight=0)

    # Vorschau-Modus (Layout vs PNG)
    preview_mode_bar = ttk.Frame(frame_preview)
    preview_mode_bar.grid(row=0, column=0, sticky="ew", padx=10, pady=6)
    preview_mode_bar.columnconfigure(10, weight=1)

    preview_mode_var = tk.StringVar(value="layout")

    btn_mode_layout = ttk.Radiobutton(preview_mode_bar, text="Layout", value="layout", variable=preview_mode_var)
    btn_mode_png = ttk.Radiobutton(preview_mode_bar, text="PNG", value="png", variable=preview_mode_var)
    btn_mode_layout.grid(row=0, column=0, sticky="w", padx=(0, 10))
    btn_mode_png.grid(row=0, column=1, sticky="w")

    btn_png_fit = ttk.Button(preview_mode_bar, text="PNG auf Rahmenhöhe")
    btn_png_fit.grid(row=0, column=2, sticky="w", padx=(16, 0))

    # Crop-Controls bleiben (werden nur bei Zuschneiden eingeblendet)
    preview_top = ttk.Frame(frame_preview)
    preview_top.grid(row=0, column=0, sticky="ew", padx=10, pady=6)
    preview_top.grid_remove()

    btn_play = ttk.Button(preview_top, text="▶")
    btn_prev = ttk.Button(preview_top, text="⏮")
    btn_next = ttk.Button(preview_top, text="⏭")
    btn_set_start = ttk.Button(preview_top, text="Start hier setzen")
    btn_cancel = ttk.Button(preview_top, text="Abbrechen")
    btn_cut = ttk.Button(preview_top, text="Schneiden")

    lbl_frame = ttk.Label(preview_top, text="Frame: –")
    lbl_end = ttk.Label(preview_top, text="Ende: –")
    lbl_loaded = ttk.Label(preview_top, text="Video: –")

    ttk.Separator(preview_top, orient="horizontal").grid(row=1, column=0, columnspan=8, sticky="ew", pady=(6, 6))

    ttk.Label(preview_top, text="Endframe:").grid(row=2, column=0, sticky="w")

    end_var = tk.IntVar(value=0)
    spn_end = ttk.Spinbox(preview_top, from_=0, to=0, width=10, textvariable=end_var)
    btn_save_end = ttk.Button(preview_top, text="Ende speichern")

    spn_end.grid(row=2, column=1, sticky="w")
    btn_save_end.grid(row=2, column=2, sticky="w", padx=(8, 0))

    btn_play.grid(row=0, column=0, padx=(0, 6))
    btn_prev.grid(row=0, column=1, padx=(0, 6))
    btn_next.grid(row=0, column=2, padx=(0, 12))
    btn_set_start.grid(row=0, column=3, padx=(0, 12))
    btn_cut.grid(row=0, column=4, padx=(0, 12))
    btn_cancel.grid(row=0, column=5, padx=(0, 12))
    lbl_frame.grid(row=0, column=6, padx=(0, 12))
    lbl_end.grid(row=0, column=7, padx=(0, 12))
    lbl_loaded.grid(row=0, column=8, sticky="w")

    preview_area = ttk.Frame(frame_preview)
    preview_area.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 6))
    preview_area.columnconfigure(0, weight=1)
    preview_area.rowconfigure(0, weight=1)

    preview_label = ttk.Label(preview_area, text="")
    preview_label.grid(row=0, column=0, sticky="nsew")

    layout_canvas = tk.Canvas(preview_area, highlightthickness=0)
    layout_canvas.grid(row=0, column=0, sticky="nsew")

    png_canvas = tk.Canvas(preview_area, highlightthickness=0)
    png_canvas.grid(row=0, column=0, sticky="nsew")
    png_canvas.grid_remove()

    def choose_slow_fast_paths() -> tuple[Path | None, Path | None]:
        if len(videos) != 2:
            return None, None

        v1 = videos[0]
        v2 = videos[1]
        t1 = extract_time_ms(v1)
        t2 = extract_time_ms(v2)
        if t1 is None or t2 is None:
            return None, None

        if t1 < t2:
            fast_p, slow_p = v1, v2
        else:
            fast_p, slow_p = v2, v1

        return slow_p, fast_p

    def get_start_for_video(p: Path) -> int:
        try:
            return int(startframes_by_name.get(p.name, 0))
        except Exception:
            return 0

    video_preview_ctrl: VideoPreviewController | None = None

    def read_frame_as_pil(p: Path, frame_idx: int):
        if video_preview_ctrl is None:
            return None
        return video_preview_ctrl.read_frame_as_pil(p, frame_idx)

    def current_png_output_format() -> LayoutPreviewOutputFormat:
        out_w, out_h = parse_preset(out_preset_var.get())
        if out_w <= 0 or out_h <= 0:
            out_w, out_h = 1280, 720
        return LayoutPreviewOutputFormat(
            out_w=int(out_w),
            out_h=int(out_h),
            hud_w=int(get_hud_width_px()),
        )

    def load_png_view_data() -> dict:
        if isinstance(png_view_data, dict):
            return png_view_data
        return {}

    def save_png_view_data(data: dict) -> None:
        nonlocal png_view_data
        png_view_data = data
        persistence.save_png_view(png_view_data)

    png_preview_ctrl = PngPreviewController(
        canvas=png_canvas,
        get_preview_area_size=lambda: (preview_area.winfo_width(), preview_area.winfo_height()),
        get_output_format=current_png_output_format,
        is_png_mode=lambda: preview_mode_var.get() == "png",
        get_png_view_key=png_view_key,
        load_png_view_data=load_png_view_data,
        save_png_view_data=save_png_view_data,
        choose_slow_fast_paths=choose_slow_fast_paths,
        get_start_for_video=get_start_for_video,
        read_frame_as_pil=read_frame_as_pil,
    )

    def png_load_state_for_current() -> None:
        png_preview_ctrl.png_load_state_for_current()

    def png_save_state_for_current() -> None:
        png_preview_ctrl.png_save_state_for_current()

    def render_png_preview(force_reload: bool = False) -> None:
        png_preview_ctrl.render_png_preview(force_reload=force_reload)

    def png_fit_to_height_both() -> None:
        png_preview_ctrl.png_fit_to_height_both()

    def png_on_wheel(e) -> None:
        png_preview_ctrl.png_on_wheel(e)

    def png_on_down(e) -> None:
        png_preview_ctrl.png_on_down(e)

    def png_on_move(e) -> None:
        png_preview_ctrl.png_on_move(e)

    def png_on_up(_e=None) -> None:
        png_preview_ctrl.png_on_up(_e)

    png_state = png_preview_ctrl.png_state

    btn_png_fit.config(command=png_fit_to_height_both)

    png_canvas.bind("<MouseWheel>", png_on_wheel)
    png_canvas.bind("<ButtonPress-1>", png_on_down)
    png_canvas.bind("<B1-Motion>", png_on_move)
    png_canvas.bind("<ButtonRelease-1>", png_on_up)

    # Transform-Merker (für Layout Maus-Events)
    hud_boxes: list[dict] = get_hud_boxes_for_current()

    def clamp(v: int, lo: int, hi: int) -> int:
        if v < lo:
            return lo
        if v > hi:
            return hi
        return v

    def set_hud_boxes_for_current_local(boxes: list[dict]) -> None:
        set_hud_boxes_for_current(boxes)

    def save_current_boxes() -> None:
        set_hud_boxes_for_current_local(hud_boxes)

    def enabled_types() -> set[str]:
        out: set[str] = set()
        try:
            for t in HUD_TYPES:
                v = hud_enabled_vars.get(t)
                if v is not None and bool(v.get()):
                    out.add(t)
        except Exception:
            pass
        return out

    def current_layout_output_format() -> LayoutPreviewOutputFormat:
        out_w, out_h = parse_preset(out_preset_var.get())
        if out_w <= 0 or out_h <= 0:
            out_w, out_h = 1280, 720
        return LayoutPreviewOutputFormat(
            out_w=int(out_w),
            out_h=int(out_h),
            hud_w=int(get_hud_width_px()),
        )

    layout_preview_ctrl: LayoutPreviewController | None = None

    def refresh_layout_preview() -> None:
        nonlocal hud_boxes, layout_preview_ctrl
        if layout_preview_ctrl is None:
            return

        try:
            area_w = max(200, preview_area.winfo_width())
            area_h = max(200, preview_area.winfo_height())
        except Exception:
            return

        hud_boxes = layout_preview_ctrl.draw_layout_preview(
            output_format=current_layout_output_format(),
            hud_boxes=hud_boxes,
            enabled_types=enabled_types(),
            area_w=area_w,
            area_h=area_h,
            load_current_boxes=get_hud_boxes_for_current,
        )

    layout_preview_ctrl = LayoutPreviewController(
        canvas=layout_canvas,
        save_current_boxes=save_current_boxes,
        redraw_preview=refresh_layout_preview,
        is_locked=lambda: video_preview_ctrl is not None and video_preview_ctrl.cap is not None,
    )

    layout_canvas.bind("<Motion>", lambda e: layout_preview_ctrl.on_layout_hover(e, hud_boxes, enabled_types()))
    layout_canvas.bind("<Leave>", lambda e: layout_preview_ctrl.on_layout_leave(e))
    layout_canvas.bind("<ButtonPress-1>", lambda e: layout_preview_ctrl.on_layout_mouse_down(e, hud_boxes, enabled_types()))
    layout_canvas.bind("<B1-Motion>", lambda e: layout_preview_ctrl.on_layout_mouse_move(e, hud_boxes))
    layout_canvas.bind("<ButtonRelease-1>", lambda e: layout_preview_ctrl.on_layout_mouse_up(e))

    # Default: Layout sichtbar
    try:
        preview_label.grid_remove()
    except Exception:
        pass
    try:
        layout_canvas.lift()
    except Exception:
        pass
    try:
        refresh_layout_preview()
    except Exception:
        pass

    scrub = ttk.Scale(frame_preview, from_=0, to=0, orient="horizontal")
    scrub.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
    scrub.grid_remove()

    def clamp_frame(idx: int) -> int:
        if video_preview_ctrl is None:
            return max(0, int(idx))
        return video_preview_ctrl.clamp_frame(int(idx))

    def set_endframe(idx: int, save: bool = True) -> None:
        if video_preview_ctrl is None:
            return
        video_preview_ctrl.set_endframe(int(idx), save=save)

    def auto_end_from_start(start_idx: int) -> None:
        if video_preview_ctrl is None:
            return
        video_preview_ctrl.auto_end_from_start(int(start_idx))

    def save_endframe_from_ui() -> None:
        if video_preview_ctrl is None:
            return
        video_preview_ctrl.save_endframe_from_ui()

    def show_progress_with_cancel(title: str, text: str):
        win = tk.Toplevel(root)
        win.title(title)
        win.transient(root)
        win.grab_set()
        win.resizable(False, False)

        frm = ttk.Frame(win, padding=12)
        frm.grid(row=0, column=0, sticky="nsew")

        lbl = ttk.Label(frm, text=text)
        lbl.grid(row=0, column=0, sticky="w")

        bar = ttk.Progressbar(frm, mode="determinate", length=420, maximum=100.0)
        bar.grid(row=1, column=0, sticky="ew", pady=(10, 0))

        btn_cancel = ttk.Button(frm, text="Abbrechen")
        btn_cancel.grid(row=2, column=0, sticky="e", pady=(10, 0))

        win.update_idletasks()
        x = root.winfo_rootx() + 80
        y = root.winfo_rooty() + 80
        win.geometry(f"+{x}+{y}")
        win.update()

        cancel_state = {"cancel": False}

        def request_cancel():
            cancel_state["cancel"] = True

        btn_cancel.config(command=request_cancel)

        def set_text(t: str):
            try:
                lbl.config(text=t)
            except Exception:
                pass

        def set_progress(pct: float):
            try:
                bar["value"] = max(0.0, min(100.0, float(pct)))
            except Exception:
                pass

        def is_cancelled() -> bool:
            return bool(cancel_state["cancel"])

        def close():
            try:
                win.grab_release()
            except Exception:
                pass
            try:
                win.destroy()
            except Exception:
                pass

        return win, close, set_text, set_progress, is_cancelled

        frm = ttk.Frame(win, padding=12)
        frm.grid(row=0, column=0, sticky="nsew")

        lbl = ttk.Label(frm, text=text)
        lbl.grid(row=0, column=0, sticky="w")

        bar = ttk.Progressbar(frm, mode="determinate", length=420, maximum=100.0)
        bar.grid(row=1, column=0, sticky="ew", pady=(10, 0))

        btn_cancel = ttk.Button(frm, text="Abbrechen")
        btn_cancel.grid(row=2, column=0, sticky="e", pady=(10, 0))

        win.update_idletasks()
        x = root.winfo_rootx() + 80
        y = root.winfo_rooty() + 80
        win.geometry(f"+{x}+{y}")
        win.update()

        cancel_state = {"cancel": False}

        def request_cancel():
            cancel_state["cancel"] = True

        btn_cancel.config(command=request_cancel)

        def set_text(t: str):
            try:
                lbl.config(text=t)
            except Exception:
                pass

        def set_progress(pct: float):
            try:
                bar["value"] = max(0.0, min(100.0, float(pct)))
            except Exception:
                pass

        def is_cancelled() -> bool:
            try:
                return bool(cancel_state["cancel"])
            except Exception:
                return False

        def close():
            try:
                win.grab_release()
            except Exception:
                pass
            try:
                win.destroy()
            except Exception:
                pass

        return win, close, set_text, set_progress, is_cancelled

    def parse_ffmpeg_time_to_sec(s: str) -> float:
        # "00:01:23.45" -> Sekunden
        try:
            s = (s or "").strip()
            if s.count(":") != 2:
                return 0.0
            hh, mm, rest = s.split(":", 2)
            ss = float(rest)
            return (int(hh) * 3600) + (int(mm) * 60) + ss
        except Exception:
            return 0.0

    def generate_compare_video() -> None:
        nonlocal app_model
        if len(videos) != 2:
            lbl_loaded.config(text="Video: Bitte genau 2 Videos wählen")
            return

        slow_p, fast_p = choose_slow_fast_paths()
        if slow_p is None or fast_p is None:
            lbl_loaded.config(text="Video: Zeit im Dateinamen fehlt (Fast/Slow)")
            return

        out_w, out_h = parse_preset(out_preset_var.get())
        if out_w <= 0 or out_h <= 0:
            out_w, out_h = 1280, 720

        hud_w = int(get_hud_width_px())
        hud_w = max(0, min(hud_w, max(0, out_w - 2)))

        ts = time.strftime("%Y%m%d-%H%M%S")
        out_name = f"compare_{ts}_{out_w}x{out_h}_hud{hud_w}.mp4"
        out_path = output_video_dir / out_name

        hud_enabled = {}
        try:
            for t, var in hud_enabled_vars.items():
                hud_enabled[str(t)] = bool(var.get())
        except Exception:
            pass

        project_root_local = find_project_root(Path(__file__))
        main_py = project_root_local / "src" / "main.py"
        if not main_py.exists():
            lbl_loaded.config(text="Video: main.py nicht gefunden")
            return

        app_model = model_from_ui_state()

        win, close, set_text, set_progress, is_cancelled = show_progress_with_cancel(
            "Video erzeugen",
            "Starte main.py…"
        )
        root.update()

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
                result = start_render(
                    project_root=project_root_local,
                    videos=list(videos),
                    csvs=list(csvs),
                    slow_p=slow_p,
                    fast_p=fast_p,
                    out_path=out_path,
                    out_aspect=str(out_aspect_var.get()),
                    out_preset=str(out_preset_var.get()),
                    out_quality=str(out_quality_var.get()),
                    hud_w=int(hud_w),
                    hud_enabled=hud_enabled,
                    app_model=app_model,
                    get_hud_boxes_for_current=get_hud_boxes_for_current,
                    png_save_state_for_current=png_save_state_for_current,
                    png_view_key=png_view_key,
                    png_state=png_state,
                    is_cancelled=is_cancelled,
                    on_progress=on_progress,
                )

                def finish_ok() -> None:
                    close()
                    if out_path.exists() and out_path.stat().st_size > 0:
                        lbl_loaded.config(text=f"Video: Fertig ({out_path.name})")
                    else:
                        lbl_loaded.config(text="Video: Render fehlgeschlagen (0 KB)")

                def finish_cancel() -> None:
                    close()
                    try:
                        if out_path.exists():
                            out_path.unlink()
                    except Exception:
                        pass
                    lbl_loaded.config(text="Video: Abgebrochen")

                def finish_error() -> None:
                    close()
                    err = str(result.get("error") or "")
                    if err == "ui_json_write_failed":
                        lbl_loaded.config(text="Video: Konnte UI-JSON nicht schreiben")
                    elif err == "main_py_not_found":
                        lbl_loaded.config(text="Video: main.py nicht gefunden")
                    else:
                        lbl_loaded.config(text="Video: Render fehlgeschlagen")

                if str(result.get("status") or "") == "cancelled":
                    root.after(0, finish_cancel)
                elif str(result.get("status") or "") == "ok":
                    root.after(0, finish_ok)
                else:
                    root.after(0, finish_error)

            except Exception:
                try:
                    root.after(0, lambda: lbl_loaded.config(text="Video: Render fehlgeschlagen"))
                    root.after(0, close)
                except Exception:
                    pass


        t = threading.Thread(target=worker, daemon=True)
        t.start()



    btn_generate.config(command=generate_compare_video)

    def show_preview_controls(show: bool) -> None:
        if show:
            try:
                preview_mode_bar.grid_remove()
            except Exception:
                pass

            preview_top.grid()
            scrub.grid()

            try:
                layout_canvas.grid_remove()
            except Exception:
                pass
            try:
                png_canvas.grid_remove()
            except Exception:
                pass

            try:
                preview_label.grid()
                preview_label.lift()
            except Exception:
                pass
        else:
            preview_top.grid_remove()
            scrub.grid_remove()

            try:
                preview_label.grid_remove()
            except Exception:
                pass

            try:
                preview_mode_bar.grid()
            except Exception:
                pass

            mode = preview_mode_var.get()
            if mode == "png":
                try:
                    layout_canvas.grid_remove()
                except Exception:
                    pass
                try:
                    png_canvas.grid()
                    png_canvas.lift()
                except Exception:
                    pass
                try:
                    png_load_state_for_current()
                    render_png_preview(force_reload=True)
                except Exception:
                    pass
            else:
                try:
                    png_canvas.grid_remove()
                except Exception:
                    pass
                try:
                    layout_canvas.grid()
                    layout_canvas.lift()
                except Exception:
                    pass
                try:
                    refresh_layout_preview()
                except Exception:
                    pass

    def on_preview_mode_change(*_args) -> None:
        if video_preview_ctrl is not None and video_preview_ctrl.cap is not None:
            return
        try:
            show_preview_controls(False)
        except Exception:
            pass

    try:
        preview_mode_var.trace_add("write", on_preview_mode_change)
    except Exception:
        pass

    video_preview_ctrl = VideoPreviewController(
        root=root,
        preview_area=preview_area,
        preview_label=preview_label,
        lbl_frame=lbl_frame,
        lbl_end=lbl_end,
        lbl_loaded=lbl_loaded,
        btn_play=btn_play,
        scrub=scrub,
        spn_end=spn_end,
        end_var=end_var,
        input_video_dir=input_video_dir,
        proxy_dir=proxy_dir,
        startframes_by_name=startframes_by_name,
        endframes_by_name=endframes_by_name,
        save_startframes=persistence.save_startframes,
        save_endframes=persistence.save_endframes,
        extract_time_ms=extract_time_ms,
        show_preview_controls=show_preview_controls,
        sync_from_folders_if_needed_ui=sync_from_folders_if_needed_ui,
        show_progress=lambda title, text: show_progress(title, text),
    )

    def make_proxy_h264(src: Path):
        if video_preview_ctrl is None:
            return None
        return video_preview_ctrl.make_proxy_h264(src)

    def try_open_for_png(p: Path):
        if video_preview_ctrl is None:
            return None
        return video_preview_ctrl.try_open_for_png(p)

    def try_open_video(path: Path):
        return VideoPreviewController.try_open_video(path)

    def seek_and_read(idx: int) -> bool:
        if video_preview_ctrl is None:
            return False
        return video_preview_ctrl.seek_and_read(idx)

    def read_next_frame() -> bool:
        if video_preview_ctrl is None:
            return False
        return video_preview_ctrl.read_next_frame()

    def render_frame(idx: int, force: bool = False) -> None:
        if video_preview_ctrl is None:
            return
        video_preview_ctrl.render_frame(idx, force=force)

    def play_tick() -> None:
        if video_preview_ctrl is None:
            return
        video_preview_ctrl.play_tick()

    def on_play_pause() -> None:
        if video_preview_ctrl is None:
            return
        video_preview_ctrl.on_play_pause()

    def on_prev_frame() -> None:
        if video_preview_ctrl is None:
            return
        video_preview_ctrl.on_prev_frame()

    def on_next_frame() -> None:
        if video_preview_ctrl is None:
            return
        video_preview_ctrl.on_next_frame()

    def on_scrub_press(_event=None) -> None:
        if video_preview_ctrl is None:
            return
        video_preview_ctrl.on_scrub_press(_event)

    def on_scrub_release(_event=None) -> None:
        if video_preview_ctrl is None:
            return
        video_preview_ctrl.on_scrub_release(_event)

    def on_scrub_move(_event=None) -> None:
        if video_preview_ctrl is None:
            return
        video_preview_ctrl.on_scrub_move(_event)

    def set_start_here() -> None:
        if video_preview_ctrl is None:
            return
        video_preview_ctrl.set_start_here()

    def cut_current_video() -> None:
        if video_preview_ctrl is None:
            return
        video_preview_ctrl.cut_current_video()

    def close_preview_video() -> None:
        if video_preview_ctrl is None:
            return
        video_preview_ctrl.close_preview_video()

    def start_crop_for_video(video_path: Path) -> None:
        if video_preview_ctrl is None:
            return
        video_preview_ctrl.start_crop_for_video(video_path)

    def on_preview_resize(_event=None) -> None:
        if video_preview_ctrl is not None and video_preview_ctrl.cap is not None:
            render_frame(video_preview_ctrl.current_frame_idx, force=True)
            return

        try:
            if preview_mode_var.get() == "png":
                render_png_preview(force_reload=False)
            else:
                refresh_layout_preview()
        except Exception:
            pass

    preview_area.bind("<Configure>", on_preview_resize)

    btn_play.config(command=on_play_pause)
    btn_prev.config(command=on_prev_frame)
    btn_next.config(command=on_next_frame)
    btn_set_start.config(command=set_start_here)
    btn_cut.config(command=cut_current_video)
    btn_cancel.config(command=close_preview_video)
    btn_save_end.config(command=save_endframe_from_ui)

    scrub.bind("<ButtonPress-1>", on_scrub_press)
    scrub.bind("<ButtonRelease-1>", on_scrub_release)
    scrub.bind("<B1-Motion>", on_scrub_move)

    def show_menu_for_item(event, kind: str, index: int) -> None:
        nonlocal videos, csvs

        if kind == "video":
            if index >= len(videos):
                return
            item = videos[index]
        else:
            if index >= len(csvs):
                return
            item = csvs[index]

        menu = tk.Menu(root, tearoff=0)

        if kind == "video":
            menu.add_command(label="Zuschneiden", command=lambda p=item: start_crop_for_video(p))
            menu.add_separator()

        def do_delete() -> None:
            nonlocal videos, csvs

            if kind == "video":
                if (
                    video_preview_ctrl is not None
                    and video_preview_ctrl.current_video_original is not None
                    and item.name == video_preview_ctrl.current_video_original.name
                ):
                    close_preview_video()

            ok = filesvc.delete_file(item)
            if not ok:
                return

            if kind == "video":
                videos = [p for p in videos if p != item]
            else:
                csvs = [p for p in csvs if p != item]

            refresh_display()

        def do_open_folder() -> None:
            filesvc.open_folder(item)

        menu.add_command(label="Löschen", command=do_delete)
        menu.add_command(label="Ordner öffnen", command=do_open_folder)
        menu.tk_popup(event.x_root, event.y_root)

    btn_v1.bind("<Button-1>", lambda e: show_menu_for_item(e, "video", 0))
    btn_v2.bind("<Button-1>", lambda e: show_menu_for_item(e, "video", 1))
    btn_c1.bind("<Button-1>", lambda e: show_menu_for_item(e, "csv", 0))
    btn_c2.bind("<Button-1>", lambda e: show_menu_for_item(e, "csv", 1))

    sync_from_folders_if_needed_ui(force=True)
    root.mainloop()


if __name__ == "__main__":
    main()
