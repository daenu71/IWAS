import configparser
import json
import math
from pathlib import Path


def _find_project_root(script_path: Path) -> Path:
    p = script_path.resolve()
    for parent in [p.parent] + list(p.parents):
        if (parent / "requirements.txt").exists():
            return parent
    return p.parent


project_root = _find_project_root(Path(__file__))
config_dir = project_root / "config"
startframes_file = config_dir / "startframes.json"
endframes_file = config_dir / "endframes.json"
defaults_ini = config_dir / "defaults.ini"
user_ini = config_dir / "user.ini"
cfg = configparser.ConfigParser()
try:
    ini_layers: list[Path] = []
    if defaults_ini.exists():
        ini_layers.append(defaults_ini)
    if user_ini.exists():
        ini_layers.append(user_ini)
    if ini_layers:
        cfg.read([str(p) for p in ini_layers], encoding="utf-8")
except Exception:
    pass
output_format_file = config_dir / "output_format.json"
hud_layout_file = config_dir / "hud_layout.json"
png_view_file = config_dir / "png_view.json"

_VIDEO_CUT_DEFAULTS: dict[str, float] = {
    "video_before_brake": 1.0,
    "video_after_full_throttle": 1.0,
    "video_minimum_between_two_curves": 2.0,
}
_VIDEO_CUT_LOGGED_ONCE = False


def load_startframes() -> dict[str, int]:
    try:
        if startframes_file.exists():
            data = json.loads(startframes_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out: dict[str, int] = {}
                for k, v in data.items():
                    try:
                        out[str(k)] = int(v)
                    except Exception:
                        pass
                return out
    except Exception:
        pass
    return {}


def save_startframes(d: dict[str, int]) -> None:
    try:
        startframes_file.write_text(json.dumps(d, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_endframes() -> dict[str, int]:
    try:
        if endframes_file.exists():
            data = json.loads(endframes_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out: dict[str, int] = {}
                for k, v in data.items():
                    try:
                        out[str(k)] = int(v)
                    except Exception:
                        pass
                return out
    except Exception:
        pass
    return {}


def save_endframes(d: dict[str, int]) -> None:
    try:
        endframes_file.write_text(json.dumps(d, indent=2), encoding="utf-8")
    except Exception:
        pass


def cfg_get(section: str, key: str, fallback: str) -> str:
    try:
        return cfg.get(section, key, fallback=fallback)
    except Exception:
        return fallback


def load_png_view() -> dict:
    try:
        if png_view_file.exists():
            data = json.loads(png_view_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_png_view(data: dict) -> None:
    try:
        png_view_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_hud_layout() -> dict:
    try:
        if hud_layout_file.exists():
            data = json.loads(hud_layout_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_hud_layout(data: dict) -> None:
    try:
        hud_layout_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_output_format() -> dict[str, str]:
    # Reihenfolge: config/output_format.json (User-Wahl) -> defaults.ini -> Fallback
    out: dict[str, str] = {
        "aspect": cfg_get("video_compare", "output_aspect", "32:9"),
        "preset": cfg_get("video_compare", "output_preset", "5120x1440"),
        "quality": cfg_get("video_compare", "output_quality", "Original"),
        "hud_width_px": cfg_get("video_compare", "hud_width_px", "320"),
    }
    try:
        if output_format_file.exists():
            data = json.loads(output_format_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                a = str(data.get("aspect") or "").strip()
                p = str(data.get("preset") or "").strip()
                q = str(data.get("quality") or "").strip()
                h = str(data.get("hud_width_px") or "").strip()
                if a:
                    out["aspect"] = a
                if p:
                    out["preset"] = p
                if q:
                    out["quality"] = q
                if h:
                    out["hud_width_px"] = h
    except Exception:
        pass
    return out


def save_output_format(d: dict[str, str]) -> None:
    try:
        merged: dict[str, str] = {}
        if output_format_file.exists():
            try:
                old = json.loads(output_format_file.read_text(encoding="utf-8"))
                if isinstance(old, dict):
                    for k, v in old.items():
                        merged[str(k)] = str(v)
            except Exception:
                pass

        for k, v in (d or {}).items():
            merged[str(k)] = str(v)

        output_format_file.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    except Exception:
        pass


def _cfg_float(section: str, key: str, fallback: float) -> float:
    try:
        v = str(cfg_get(section, key, str(fallback))).strip()
        return float(v) if v else float(fallback)
    except Exception:
        return float(fallback)


def _cfg_float_opt(section: str, key: str) -> float | None:
    try:
        v = str(cfg_get(section, key, "")).strip()
        return float(v) if v else None
    except Exception:
        return None


def _cfg_int(section: str, key: str, fallback: int) -> int:
    try:
        v = str(cfg_get(section, key, str(fallback))).strip()
        return int(float(v)) if v else int(fallback)
    except Exception:
        return int(fallback)


def _cfg_int_opt(section: str, key: str) -> int | None:
    try:
        v = str(cfg_get(section, key, "")).strip()
        return int(float(v)) if v else None
    except Exception:
        return None


def _append_log_line(log_file: Path | None, line: str) -> None:
    if log_file is None:
        return
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(str(line).rstrip("\n") + "\n")
    except Exception:
        pass


def _parse_non_negative_finite_float(raw: str) -> tuple[float | None, str | None]:
    s = str(raw).strip()
    try:
        value = float(s)
    except Exception:
        return None, "must be a finite float >= 0"
    if not math.isfinite(value):
        return None, "must be a finite float >= 0"
    if value < 0.0:
        return None, "must be >= 0"
    return float(value), None


def _log_video_cut_once(values: dict[str, float], log_file: Path | None = None) -> None:
    global _VIDEO_CUT_LOGGED_ONCE
    if _VIDEO_CUT_LOGGED_ONCE:
        return
    _VIDEO_CUT_LOGGED_ONCE = True
    _append_log_line(
        log_file,
        "video_cut: "
        f"before_brake={values['video_before_brake']}, "
        f"after_full_throttle={values['video_after_full_throttle']}, "
        f"minimum_between_two_curves={values['video_minimum_between_two_curves']}",
    )


def load_video_cut_settings(*, video_mode: str, log_file: Path | None = None) -> dict[str, float]:
    mode = str(video_mode or "full").strip().lower()
    if mode not in ("full", "cut"):
        mode = "full"

    values: dict[str, float] = {}
    invalid: list[str] = []
    for key, default in _VIDEO_CUT_DEFAULTS.items():
        raw = cfg_get("video_cut", key, str(default))
        parsed, err = _parse_non_negative_finite_float(raw)
        if err is None and parsed is not None:
            values[key] = float(parsed)
            continue
        invalid.append(f"{key}={raw!r} (expected {err})")

    if invalid:
        msg = "invalid [video_cut] config: " + "; ".join(invalid)
        if mode == "cut":
            _append_log_line(log_file, f"ERROR: {msg}")
            raise ValueError(msg)
        _append_log_line(
            log_file,
            f"WARNING: {msg}; video_mode=full -> ignoring video_cut values and falling back to defaults.",
        )
        values = dict(_VIDEO_CUT_DEFAULTS)

    _log_video_cut_once(values, log_file=log_file)
    return values
