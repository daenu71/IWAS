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
import queue
import sys

import cv2
from PIL import Image, ImageTk

from core.models import (
    AppModel,
    HudLayoutState,
    OutputFormat,
    PngViewState,
    Profile,
    RenderPayload,
)
from core import persistence


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
                draw_layout_preview()
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
                        draw_layout_preview()
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
            draw_layout_preview()
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
                draw_layout_preview()
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

        if cap is not None:
            return

        try:
            if preview_mode_var.get() == "png":
                png_load_state_for_current()
                render_png_preview(force_reload=False)
            else:
                draw_layout_preview()
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

    def open_folder(path: Path) -> None:
        try:
            os.startfile(str(path.parent))
        except Exception:
            pass

    def delete_file(path: Path) -> bool:
        try:
            if path.exists():
                path.unlink()
            return True
        except Exception:
            return False

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
                draw_layout_preview()
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
                if cap is None:
                    if preview_mode_var.get() == "png":
                        png_load_state_for_current()
                        render_png_preview(force_reload=False)
                    else:
                        draw_layout_preview()
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

    def copy_to_dir(src: Path, dst_dir: Path) -> Path:
        dst = dst_dir / src.name
        if not dst.exists():
            shutil.copy2(src, dst)
        return dst

    last_scan_sig: tuple[tuple[str, ...], tuple[str, ...]] | None = None

    def scan_folders_signature() -> tuple[tuple[str, ...], tuple[str, ...]]:
        vid_exts = {".mp4", ".mkv", ".mov", ".avi"}
        vids = tuple(sorted([p.name for p in input_video_dir.iterdir() if p.is_file() and p.suffix.lower() in vid_exts]))
        cs = tuple(sorted([p.name for p in input_csv_dir.glob("*.csv") if p.is_file()]))
        return vids, cs

    def sync_from_folders_if_needed(force: bool = False) -> None:
        nonlocal videos, csvs, last_scan_sig

        sig = scan_folders_signature()
        if (not force) and (sig == last_scan_sig):
            return
        last_scan_sig = sig

        available_videos = [input_video_dir / n for n in sig[0]]
        available_csvs = [input_csv_dir / n for n in sig[1]]

        videos = [p for p in videos if p.exists()]
        csvs = [p for p in csvs if p.exists()]

        if len(videos) == 0 and len(available_videos) > 0:
            videos = available_videos[:2]
        else:
            videos = [p for p in videos if p in available_videos][:2]

        if len(csvs) == 0 and len(available_csvs) > 0:
            csvs = available_csvs[:2]
        else:
            csvs = [p for p in csvs if p in available_csvs][:2]

        refresh_display()

    def periodic_folder_watch() -> None:
        sync_from_folders_if_needed(force=False)
        root.after(1000, periodic_folder_watch)

    periodic_folder_watch()

    def select_files() -> None:
        paths = filedialog.askopenfilenames(
            title="Dateien auswählen (2 Videos + optional CSV)",
            filetypes=[
                ("Videos und CSV", "*.mp4 *.csv"),
                ("Video", "*.mp4"),
                ("CSV", "*.csv"),
            ],
        )
        if not paths:
            return

        selected_videos: list[Path] = []
        selected_csvs: list[Path] = []

        for p in paths:
            pp = Path(p)
            suf = pp.suffix.lower()
            if suf == ".mp4":
                selected_videos.append(pp)
            elif suf == ".csv":
                selected_csvs.append(pp)

        if len(selected_videos) == 0 and len(selected_csvs) > 0:
            copied_csvs: list[Path] = []
            for c in selected_csvs[:2]:
                copied_csvs.append(copy_to_dir(c, input_csv_dir))

            csvs[:] = copied_csvs[:2]
            refresh_display()
            return

        if len(selected_videos) != 2:
            videos.clear()
            csvs.clear()
            refresh_display()
            lbl_fast.config(text="Fast: Bitte genau 2 Videos wählen")
            lbl_slow.config(text="Slow: –")
            close_preview_video()
            return

        copied_videos: list[Path] = []
        for v in selected_videos:
            copied_videos.append(copy_to_dir(v, input_video_dir))

        copied_csvs: list[Path] = []
        for c in selected_csvs[:2]:
            copied_csvs.append(copy_to_dir(c, input_csv_dir))

        videos[:] = copied_videos[:2]
        csvs[:] = copied_csvs[:2]
        refresh_display()

    btn_select.config(command=select_files)
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
    
    
    # --- PNG Debug Logging ---
    PNG_DEBUG = False

    def _png_dbg(msg: str) -> None:
        if not PNG_DEBUG:
            return
        try:
            ts = time.strftime("%H:%M:%S")
        except Exception:
            ts = "??:??:??"
        try:
            print(f"[PNGDBG {ts}] {msg}")
        except Exception:
            pass
    

    # PNG State (pro Seite)
    png_state = {
        "L": {"zoom": 1.0, "off_x": 0, "off_y": 0, "fit_to_height": False},
        "R": {"zoom": 1.0, "off_x": 0, "off_y": 0, "fit_to_height": False},
        "drag": False,
        "drag_side": "",
        "drag_x": 0,
        "drag_y": 0,
    }

    # Letzte Frame-Geometrie (für Hit-Tests)
    png_frame_last = {
        "x0": 0, "y0": 0, "x1": 0, "y1": 0,
        "lx0": 0, "lx1": 0,
        "mx0": 0, "mx1": 0,
        "rx0": 0, "rx1": 0,
        "valid": False,
        "scale": 1.0,
    }

    png_img_left = None
    png_img_right = None
    png_left_name = ""
    png_right_name = ""
    png_left_start = -1
    png_right_start = -1

    def png_load_state_for_current() -> None:
        key = png_view_key()
        d = png_view_data.get(key)
        if not isinstance(d, dict):
            return

        def _get_float(name: str, default: float) -> float:
            try:
                return float(d.get(name, default))
            except Exception:
                return float(default)

        def _get_int(name: str, default: int) -> int:
            try:
                return int(d.get(name, default))
            except Exception:
                return int(default)

        def _get_bool(name: str, default: bool) -> bool:
            try:
                v = d.get(name, default)
                return bool(v)
            except Exception:
                return bool(default)

        zl = _get_float("zoom_l", 1.0)
        zr = _get_float("zoom_r", 1.0)

        if zl < 0.1:
            zl = 0.1
        if zl > 20.0:
            zl = 20.0
        if zr < 0.1:
            zr = 0.1
        if zr > 20.0:
            zr = 20.0

        png_state["L"]["zoom"] = float(zl)
        png_state["L"]["off_x"] = _get_int("off_lx", 0)
        png_state["L"]["off_y"] = _get_int("off_ly", 0)
        png_state["L"]["fit_to_height"] = _get_bool("fit_l", False)

        png_state["R"]["zoom"] = float(zr)
        png_state["R"]["off_x"] = _get_int("off_rx", 0)
        png_state["R"]["off_y"] = _get_int("off_ry", 0)
        png_state["R"]["fit_to_height"] = _get_bool("fit_r", False)

    def png_save_state_for_current() -> None:
        key = png_view_key()
        png_view_data[key] = {
            "zoom_l": float(png_state["L"]["zoom"]),
            "off_lx": int(png_state["L"]["off_x"]),
            "off_ly": int(png_state["L"]["off_y"]),
            "fit_l": bool(png_state["L"].get("fit_to_height", False)),
            "zoom_r": float(png_state["R"]["zoom"]),
            "off_rx": int(png_state["R"]["off_x"]),
            "off_ry": int(png_state["R"]["off_y"]),
            "fit_r": bool(png_state["R"].get("fit_to_height", False)),
        }
        persistence.save_png_view(png_view_data)

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

    def ffmpeg_exists() -> bool:
        try:
            from shutil import which
            return which("ffmpeg") is not None
        except Exception:
            return False

    def make_proxy_h264(src: Path) -> Path | None:
        if not ffmpeg_exists():
            return None

        safe_name = src.stem + "__proxy_h264.mp4"
        dst = proxy_dir / safe_name

        if dst.exists() and dst.stat().st_size > 0:
            return dst

        try:
            lbl_loaded.config(text=f"Video: Proxy wird erstellt… ({src.name})")
            root.update_idletasks()

            cmd = [
                "ffmpeg",
                "-y",
                "-i", str(src),
                "-an",
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-preset", "veryfast",
                "-crf", "20",
                str(dst),
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

            if dst.exists() and dst.stat().st_size > 0:
                return dst
        except Exception:
            pass
        return None

    def try_open_for_png(p: Path) -> cv2.VideoCapture | None:
        c = cv2.VideoCapture(str(p))
        if c is not None and c.isOpened():
            return c
        try:
            if c is not None:
                c.release()
        except Exception:
            pass

        proxy = make_proxy_h264(p)
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

    def read_frame_as_pil(p: Path, frame_idx: int) -> Image.Image | None:
        c = try_open_for_png(p)
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

    def compute_frame_rect_for_preview() -> tuple[int, int, int, int, float, int, int, int]:
        # Returns: x0,y0,x1,y1,scale,out_w,out_h,hud_w
        area_w = max(200, preview_area.winfo_width())
        area_h = max(200, preview_area.winfo_height())

        out_w, out_h = parse_preset(out_preset_var.get())
        if out_w <= 0 or out_h <= 0:
            out_w, out_h = 1280, 720

        hud_w = get_hud_width_px()
        hud_w = max(0, min(int(hud_w), max(0, out_w - 2)))

        pad = 10
        avail_w = max(50, area_w - 2 * pad)
        avail_h = max(50, area_h - 2 * pad)

        scale = min(avail_w / max(1, out_w), avail_h / max(1, out_h))
        draw_w = int(out_w * scale)
        draw_h = int(out_h * scale)

        x0 = int((area_w - draw_w) / 2)
        y0 = int((area_h - draw_h) / 2)
        x1 = x0 + draw_w
        y1 = y0 + draw_h
        return x0, y0, x1, y1, float(scale), int(out_w), int(out_h), int(hud_w)

    def pil_paste_clipped(dst: Image.Image, src: Image.Image, region_box: tuple[int, int, int, int], pos_xy: tuple[int, int]) -> None:
        # region_box is the allowed region in dst coords.
        rx0, ry0, rx1, ry1 = region_box
        px, py = pos_xy

        sx0 = px
        sy0 = py
        sx1 = px + src.size[0]
        sy1 = py + src.size[1]

        ix0 = max(rx0, sx0)
        iy0 = max(ry0, sy0)
        ix1 = min(rx1, sx1)
        iy1 = min(ry1, sy1)

        if ix1 <= ix0 or iy1 <= iy0:
            return

        crop_x0 = ix0 - sx0
        crop_y0 = iy0 - sy0
        crop_x1 = crop_x0 + (ix1 - ix0)
        crop_y1 = crop_y0 + (iy1 - iy0)

        src_c = src.crop((int(crop_x0), int(crop_y0), int(crop_x1), int(crop_y1)))
        dst.paste(src_c, (int(ix0), int(iy0)))
        
    def _png_region_out(side: str, out_w: int, out_h: int, hud_w: int) -> tuple[int, int, int, int]:
        """
        Region im Output-Koordinatensystem (nicht Preview-Pixel!):
        Links:  [0 .. side_w)
        HUD:    [side_w .. side_w+hud_w)
        Rechts: [side_w+hud_w .. out_w)
        """
        side_w = int((out_w - hud_w) / 2)
        if side_w < 1:
            side_w = int(out_w / 2)

        if side == "L":
            return 0, 0, side_w, out_h
        # "R"
        return side_w + hud_w, 0, out_w, out_h

    def _clamp_png_cover(
        side: str,
        src_img: Image.Image,
        out_w: int,
        out_h: int,
        hud_w: int,
        enforce_cover: bool = True,
        enforce_cover_zoom: bool = True,
    ) -> None:
        try:
            _png_dbg(
                f"CLAMP ENTER side={side} "
                f"fit={bool(png_state[side].get('fit_to_height', False))} "
                f"zoom={float(png_state[side].get('zoom', 0.0)):.6f} "
                f"off=({int(png_state[side].get('off_x', 0))},{int(png_state[side].get('off_y', 0))}) "
                f"src=({src_img.size[0]}x{src_img.size[1]}) out=({out_w}x{out_h}) hud_w={hud_w} "
                f"enforce_cover={bool(enforce_cover)} enforce_cover_zoom={bool(enforce_cover_zoom)}"
            )
        except Exception:
            pass

        """
        enforce_cover=True:
          - Offsets werden so geclamped, dass (wenn möglich) keine Lücken sichtbar sind.
        enforce_cover_zoom=True zusätzlich:
          - Zoom wird mindestens auf min_zoom (Cover) angehoben.
        Wenn das Bild kleiner als die Region ist (ow<rw oder oh<rh):
          - Offsets werden auf 0 gesetzt (zentriert), weil "Cover" nicht möglich ist.
        Offsets bleiben in Output-Pixeln.
        """
        if src_img is None:
            return

        rx0, ry0, rx1, ry1 = _png_region_out(side, out_w, out_h, hud_w)
        rw = max(1, rx1 - rx0)
        rh = max(1, ry1 - ry0)

        sw = max(1.0, float(src_img.size[0]))
        sh = max(1.0, float(src_img.size[1]))

        # Minimaler Zoom, damit Region komplett abgedeckt wird (Cover)
        min_zoom = max(float(rw) / sw, float(rh) / sh)

        fit = bool(png_state[side].get("fit_to_height", False))

        # Grund-Zoom aus State
        try:
            z_out = float(png_state[side].get("zoom", 1.0))
        except Exception:
            z_out = 1.0

        # Fit-Modus: exakt Cover-Zoom
        if fit:
            z_out = float(min_zoom)

        # Zoom clamp (immer)
        if z_out < 0.1:
            z_out = 0.1
        if z_out > 20.0:
            z_out = 20.0

        # Cover-Zoom nur wenn gewünscht
        if enforce_cover and enforce_cover_zoom and z_out < min_zoom:
            z_out = float(min_zoom)

        png_state[side]["zoom"] = float(z_out)

        # Skaliertes Bild in Output-Pixeln
        ow = max(1, int(round(sw * z_out)))
        oh = max(1, int(round(sh * z_out)))

        # Center-Base (wie im Render)
        base_cx = float(rx0) + (float(rw) - float(ow)) / 2.0
        base_cy = float(ry0) + (float(rh) - float(oh)) / 2.0

        if enforce_cover:
            try:
                ox = float(png_state[side].get("off_x", 0))
            except Exception:
                ox = 0.0
            try:
                oy = float(png_state[side].get("off_y", 0))
            except Exception:
                oy = 0.0

            # Wenn Cover in einer Achse nicht möglich ist: Offset neutralisieren
            if ow < rw:
                ox = 0.0
            else:
                min_off_x = float(rx1 - ow) - base_cx
                max_off_x = float(rx0) - base_cx
                if min_off_x > max_off_x:
                    min_off_x, max_off_x = max_off_x, min_off_x
                if ox < min_off_x:
                    ox = min_off_x
                if ox > max_off_x:
                    ox = max_off_x

            if oh < rh:
                oy = 0.0
            else:
                min_off_y = float(ry1 - oh) - base_cy
                max_off_y = float(ry0) - base_cy
                if min_off_y > max_off_y:
                    min_off_y, max_off_y = max_off_y, min_off_y
                if oy < min_off_y:
                    oy = min_off_y
                if oy > max_off_y:
                    oy = max_off_y

            png_state[side]["off_x"] = int(round(ox))
            png_state[side]["off_y"] = int(round(oy))

        try:
            _png_dbg(
                f"CLAMP EXIT  side={side} "
                f"fit={bool(png_state[side].get('fit_to_height', False))} "
                f"zoom={float(png_state[side].get('zoom', 0.0)):.6f} "
                f"off=({int(png_state[side].get('off_x', 0))},{int(png_state[side].get('off_y', 0))}) "
                f"min_zoom={float(min_zoom):.6f} rw={rw} rh={rh} ow={ow} oh={oh} "
                f"enforce_cover={bool(enforce_cover)} enforce_cover_zoom={bool(enforce_cover_zoom)}"
            )
        except Exception:
            pass


    def render_png_preview(force_reload: bool = False) -> None:
        nonlocal png_img_left, png_img_right
        nonlocal png_left_name, png_right_name, png_left_start, png_right_start

        if preview_mode_var.get() != "png":
            return

        slow_p, fast_p = choose_slow_fast_paths()
        if slow_p is None or fast_p is None:
            png_canvas.delete("all")
            png_canvas.create_text(20, 20, anchor="nw", text="PNG-Vorschau: Bitte 2 Videos mit Zeit im Namen wählen.")
            png_frame_last["valid"] = False
            return

        s_start = get_start_for_video(slow_p)
        f_start = get_start_for_video(fast_p)

        need_reload = force_reload
        if slow_p.name != png_left_name or fast_p.name != png_right_name:
            need_reload = True
        if s_start != png_left_start or f_start != png_right_start:
            need_reload = True

        if need_reload:
            png_left_name = slow_p.name
            png_right_name = fast_p.name
            png_left_start = s_start
            png_right_start = f_start

            img_l = read_frame_as_pil(slow_p, s_start)
            img_r = read_frame_as_pil(fast_p, f_start)

            if img_l is None or img_r is None:
                png_canvas.delete("all")
                png_canvas.create_text(20, 20, anchor="nw", text="PNG-Vorschau: Kann Frames nicht lesen (Codec?).")
                png_frame_last["valid"] = False
                return

            png_img_left = img_l
            png_img_right = img_r

        if png_img_left is None or png_img_right is None:
            png_frame_last["valid"] = False
            return

        x0, y0, x1, y1, scale, out_w, out_h, hud_w = compute_frame_rect_for_preview()

        draw_w = max(1, x1 - x0)
        draw_h = max(1, y1 - y0)

        side_w = int((out_w - hud_w) / 2)
        if side_w < 1:
            side_w = int(out_w / 2)

        side_w_px = int(round(side_w * scale))
        hud_w_px = int(round(hud_w * scale))

        # Regionen im Frame (in Frame-Koordinaten, 0..draw_w)
        lx0 = 0
        lx1 = max(0, side_w_px)
        mx0 = lx1
        mx1 = min(draw_w, mx0 + max(0, hud_w_px))
        rx0 = mx1
        rx1 = draw_w

        # Merker für Hit-Test (Canvas-Koords)
        png_frame_last["x0"] = int(x0)
        png_frame_last["y0"] = int(y0)
        png_frame_last["x1"] = int(x1)
        png_frame_last["y1"] = int(y1)
        png_frame_last["lx0"] = int(x0 + lx0)
        png_frame_last["lx1"] = int(x0 + lx1)
        png_frame_last["mx0"] = int(x0 + mx0)
        png_frame_last["mx1"] = int(x0 + mx1)
        png_frame_last["rx0"] = int(x0 + rx0)
        png_frame_last["rx1"] = int(x0 + rx1)
        png_frame_last["valid"] = True
        png_frame_last["scale"] = float(scale)

        # Composite (Frame als 1 Bild)
        bg = Image.new("RGB", (draw_w, draw_h), (245, 245, 245))

        def render_side(side: str, src_img: Image.Image, region: tuple[int, int, int, int]) -> None:
            # Zoom/Offset sind in Output-Pixeln gespeichert (stabil, egal wie groß das App-Fenster ist)
            rx0, ry0, rx1, ry1 = region
            rw = max(1, rx1 - rx0)
            rh = max(1, ry1 - ry0)

            fit = bool(png_state[side].get("fit_to_height", False))

            if fit:
                # "Fit" bedeutet hier: Cover (keine Lücken)
                sw = max(1.0, float(src_img.size[0]))
                sh = max(1.0, float(src_img.size[1]))
                # region ist in Preview-Pixeln -> wir wollen Output-Zoom:
                # Preview-Zoom = Output-Zoom * scale  => Output-Zoom = (CoverZoomPx / scale)
                min_zoom_px = max(float(rw) / sw, float(rh) / sh)
                z_out = float(min_zoom_px) / max(0.0001, float(scale))
            else:
                z_out = float(png_state[side].get("zoom", 1.0))

            if z_out < 0.1:
                z_out = 0.1
            if z_out > 20.0:
                z_out = 20.0

            # Zoom in Preview-Pixel umrechnen
            z_px = float(z_out) * float(scale)

            ow = max(1, int(round(src_img.size[0] * z_px)))
            oh = max(1, int(round(src_img.size[1] * z_px)))

            img2 = src_img.resize((ow, oh), Image.LANCZOS)

            # Offsets sind in Output-Pixeln (stabil bei Resize)
            off_x_out = int(png_state[side].get("off_x", 0))
            off_y_out = int(png_state[side].get("off_y", 0))
            off_x_px = int(round(off_x_out * scale))
            off_y_px = int(round(off_y_out * scale))

            rx0, ry0, rx1, ry1 = region
            rw = max(1, rx1 - rx0)
            rh = max(1, ry1 - ry0)

            base_x = int(rx0 + (rw - ow) / 2) + off_x_px
            base_y = int(ry0 + (rh - oh) / 2) + off_y_px

            pil_paste_clipped(bg, img2, region, (base_x, base_y))

        render_side("L", png_img_left, (lx0, 0, lx1, draw_h))
        render_side("R", png_img_right, (rx0, 0, rx1, draw_h))

        # Rahmen / Trenner zeichnen wir im Canvas (nicht im Bild)
        tk_img = ImageTk.PhotoImage(bg)

        png_canvas.delete("all")
        png_canvas.create_image(x0, y0, anchor="nw", image=tk_img)

        # Rahmen + Bereiche
        png_canvas.create_rectangle(x0, y0, x1, y1)
        png_canvas.create_rectangle(x0 + lx0, y0, x0 + lx1, y1)
        png_canvas.create_rectangle(x0 + mx0, y0, x0 + mx1, y1)
        png_canvas.create_rectangle(x0 + rx0, y0, x0 + rx1, y1)

        png_canvas.create_text(x0 + 10, y0 + 10, anchor="nw", text="Slow")
        png_canvas.create_text(x0 + rx0 + 10, y0 + 10, anchor="nw", text="Fast")
        if hud_w_px > 0:
            png_canvas.create_text(int(x0 + (mx0 + mx1) / 2), y0 + 20, anchor="n", text=f"HUD\n{hud_w}px")

        # Wichtig: Referenz halten
        png_canvas._tk_img = tk_img

    def png_hit_side(x: int, y: int) -> str:
        if not bool(png_frame_last.get("valid")):
            return ""
        if x < int(png_frame_last["x0"]) or x > int(png_frame_last["x1"]):
            return ""
        if y < int(png_frame_last["y0"]) or y > int(png_frame_last["y1"]):
            return ""
        if x >= int(png_frame_last["lx0"]) and x <= int(png_frame_last["lx1"]):
            return "L"
        if x >= int(png_frame_last["rx0"]) and x <= int(png_frame_last["rx1"]):
            return "R"
        return ""

    def png_on_wheel(e) -> None:
        if preview_mode_var.get() != "png":
            return
            
        try:
            _png_dbg(
                f"WHEEL ENTER side=? x={int(e.x)} y={int(e.y)} "
                f"delta={getattr(e, 'delta', None)}"
            )
        except Exception:
            pass
            

        side = png_hit_side(int(e.x), int(e.y))
        if side not in ("L", "R"):
            return
            
        try:
            _png_dbg(
                f"WHEEL SIDE side={side} "
                f"fit={bool(png_state[side].get('fit_to_height', False))} "
                f"zoom={float(png_state[side].get('zoom', 0.0)):.6f} "
                f"off=({int(png_state[side].get('off_x', 0))},{int(png_state[side].get('off_y', 0))}) "
                f"e.xy=({int(e.x)},{int(e.y)}) delta={getattr(e, 'delta', None)}"
            )
        except Exception:
            pass            

        try:
            d = int(e.delta)
        except Exception:
            d = 0

        # Bild holen
        if png_img_left is None or png_img_right is None:
            render_png_preview(force_reload=True)

        src_img = png_img_left if side == "L" else png_img_right
        if src_img is None:
            return

        # Output-Region bestimmen (für Cover-Minimum)
        out_w, out_h = parse_preset(out_preset_var.get())
        if out_w <= 0 or out_h <= 0:
            out_w, out_h = 1280, 720
        hud_w = int(get_hud_width_px())
        hud_w = max(0, min(hud_w, max(0, out_w - 2)))

        rx0, ry0, rx1, ry1 = _png_region_out(side, out_w, out_h, hud_w)
        rw = max(1, rx1 - rx0)
        rh = max(1, ry1 - ry0)

        sw = max(1.0, float(src_img.size[0]))
        sh = max(1.0, float(src_img.size[1]))

        # Minimaler Zoom, damit die Region vollständig abgedeckt ist (Cover)
        min_zoom = max(float(rw) / sw, float(rh) / sh)

        # Wenn wir im Fit-Modus waren, starte sauber bei min_zoom (kein Sprung)
        fit_now = bool(png_state[side].get("fit_to_height", False))
        try:
            cur_z = float(png_state[side].get("zoom", 1.0))
        except Exception:
            cur_z = 1.0

        z = float(cur_z)

        if d > 0:
            z *= 1.10
        elif d < 0:
            z /= 1.10

        # Manuell darf kleiner werden (kein Cover-Zwang)
        if z < 0.1:
            z = 0.1
        if z > 20.0:
            z = 20.0

        # Wheel-Zoom bedeutet: ab jetzt manuell
        png_state[side]["fit_to_height"] = False
        png_state[side]["zoom"] = float(z)

        # Nur clampen ohne "Snap"/Cover
        try:
            _clamp_png_cover(side, src_img, out_w, out_h, hud_w, enforce_cover=False)
        except Exception:
            pass
            
        try:
            _png_dbg(
                f"WHEEL EXIT side={side} "
                f"fit={bool(png_state[side].get('fit_to_height', False))} "
                f"zoom={float(png_state[side].get('zoom', 0.0)):.6f} "
                f"off=({int(png_state[side].get('off_x', 0))},{int(png_state[side].get('off_y', 0))}) "
                f"min_zoom={float(min_zoom):.6f}"
            )
        except Exception:
            pass            

        png_save_state_for_current()
        render_png_preview(force_reload=False)
        
        
    def png_on_down(e) -> None:
        if preview_mode_var.get() != "png":
            return
        side = png_hit_side(int(e.x), int(e.y))
        if side not in ("L", "R"):
            return
        png_state["drag"] = True
        png_state["drag_side"] = side
        png_state["drag_x"] = int(e.x)
        png_state["drag_y"] = int(e.y)

    def png_on_move(e) -> None:
        if preview_mode_var.get() != "png":
            return
        if not png_state.get("drag"):
            return
        side = str(png_state.get("drag_side") or "")
        if side not in ("L", "R"):
            return
            
        try:
            _png_dbg(
                f"MOVE ENTER side={side} "
                f"fit={bool(png_state[side].get('fit_to_height', False))} "
                f"zoom={float(png_state[side].get('zoom', 0.0)):.6f} "
                f"off=({int(png_state[side].get('off_x', 0))},{int(png_state[side].get('off_y', 0))}) "
                f"e.xy=({int(e.x)},{int(e.y)})"
            )
        except Exception:
            pass            

        dx_px = int(e.x) - int(png_state["drag_x"])
        dy_px = int(e.y) - int(png_state["drag_y"])
        png_state["drag_x"] = int(e.x)
        png_state["drag_y"] = int(e.y)

        scale = float(png_frame_last.get("scale") or 1.0)
        if scale <= 0.0001:
            scale = 1.0

        dx_out = int(round(dx_px / scale))
        dy_out = int(round(dy_px / scale))

        png_state[side]["off_x"] = int(png_state[side]["off_x"]) + dx_out
        png_state[side]["off_y"] = int(png_state[side]["off_y"]) + dy_out

        render_png_preview(force_reload=False)

    def png_on_up(_e=None) -> None:
        
        try:
            _png_dbg("UP ENTER")
        except Exception:
            pass        
        if preview_mode_var.get() != "png":
            return

        side = str(png_state.get("drag_side") or "")
        png_state["drag"] = False
        png_state["drag_side"] = ""

        # Beim Loslassen: automatisch "snappen" (keine Lücken)
        try:
            out_w, out_h = parse_preset(out_preset_var.get())
            if out_w <= 0 or out_h <= 0:
                out_w, out_h = 1280, 720
            hud_w = int(get_hud_width_px())
            hud_w = max(0, min(hud_w, max(0, out_w - 2)))

            if side in ("L", "R"):
                src_img = png_img_left if side == "L" else png_img_right
                if src_img is not None:
                    _clamp_png_cover(
                        side,
                        src_img,
                        out_w,
                        out_h,
                        hud_w,
                        enforce_cover=True,
                        enforce_cover_zoom=False,
                    )
        except Exception:
            pass
            
        try:
            if side in ("L", "R"):
                _png_dbg(
                    f"UP EXIT side={side} "
                    f"fit={bool(png_state[side].get('fit_to_height', False))} "
                    f"zoom={float(png_state[side].get('zoom', 0.0)):.6f} "
                    f"off=({int(png_state[side].get('off_x', 0))},{int(png_state[side].get('off_y', 0))})"
                )
        except Exception:
            pass            

        png_save_state_for_current()
        render_png_preview(force_reload=False)

    def png_fit_to_height_both() -> None:
        nonlocal png_img_left, png_img_right
        if preview_mode_var.get() != "png":
            return
        if png_img_left is None or png_img_right is None:
            render_png_preview(force_reload=True)
        if png_img_left is None or png_img_right is None:
            return

        out_w, out_h = parse_preset(out_preset_var.get())
        if out_w <= 0 or out_h <= 0:
            out_w, out_h = 1280, 720

        hud_w = int(get_hud_width_px())
        hud_w = max(0, min(hud_w, max(0, out_w - 2)))

        def cover_zoom_for(side: str, img: Image.Image) -> float:
            rx0, ry0, rx1, ry1 = _png_region_out(side, out_w, out_h, hud_w)
            rw = max(1, rx1 - rx0)
            rh = max(1, ry1 - ry0)
            sw = max(1.0, float(img.size[0]))
            sh = max(1.0, float(img.size[1]))
            z = max(float(rw) / sw, float(rh) / sh)
            if z < 0.1:
                z = 0.1
            if z > 20.0:
                z = 20.0
            return float(z)

        zl = cover_zoom_for("L", png_img_left)
        zr = cover_zoom_for("R", png_img_right)

        # Fit-Mode aktiv, Zoom auf "Cover" setzen, Offsets neutral
        png_state["L"]["fit_to_height"] = True
        png_state["R"]["fit_to_height"] = True
        png_state["L"]["zoom"] = float(zl)
        png_state["R"]["zoom"] = float(zr)
        png_state["L"]["off_x"] = 0
        png_state["L"]["off_y"] = 0
        png_state["R"]["off_x"] = 0
        png_state["R"]["off_y"] = 0

        # Final clamp (eigentlich redundant, aber safe)
        try:
            _clamp_png_cover("L", png_img_left, out_w, out_h, hud_w)
            _clamp_png_cover("R", png_img_right, out_w, out_h, hud_w)
        except Exception:
            pass

        png_save_state_for_current()
        render_png_preview(force_reload=False)

    btn_png_fit.config(command=png_fit_to_height_both)

    png_canvas.bind("<MouseWheel>", png_on_wheel)
    png_canvas.bind("<ButtonPress-1>", png_on_down)
    png_canvas.bind("<B1-Motion>", png_on_move)
    png_canvas.bind("<ButtonRelease-1>", png_on_up)

    # Transform-Merker (für Layout Maus-Events)
    layout_last: dict[str, int | float] = {
        "out_w": 0,
        "out_h": 0,
        "hud_w": 0,
        "side_w": 0,
        "x0": 0,
        "y0": 0,
        "scale": 1.0,
    }

    hud_boxes: list[dict] = get_hud_boxes_for_current()
    hud_active_id: str | None = None
    hud_mode: str = ""  # "drag" oder "resize"
    hud_drag_dx: int = 0
    hud_drag_dy: int = 0

    def clamp(v: int, lo: int, hi: int) -> int:
        if v < lo:
            return lo
        if v > hi:
            return hi
        return v

    def ensure_boxes_in_hud_area() -> None:
        out_w = int(layout_last.get("out_w") or 0)
        out_h = int(layout_last.get("out_h") or 0)
        hud_w = int(layout_last.get("hud_w") or 0)
        side_w = int(layout_last.get("side_w") or 0)
        if out_w <= 0 or out_h <= 0:
            return

        hud_x0 = side_w
        hud_x1 = side_w + hud_w

        for b in hud_boxes:
            try:
                w = max(40, int(b.get("w", 200)))
                h = max(30, int(b.get("h", 100)))
                x = int(b.get("x", 0))
                y = int(b.get("y", 0))
            except Exception:
                continue

            if x == 0:
                x = hud_x0 + 10

            x = clamp(x, hud_x0, max(hud_x0, hud_x1 - w))
            y = clamp(y, 0, max(0, out_h - h))

            b["x"] = int(x)
            b["y"] = int(y)
            b["w"] = int(w)
            b["h"] = int(h)

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

    def draw_layout_preview() -> None:
        nonlocal hud_boxes

        try:
            area_w = max(200, preview_area.winfo_width())
            area_h = max(200, preview_area.winfo_height())
        except Exception:
            return

        out_w, out_h = parse_preset(out_preset_var.get())
        if out_w <= 0 or out_h <= 0:
            out_w, out_h = 1280, 720

        hud_w = get_hud_width_px()
        hud_w = max(0, min(int(hud_w), max(0, out_w - 2)))

        side_w = int((out_w - hud_w) / 2)

        pad = 10
        avail_w = max(50, area_w - 2 * pad)
        avail_h = max(50, area_h - 2 * pad)

        scale = min(avail_w / max(1, out_w), avail_h / max(1, out_h))
        draw_w = int(out_w * scale)
        draw_h = int(out_h * scale)

        x0 = int((area_w - draw_w) / 2)
        y0 = int((area_h - draw_h) / 2)
        x1 = x0 + draw_w
        y1 = y0 + draw_h

        layout_last["out_w"] = int(out_w)
        layout_last["out_h"] = int(out_h)
        layout_last["hud_w"] = int(hud_w)
        layout_last["side_w"] = int(side_w)
        layout_last["x0"] = int(x0)
        layout_last["y0"] = int(y0)
        layout_last["scale"] = float(scale)

        side_w_px = int(side_w * scale)
        hud_w_px = int(hud_w * scale)

        layout_canvas.delete("all")

        layout_canvas.create_rectangle(x0, y0, x1, y1)

        lx0 = x0
        lx1 = x0 + side_w_px
        mx0 = lx1
        mx1 = mx0 + hud_w_px
        rx0 = mx1
        rx1 = x1

        layout_canvas.create_rectangle(lx0, y0, lx1, y1)
        layout_canvas.create_rectangle(mx0, y0, mx1, y1)
        layout_canvas.create_rectangle(rx0, y0, rx1, y1)

        layout_canvas.create_text(int((lx0 + lx1) / 2), int((y0 + y1) / 2), text="Slow")
        layout_canvas.create_text(int((mx0 + mx1) / 2), int((y0 + y1) / 2) - 40, text=f"HUD\n{hud_w}px")
        layout_canvas.create_text(int((rx0 + rx1) / 2), int((y0 + y1) / 2), text="Fast")

        if (hud_active_id is None) and (hud_mode == ""):
            hud_boxes = get_hud_boxes_for_current()
            ensure_boxes_in_hud_area()
        else:
            ensure_boxes_in_hud_area()

        en = enabled_types()

        def out_to_canvas(x: int, y: int) -> tuple[int, int]:
            cx = int(x0 + (x * scale))
            cy = int(y0 + (y * scale))
            return cx, cy

        layout_canvas.create_rectangle(mx0, y0, mx1, y1)

        for b in hud_boxes:
            t = str(b.get("type") or "")
            if t not in en:
                continue

            try:
                bx = int(b.get("x", 0))
                by = int(b.get("y", 0))
                bw = int(b.get("w", 200))
                bh = int(b.get("h", 100))
            except Exception:
                continue

            c0x, c0y = out_to_canvas(bx, by)
            c1x, c1y = out_to_canvas(bx + bw, by + bh)

            tag = f"hud_{t.replace(' ', '_').replace('/', '_')}"
            layout_canvas.create_rectangle(c0x, c0y, c1x, c1y, tags=("hud_box", tag))
            layout_canvas.create_text(int((c0x + c1x) / 2), int((c0y + c1y) / 2), text=t, tags=("hud_box", tag))

            hx0 = max(c0x, c1x - 12)
            hy0 = max(c0y, c1y - 12)
            layout_canvas.create_rectangle(hx0, hy0, c1x, c1y, tags=("hud_handle", tag))

    def get_active_box_by_type(t: str) -> dict | None:
        for b in hud_boxes:
            if str(b.get("type") or "") == t:
                return b
        return None

    def canvas_to_out_xy(cx: float, cy: float) -> tuple[float, float]:
        x0 = float(layout_last.get("x0") or 0)
        y0 = float(layout_last.get("y0") or 0)
        scale = float(layout_last.get("scale") or 1.0)
        if scale <= 0.0001:
            scale = 1.0
        return ((cx - x0) / scale, (cy - y0) / scale)

    def hud_bounds_out() -> tuple[int, int, int, int]:
        out_h = int(layout_last.get("out_h") or 0)
        hud_w = int(layout_last.get("hud_w") or 0)
        side_w = int(layout_last.get("side_w") or 0)
        hud_x0 = side_w
        hud_x1 = side_w + hud_w
        return hud_x0, hud_x1, 0, out_h

    def clamp_box_in_hud(b: dict) -> None:
        hud_x0, hud_x1, y0, out_h = hud_bounds_out()
        try:
            x = float(b.get("x", 0))
            y = float(b.get("y", 0))
            w = float(b.get("w", 200))
            h = float(b.get("h", 100))
        except Exception:
            return

        w = max(40.0, w)
        h = max(30.0, h)

        max_x = max(float(hud_x0), float(hud_x1) - w)
        max_y = max(float(y0), float(out_h) - h)

        if x < hud_x0:
            x = float(hud_x0)
        if x > max_x:
            x = float(max_x)
        if y < y0:
            y = float(y0)
        if y > max_y:
            y = float(max_y)

        b["x"] = int(round(x))
        b["y"] = int(round(y))
        b["w"] = int(round(w))
        b["h"] = int(round(h))

    def hit_test_box(event_x: int, event_y: int) -> tuple[str | None, str]:
        if int(layout_last.get("out_w") or 0) <= 0:
            return None, ""

        ox, oy = canvas_to_out_xy(float(event_x), float(event_y))
        en = enabled_types()

        scale = float(layout_last.get("scale") or 1.0)
        edge_tol_out = 8.0 / max(0.0001, scale)

        hit_t: str | None = None
        hit_mode: str = ""

        for b in hud_boxes:
            t = str(b.get("type") or "")
            if t not in en:
                continue

            bx = float(b.get("x", 0))
            by = float(b.get("y", 0))
            bw = float(b.get("w", 200))
            bh = float(b.get("h", 100))

            if ox < bx or oy < by or ox > (bx + bw) or oy > (by + bh):
                continue

            left = abs(ox - bx) <= edge_tol_out
            right = abs(ox - (bx + bw)) <= edge_tol_out
            top = abs(oy - by) <= edge_tol_out
            bottom = abs(oy - (by + bh)) <= edge_tol_out

            mode = "move"
            if top and left:
                mode = "nw"
            elif top and right:
                mode = "ne"
            elif bottom and left:
                mode = "sw"
            elif bottom and right:
                mode = "se"
            elif top:
                mode = "n"
            elif bottom:
                mode = "s"
            elif left:
                mode = "w"
            elif right:
                mode = "e"

            hit_t = t
            hit_mode = mode

        return hit_t, hit_mode

    def cursor_for_mode(mode: str) -> str:
        if mode == "move":
            return "fleur"
        if mode in ("n", "s"):
            return "sb_v_double_arrow"
        if mode in ("e", "w"):
            return "sb_h_double_arrow"
        if mode in ("ne", "sw"):
            return "top_right_corner"
        if mode in ("nw", "se"):
            return "top_left_corner"
        return ""

    hud_active_id = None
    hud_mode = ""
    hud_start_mouse_ox: float = 0.0
    hud_start_mouse_oy: float = 0.0
    hud_start_x: float = 0.0
    hud_start_y: float = 0.0
    hud_start_w: float = 0.0
    hud_start_h: float = 0.0

    def on_layout_hover(e) -> None:
        if cap is not None:
            try:
                layout_canvas.configure(cursor="")
            except Exception:
                pass
            return

        t, mode = hit_test_box(int(e.x), int(e.y))
        cur = cursor_for_mode(mode) if t is not None else ""
        try:
            layout_canvas.configure(cursor=cur)
        except Exception:
            pass

    def on_layout_leave(_e=None) -> None:
        try:
            layout_canvas.configure(cursor="")
        except Exception:
            pass

    def on_layout_mouse_down(e) -> None:
        nonlocal hud_active_id, hud_mode
        nonlocal hud_start_mouse_ox, hud_start_mouse_oy
        nonlocal hud_start_x, hud_start_y, hud_start_w, hud_start_h

        if cap is not None:
            return

        t, mode = hit_test_box(int(e.x), int(e.y))
        if t is None:
            hud_active_id = None
            hud_mode = ""
            return

        hud_active_id = t
        hud_mode = mode

        ox, oy = canvas_to_out_xy(float(e.x), float(e.y))
        hud_start_mouse_ox = ox
        hud_start_mouse_oy = oy

        b = get_active_box_by_type(t)
        if b is None:
            hud_active_id = None
            hud_mode = ""
            return

        hud_start_x = float(b.get("x", 0))
        hud_start_y = float(b.get("y", 0))
        hud_start_w = float(b.get("w", 200))
        hud_start_h = float(b.get("h", 100))

    def on_layout_mouse_move(e) -> None:
        if cap is not None:
            return
        if hud_active_id is None or hud_mode == "":
            return

        b = get_active_box_by_type(hud_active_id)
        if b is None:
            return

        ox, oy = canvas_to_out_xy(float(e.x), float(e.y))
        dx = ox - hud_start_mouse_ox
        dy = oy - hud_start_mouse_oy

        min_w = 40.0
        min_h = 30.0

        x = hud_start_x
        y = hud_start_y
        w = hud_start_w
        h = hud_start_h

        if hud_mode == "move":
            x = hud_start_x + dx
            y = hud_start_y + dy
        else:
            if "e" in hud_mode:
                w = max(min_w, hud_start_w + dx)
            if "s" in hud_mode:
                h = max(min_h, hud_start_h + dy)

            if "w" in hud_mode:
                x = hud_start_x + dx
                w = max(min_w, hud_start_w - dx)

            if "n" in hud_mode:
                y = hud_start_y + dy
                h = max(min_h, hud_start_h - dy)

        b["x"] = int(round(x))
        b["y"] = int(round(y))
        b["w"] = int(round(w))
        b["h"] = int(round(h))

        clamp_box_in_hud(b)
        draw_layout_preview()

    def on_layout_mouse_up(_e=None) -> None:
        nonlocal hud_active_id, hud_mode
        if cap is not None:
            return
        if hud_active_id is None:
            return

        try:
            layout_canvas.configure(cursor="")
        except Exception:
            pass

        hud_active_id = None
        hud_mode = ""
        save_current_boxes()

    layout_canvas.bind("<Motion>", on_layout_hover)
    layout_canvas.bind("<Leave>", on_layout_leave)
    layout_canvas.bind("<ButtonPress-1>", on_layout_mouse_down)
    layout_canvas.bind("<B1-Motion>", on_layout_mouse_move)
    layout_canvas.bind("<ButtonRelease-1>", on_layout_mouse_up)

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
        draw_layout_preview()
    except Exception:
        pass

    scrub = ttk.Scale(frame_preview, from_=0, to=0, orient="horizontal")
    scrub.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
    scrub.grid_remove()

    current_video_original: Path | None = None
    current_video_opened: Path | None = None
    cap: cv2.VideoCapture | None = None

    current_frame_idx: int = 0
    total_frames: int = 0
    fps: float = 30.0

    end_frame_idx: int = 0

    def clamp_frame(idx: int) -> int:
        if total_frames <= 0:
            return max(0, idx)
        if idx < 0:
            return 0
        if idx > total_frames - 1:
            return total_frames - 1
        return idx

    def set_endframe(idx: int, save: bool = True) -> None:
        nonlocal end_frame_idx, endframes_by_name
        idx = clamp_frame(int(idx))
        end_frame_idx = idx

        try:
            spn_end.configure(from_=0, to=max(0, total_frames - 1))
        except Exception:
            pass

        try:
            end_var.set(int(end_frame_idx))
        except Exception:
            pass

        lbl_end.config(text=f"Ende: {end_frame_idx}")

        if save and current_video_original is not None:
            endframes_by_name[current_video_original.name] = int(end_frame_idx)
            persistence.save_endframes(endframes_by_name)

    def auto_end_from_start(start_idx: int) -> None:
        lap_ms = extract_time_ms(current_video_original) if current_video_original is not None else None
        if lap_ms is None:
            set_endframe(total_frames - 1, save=True)
            return
        dur_frames = int(round((lap_ms / 1000.0) * max(1.0, fps)))
        set_endframe(int(start_idx) + int(dur_frames), save=True)

    def save_endframe_from_ui() -> None:
        try:
            set_endframe(int(end_var.get()), save=True)
        except Exception:
            pass

    def safe_unlink(p: Path) -> None:
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass

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

        # Optional: CSVs (wenn vorhanden)
        vnames = [p.name for p in videos[:2]]
        cnames = [p.name for p in csvs[:2]]

        hud_enabled = {}
        try:
            for t, var in hud_enabled_vars.items():
                hud_enabled[str(t)] = bool(var.get())
        except Exception:
            pass

        project_root_local = find_project_root(Path(__file__))
        run_json_path = project_root_local / "config" / "ui_last_run.json"
        try:
            run_json_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        # Story 6: Gear & RPM HUD (Update-Rate)
        gear_rpm_update_hz = persistence._cfg_int("video_compare", "gear_rpm_update_hz", 60)
        if gear_rpm_update_hz < 1:
            gear_rpm_update_hz = 1
        if gear_rpm_update_hz > 60:
            gear_rpm_update_hz = 60

        # Story 5: Speed HUD (Einheit + Update-Rate)
        speed_units = str(persistence.cfg_get("video_compare", "speed_units", "kmh")).strip().lower()
        if speed_units not in ("kmh", "mph"):
            speed_units = "kmh"

        speed_update_hz = persistence._cfg_int("video_compare", "speed_update_hz", 60)
        if speed_update_hz < 1:
            speed_update_hz = 1
        if speed_update_hz > 60:
            speed_update_hz = 60

        hud_win_default_before = persistence._cfg_float("video_compare", "hud_window_default_before_s", 10.0)
        hud_win_default_after = persistence._cfg_float("video_compare", "hud_window_default_after_s", 10.0)

        hud_win_overrides: dict[str, dict[str, float]] = {}

        def _add_override(hud_name: str, ini_prefix: str) -> None:
            b = persistence._cfg_float_opt("video_compare", f"hud_window_{ini_prefix}_before_s")
            a = persistence._cfg_float_opt("video_compare", f"hud_window_{ini_prefix}_after_s")
            if b is None and a is None:
                return
            d: dict[str, float] = {}
            if b is not None:
                d["before_s"] = float(b)
            if a is not None:
                d["after_s"] = float(a)
            hud_win_overrides[hud_name] = d

        _add_override("Throttle / Brake", "throttle_brake")
        _add_override("Steering", "steering")
        _add_override("Delta", "delta")
        _add_override("Line Delta", "line_delta")
        _add_override("Under-/Oversteer", "under_oversteer")
        
        # Story 3: HUD-Kurvenpunkte (Punktdichte)
        hud_pts_default = persistence._cfg_int("video_compare", "hud_curve_points_default", 180)

        hud_pts_overrides: dict[str, int] = {}

        def _add_pts_override(hud_name: str, ini_suffix: str) -> None:
            v = persistence._cfg_int_opt("video_compare", f"hud_curve_points_{ini_suffix}")
            if v is None:
                return
            hud_pts_overrides[hud_name] = int(v)

        _add_pts_override("Throttle / Brake", "throttle_brake")
        _add_pts_override("Steering", "steering")
        _add_pts_override("Delta", "delta")
        _add_pts_override("Line Delta", "line_delta")
        _add_pts_override("Under-/Oversteer", "under_oversteer")

        app_model = model_from_ui_state()
        payload = {
            "version": 1,
            "videos": vnames,
            "csvs": cnames,
            "slow_video": str(slow_p),
            "fast_video": str(fast_p),
            "out_video": str(out_path),
            "output": {
                "aspect": str(out_aspect_var.get()),
                "preset": str(out_preset_var.get()),
                "quality": str(out_quality_var.get()),
                "hud_width_px": int(hud_w),
            },
            # Welche HUDs aktiv sind
            "hud_enabled": hud_enabled,
            # HUD-Positionen und Größen (nur aktueller Layout-Key)
            "hud_boxes": {},
            # Story 2: HUD-Fenster (Sekunden)
            "hud_window": {
                "default_before_s": float(hud_win_default_before),
                "default_after_s": float(hud_win_default_after),
                "overrides": hud_win_overrides,
            },

            # Story 5: Speed HUD (Einheit + Update-Rate)
            "hud_speed": {
                "units": str(speed_units),
                "update_hz": int(speed_update_hz),
            },

            # Story 3: HUD-Kurvenpunkte (Punktdichte)
            "hud_curve_points": {
                "default": int(hud_pts_default),
                "overrides": hud_pts_overrides,
            },
            
            # Story 6: Gear & RPM HUD (Update-Rate)
            "hud_gear_rpm": {
                "update_hz": int(gear_rpm_update_hz),
            },
            
            # PNG-Ausrichtung (nur aktueller Zustand)
            "png_view_key": "",
            "png_view_state": {"L": {}, "R": {}},
            # Gesamt-States (für Profil / Zukunft)
            "hud_layout_data": app_model.hud_layout.hud_layout_data,
            "png_view_data": app_model.png_view.png_view_data,
        }
        payload = RenderPayload(
            version=payload.get("version", 1),
            videos=payload.get("videos", []),
            csvs=payload.get("csvs", []),
            slow_video=payload.get("slow_video", ""),
            fast_video=payload.get("fast_video", ""),
            out_video=payload.get("out_video", ""),
            output=OutputFormat.from_dict(payload.get("output") if isinstance(payload.get("output"), dict) else {}),
            hud_enabled=payload.get("hud_enabled", {}),
            hud_boxes=payload.get("hud_boxes", {}),
            hud_window=payload.get("hud_window", {}),
            hud_speed=payload.get("hud_speed", {}),
            hud_curve_points=payload.get("hud_curve_points", {}),
            hud_gear_rpm=payload.get("hud_gear_rpm", {}),
            png_view_key=payload.get("png_view_key", ""),
            png_view_state=payload.get("png_view_state", {"L": {}, "R": {}}),
            hud_layout_data=payload.get("hud_layout_data", {}),
            png_view_data=payload.get("png_view_data", {}),
        ).to_dict()

        # Aktuelle HUD-Boxen einsammeln
        try:
            boxes_list = get_hud_boxes_for_current()
            boxes_map = {}
            for b in boxes_list:
                if not isinstance(b, dict):
                    continue
                t = str(b.get("type") or "").strip()
                if not t:
                    continue
                boxes_map[t] = {
                    "x": int(b.get("x") or 0),
                    "y": int(b.get("y") or 0),
                    "w": int(b.get("w") or 0),
                    "h": int(b.get("h") or 0),
                }
            payload["hud_boxes"] = boxes_map
        except Exception:
            pass
            
        # Story 3: HUD-Kurvenpunkte Overrides finalisieren
        # Regel: ini-Wert 0 => Maximalwert passend zur HUD-Box-Breite (px)
        try:
            hb = payload.get("hud_boxes") or {}
            if isinstance(hb, dict):
                for hud_name, v in list(hud_pts_overrides.items()):
                    try:
                        vv = int(v)
                    except Exception:
                        continue

                    if vv == 0:
                        box = hb.get(hud_name) or {}
                        try:
                            w = int(box.get("w") or 0)
                        except Exception:
                            w = 0

                        # Wenn Breite nicht sinnvoll ermittelbar ist: Override entfernen => Default verwenden
                        if w > 0:
                            hud_pts_overrides[hud_name] = int(w)
                        else:
                            try:
                                del hud_pts_overrides[hud_name]
                            except Exception:
                                pass
        except Exception:
            pass

        # (Wichtig) payload enthält bereits hud_curve_points -> hier nur sicherstellen,
        # dass die Overrides mit den finalen Werten geschrieben sind
        try:
            cp = payload.get("hud_curve_points")
            if isinstance(cp, dict):
                cp["overrides"] = hud_pts_overrides
        except Exception:
            pass

        # Aktuellen PNG-State sichern + in Payload schreiben
        try:
            png_save_state_for_current()
            payload["png_view_key"] = str(png_view_key())
            payload["png_view_state"] = {
                "L": {
                    "zoom": float(png_state["L"].get("zoom", 1.0)),
                    "off_x": int(png_state["L"].get("off_x", 0)),
                    "off_y": int(png_state["L"].get("off_y", 0)),
                    "fit_to_height": bool(png_state["L"].get("fit_to_height", False)),
                },
                "R": {
                    "zoom": float(png_state["R"].get("zoom", 1.0)),
                    "off_x": int(png_state["R"].get("off_x", 0)),
                    "off_y": int(png_state["R"].get("off_y", 0)),
                    "fit_to_height": bool(png_state["R"].get("fit_to_height", False)),
                },
            }
        except Exception:
            pass

        try:
            run_json_path.write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8"
            )
        except Exception:
            lbl_loaded.config(text="Video: Konnte UI-JSON nicht schreiben")
            return

        main_py = project_root_local / "src" / "main.py"
        if not main_py.exists():
            lbl_loaded.config(text="Video: main.py nicht gefunden")
            return

        win, close, set_text, set_progress, is_cancelled = show_progress_with_cancel(
            "Video erzeugen",
            "Starte main.py…"
        )
        root.update()

        def worker() -> None:
            p = None
            try:
                set_text("main.py läuft… (Abbruch möglich)")
                try:
                    set_progress(0.0)
                except Exception:
                    pass

                # Gesamtdauer aus Dateinamen (mm.ss.mmm) – wir nehmen die längere
                total_ms_a = extract_time_ms(slow_p) or 0
                total_ms_b = extract_time_ms(fast_p) or 0
                total_ms = int(max(total_ms_a, total_ms_b))
                total_sec = float(total_ms) / 1000.0 if total_ms > 0 else 0.0

                # main.py starten – stdout+stderr zusammen, damit wir:
                # 1) wieder alles im PowerShell sehen
                # 2) "time=.." aus ffmpeg parsen können
                #
                # WICHTIG: -u => unbuffered, damit Fortschritt sofort kommt
                import sys as _sys
                cmd = [_sys.executable, "-u", str(main_py), "--ui-json", str(run_json_path)]
                p = subprocess.Popen(
                    cmd,
                    cwd=str(project_root_local),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )

                q_lines: "queue.Queue[str]" = queue.Queue()

                def _reader(stream) -> None:
                    try:
                        if stream is None:
                            return
                        for raw in stream:
                            q_lines.put(raw)
                    except Exception:
                        pass

                threading.Thread(target=_reader, args=(p.stdout,), daemon=True).start()

                last_ui_update = 0.0
                last_sec = 0.0
                last_pct = -1.0

                # Hilfsparser:
                # 1) klassisch: "time=HH:MM:SS.xx"
                # 2) ffmpeg -progress: "out_time_ms=12345678" oder "out_time=HH:MM:SS.xxxxxx"
                time_re = re.compile(r"time=(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?")
                out_time_ms_re = re.compile(r"out_time_ms=(\d+)")
                out_time_re = re.compile(r"out_time=(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?")

                # PowerShell-Ausgabe optional (default: AUS)
                show_live = (os.environ.get("IRVC_UI_SHOW_LOG") or "").strip() == "1"

                # Wenn Debug-Cut aktiv ist, soll der Fortschritt auch nur bis dbg_max_s laufen
                dbg_max_s = 0.0
                try:
                    dbg_max_s = float((os.environ.get("IRVC_DEBUG_MAX_S") or "").strip() or "0")
                except Exception:
                    dbg_max_s = 0.0
                if dbg_max_s > 0.0 and total_sec > 0:
                    total_sec = min(total_sec, dbg_max_s)

                while True:
                    if is_cancelled():
                        try:
                            set_text("Abbruch…")
                        except Exception:
                            pass
                        try:
                            if p is not None and p.pid:
                                subprocess.run(
                                    ["taskkill", "/PID", str(p.pid), "/T", "/F"],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    check=False,
                                )
                        except Exception:
                            pass
                        break

                    rc = p.poll()
                    if rc is not None and q_lines.empty():
                        break

                    try:
                        line = q_lines.get(timeout=0.1)
                    except Exception:
                        line = ""

                    if line:
                        # Live-Ausgabe nur wenn gewünscht
                        if show_live:
                            try:
                                print(line, end="", flush=True)
                            except Exception:
                                pass

                        # Parser 1: time=HH:MM:SS.xxx
                        m = time_re.search(line)
                        if m:
                            try:
                                hh = int(m.group(1))
                                mm = int(m.group(2))
                                ss = int(m.group(3))
                                frac = m.group(4) or "0"
                                frac = (frac + "000")[:3]
                                ms = int(frac)
                                sec = hh * 3600.0 + mm * 60.0 + ss + (ms / 1000.0)
                                if sec > last_sec:
                                    last_sec = sec
                            except Exception:
                                pass

                        # Parser 2: out_time_ms=12345678
                        m2 = out_time_ms_re.search(line)
                        if m2:
                            try:
                                sec = float(int(m2.group(1))) / 1000000.0
                                if sec > last_sec:
                                    last_sec = sec
                            except Exception:
                                pass

                        # Parser 3: out_time=HH:MM:SS.xxxxxx
                        m3 = out_time_re.search(line)
                        if m3:
                            try:
                                hh = int(m3.group(1))
                                mm = int(m3.group(2))
                                ss = int(m3.group(3))
                                frac = m3.group(4) or "0"
                                frac = (frac + "000000")[:6]
                                us = int(frac[:6])
                                sec = hh * 3600.0 + mm * 60.0 + ss + (us / 1000000.0)
                                if sec > last_sec:
                                    last_sec = sec
                            except Exception:
                                pass

                    now = time.time()
                    if total_sec > 0 and (now - last_ui_update) >= 1.0:
                        pct = (last_sec / total_sec) * 100.0
                        if pct < 0.0:
                            pct = 0.0
                        if pct > 100.0:
                            pct = 100.0

                        if abs(pct - last_pct) >= 0.5 or pct >= 100.0:
                            try:
                                set_progress(float(pct))
                            except Exception:
                                pass
                            try:
                                set_text(f"Render läuft… {pct:.0f}%")
                            except Exception:
                                pass
                            last_pct = pct

                        last_ui_update = now


                # Am Ende auf 100% setzen (wenn nicht abgebrochen)
                if not is_cancelled():
                    try:
                        set_progress(100.0)
                    except Exception:
                        pass

                try:
                    if p is not None:
                        p.wait(timeout=5)
                except Exception:
                    pass

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

                if is_cancelled():
                    root.after(0, finish_cancel)
                else:
                    root.after(0, finish_ok)

            except Exception:
                try:
                    root.after(0, lambda: lbl_loaded.config(text="Video: Render fehlgeschlagen"))
                    root.after(0, close)
                except Exception:
                    pass
            finally:
                try:
                    if p is not None and p.poll() is None:
                        p.kill()
                except Exception:
                    pass


        t = threading.Thread(target=worker, daemon=True)
        t.start()



    btn_generate.config(command=generate_compare_video)
    
    def cut_current_video() -> None:
        nonlocal is_playing

        if current_video_original is None:
            return
        if not ffmpeg_exists():
            lbl_loaded.config(text="Video: ffmpeg fehlt (Schneiden nicht möglich)")
            return

        s = clamp_frame(int(startframes_by_name.get(current_video_original.name, 0)))
        e = clamp_frame(int(end_frame_idx))

        if e <= s:
            lbl_loaded.config(text="Video: Endframe muss > Startframe sein")
            return

        is_playing = False
        btn_play.config(text="▶")

        start_sec = s / max(1.0, fps)
        dur_sec = (max(0, (e - s) + 1)) / max(1.0, fps)

        src = current_video_original
        dst_final = input_video_dir / src.name
        tmp = input_video_dir / (src.stem + "__cut_tmp.mp4")

        close_preview_video()

        if src is not None:
            proxy_path = proxy_dir / (src.stem + "__proxy_h264.mp4")
            safe_unlink(proxy_path)

        progress_win, progress_close = show_progress("Schneiden", "Video wird geschnitten… Bitte warten.")
        root.update()

        try:
            lbl_loaded.config(text="Video: Schneiden läuft…")
            root.update_idletasks()

            cmd = [
                "ffmpeg",
                "-y",
                "-ss", f"{start_sec:.6f}",
                "-i", str(dst_final),
                "-t", f"{dur_sec:.6f}",
                "-an",
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-preset", "veryfast",
                "-crf", "20",
                str(tmp),
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

            if not (tmp.exists() and tmp.stat().st_size > 0):
                lbl_loaded.config(text="Video: Schneiden fehlgeschlagen")
                safe_unlink(tmp)
                sync_from_folders_if_needed(force=True)
                return

            safe_unlink(dst_final)
            tmp.replace(dst_final)

            startframes_by_name[dst_final.name] = 0
            persistence.save_startframes(startframes_by_name)

            endframes_by_name[dst_final.name] = clamp_frame(int(dur_sec * max(1.0, fps)))
            persistence.save_endframes(endframes_by_name)

            lbl_loaded.config(text="Video: Geschnitten & ersetzt")
        except Exception:
            lbl_loaded.config(text="Video: Schneiden fehlgeschlagen")
            safe_unlink(tmp)
        finally:
            progress_close()

        sync_from_folders_if_needed(force=True)
        start_crop_for_video(dst_final)

    is_playing: bool = False
    speed_factor: float = 1.0
    tk_img = None

    scrub_is_dragging: bool = False
    last_render_ts: float = 0.0

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
                    draw_layout_preview()
                except Exception:
                    pass

    def on_preview_mode_change(*_args) -> None:
        if cap is not None:
            return
        try:
            show_preview_controls(False)
        except Exception:
            pass

    try:
        preview_mode_var.trace_add("write", on_preview_mode_change)
    except Exception:
        pass

    def close_preview_video() -> None:
        nonlocal cap, current_video_original, current_video_opened, current_frame_idx, is_playing, tk_img, total_frames, speed_factor
        is_playing = False
        current_frame_idx = 0
        current_video_original = None
        current_video_opened = None
        tk_img = None
        total_frames = 0
        speed_factor = 1.0
        btn_play.config(text="▶")

        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        cap = None

        preview_label.config(image="", text="")
        lbl_frame.config(text="Frame: –")
        lbl_loaded.config(text="Video: –")
        scrub.configure(from_=0, to=0)
        scrub.set(0)
        show_preview_controls(False)

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

    def render_image_from_frame(frame) -> None:
        nonlocal tk_img
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)

        area_w = max(200, preview_area.winfo_width())
        area_h = max(200, preview_area.winfo_height())
        img.thumbnail((area_w, area_h), Image.LANCZOS)

        tk_img = ImageTk.PhotoImage(img)
        preview_label.config(image=tk_img, text="")

    def seek_and_read(idx: int) -> bool:
        nonlocal cap, current_frame_idx, total_frames, fps

        if cap is None:
            return False

        if total_frames > 0:
            if idx < 0:
                idx = 0
            if idx > total_frames - 1:
                idx = total_frames - 1

        cap.set(cv2.CAP_PROP_POS_FRAMES, float(idx))
        ok, frame = cap.read()
        if not ok or frame is None:
            return False

        current_frame_idx = idx

        fps_val = cap.get(cv2.CAP_PROP_FPS)
        if fps_val and fps_val > 0.1:
            fps = float(fps_val)

        render_image_from_frame(frame)
        lbl_frame.config(text=f"Frame: {current_frame_idx}")

        if (not scrub_is_dragging) and total_frames > 0:
            scrub.set(current_frame_idx)

        return True

    def read_next_frame() -> bool:
        nonlocal cap, current_frame_idx, total_frames, fps

        if cap is None:
            return False

        ok, frame = cap.read()
        if not ok or frame is None:
            return False

        pos = int(cap.get(cv2.CAP_PROP_POS_FRAMES) or 0)
        current_frame_idx = max(0, pos - 1)

        fps_val = cap.get(cv2.CAP_PROP_FPS)
        if fps_val and fps_val > 0.1:
            fps = float(fps_val)

        render_image_from_frame(frame)
        lbl_frame.config(text=f"Frame: {current_frame_idx}")

        if (not scrub_is_dragging) and total_frames > 0:
            scrub.set(current_frame_idx)

        return True

    def render_frame(idx: int, force: bool = False) -> None:
        nonlocal last_render_ts
        now = time.time()
        if (not force) and (now - last_render_ts) < 0.02:
            return
        last_render_ts = now
        seek_and_read(idx)

    def play_tick() -> None:
        nonlocal is_playing
        if not is_playing:
            return

        ok = read_next_frame()
        if not ok:
            is_playing = False
            btn_play.config(text="▶")
            return

        base = 1000.0 / max(1.0, fps)
        delay = int(base / max(0.25, speed_factor))
        delay = max(1, delay)
        root.after(delay, play_tick)

    def on_play_pause() -> None:
        nonlocal is_playing
        if cap is None:
            return
        is_playing = not is_playing
        btn_play.config(text="⏸" if is_playing else "▶")
        if is_playing:
            play_tick()

    def on_prev_frame() -> None:
        nonlocal is_playing
        if cap is None:
            return
        is_playing = False
        btn_play.config(text="▶")
        render_frame(current_frame_idx - 1, force=True)

    def on_next_frame() -> None:
        nonlocal is_playing
        if cap is None:
            return
        is_playing = False
        btn_play.config(text="▶")
        render_frame(current_frame_idx + 1, force=True)

    def on_scrub_press(_event=None) -> None:
        nonlocal scrub_is_dragging
        scrub_is_dragging = True

    def on_scrub_release(_event=None) -> None:
        nonlocal scrub_is_dragging, is_playing
        scrub_is_dragging = False
        if cap is None:
            return
        is_playing = False
        btn_play.config(text="▶")
        render_frame(int(scrub.get()), force=True)

    def on_scrub_move(_event=None) -> None:
        if cap is None:
            return
        if scrub_is_dragging:
            render_frame(int(scrub.get()), force=False)

    def set_start_here() -> None:
        nonlocal startframes_by_name
        if current_video_original is None:
            return
        startframes_by_name[current_video_original.name] = int(current_frame_idx)
        persistence.save_startframes(startframes_by_name)
        auto_end_from_start(int(current_frame_idx))

    def start_crop_for_video(video_path: Path) -> None:
        nonlocal cap, current_video_original, current_video_opened, current_frame_idx, is_playing, fps, speed_factor, total_frames

        close_preview_video()

        current_video_original = video_path
        current_video_opened = video_path

        c, status = try_open_video(video_path)
        if c is None and status in ("open_failed", "read_failed"):
            proxy = make_proxy_h264(video_path)
            if proxy is not None:
                current_video_opened = proxy
                c, status = try_open_video(proxy)

        if c is None:
            lbl_loaded.config(text="Video: Kann nicht gelesen werden (Codec?)")
            preview_label.config(text="Dieses Video kann hier nicht gelesen werden.\nBitte erst in H.264 umwandeln.\nOder ffmpeg installieren.")
            show_preview_controls(False)
            return

        cap = c

        fps_val = cap.get(cv2.CAP_PROP_FPS)
        fps = float(fps_val) if (fps_val and fps_val > 0.1) else 30.0

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total_frames < 1:
            total_frames = 1

        scrub.configure(from_=0, to=max(0, total_frames - 1))
        scrub.set(0)

        speed_factor = 1.0

        start_idx = int(startframes_by_name.get(video_path.name, 0))
        current_frame_idx = start_idx

        saved_end = endframes_by_name.get(video_path.name)
        if saved_end is not None:
            set_endframe(int(saved_end), save=False)
        else:
            auto_end_from_start(int(start_idx))

        lbl_loaded.config(text=f"Video: {video_path.name}")

        show_preview_controls(True)

        def _late_render() -> None:
            render_frame(current_frame_idx, force=True)

        root.after(60, _late_render)

    def on_preview_resize(_event=None) -> None:
        if cap is not None:
            render_frame(current_frame_idx, force=True)
            return

        try:
            if preview_mode_var.get() == "png":
                render_png_preview(force_reload=False)
            else:
                draw_layout_preview()
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
                if current_video_original is not None and item.name == current_video_original.name:
                    close_preview_video()

            ok = delete_file(item)
            if not ok:
                return

            if kind == "video":
                videos = [p for p in videos if p != item]
            else:
                csvs = [p for p in csvs if p != item]

            refresh_display()

        def do_open_folder() -> None:
            open_folder(item)

        menu.add_command(label="Löschen", command=do_delete)
        menu.add_command(label="Ordner öffnen", command=do_open_folder)
        menu.tk_popup(event.x_root, event.y_root)

    btn_v1.bind("<Button-1>", lambda e: show_menu_for_item(e, "video", 0))
    btn_v2.bind("<Button-1>", lambda e: show_menu_for_item(e, "video", 1))
    btn_c1.bind("<Button-1>", lambda e: show_menu_for_item(e, "csv", 0))
    btn_c2.bind("<Button-1>", lambda e: show_menu_for_item(e, "csv", 1))

    sync_from_folders_if_needed(force=True)
    root.mainloop()


if __name__ == "__main__":
    main()
