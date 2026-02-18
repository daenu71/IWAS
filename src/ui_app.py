import tkinter as tk
from tkinter import ttk, filedialog
import math
import re
from pathlib import Path
import json
import os
import subprocess
import threading

from core.models import (
    AppModel,
    HudLayoutState,
    LayoutConfig,
    OutputFormat,
    PngViewState,
    PROFILE_SCHEMA_VERSION,
    Profile,
    VIDEO_CUT_DEFAULTS,
    migrate_profile_contract_dict,
    migrate_ui_last_run_contract_dict,
)
from core import persistence, filesvc, profile_service, render_service
from core.output_geometry import (
    Rect,
    build_output_geometry_for_size,
    layout_horizontal_frame_hud_boxes,
    split_horizontal_top_bottom_rows,
    split_weighted_lengths,
    vertical_fit_weight_for_hud_key,
)
from preview.layout_preview import LayoutPreviewController, OutputFormat as LayoutPreviewOutputFormat
from preview.png_preview import PngPreviewController
from preview.video_preview import VideoPreviewController
from ui.controller import Controller, UIContext


TIME_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{3})")


def _debug_swallowed_enabled() -> bool:
    s = str(os.environ.get("IRVC_DEBUG_SWALLOWED", "") or "").strip().lower()
    return s in ("1", "true", "yes", "on")


