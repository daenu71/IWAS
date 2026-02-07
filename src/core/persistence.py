import configparser
import json
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
cfg = configparser.ConfigParser()
try:
    if defaults_ini.exists():
        cfg.read(defaults_ini, encoding="utf-8")
except Exception:
    pass
output_format_file = config_dir / "output_format.json"
hud_layout_file = config_dir / "hud_layout.json"
png_view_file = config_dir / "png_view.json"


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