def _safe(fn, *args, default=None, label: str | None = None, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        if _debug_swallowed_enabled():
            name = label or getattr(fn, "__name__", "call")
            try:
                print(f"[IRVC_DEBUG_SWALLOWED] {name}: {e}")
            except Exception:
                pass
        return default


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
    ui_last_run_file = config_dir / "ui_last_run.json"
    startframes_by_name: dict[str, int] = persistence.load_startframes()
    
    profiles_dir = config_dir / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)

    endframes_by_name: dict[str, int] = persistence.load_endframes()

    def _normalize_video_layout(raw: object) -> str:
        try:
            layout = str(raw or "LR").strip().upper()
        except Exception:
            layout = "LR"
        if layout not in ("LR", "TB"):
            layout = "LR"
        return str(layout)

    def _normalize_video_mode(raw: object) -> str:
        try:
            mode = str(raw or "full").strip().lower()
        except Exception:
            mode = "full"
        if mode not in ("full", "cut"):
            mode = "full"
        return str(mode)

    def _normalize_video_cut_seconds(raw: object, default: float) -> float:
        try:
            value = float(raw)
        except Exception:
            return float(default)
        if not math.isfinite(value):
            return float(default)
        if value < 0.0:
            return float(default)
        return float(value)

    def _load_ui_last_run_payload() -> dict:
        try:
            if not ui_last_run_file.exists():
                return {}
            data = json.loads(ui_last_run_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            migrated = migrate_ui_last_run_contract_dict(data)
            if migrated:
                try:
                    ui_last_run_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
                except Exception:
                    pass
            return data
        except Exception:
            return {}

    def _load_layout_config_from_ui_last_run() -> LayoutConfig:
        try:
            data = _load_ui_last_run_payload()
            if not isinstance(data, dict) or not data:
                return LayoutConfig()
            return LayoutConfig.from_dict(data)
        except Exception:
            return LayoutConfig()

    def _load_video_state_from_ui_last_run() -> dict | None:
        try:
            data = _load_ui_last_run_payload()
            if not isinstance(data, dict) or not data:
                return None
            return {
                "video_mode": _normalize_video_mode(data.get("video_mode", "full")),
                "video_before_brake": _normalize_video_cut_seconds(
                    data.get("video_before_brake", VIDEO_CUT_DEFAULTS["video_before_brake"]),
                    VIDEO_CUT_DEFAULTS["video_before_brake"],
                ),
                "video_after_full_throttle": _normalize_video_cut_seconds(
                    data.get("video_after_full_throttle", VIDEO_CUT_DEFAULTS["video_after_full_throttle"]),
                    VIDEO_CUT_DEFAULTS["video_after_full_throttle"],
                ),
                "video_minimum_between_two_curves": _normalize_video_cut_seconds(
                    data.get("video_minimum_between_two_curves", VIDEO_CUT_DEFAULTS["video_minimum_between_two_curves"]),
                    VIDEO_CUT_DEFAULTS["video_minimum_between_two_curves"],
                ),
            }
        except Exception:
            return None

    left_column = ttk.Frame(root)
    left_top_files_frame = ttk.LabelFrame(left_column, text="Dateibereich")
    left_scroll_settings_frame = ttk.LabelFrame(left_column, text="Einstellungen")
    frame_preview = ttk.LabelFrame(root, text="Vorschau")

    left_column.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
    left_column.columnconfigure(0, weight=1)
    left_column.rowconfigure(0, weight=0)
    left_column.rowconfigure(1, weight=1)

    left_top_files_frame.grid(row=0, column=0, sticky="new")
    left_scroll_settings_frame.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
    left_scroll_settings_frame.columnconfigure(0, weight=1)
    left_scroll_settings_frame.rowconfigure(0, weight=1)

    settings_canvas = tk.Canvas(left_scroll_settings_frame, highlightthickness=0, borderwidth=0)
    settings_vscroll = ttk.Scrollbar(left_scroll_settings_frame, orient="vertical", command=settings_canvas.yview)
    settings_canvas.configure(yscrollcommand=settings_vscroll.set)
    settings_canvas.grid(row=0, column=0, sticky="nsew")
    settings_vscroll.grid(row=0, column=1, sticky="ns")

    settings_inner = ttk.Frame(settings_canvas)
    settings_canvas_window = settings_canvas.create_window((0, 0), window=settings_inner, anchor="nw")

    def _update_settings_scrollregion(_event=None) -> None:
        try:
            settings_canvas.configure(scrollregion=settings_canvas.bbox("all"))
        except Exception:
            pass

    def _on_settings_canvas_configure(event) -> None:
        try:
            settings_canvas.itemconfigure(settings_canvas_window, width=max(0, int(event.width)))
        except Exception:
            pass
        _update_settings_scrollregion()

    settings_inner.bind("<Configure>", _update_settings_scrollregion)
    settings_canvas.bind("<Configure>", _on_settings_canvas_configure)

    def _pointer_over_settings_canvas() -> bool:
        try:
            if not settings_canvas.winfo_ismapped():
                return False
            px = int(root.winfo_pointerx())
            py = int(root.winfo_pointery())
            x0 = int(settings_canvas.winfo_rootx())
            y0 = int(settings_canvas.winfo_rooty())
            x1 = x0 + int(settings_canvas.winfo_width())
            y1 = y0 + int(settings_canvas.winfo_height())
            return x0 <= px < x1 and y0 <= py < y1
        except Exception:
            return False

    def _on_settings_mousewheel(event):
        if not _pointer_over_settings_canvas():
            return None
        step = 0
        try:
            if getattr(event, "delta", 0):
                step = -int(event.delta / 120)
                if step == 0:
                    step = -1 if int(event.delta) > 0 else 1
            elif getattr(event, "num", None) == 4:
                step = -1
            elif getattr(event, "num", None) == 5:
                step = 1
        except Exception:
            step = 0
        if step == 0:
            return None
        try:
            settings_canvas.yview_scroll(step, "units")
            return "break"
        except Exception:
            return None

    root.bind_all("<MouseWheel>", _on_settings_mousewheel, add="+")
    root.bind_all("<Button-4>", _on_settings_mousewheel, add="+")
    root.bind_all("<Button-5>", _on_settings_mousewheel, add="+")

    frame_files = left_top_files_frame
    frame_settings = settings_inner

    # Vorschau soll die ganze rechte Seite füllen (ohne "Aktionen")
    frame_preview.grid(row=0, column=1, sticky="nsew", padx=(0, 10), pady=10)
    # Grid-Gewichte: rechte Seite (Vorschau) wächst mit dem Fenster
    root.grid_columnconfigure(0, weight=0)
    root.grid_columnconfigure(1, weight=1)
    root.grid_rowconfigure(0, weight=1)

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

    def _norm_boxes_list(raw_boxes: object, *, add_missing: bool) -> list[dict]:
        out: list[dict] = []
        if isinstance(raw_boxes, list):
            for b in raw_boxes:
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
        if add_missing:
            have = {str(b.get("type") or "") for b in out}
            for d in default_hud_boxes():
                tt = str(d.get("type") or "")
                if tt in HUD_TYPES and tt not in have:
                    out.append(dict(d))
        return out

    def _layout_cfg() -> LayoutConfig:
        nonlocal app_model
        cfg = app_model.layout_config if isinstance(app_model.layout_config, LayoutConfig) else LayoutConfig()
        app_model.layout_config = cfg
        return cfg

    def _coerce_hud_bg_alpha(raw: object) -> int:
        try:
            v = int(round(float(raw)))
        except Exception:
            v = 255
        if v < 0:
            v = 0
        if v > 255:
            v = 255
        return int(v)

    def _is_hud_free_mode() -> bool:
        try:
            mode = str(_layout_cfg().hud_mode or "frame").strip().lower()
        except Exception:
            mode = "frame"
        return mode == "free"

    def _hud_free_boxes_to_list(cfg: LayoutConfig) -> list[dict]:
        out: list[dict] = []
        boxes_map = cfg.hud_free.boxes_abs_out if isinstance(cfg.hud_free.boxes_abs_out, dict) else {}
        for hud_key in HUD_TYPES:
            box = boxes_map.get(hud_key)
            if not isinstance(box, dict):
                continue
            try:
                x = int(box.get("x", 0))
                y = int(box.get("y", 0))
                w = int(box.get("w", 0))
                h = int(box.get("h", 0))
            except Exception:
                continue
            if w <= 0 or h <= 0:
                continue
            out.append({"type": str(hud_key), "x": x, "y": y, "w": max(40, w), "h": max(30, h)})
        return out

    def _set_hud_free_boxes_from_list(cfg: LayoutConfig, boxes: list[dict]) -> None:
        out_map: dict[str, dict[str, int]] = {}
        for b in boxes:
            if not isinstance(b, dict):
                continue
            t = str(b.get("type") or "").strip()
            if t not in HUD_TYPES:
                continue
            try:
                x = int(b.get("x", 0))
                y = int(b.get("y", 0))
                w = int(b.get("w", 0))
                h = int(b.get("h", 0))
            except Exception:
                continue
            if w <= 0 or h <= 0:
                continue
            out_map[t] = {"x": int(x), "y": int(y), "w": int(max(40, w)), "h": int(max(30, h))}
        cfg.hud_free.boxes_abs_out = out_map

    def _seed_free_boxes_from_legacy_if_missing() -> None:
        cfg = _layout_cfg()
        if not _is_hud_free_mode():
            return
        if _hud_free_boxes_to_list(cfg):
            return
        legacy = _norm_boxes_list(hud_layout_data.get(hud_layout_key()), add_missing=True)
        if legacy:
            _set_hud_free_boxes_from_list(cfg, legacy)

    def get_hud_boxes_for_current() -> list[dict]:
        key = hud_layout_key()
        cfg = _layout_cfg()
        if _is_hud_free_mode():
            return _hud_free_boxes_to_list(cfg)

        boxes = _norm_boxes_list(hud_layout_data.get(key), add_missing=True)
        if boxes:
            return boxes
        return default_hud_boxes()

    def set_hud_boxes_for_current(boxes: list[dict]) -> None:
        cfg = _layout_cfg()
        if _is_hud_free_mode():
            _set_hud_free_boxes_from_list(cfg, boxes)
            return
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
    video_mode_var = tk.StringVar(value="full")

    # UI im Einstellungen-Block
    frame_settings.columnconfigure(0, weight=0)
    frame_settings.columnconfigure(1, weight=1)
    frame_settings.columnconfigure(2, weight=0)

    ttk.Label(frame_settings, text="Output-Format", font=("Segoe UI", 10, "bold")).grid(
        row=0, column=0, sticky="w", padx=10, pady=(10, 6)
    )
    frame_video_mode = ttk.Frame(frame_settings)
    frame_video_mode.grid(row=0, column=1, columnspan=2, sticky="w", padx=10, pady=(10, 6))

    def on_video_mode_changed() -> None:
        mode = _normalize_video_mode(video_mode_var.get())
        try:
            video_mode_var.set(mode)
        except Exception:
            pass
        try:
            app_model.video_mode = mode
        except Exception:
            pass
        try:
            _save_layout_to_ui_last_run()
        except Exception:
            pass

    ttk.Radiobutton(
        frame_video_mode,
        text="Full",
        variable=video_mode_var,
        value="full",
        command=on_video_mode_changed,
    ).grid(row=0, column=0, sticky="w", padx=(0, 8))
    ttk.Radiobutton(
        frame_video_mode,
        text="Cut",
        variable=video_mode_var,
        value="cut",
        command=on_video_mode_changed,
    ).grid(row=0, column=1, sticky="w")

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
    try:
        app_model.layout_config = _load_layout_config_from_ui_last_run()
    except Exception:
        pass
    try:
        loaded_video_state = _load_video_state_from_ui_last_run()
        if isinstance(loaded_video_state, dict):
            app_model.video_mode = _normalize_video_mode(loaded_video_state.get("video_mode", "full"))
            app_model.video_before_brake = _normalize_video_cut_seconds(
                loaded_video_state.get("video_before_brake", VIDEO_CUT_DEFAULTS["video_before_brake"]),
                VIDEO_CUT_DEFAULTS["video_before_brake"],
            )
            app_model.video_after_full_throttle = _normalize_video_cut_seconds(
                loaded_video_state.get("video_after_full_throttle", VIDEO_CUT_DEFAULTS["video_after_full_throttle"]),
                VIDEO_CUT_DEFAULTS["video_after_full_throttle"],
            )
            app_model.video_minimum_between_two_curves = _normalize_video_cut_seconds(
                loaded_video_state.get("video_minimum_between_two_curves", VIDEO_CUT_DEFAULTS["video_minimum_between_two_curves"]),
                VIDEO_CUT_DEFAULTS["video_minimum_between_two_curves"],
            )
    except Exception:
        pass
    try:
        app_model.video_mode = _normalize_video_mode(getattr(app_model, "video_mode", "full"))
    except Exception:
        app_model.video_mode = "full"
    try:
        app_model.video_before_brake = _normalize_video_cut_seconds(
            getattr(app_model, "video_before_brake", VIDEO_CUT_DEFAULTS["video_before_brake"]),
            VIDEO_CUT_DEFAULTS["video_before_brake"],
        )
    except Exception:
        app_model.video_before_brake = VIDEO_CUT_DEFAULTS["video_before_brake"]
    try:
        app_model.video_after_full_throttle = _normalize_video_cut_seconds(
            getattr(app_model, "video_after_full_throttle", VIDEO_CUT_DEFAULTS["video_after_full_throttle"]),
            VIDEO_CUT_DEFAULTS["video_after_full_throttle"],
        )
    except Exception:
        app_model.video_after_full_throttle = VIDEO_CUT_DEFAULTS["video_after_full_throttle"]
    try:
        app_model.video_minimum_between_two_curves = _normalize_video_cut_seconds(
            getattr(app_model, "video_minimum_between_two_curves", VIDEO_CUT_DEFAULTS["video_minimum_between_two_curves"]),
            VIDEO_CUT_DEFAULTS["video_minimum_between_two_curves"],
        )
    except Exception:
        app_model.video_minimum_between_two_curves = VIDEO_CUT_DEFAULTS["video_minimum_between_two_curves"]
    try:
        video_mode_var.set(_normalize_video_mode(getattr(app_model, "video_mode", "full")))
    except Exception:
        pass
    hud_free_mode_var = tk.BooleanVar(value=False)
    hud_bg_alpha_var = tk.DoubleVar(value=255.0)
    hud_frame_orientation_var = tk.StringVar(value="vertical")
    hud_frame_anchor_var = tk.StringVar(value="center")
    video_layout_var = tk.StringVar(value="LR")
    video_scale_pct_var = tk.IntVar(value=100)
    video_shift_x_var = tk.IntVar(value=0)
    video_shift_y_var = tk.IntVar(value=0)
    video_mirror_shift_x_var = tk.BooleanVar(value=False)
    video_mirror_shift_y_var = tk.BooleanVar(value=False)
    video_transform_var_syncing = False

    def _sync_hud_mode_var_from_model() -> None:
        try:
            mode = str(_layout_cfg().hud_mode or "frame").strip().lower()
        except Exception:
            mode = "frame"
        try:
            hud_free_mode_var.set(mode == "free")
        except Exception:
            pass
        try:
            _update_hud_mode_visibility()
        except Exception:
            pass

    def _sync_hud_bg_alpha_var_from_model() -> None:
        try:
            alpha = _coerce_hud_bg_alpha(_layout_cfg().hud_free.bg_alpha)
        except Exception:
            alpha = 255
        try:
            hud_bg_alpha_var.set(float(alpha))
        except Exception:
            pass

    def _norm_hud_frame_values(orientation_raw: object, anchor_raw: object) -> tuple[str, str]:
        try:
            orientation = str(orientation_raw or "vertical").strip().lower()
        except Exception:
            orientation = "vertical"
        if orientation not in ("vertical", "horizontal"):
            orientation = "vertical"

        try:
            anchor = str(anchor_raw or "").strip().lower()
        except Exception:
            anchor = ""
        if orientation == "vertical":
            if anchor not in ("left", "center", "right"):
                anchor = "center"
        else:
            if anchor not in ("top", "center", "bottom", "top_bottom"):
                anchor = "bottom"
        return orientation, anchor

    def _sync_hud_frame_vars_from_model() -> None:
        cfg = _layout_cfg()
        orientation, anchor = _norm_hud_frame_values(
            getattr(cfg.hud_frame, "orientation", "vertical"),
            getattr(cfg.hud_frame, "anchor", "center"),
        )
        try:
            hud_frame_orientation_var.set(str(orientation))
            hud_frame_anchor_var.set(str(anchor))
        except Exception:
            pass
        cfg.hud_frame.orientation = str(orientation)
        cfg.hud_frame.anchor = str(anchor)
        try:
            _update_hud_mode_visibility()
        except Exception:
            pass

    def _save_layout_to_ui_last_run() -> None:
        cfg = _layout_cfg()
        try:
            payload: dict = {}
            if ui_last_run_file.exists():
                old_data = json.loads(ui_last_run_file.read_text(encoding="utf-8"))
                if isinstance(old_data, dict):
                    payload = old_data
            migrate_ui_last_run_contract_dict(payload)
            payload.update(cfg.to_dict())
            mode = _normalize_video_mode(getattr(app_model, "video_mode", "full"))
            before_s = _normalize_video_cut_seconds(
                getattr(app_model, "video_before_brake", VIDEO_CUT_DEFAULTS["video_before_brake"]),
                VIDEO_CUT_DEFAULTS["video_before_brake"],
            )
            after_s = _normalize_video_cut_seconds(
                getattr(app_model, "video_after_full_throttle", VIDEO_CUT_DEFAULTS["video_after_full_throttle"]),
                VIDEO_CUT_DEFAULTS["video_after_full_throttle"],
            )
            between_s = _normalize_video_cut_seconds(
                getattr(app_model, "video_minimum_between_two_curves", VIDEO_CUT_DEFAULTS["video_minimum_between_two_curves"]),
                VIDEO_CUT_DEFAULTS["video_minimum_between_two_curves"],
            )
            app_model.video_mode = str(mode)
            app_model.video_before_brake = float(before_s)
            app_model.video_after_full_throttle = float(after_s)
            app_model.video_minimum_between_two_curves = float(between_s)
            payload["video_mode"] = str(mode)
            payload["video_before_brake"] = float(before_s)
            payload["video_after_full_throttle"] = float(after_s)
            payload["video_minimum_between_two_curves"] = float(between_s)
            ui_last_run_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _sync_video_layout_var_from_model() -> None:
        cfg = _layout_cfg()
        layout_value = _normalize_video_layout(getattr(cfg, "video_layout", "LR"))
        cfg.video_layout = str(layout_value)
        try:
            video_layout_var.set(str(layout_value))
        except Exception:
            pass

    def _sync_video_mode_var_from_model() -> None:
        mode = _normalize_video_mode(getattr(app_model, "video_mode", "full"))
        try:
            app_model.video_mode = mode
        except Exception:
            pass
        try:
            video_mode_var.set(mode)
        except Exception:
            pass

    def _sync_video_cut_values_from_model() -> None:
        try:
            app_model.video_before_brake = _normalize_video_cut_seconds(
                getattr(app_model, "video_before_brake", VIDEO_CUT_DEFAULTS["video_before_brake"]),
                VIDEO_CUT_DEFAULTS["video_before_brake"],
            )
        except Exception:
            app_model.video_before_brake = VIDEO_CUT_DEFAULTS["video_before_brake"]
        try:
            app_model.video_after_full_throttle = _normalize_video_cut_seconds(
                getattr(app_model, "video_after_full_throttle", VIDEO_CUT_DEFAULTS["video_after_full_throttle"]),
                VIDEO_CUT_DEFAULTS["video_after_full_throttle"],
            )
        except Exception:
            app_model.video_after_full_throttle = VIDEO_CUT_DEFAULTS["video_after_full_throttle"]
        try:
            app_model.video_minimum_between_two_curves = _normalize_video_cut_seconds(
                getattr(app_model, "video_minimum_between_two_curves", VIDEO_CUT_DEFAULTS["video_minimum_between_two_curves"]),
                VIDEO_CUT_DEFAULTS["video_minimum_between_two_curves"],
            )
        except Exception:
            app_model.video_minimum_between_two_curves = VIDEO_CUT_DEFAULTS["video_minimum_between_two_curves"]

    def _apply_video_layout_from_var(refresh_preview: bool = True) -> None:
        cfg = _layout_cfg()
        layout_value = _normalize_video_layout(video_layout_var.get())
        cfg.video_layout = str(layout_value)
        try:
            video_layout_var.set(str(layout_value))
        except Exception:
            pass
        _save_layout_to_ui_last_run()
        try:
            update_png_fit_button_text()
        except Exception:
            pass
        if not refresh_preview:
            return
        try:
            refresh_layout_preview()
        except Exception:
            pass

    hud_fit_trigger_count = 0

    def _run_hud_fit_if_frame_mode(reason: str) -> bool:
        nonlocal hud_fit_trigger_count
        cfg = _layout_cfg()
        try:
            hud_mode_now = str(getattr(cfg, "hud_mode", "frame") or "frame").strip().lower()
        except Exception:
            hud_mode_now = "frame"
        if hud_mode_now != "frame":
            return False
        try:
            hud_fit_to_frame_width()
            hud_fit_trigger_count += 1
            dbg_fit = False
            try:
                dbg_raw = str(os.environ.get("IRVC_DEBUG") or "").strip().lower()
                dbg_fit = _debug_swallowed_enabled() or (dbg_raw in ("1", "true", "yes", "on"))
            except Exception:
                dbg_fit = False
            if dbg_fit:
                print(f"[IRVC_DEBUG] HUD fit trigger #{hud_fit_trigger_count} (reason={reason})")
            return True
        except Exception:
            return False

    def _apply_hud_frame_from_vars(refresh_preview: bool = True, size_transform: str | None = None) -> None:
        cfg = _layout_cfg()
        prev_orientation, prev_anchor = _norm_hud_frame_values(
            getattr(cfg.hud_frame, "orientation", "vertical"),
            getattr(cfg.hud_frame, "anchor", "center"),
        )
        orientation, anchor = _norm_hud_frame_values(
            hud_frame_orientation_var.get(),
            hud_frame_anchor_var.get(),
        )
        frame_changed = (str(orientation) != str(prev_orientation)) or (str(anchor) != str(prev_anchor))
        cfg.hud_frame.orientation = str(orientation)
        cfg.hud_frame.anchor = str(anchor)
        try:
            hud_frame_orientation_var.set(str(orientation))
            hud_frame_anchor_var.set(str(anchor))
        except Exception:
            pass
        try:
            _update_hud_mode_visibility()
        except Exception:
            pass
        if size_transform is not None:
            try:
                old_size_value = int(hud_width_var.get())
            except Exception:
                old_size_value = 0
            new_size_value = int(old_size_value)
            if size_transform == "vertical_to_horizontal":
                new_size_value = max(1, int(round(float(old_size_value) / 4.0)))
            elif size_transform == "horizontal_to_vertical":
                new_size_value = max(1, int(round(float(old_size_value) * 4.0)))
            try:
                hud_width_var.set(int(new_size_value))
            except Exception:
                pass
        if not refresh_preview:
            return
        try:
            refresh_layout_preview()
        except Exception:
            pass
        if not frame_changed:
            return
        fit_reason = f"radio-after-ui: orientation={orientation}, anchor={anchor}"
        if str(anchor) == "top_bottom":
            fit_reason = "radio-after-ui: top_bottom"
        try:
            root.after(0, lambda reason=fit_reason: _run_hud_fit_if_frame_mode(reason))
        except Exception:
            _run_hud_fit_if_frame_mode(fit_reason)

    def _on_hud_frame_orientation_changed(refresh_preview: bool = True) -> None:
        cfg = _layout_cfg()
        prev_orientation, _prev_anchor = _norm_hud_frame_values(
            getattr(cfg.hud_frame, "orientation", "vertical"),
            getattr(cfg.hud_frame, "anchor", "center"),
        )
        new_orientation, _tmp_anchor = _norm_hud_frame_values(
            hud_frame_orientation_var.get(),
            hud_frame_anchor_var.get(),
        )
        size_transform: str | None = None
        if new_orientation != prev_orientation:
            if new_orientation == "horizontal":
                hud_frame_anchor_var.set("bottom")
                size_transform = "vertical_to_horizontal"
            else:
                hud_frame_anchor_var.set("center")
                size_transform = "horizontal_to_vertical"
        _apply_hud_frame_from_vars(refresh_preview=refresh_preview, size_transform=size_transform)

    def _on_hud_frame_anchor_changed(refresh_preview: bool = True) -> None:
        orientation, anchor = _norm_hud_frame_values(
            hud_frame_orientation_var.get(),
            hud_frame_anchor_var.get(),
        )
        dbg_fit = False
        try:
            dbg_raw = str(os.environ.get("IRVC_DEBUG") or "").strip().lower()
            dbg_fit = _debug_swallowed_enabled() or (dbg_raw in ("1", "true", "yes", "on"))
        except Exception:
            dbg_fit = False
        if dbg_fit and str(anchor) == "top_bottom":
            print(f"[IRVC_DEBUG] HUD anchor radio set (orientation={orientation}, anchor={anchor})")
        _apply_hud_frame_from_vars(refresh_preview=refresh_preview)

    def _coerce_video_scale_pct(raw: object) -> int:
        try:
            v = int(round(float(raw)))
        except Exception:
            v = 100
        if v < 10:
            v = 10
        if v > 300:
            v = 300
        return int(v)

    def _coerce_video_shift_px(raw: object) -> int:
        try:
            v = int(round(float(raw)))
        except Exception:
            v = 0
        if v < -2000:
            v = -2000
        if v > 2000:
            v = 2000
        return int(v)

    def _sync_video_transform_vars_from_model() -> None:
        nonlocal video_transform_var_syncing
        cfg = _layout_cfg()
        vt = cfg.video_transform
        scale_pct = _coerce_video_scale_pct(getattr(vt, "scale_pct", 100))
        shift_x_px = _coerce_video_shift_px(getattr(vt, "shift_x_px", 0))
        shift_y_px = _coerce_video_shift_px(getattr(vt, "shift_y_px", 0))
        mirror_shift_x = bool(getattr(vt, "mirror_shift_x", False))
        mirror_shift_y = bool(getattr(vt, "mirror_shift_y", False))
        vt.scale_pct = int(scale_pct)
        vt.shift_x_px = int(shift_x_px)
        vt.shift_y_px = int(shift_y_px)
        vt.mirror_shift_x = bool(mirror_shift_x)
        vt.mirror_shift_y = bool(mirror_shift_y)
        video_transform_var_syncing = True
        try:
            video_scale_pct_var.set(int(scale_pct))
            video_shift_x_var.set(int(shift_x_px))
            video_shift_y_var.set(int(shift_y_px))
            video_mirror_shift_x_var.set(bool(mirror_shift_x))
            video_mirror_shift_y_var.set(bool(mirror_shift_y))
        finally:
            video_transform_var_syncing = False

    def _apply_hud_mode_from_var(refresh_preview: bool = True) -> None:
        cfg = _layout_cfg()
        cfg.hud_mode = "free" if bool(hud_free_mode_var.get()) else "frame"
        if str(cfg.hud_mode) == "free":
            try:
                _seed_free_boxes_from_legacy_if_missing()
            except Exception:
                pass
        try:
            _update_hud_mode_visibility()
        except Exception:
            pass
        if not refresh_preview:
            return
        if _run_hud_fit_if_frame_mode("hud_mode_changed"):
            return
        try:
            refresh_layout_preview()
        except Exception:
            pass

    def _apply_hud_bg_alpha_from_var(refresh_preview: bool = True) -> None:
        cfg = _layout_cfg()
        cfg.hud_free.bg_alpha = _coerce_hud_bg_alpha(hud_bg_alpha_var.get())
        if not refresh_preview:
            return
        try:
            refresh_layout_preview()
        except Exception:
            pass

    def _apply_video_transform_from_vars(refresh_preview: bool = True) -> None:
        nonlocal video_transform_var_syncing
        if video_transform_var_syncing:
            return
        cfg = _layout_cfg()
        vt = cfg.video_transform
        try:
            raw_scale = video_scale_pct_var.get()
        except Exception:
            raw_scale = 100
        try:
            raw_shift_x = video_shift_x_var.get()
        except Exception:
            raw_shift_x = 0
        try:
            raw_shift_y = video_shift_y_var.get()
        except Exception:
            raw_shift_y = 0
        try:
            raw_mirror_shift_x = video_mirror_shift_x_var.get()
        except Exception:
            raw_mirror_shift_x = False
        try:
            raw_mirror_shift_y = video_mirror_shift_y_var.get()
        except Exception:
            raw_mirror_shift_y = False
        scale_pct = _coerce_video_scale_pct(raw_scale)
        shift_x_px = _coerce_video_shift_px(raw_shift_x)
        shift_y_px = _coerce_video_shift_px(raw_shift_y)
        mirror_shift_x = bool(raw_mirror_shift_x)
        mirror_shift_y = bool(raw_mirror_shift_y)
        video_transform_var_syncing = True
        try:
            video_scale_pct_var.set(int(scale_pct))
            video_shift_x_var.set(int(shift_x_px))
            video_shift_y_var.set(int(shift_y_px))
            video_mirror_shift_x_var.set(bool(mirror_shift_x))
            video_mirror_shift_y_var.set(bool(mirror_shift_y))
        finally:
            video_transform_var_syncing = False
        vt.scale_pct = int(scale_pct)
        vt.shift_x_px = int(shift_x_px)
        vt.shift_y_px = int(shift_y_px)
        vt.mirror_shift_x = bool(mirror_shift_x)
        vt.mirror_shift_y = bool(mirror_shift_y)
        if not refresh_preview:
            return
        try:
            refresh_layout_preview()
        except Exception:
            pass

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
            layout_config=app_model.layout_config if isinstance(app_model.layout_config, LayoutConfig) else LayoutConfig(),
            video_mode=_normalize_video_mode(video_mode_var.get()),
            video_before_brake=_normalize_video_cut_seconds(
                getattr(app_model, "video_before_brake", VIDEO_CUT_DEFAULTS["video_before_brake"]),
                VIDEO_CUT_DEFAULTS["video_before_brake"],
            ),
            video_after_full_throttle=_normalize_video_cut_seconds(
                getattr(app_model, "video_after_full_throttle", VIDEO_CUT_DEFAULTS["video_after_full_throttle"]),
                VIDEO_CUT_DEFAULTS["video_after_full_throttle"],
            ),
            video_minimum_between_two_curves=_normalize_video_cut_seconds(
                getattr(app_model, "video_minimum_between_two_curves", VIDEO_CUT_DEFAULTS["video_minimum_between_two_curves"]),
                VIDEO_CUT_DEFAULTS["video_minimum_between_two_curves"],
            ),
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
            video_mode_var.set(_normalize_video_mode(getattr(model, "video_mode", "full")))
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
        _sync_hud_mode_var_from_model()
        _sync_hud_frame_vars_from_model()
        _sync_hud_bg_alpha_var_from_model()
        _sync_video_mode_var_from_model()
        _sync_video_cut_values_from_model()
        _sync_video_layout_var_from_model()
        _sync_video_transform_vars_from_model()

    def set_app_model(model: AppModel) -> None:
        nonlocal app_model
        app_model = model
        try:
            app_model.video_mode = _normalize_video_mode(getattr(app_model, "video_mode", video_mode_var.get()))
        except Exception:
            app_model.video_mode = _normalize_video_mode(video_mode_var.get())
        _sync_video_cut_values_from_model()

    def profile_model_from_ui_state(
        videos_names: list[str],
        csv_names: list[str],
        starts: dict[str, int],
        ends: dict[str, int],
    ) -> Profile:
        m = model_from_ui_state()
        return Profile(
            version=int(PROFILE_SCHEMA_VERSION),
            videos=videos_names,
            csvs=csv_names,
            startframes=starts,
            endframes=ends,
            output=m.output,
            hud_layout_data=m.hud_layout.hud_layout_data,
            png_view_data=m.png_view.png_view_data,
            layout_config=m.layout_config,
            video_mode=_normalize_video_mode(m.video_mode),
            video_before_brake=_normalize_video_cut_seconds(
                m.video_before_brake,
                VIDEO_CUT_DEFAULTS["video_before_brake"],
            ),
            video_after_full_throttle=_normalize_video_cut_seconds(
                m.video_after_full_throttle,
                VIDEO_CUT_DEFAULTS["video_after_full_throttle"],
            ),
            video_minimum_between_two_curves=_normalize_video_cut_seconds(
                m.video_minimum_between_two_curves,
                VIDEO_CUT_DEFAULTS["video_minimum_between_two_curves"],
            ),
        )

    apply_model_to_ui_state(model_from_ui_state())

    lbl_hud_size = ttk.Label(frame_settings, text="HUD-width (px):")
    spn_hud = ttk.Spinbox(frame_settings, from_=0, to=10000, width=10, textvariable=hud_width_var)

    lbl_out_fps = ttk.Label(frame_settings, text="FPS (vom Video): –")
    lbl_out_fps.grid(row=6, column=0, columnspan=3, sticky="w", padx=10, pady=(6, 6))

    controller: Controller | None = None

    def on_hud_width_change(_event=None) -> None:
        if controller is None:
            return
        controller.on_hud_width_change(_event)

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
        - und verteilt sie je nach Frame-Layout deterministisch auf den Zielbereich
        """
        nonlocal hud_boxes

        def _hud_weight_for_box(box: dict) -> float:
            hud_key = ""
            try:
                hud_key = str(box.get("type") or "")
            except Exception:
                hud_key = ""
            return float(vertical_fit_weight_for_hud_key(hud_key))

        def _split_weighted(total: int, items: list[dict]) -> list[int]:
            weights: list[float] = []
            for b in items:
                w = float(_hud_weight_for_box(b))
                if w <= 0.0:
                    w = 1.0
                weights.append(w)
            return split_weighted_lengths(int(total), weights)

        def _active_hud_boxes_in_order(boxes: list[dict], enabled: set[str]) -> list[dict]:
            out: list[dict] = []
            for b in boxes:
                if not isinstance(b, dict):
                    continue
                t = str(b.get("type") or "")
                if t in enabled:
                    out.append(b)
            return out

        def _norm_hud_frame(cfg: LayoutConfig) -> tuple[str, str]:
            orientation = "vertical"
            anchor = "center"
            try:
                orientation = str(cfg.hud_frame.orientation or "vertical").strip().lower()
            except Exception:
                orientation = "vertical"
            if orientation not in ("vertical", "horizontal"):
                orientation = "vertical"

            default_anchor = "center" if orientation == "vertical" else "bottom"
            try:
                anchor = str(cfg.hud_frame.anchor or default_anchor).strip().lower()
            except Exception:
                anchor = default_anchor
            if orientation == "vertical":
                if anchor not in ("left", "center", "right"):
                    anchor = "center"
            else:
                if anchor not in ("top", "center", "bottom", "top_bottom"):
                    anchor = "bottom"
            return orientation, anchor

        def _to_rect_xywh(rect: object) -> tuple[int, int, int, int] | None:
            try:
                x = int(getattr(rect, "x"))
                y = int(getattr(rect, "y"))
                w = int(getattr(rect, "w"))
                h = int(getattr(rect, "h"))
                if w <= 0 or h <= 0:
                    return None
                return x, y, w, h
            except Exception:
                return None

        def _fit_legacy(enabled: set[str], hud_x0: int, fit_hud_w: int, fit_out_h: int) -> None:
            for b in hud_boxes:
                try:
                    t = str(b.get("type") or "")
                except Exception:
                    continue
                if t not in enabled:
                    continue

                try:
                    y = int(b.get("y", 0))
                    h = int(b.get("h", 100))
                except Exception:
                    y = 0
                    h = 100

                h = max(30, int(h))
                y = clamp(int(y), 0, max(0, int(fit_out_h) - h))

                b["x"] = int(hud_x0)
                b["y"] = int(y)
                b["w"] = int(fit_hud_w)
                b["h"] = int(h)

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

        hud_x0_legacy = int(side_w)

        en = enabled_types()
        active_boxes = _active_hud_boxes_in_order(hud_boxes, en)
        if not active_boxes:
            try:
                save_current_boxes()
            except Exception:
                pass
            try:
                refresh_layout_preview()
            except Exception:
                pass
            return

        layout_cfg = app_model.layout_config if isinstance(app_model.layout_config, LayoutConfig) else LayoutConfig()
        orientation, anchor = _norm_hud_frame(layout_cfg)
        hud_mode = str(getattr(layout_cfg, "hud_mode", "frame") or "frame").strip().lower()
        if hud_mode not in ("frame", "free"):
            hud_mode = "frame"

        try:
            geom = build_output_geometry_for_size(
                out_w=int(out_w),
                out_h=int(out_h),
                hud_width_px=int(hud_w),
                layout_config=layout_cfg,
            )
        except Exception:
            geom = build_output_geometry_for_size(
                out_w=int(out_w),
                out_h=int(out_h),
                hud_width_px=int(hud_w),
                layout_config=None,
            )

        frame_rects = tuple(getattr(geom, "hud_rects", ()) or ())
        if hud_mode != "frame" or not frame_rects:
            _fit_legacy(en, hud_x0_legacy, hud_w, out_h)
        elif orientation == "vertical":
            r = _to_rect_xywh(frame_rects[0])
            if r is None:
                _fit_legacy(en, hud_x0_legacy, hud_w, out_h)
            else:
                fx, fy, fw, fh = r
                heights = _split_weighted(fh, active_boxes)
                cur_y = int(fy)
                for i, b in enumerate(active_boxes):
                    h_i = int(heights[i]) if i < len(heights) else 0
                    b["x"] = int(fx)
                    b["y"] = int(cur_y)
                    b["w"] = int(fw)
                    b["h"] = int(h_i)
                    cur_y += h_i
        else:
            rect_items: list[tuple[int, int, int, int]] = []
            for rr in frame_rects:
                ri = _to_rect_xywh(rr)
                if ri is not None:
                    rect_items.append(ri)
            rect_items.sort(key=lambda it: (int(it[1]), int(it[0])))

            if anchor == "top_bottom" and len(rect_items) < 2:
                _fit_legacy(en, hud_x0_legacy, hud_w, out_h)
            elif not rect_items:
                _fit_legacy(en, hud_x0_legacy, hud_w, out_h)
            else:
                placed = layout_horizontal_frame_hud_boxes(
                    active_boxes=active_boxes,
                    frame_rects=tuple(Rect(int(x), int(y), int(w), int(h)) for x, y, w, h in rect_items),
                    anchor=str(anchor),
                )
                if not placed:
                    _fit_legacy(en, hud_x0_legacy, hud_w, out_h)
                else:
                    for b, rr in placed:
                        b["x"] = int(rr.x)
                        b["y"] = int(rr.y)
                        b["w"] = int(rr.w)
                        b["h"] = int(rr.h)

        def _sum_unique_column_width(items: list[dict]) -> int:
            seen: set[tuple[int, int]] = set()
            total = 0
            for b in items:
                try:
                    bx = int(b.get("x", 0))
                    bw = int(b.get("w", 0))
                except Exception:
                    continue
                key = (int(bx), int(max(0, bw)))
                if key in seen:
                    continue
                seen.add(key)
                total += int(max(0, bw))
            return int(total)

        dbg_fit = False
        try:
            dbg_raw = str(os.environ.get("IRVC_DEBUG") or "").strip().lower()
            dbg_fit = _debug_swallowed_enabled() or (dbg_raw in ("1", "true", "yes", "on"))
        except Exception:
            dbg_fit = False
        if dbg_fit and hud_mode == "frame" and frame_rects:
            rect_items_dbg: list[tuple[int, int, int, int]] = []
            for rr in frame_rects:
                ri = _to_rect_xywh(rr)
                if ri is not None:
                    rect_items_dbg.append(ri)

            for b in active_boxes:
                try:
                    bx = int(b.get("x", 0))
                    by = int(b.get("y", 0))
                    bw = int(b.get("w", 0))
                    bh = int(b.get("h", 0))
                except Exception:
                    raise AssertionError("HUD-Fit: Box-Koordinaten unlesbar.")
                ok_in_any = False
                for rx, ry, rw, rh in rect_items_dbg:
                    if bx >= rx and by >= ry and (bx + bw) <= (rx + rw) and (by + bh) <= (ry + rh):
                        ok_in_any = True
                        break
                if not ok_in_any:
                    raise AssertionError(f"HUD-Fit: Box ausserhalb Frame ({b.get('type', '?')}).")

            if orientation == "vertical" and rect_items_dbg:
                _, _, _, rh = rect_items_dbg[0]
                sum_h = sum(max(0, int(b.get("h", 0))) for b in active_boxes)
                if int(sum_h) != int(rh):
                    raise AssertionError(f"HUD-Fit: Height-Summe stimmt nicht ({sum_h} != {rh}).")
            elif orientation == "horizontal":
                rect_items_dbg.sort(key=lambda it: (int(it[1]), int(it[0])))
                if anchor == "top_bottom" and len(rect_items_dbg) >= 2:
                    top_items, bottom_items = split_horizontal_top_bottom_rows(active_boxes)
                    top_w = _sum_unique_column_width(top_items)
                    if int(top_w) != int(rect_items_dbg[0][2]):
                        raise AssertionError(f"HUD-Fit: Top-Width-Summe stimmt nicht ({top_w} != {rect_items_dbg[0][2]}).")
                    if bottom_items:
                        bottom_w = _sum_unique_column_width(bottom_items)
                        if int(bottom_w) != int(rect_items_dbg[1][2]):
                            raise AssertionError(
                                f"HUD-Fit: Bottom-Width-Summe stimmt nicht ({bottom_w} != {rect_items_dbg[1][2]})."
                            )
                elif rect_items_dbg:
                    sum_w = _sum_unique_column_width(active_boxes)
                    if int(sum_w) != int(rect_items_dbg[0][2]):
                        raise AssertionError(f"HUD-Fit: Width-Summe stimmt nicht ({sum_w} != {rect_items_dbg[0][2]}).")

        # Persistieren + neu zeichnen
        try:
            save_current_boxes()
        except Exception:
            pass

        try:
            refresh_layout_preview()
        except Exception:
            pass

    btn_hud_fit = ttk.Button(frame_settings, text="HUDs auf Rahmenbreite", command=hud_fit_to_frame_width)

    hud_mode_row = 10 + len(HUD_TYPES)
    ttk.Separator(frame_settings, orient="horizontal").grid(
        row=hud_mode_row, column=0, columnspan=3, sticky="ew", padx=10, pady=(8, 6)
    )
    ttk.Label(frame_settings, text="HUD mode", font=("Segoe UI", 10, "bold")).grid(
        row=hud_mode_row + 1, column=0, sticky="w", padx=10, pady=(0, 2)
    )
    cb_hud_free = ttk.Checkbutton(
        frame_settings,
        text="Free-Mode",
        style="Switch.TCheckbutton",
        variable=hud_free_mode_var,
        command=lambda: _apply_hud_mode_from_var(refresh_preview=True),
    )
    cb_hud_free.grid(row=hud_mode_row + 1, column=1, columnspan=2, sticky="w", padx=10, pady=(0, 4))
    frm_hud_frame_controls = ttk.Frame(frame_settings)
    frm_hud_frame_controls.grid(row=hud_mode_row + 2, column=0, columnspan=3, sticky="ew", padx=10, pady=(2, 4))
    frm_hud_frame_controls.columnconfigure(0, weight=0)
    frm_hud_frame_controls.columnconfigure(1, weight=1)

    ttk.Label(frm_hud_frame_controls, text="Alignment:").grid(row=0, column=0, sticky="w", pady=(0, 2))
    frm_orientation = ttk.Frame(frm_hud_frame_controls)
    frm_orientation.grid(row=0, column=1, sticky="w", pady=(0, 2))
    ttk.Radiobutton(
        frm_orientation,
        text="Vertical",
        value="vertical",
        variable=hud_frame_orientation_var,
        command=lambda: _on_hud_frame_orientation_changed(refresh_preview=True),
    ).grid(row=0, column=0, sticky="w", padx=(0, 8))
    ttk.Radiobutton(
        frm_orientation,
        text="Horizontal",
        value="horizontal",
        variable=hud_frame_orientation_var,
        command=lambda: _on_hud_frame_orientation_changed(refresh_preview=True),
    ).grid(row=0, column=1, sticky="w")

    frm_anchor_vertical = ttk.Frame(frm_hud_frame_controls)
    frm_anchor_vertical.grid(row=1, column=1, sticky="w", pady=(0, 2))
    ttk.Radiobutton(
        frm_anchor_vertical,
        text="Left",
        value="left",
        variable=hud_frame_anchor_var,
        command=lambda: _on_hud_frame_anchor_changed(refresh_preview=True),
    ).grid(row=0, column=0, sticky="w", padx=(0, 8))
    ttk.Radiobutton(
        frm_anchor_vertical,
        text="Centre",
        value="center",
        variable=hud_frame_anchor_var,
        command=lambda: _on_hud_frame_anchor_changed(refresh_preview=True),
    ).grid(row=0, column=1, sticky="w", padx=(0, 8))
    ttk.Radiobutton(
        frm_anchor_vertical,
        text="Right",
        value="right",
        variable=hud_frame_anchor_var,
        command=lambda: _on_hud_frame_anchor_changed(refresh_preview=True),
    ).grid(row=0, column=2, sticky="w")

    frm_anchor_horizontal = ttk.Frame(frm_hud_frame_controls)
    frm_anchor_horizontal.grid(row=1, column=1, sticky="w", pady=(0, 2))
    ttk.Radiobutton(
        frm_anchor_horizontal,
        text="Top",
        value="top",
        variable=hud_frame_anchor_var,
        command=lambda: _on_hud_frame_anchor_changed(refresh_preview=True),
    ).grid(row=0, column=0, sticky="w", padx=(0, 8))
    ttk.Radiobutton(
        frm_anchor_horizontal,
        text="Middle",
        value="center",
        variable=hud_frame_anchor_var,
        command=lambda: _on_hud_frame_anchor_changed(refresh_preview=True),
    ).grid(row=0, column=1, sticky="w", padx=(0, 8))
    ttk.Radiobutton(
        frm_anchor_horizontal,
        text="Bottom",
        value="bottom",
        variable=hud_frame_anchor_var,
        command=lambda: _on_hud_frame_anchor_changed(refresh_preview=True),
    ).grid(row=0, column=2, sticky="w", padx=(0, 8))
    ttk.Radiobutton(
        frm_anchor_horizontal,
        text="Top & Bottom",
        value="top_bottom",
        variable=hud_frame_anchor_var,
        command=lambda: _on_hud_frame_anchor_changed(refresh_preview=True),
    ).grid(row=0, column=3, sticky="w")

    lbl_hud_size.grid(row=hud_mode_row + 3, column=0, sticky="w", padx=10, pady=(0, 2))
    spn_hud.grid(row=hud_mode_row + 3, column=1, sticky="w", padx=10, pady=(0, 2))
    btn_hud_fit.grid(row=hud_mode_row + 3, column=2, sticky="w", padx=(10, 10), pady=(0, 2))

    lbl_hud_bg_alpha = ttk.Label(frame_settings, text="HUD Background Alpha:")
    lbl_hud_bg_alpha.grid(
        row=hud_mode_row + 4, column=0, sticky="w", padx=10, pady=(0, 2)
    )
    sld_hud_bg_alpha = ttk.Scale(
        frame_settings,
        from_=0,
        to=255,
        orient="horizontal",
        variable=hud_bg_alpha_var,
        command=lambda _v: _apply_hud_bg_alpha_from_var(refresh_preview=True),
    )
    sld_hud_bg_alpha.grid(row=hud_mode_row + 4, column=1, columnspan=2, sticky="ew", padx=10, pady=(0, 4))

    def _update_hud_mode_visibility() -> None:
        is_free = bool(hud_free_mode_var.get())
        orientation, anchor = _norm_hud_frame_values(
            hud_frame_orientation_var.get(),
            hud_frame_anchor_var.get(),
        )
        try:
            hud_frame_orientation_var.set(str(orientation))
            hud_frame_anchor_var.set(str(anchor))
        except Exception:
            pass
        if orientation == "vertical":
            lbl_hud_size.config(text="HUD-width (px):")
            frm_anchor_horizontal.grid_remove()
            frm_anchor_vertical.grid()
        else:
            lbl_hud_size.config(text="HUD-height (px):")
            frm_anchor_vertical.grid_remove()
            frm_anchor_horizontal.grid()
        if is_free:
            lbl_hud_size.grid_remove()
            spn_hud.grid_remove()
            btn_hud_fit.grid_remove()
            frm_hud_frame_controls.grid_remove()
            lbl_hud_bg_alpha.grid()
            sld_hud_bg_alpha.grid()
        else:
            lbl_hud_size.grid()
            spn_hud.grid()
            btn_hud_fit.grid()
            frm_hud_frame_controls.grid()
            lbl_hud_bg_alpha.grid_remove()
            sld_hud_bg_alpha.grid_remove()

    _update_hud_mode_visibility()

    video_align_row = hud_mode_row + 5
    ttk.Separator(frame_settings, orient="horizontal").grid(
        row=video_align_row, column=0, columnspan=3, sticky="ew", padx=10, pady=(8, 6)
    )
    ttk.Label(frame_settings, text="Video alignment", font=("Segoe UI", 10, "bold")).grid(
        row=video_align_row + 1, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 2)
    )
    frm_video_alignment = ttk.Frame(frame_settings)
    frm_video_alignment.grid(row=video_align_row + 2, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 4))
    ttk.Radiobutton(
        frm_video_alignment,
        text="Left / Right",
        value="LR",
        variable=video_layout_var,
        command=lambda: _apply_video_layout_from_var(refresh_preview=True),
    ).grid(row=0, column=0, sticky="w", padx=(0, 10))
    ttk.Radiobutton(
        frm_video_alignment,
        text="Top / Bottom",
        value="TB",
        variable=video_layout_var,
        command=lambda: _apply_video_layout_from_var(refresh_preview=True),
    ).grid(row=0, column=1, sticky="w")

    video_place_row = video_align_row + 3
    ttk.Separator(frame_settings, orient="horizontal").grid(
        row=video_place_row, column=0, columnspan=3, sticky="ew", padx=10, pady=(8, 6)
    )
    ttk.Label(frame_settings, text="Video-Placement", font=("Segoe UI", 10, "bold")).grid(
        row=video_place_row + 1, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 6)
    )

    ttk.Label(frame_settings, text="Scale (%):").grid(row=video_place_row + 2, column=0, sticky="w", padx=10, pady=2)
    sld_video_scale = tk.Scale(
        frame_settings,
        from_=10,
        to=300,
        resolution=1,
        orient="horizontal",
        showvalue=False,
        variable=video_scale_pct_var,
    )
    sld_video_scale.grid(row=video_place_row + 2, column=1, sticky="ew", padx=10, pady=2)
    spn_video_scale = ttk.Spinbox(
        frame_settings,
        from_=10,
        to=300,
        increment=1,
        width=8,
        textvariable=video_scale_pct_var,
        command=lambda: _apply_video_transform_from_vars(refresh_preview=True),
    )
    spn_video_scale.grid(row=video_place_row + 2, column=2, sticky="w", padx=10, pady=2)

    ttk.Label(frame_settings, text="Shift X (px):").grid(row=video_place_row + 3, column=0, sticky="w", padx=10, pady=2)
    spn_video_shift_x = ttk.Spinbox(
        frame_settings,
        from_=-2000,
        to=2000,
        increment=10,
        width=8,
        textvariable=video_shift_x_var,
        command=lambda: _apply_video_transform_from_vars(refresh_preview=True),
    )
    spn_video_shift_x.grid(row=video_place_row + 3, column=1, sticky="w", padx=10, pady=2)
    chk_video_mirror_shift_x = ttk.Checkbutton(
        frame_settings,
        text="mirror",
        variable=video_mirror_shift_x_var,
        command=lambda: _apply_video_transform_from_vars(refresh_preview=True),
    )
    chk_video_mirror_shift_x.grid(row=video_place_row + 3, column=2, sticky="w", padx=10, pady=2)

    ttk.Label(frame_settings, text="Shift Y (px):").grid(row=video_place_row + 4, column=0, sticky="w", padx=10, pady=2)
    spn_video_shift_y = ttk.Spinbox(
        frame_settings,
        from_=-2000,
        to=2000,
        increment=10,
        width=8,
        textvariable=video_shift_y_var,
        command=lambda: _apply_video_transform_from_vars(refresh_preview=True),
    )
    spn_video_shift_y.grid(row=video_place_row + 4, column=1, sticky="w", padx=10, pady=2)
    chk_video_mirror_shift_y = ttk.Checkbutton(
        frame_settings,
        text="mirror",
        variable=video_mirror_shift_y_var,
        command=lambda: _apply_video_transform_from_vars(refresh_preview=True),
    )
    chk_video_mirror_shift_y.grid(row=video_place_row + 4, column=2, sticky="w", padx=10, pady=2)

    def reset_video_placement() -> None:
        cfg = _layout_cfg()
        vt = cfg.video_transform
        vt.scale_pct = 100
        vt.shift_x_px = 0
        vt.shift_y_px = 0
        _sync_video_transform_vars_from_model()
        try:
            refresh_layout_preview()
        except Exception:
            pass

    btn_reset_video_placement = ttk.Button(
        frame_settings,
        text="Video-Placement reset",
        command=reset_video_placement,
    )
    btn_reset_video_placement.grid(row=video_place_row + 5, column=0, sticky="w", padx=10, pady=(6, 2))

    for _video_var in (
        video_scale_pct_var,
        video_shift_x_var,
        video_shift_y_var,
        video_mirror_shift_x_var,
        video_mirror_shift_y_var,
    ):
        try:
            _video_var.trace_add("write", lambda *_: _apply_video_transform_from_vars(refresh_preview=True))
        except Exception:
            pass
    spn_video_scale.bind("<Return>", lambda _e: _apply_video_transform_from_vars(refresh_preview=True))
    spn_video_scale.bind("<FocusOut>", lambda _e: _apply_video_transform_from_vars(refresh_preview=True))
    spn_video_shift_x.bind("<Return>", lambda _e: _apply_video_transform_from_vars(refresh_preview=True))
    spn_video_shift_x.bind("<FocusOut>", lambda _e: _apply_video_transform_from_vars(refresh_preview=True))
    spn_video_shift_y.bind("<Return>", lambda _e: _apply_video_transform_from_vars(refresh_preview=True))
    spn_video_shift_y.bind("<FocusOut>", lambda _e: _apply_video_transform_from_vars(refresh_preview=True))


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
                _safe(video_info_inflight.discard, key, label="request_video_info.inflight_discard")
                _safe(refresh_display, label="request_video_info.refresh_display")

            _safe(root.after, 0, apply, label="request_video_info.after")

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            _safe(video_info_inflight.discard, key, label="request_video_info.thread_start_discard")

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
        if controller is None:
            return
        controller.on_output_change(_event)

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

    def get_selected_files() -> tuple[list[Path], list[Path]]:
        return list(videos), list(csvs)

    def set_selected_files(new_videos: list[Path], new_csvs: list[Path]) -> None:
        videos[:] = list(new_videos[:2])
        csvs[:] = list(new_csvs[:2])

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
        
        def _set_app_model(model: AppModel) -> None:
            nonlocal app_model
            app_model = model
            try:
                app_model.video_mode = _normalize_video_mode(video_mode_var.get())
            except Exception:
                app_model.video_mode = "full"
            _sync_video_cut_values_from_model()

        return profile_service.build_profile_dict(
            videos=videos,
            csvs=csvs,
            startframes_by_name=startframes_by_name,
            endframes_by_name=endframes_by_name,
            profile_model_from_ui_state=profile_model_from_ui_state,
            set_app_model=_set_app_model,
        )

    def apply_profile_dict(d: dict) -> None:
        nonlocal videos, csvs, hud_layout_data, png_view_data, last_scan_sig, app_model

        try:
            if isinstance(d, dict):
                migrate_profile_contract_dict(d)
            loaded = Profile.from_dict(d if isinstance(d, dict) else {})
            app_model.layout_config = loaded.layout_config
            app_model.video_mode = _normalize_video_mode(loaded.video_mode)
            app_model.video_before_brake = _normalize_video_cut_seconds(
                loaded.video_before_brake,
                VIDEO_CUT_DEFAULTS["video_before_brake"],
            )
            app_model.video_after_full_throttle = _normalize_video_cut_seconds(
                loaded.video_after_full_throttle,
                VIDEO_CUT_DEFAULTS["video_after_full_throttle"],
            )
            app_model.video_minimum_between_two_curves = _normalize_video_cut_seconds(
                loaded.video_minimum_between_two_curves,
                VIDEO_CUT_DEFAULTS["video_minimum_between_two_curves"],
            )
            _sync_hud_mode_var_from_model()
            _sync_hud_frame_vars_from_model()
            _sync_hud_bg_alpha_var_from_model()
            _sync_video_mode_var_from_model()
            _sync_video_cut_values_from_model()
            _sync_video_layout_var_from_model()
            _sync_video_transform_vars_from_model()
            _save_layout_to_ui_last_run()
        except Exception:
            pass

        def _set_hud_layout_data(data: dict) -> None:
            nonlocal hud_layout_data
            hud_layout_data = data

        def _set_png_view_data(data: dict) -> None:
            nonlocal png_view_data
            png_view_data = data

        def _sync_app_model_from_ui_state() -> None:
            nonlocal app_model
            app_model = model_from_ui_state()

        def _set_videos(data: list[Path]) -> None:
            nonlocal videos
            videos = data

        def _set_csvs(data: list[Path]) -> None:
            nonlocal csvs
            csvs = data

        def _reset_last_scan_sig() -> None:
            nonlocal last_scan_sig
            last_scan_sig = None

        profile_service.apply_profile_dict(
            profile=d,
            set_out_aspect=out_aspect_var.set,
            set_out_quality=out_quality_var.set,
            set_out_preset=out_preset_var.set,
            set_hud_width_px=hud_width_var.set,
            get_out_aspect=out_aspect_var.get,
            get_out_quality=out_quality_var.get,
            get_out_preset=out_preset_var.get,
            get_hud_width_px=get_hud_width_px,
            save_output_format=persistence.save_output_format,
            set_hud_layout_data=_set_hud_layout_data,
            save_hud_layout=persistence.save_hud_layout,
            set_png_view_data=_set_png_view_data,
            save_png_view=persistence.save_png_view,
            sync_app_model_from_ui_state=_sync_app_model_from_ui_state,
            startframes_by_name=startframes_by_name,
            endframes_by_name=endframes_by_name,
            save_startframes=persistence.save_startframes,
            save_endframes=persistence.save_endframes,
            input_video_dir=input_video_dir,
            input_csv_dir=input_csv_dir,
            set_videos=_set_videos,
            set_csvs=_set_csvs,
            reset_last_scan_sig=_reset_last_scan_sig,
            close_preview_video=close_preview_video,
            refresh_display=refresh_display,
            get_preview_mode=preview_mode_var.get,
            png_load_state_for_current=png_load_state_for_current,
            render_png_preview=render_png_preview,
            refresh_layout_preview=refresh_layout_preview,
        )
        try:
            _seed_free_boxes_from_legacy_if_missing()
            _sync_hud_mode_var_from_model()
            _sync_hud_frame_vars_from_model()
            _sync_hud_bg_alpha_var_from_model()
            _sync_video_mode_var_from_model()
            _sync_video_cut_values_from_model()
            _sync_video_layout_var_from_model()
            _sync_video_transform_vars_from_model()
            _save_layout_to_ui_last_run()
        except Exception:
            pass

    def profile_save_dialog() -> None:
        if controller is None:
            return
        controller.on_profile_save()

    def profile_load_dialog() -> None:
        if controller is None:
            return
        controller.on_profile_load()

    def open_file_dialog(*, multiple: bool = False, **kwargs):
        if multiple:
            return filedialog.askopenfilenames(**kwargs)
        return filedialog.askopenfilename(**kwargs)

    def save_file_dialog(**kwargs):
        return filedialog.asksaveasfilename(**kwargs)
            

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
                    png_load_state_for_current()
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
        if controller is None:
            return
        controller.on_select_files()

    btn_select.config(command=on_select_files)
    btn_profile_save.config(command=profile_save_dialog)
    btn_profile_load.config(command=profile_load_dialog)

    # ---- Vorschau ----

    frame_preview.columnconfigure(0, weight=1)
    frame_preview.rowconfigure(0, weight=0)
    frame_preview.rowconfigure(1, weight=1)
    frame_preview.rowconfigure(2, weight=0)

    # Unified Preview
    preview_mode_bar = ttk.Frame(frame_preview)
    preview_mode_bar.grid(row=0, column=0, sticky="ew", padx=10, pady=6)
    preview_mode_bar.columnconfigure(10, weight=1)

    preview_mode_var = tk.StringVar(value="png")

    show_video_rects_var = tk.BooleanVar(value=True)
    show_hud_boxes_var = tk.BooleanVar(value=True)
    show_labels_var = tk.BooleanVar(value=True)

    btn_png_fit = ttk.Button(preview_mode_bar, text="Video auf Rahmenh\u00f6he")
    btn_png_fit.grid(row=0, column=0, sticky="w", padx=(0, 12))
    ttk.Checkbutton(preview_mode_bar, text="Show video rects", variable=show_video_rects_var).grid(
        row=0, column=1, sticky="w", padx=(0, 8)
    )
    ttk.Checkbutton(preview_mode_bar, text="Show HUD boxes", variable=show_hud_boxes_var).grid(
        row=0, column=2, sticky="w", padx=(0, 8)
    )
    ttk.Checkbutton(preview_mode_bar, text="Show labels", variable=show_labels_var).grid(
        row=0, column=3, sticky="w"
    )

    def get_preview_overlay_flags() -> dict[str, bool]:
        return {
            "video_rects": bool(show_video_rects_var.get()),
            "hud_boxes": bool(show_hud_boxes_var.get()),
            "labels": bool(show_labels_var.get()),
        }

    def update_png_fit_button_text() -> None:
        try:
            layout_mode = str(_layout_cfg().video_layout or "LR").strip().upper()
        except Exception:
            layout_mode = "LR"
        if layout_mode == "TB":
            btn_png_fit.config(text="Video auf Rahmenbreite")
        else:
            btn_png_fit.config(text="Video auf Rahmenh\u00f6he")

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

    preview_canvas = tk.Canvas(preview_area, highlightthickness=0)
    preview_canvas.grid(row=0, column=0, sticky="nsew")

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
            layout_config=app_model.layout_config,
        )

    def load_png_view_data() -> dict:
        if isinstance(png_view_data, dict):
            return png_view_data
        return {}

    def save_png_view_data(data: dict) -> None:
        nonlocal png_view_data
        png_view_data = data
        persistence.save_png_view(png_view_data)

    layout_preview_ctrl: LayoutPreviewController | None = None

    def on_preview_geometry(geom: object, x0: int, y0: int, scale: float, out_w: int, out_h: int, hud_w: int) -> None:
        nonlocal hud_boxes, layout_preview_ctrl
        if layout_preview_ctrl is None:
            return
        layout_preview_ctrl.update_layout_state(
            geom=geom,
            out_w=int(out_w),
            out_h=int(out_h),
            hud_w=int(hud_w),
            x0=int(x0),
            y0=int(y0),
            scale=float(scale),
        )
        layout_preview_ctrl.ensure_boxes_in_hud_area(hud_boxes)

    png_preview_ctrl = PngPreviewController(
        canvas=preview_canvas,
        get_preview_area_size=lambda: (preview_area.winfo_width(), preview_area.winfo_height()),
        get_output_format=current_png_output_format,
        is_png_mode=lambda: True,
        get_png_view_key=png_view_key,
        load_png_view_data=load_png_view_data,
        save_png_view_data=save_png_view_data,
        choose_slow_fast_paths=choose_slow_fast_paths,
        get_start_for_video=get_start_for_video,
        read_frame_as_pil=read_frame_as_pil,
        get_hud_boxes=lambda: hud_boxes,
        get_enabled_types=lambda: enabled_types(),
        get_overlay_flags=get_preview_overlay_flags,
        on_preview_geometry=on_preview_geometry,
        on_video_transform_changed=lambda: _sync_video_transform_vars_from_model(),
    )

    def png_load_state_for_current() -> None:
        png_preview_ctrl.png_load_state_for_current()

    def png_save_state_for_current() -> None:
        png_preview_ctrl.png_save_state_for_current()

    def render_png_preview(force_reload: bool = False) -> None:
        png_preview_ctrl.render_png_preview(force_reload=force_reload)

    def fit_video_for_LR() -> None:
        png_preview_ctrl.fit_video_for_LR()

    def fit_video_for_TB() -> None:
        png_preview_ctrl.fit_video_for_TB()

    def fit_video_for_current_layout() -> None:
        layout_mode = _normalize_video_layout(_layout_cfg().video_layout)
        if layout_mode == "TB":
            fit_video_for_TB()
        else:
            fit_video_for_LR()

    def png_fit_to_height_both() -> None:
        fit_video_for_current_layout()

    def png_on_wheel(e) -> None:
        png_preview_ctrl.png_on_wheel(e)

    def png_on_down(e) -> None:
        png_preview_ctrl.png_on_down(e)

    def png_on_move(e) -> None:
        png_preview_ctrl.png_on_move(e)

    def png_on_up(_e=None) -> None:
        png_preview_ctrl.png_on_up(_e)

    png_state = png_preview_ctrl.png_state

    btn_png_fit.config(command=fit_video_for_current_layout)


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

    def get_hud_enabled() -> dict[str, bool]:
        out: dict[str, bool] = {}
        try:
            for t, var in hud_enabled_vars.items():
                out[str(t)] = bool(var.get())
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
            layout_config=app_model.layout_config,
        )

    def refresh_layout_preview() -> None:
        nonlocal hud_boxes, layout_preview_ctrl
        if layout_preview_ctrl is None:
            return
        if (layout_preview_ctrl.hud_active_id is None) and (layout_preview_ctrl.hud_mode == ""):
            hud_boxes = get_hud_boxes_for_current()
        layout_preview_ctrl.ensure_boxes_in_hud_area(hud_boxes)
        update_png_fit_button_text()
        render_png_preview(force_reload=False)

    layout_preview_ctrl = LayoutPreviewController(
        canvas=preview_canvas,
        save_current_boxes=save_current_boxes,
        redraw_preview=refresh_layout_preview,
        is_locked=lambda: video_preview_ctrl is not None and video_preview_ctrl.cap is not None,
    )

    def on_preview_canvas_motion(e) -> None:
        if layout_preview_ctrl.hud_active_id is not None:
            return
        if bool(png_state.get("drag")):
            return
        layout_preview_ctrl.on_layout_hover(e, hud_boxes, enabled_types())

    def on_preview_canvas_down(e) -> None:
        if bool(show_hud_boxes_var.get()):
            t, _mode = layout_preview_ctrl.hit_test_box(int(e.x), int(e.y), hud_boxes, enabled_types())
            if t is not None:
                layout_preview_ctrl.on_layout_mouse_down(e, hud_boxes, enabled_types())
                return
        png_on_down(e)

    def on_preview_canvas_drag(e) -> None:
        if layout_preview_ctrl.hud_active_id is not None:
            layout_preview_ctrl.on_layout_mouse_move(e, hud_boxes)
            return
        png_on_move(e)

    def on_preview_canvas_up(e) -> None:
        if layout_preview_ctrl.hud_active_id is not None:
            layout_preview_ctrl.on_layout_mouse_up(e)
            return
        png_on_up(e)
        layout_preview_ctrl.on_layout_leave(e)

    preview_canvas.bind("<MouseWheel>", png_on_wheel)
    preview_canvas.bind("<Motion>", on_preview_canvas_motion)
    preview_canvas.bind("<Leave>", lambda e: layout_preview_ctrl.on_layout_leave(e))
    preview_canvas.bind("<ButtonPress-1>", on_preview_canvas_down)
    preview_canvas.bind("<B1-Motion>", on_preview_canvas_drag)
    preview_canvas.bind("<ButtonRelease-1>", on_preview_canvas_up)

    for _overlay_var in (show_video_rects_var, show_hud_boxes_var, show_labels_var):
        try:
            _overlay_var.trace_add("write", lambda *_: refresh_layout_preview())
        except Exception:
            pass

    # Default: Unified Preview sichtbar
    try:
        preview_label.grid_remove()
    except Exception:
        pass
    try:
        preview_canvas.lift()
    except Exception:
        pass
    try:
        png_load_state_for_current()
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
            _safe(lbl.config, text=t, label="show_progress_with_cancel.set_text")

        def set_progress(pct: float):
            _safe(bar.__setitem__, "value", max(0.0, min(100.0, float(pct))), label="show_progress_with_cancel.set_progress")

        def is_cancelled() -> bool:
            return bool(cancel_state["cancel"])

        def close():
            _safe(win.grab_release, label="show_progress_with_cancel.grab_release")
            _safe(win.destroy, label="show_progress_with_cancel.destroy")

        return win, close, set_text, set_progress, is_cancelled

    def show_progress(title: str, text: str):
        win, close, _set_text, _set_progress, _is_cancelled = show_progress_with_cancel(title, text)

        def hide_cancel_button() -> None:
            for child in win.winfo_children():
                for grand in child.winfo_children():
                    if isinstance(grand, ttk.Button):
                        grand.grid_remove()
            win.update_idletasks()

        _safe(hide_cancel_button, label="show_progress.hide_cancel")
        return win, close

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
        if controller is None:
            return
        controller.on_generate()



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
                preview_canvas.grid_remove()
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

            try:
                preview_canvas.grid()
                preview_canvas.lift()
            except Exception:
                pass
            try:
                png_load_state_for_current()
                refresh_layout_preview()
            except Exception:
                pass

    def on_preview_mode_change(*_args) -> None:
        if controller is None:
            return
        controller.on_preview_mode_change(*_args)

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
        show_progress=show_progress,
    )

    ui_ctx = UIContext(
        get_input_video_dir=lambda: input_video_dir,
        get_input_csv_dir=lambda: input_csv_dir,
        get_current_output_preset=lambda: out_preset_var.get(),
        get_hud_width_px=get_hud_width_px,
        get_output_format=lambda: {
            "aspect": out_aspect_var.get(),
            "preset": out_preset_var.get(),
            "quality": out_quality_var.get(),
        },
        get_hud_layout_data=lambda: hud_layout_data,
        get_png_view_data=lambda: png_view_data,
        get_startframes=lambda: startframes_by_name,
        get_endframes=lambda: endframes_by_name,
        get_selected_files=get_selected_files,
        set_selected_files=set_selected_files,
        set_status=lambda text: lbl_loaded.config(text=text),
        set_app_model=set_app_model,
        set_output_preset=out_preset_var.set,
        set_hud_width_px=hud_width_var.set,
        open_file_dialog=open_file_dialog,
        save_file_dialog=save_file_dialog,
        schedule_after=lambda ms, fn: root.after(ms, fn),
        save_output_format=persistence.save_output_format,
        get_presets_for_aspect=get_presets_for_aspect,
        set_output_preset_values=lambda presets: cmb_preset.config(values=presets),
        get_preview_mode=preview_mode_var.get,
        refresh_layout_preview=refresh_layout_preview,
        render_png_preview=render_png_preview,
        png_load_state_for_current=png_load_state_for_current,
        png_save_state_for_current=png_save_state_for_current,
        close_preview_video=lambda: video_preview_ctrl.close_preview_video() if video_preview_ctrl is not None else None,
        refresh_display=refresh_display,
        set_fast_text=lambda text: lbl_fast.config(text=text),
        set_slow_text=lambda text: lbl_slow.config(text=text),
        get_profiles_dir=lambda: profiles_dir,
        build_profile_dict=build_profile_dict,
        apply_profile_dict=apply_profile_dict,
        choose_slow_fast_paths=choose_slow_fast_paths,
        parse_preset=parse_preset,
        get_output_video_dir=lambda: output_video_dir,
        get_project_root=lambda: find_project_root(Path(__file__)),
        get_hud_enabled=get_hud_enabled,
        model_from_ui_state=model_from_ui_state,
        get_hud_boxes_for_current=get_hud_boxes_for_current,
        png_view_key=png_view_key,
        get_png_state=lambda: png_state,
        show_progress_with_cancel=show_progress_with_cancel,
        update_ui=root.update,
        show_preview_controls=show_preview_controls,
    )
    controller = Controller(
        ui=ui_ctx,
        render_service=render_service,
        profile_service=profile_service,
        files_service=filesvc,
        get_layout_preview_ctrl=lambda: layout_preview_ctrl,
        get_png_preview_ctrl=lambda: png_preview_ctrl,
        get_video_preview_ctrl=lambda: video_preview_ctrl,
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
        if controller is None:
            return
        controller.on_preview_resize(_event)

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

    STARTUP_SETTINGS_VIEWPORT_HEIGHT = 340
    RESIZE_SETTINGS_VIEWPORT_MIN = 140
    _files_req_height = max(1, int(frame_files.winfo_reqheight()))

    def _settings_frame_chrome_height() -> int:
        try:
            chrome = int(left_scroll_settings_frame.winfo_reqheight()) - int(settings_canvas.winfo_reqheight())
        except Exception:
            chrome = 0
        if chrome < 0:
            chrome = 0
        return int(chrome)

    def _apply_window_layout_policy(startup: bool = False) -> None:
        nonlocal _files_req_height
        try:
            root.update_idletasks()
        except Exception:
            pass

        try:
            _files_req_height = max(int(_files_req_height), int(frame_files.winfo_reqheight()))
        except Exception:
            pass
        files_req_height = max(1, int(_files_req_height))
        settings_chrome = _settings_frame_chrome_height()

        files_min_visible = max(110, min(files_req_height, int(round(files_req_height * 0.8))))
        left_column.rowconfigure(0, minsize=int(files_min_visible))
        left_column.rowconfigure(1, minsize=int(RESIZE_SETTINGS_VIEWPORT_MIN))

        screen_w = max(1, int(root.winfo_screenwidth()))
        screen_h = max(1, int(root.winfo_screenheight()))
        max_window_w = max(320, int(screen_w * 0.95))
        max_window_h = max(240, int(screen_h * 0.95))

        min_w = max(760, int(left_column.winfo_reqwidth()) + 320)
        min_h = int(files_min_visible) + 10 + int(settings_chrome) + int(RESIZE_SETTINGS_VIEWPORT_MIN)
        min_w = min(int(min_w), max(320, int(screen_w - 40)))
        min_h = min(int(min_h), max(240, int(screen_h - 60)))
        root.minsize(int(max(320, min_w)), int(max(240, min_h)))

        if not startup:
            return

        required_left_height = int(files_req_height) + 10 + int(settings_chrome) + int(STARTUP_SETTINGS_VIEWPORT_HEIGHT)
        required_w = max(
            int(root.winfo_reqwidth()),
            int(left_column.winfo_reqwidth()) + int(frame_preview.winfo_reqwidth()) + 40,
        )
        required_h = max(
            int(root.winfo_reqheight()),
            int(frame_preview.winfo_reqheight()),
            int(required_left_height),
        )

        current_w = max(1, int(root.winfo_width()))
        current_h = max(1, int(root.winfo_height()))
        target_w = min(int(max_window_w), max(int(current_w), int(required_w)))
        target_h = min(int(max_window_h), max(int(current_h), int(required_h)))
        if target_w > current_w or target_h > current_h:
            root.geometry(f"{int(target_w)}x{int(target_h)}")

    def _on_root_resize(event=None) -> None:
        if event is not None and event.widget is not root:
            return
        _apply_window_layout_policy(startup=False)

    root.bind("<Configure>", _on_root_resize, add="+")

    def _startup_initialize_png_preview() -> None:
        try:
            mode = str(preview_mode_var.get() or "png").strip().lower()
        except Exception:
            mode = "png"
        if mode != "png":
            return
        try:
            png_load_state_for_current()
        except Exception:
            pass
        try:
            fit_video_for_current_layout()
        except Exception:
            try:
                refresh_layout_preview()
            except Exception:
                pass
        _run_hud_fit_if_frame_mode("startup_png_preview_initialized")

    sync_from_folders_if_needed_ui(force=True)
    try:
        root.after(0, lambda: _apply_window_layout_policy(startup=True))
    except Exception:
        _apply_window_layout_policy(startup=True)
    try:
        root.after(0, _startup_initialize_png_preview)
    except Exception:
        _startup_initialize_png_preview()
    root.mainloop()


if __name__ == "__main__":
    main()
