"""Persistence layer for ini/json settings and runtime state."""

import configparser
import json
import logging
import math
from pathlib import Path

from core.resources import get_resource_path

_LOG = logging.getLogger(__name__)


def _find_project_root(script_path: Path) -> Path:
    """Find project root."""
    try:
        return get_resource_path()
    except Exception:
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

_COACHING_RECORDING_SECTION = "coaching_recording"
_COACHING_RECORDING_INT_RANGES: dict[str, tuple[int, int]] = {
    "irsdk_sample_hz": (0, 1000),
    "coaching_retention_months": (1, 120),
    "coaching_low_disk_warning_gb": (1, 2000),
}
_COACHING_RECORDING_DEFAULTS: dict[str, object] = {
    "coaching_recording_enabled": True,
    "coaching_storage_dir": r"C:\iWAS\data\coaching",
    "irsdk_sample_hz": 120,
    "coaching_retention_months_enabled": False,
    "coaching_retention_months": 6,
    "coaching_low_disk_warning_enabled": False,
    "coaching_low_disk_warning_gb": 20,
    "coaching_auto_delete_enabled": False,
}
_COACHING_STORAGE_DIR_FALLBACK = Path(r"C:\iWAS\data\coaching")

_VIDEO_CUT_DEFAULTS: dict[str, float] = {
    "video_before_brake": 1.0,
    "video_after_full_throttle": 1.0,
    "video_minimum_between_two_curves": 2.0,
}
_VIDEO_CUT_LOGGED_ONCE = False


def load_startframes() -> dict[str, int]:
    """Load data startframes."""
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
    """Save data startframes."""
    try:
        startframes_file.write_text(json.dumps(d, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_endframes() -> dict[str, int]:
    """Load data endframes."""
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
    """Save data endframes."""
    try:
        endframes_file.write_text(json.dumps(d, indent=2), encoding="utf-8")
    except Exception:
        pass


def cfg_get(section: str, key: str, fallback: str) -> str:
    """Implement cfg get logic."""
    try:
        return cfg.get(section, key, fallback=fallback)
    except Exception:
        return fallback


def _cfg_bool(section: str, key: str, fallback: bool) -> bool:
    """Implement cfg bool logic."""
    try:
        raw = str(cfg_get(section, key, "true" if fallback else "false")).strip().lower()
    except Exception:
        return bool(fallback)
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return bool(fallback)


def _coerce_bool(raw: object, fallback: bool) -> bool:
    """Coerce bool."""
    if isinstance(raw, bool):
        return bool(raw)
    try:
        token = str(raw).strip().lower()
    except Exception:
        return bool(fallback)
    if token in ("1", "true", "yes", "on"):
        return True
    if token in ("0", "false", "no", "off"):
        return False
    return bool(fallback)


def _coerce_int_in_range(raw: object, fallback: int, *, min_value: int, max_value: int) -> int:
    """Coerce int in range."""
    try:
        value = int(float(str(raw).strip()))
    except Exception:
        value = int(fallback)
    if value < int(min_value):
        return int(min_value)
    if value > int(max_value):
        return int(max_value)
    return int(value)


def _coerce_str(raw: object, fallback: str) -> str:
    """Coerce str."""
    try:
        return str(raw).strip()
    except Exception:
        return str(fallback)


def _persist_coaching_storage_dir_if_user_empty(effective_dir: str) -> None:
    """Implement persist coaching storage dir if user empty logic."""
    user_cp = configparser.ConfigParser()
    try:
        if user_ini.exists():
            user_cp.read(user_ini, encoding="utf-8")
    except Exception:
        pass

    existing = ""
    try:
        if user_cp.has_section(_COACHING_RECORDING_SECTION) and user_cp.has_option(
            _COACHING_RECORDING_SECTION, "coaching_storage_dir"
        ):
            existing = str(user_cp.get(_COACHING_RECORDING_SECTION, "coaching_storage_dir", fallback="")).strip()
    except Exception:
        existing = ""
    if existing:
        return

    try:
        if not user_cp.has_section(_COACHING_RECORDING_SECTION):
            user_cp.add_section(_COACHING_RECORDING_SECTION)
        user_cp.set(_COACHING_RECORDING_SECTION, "coaching_storage_dir", effective_dir)
        if not cfg.has_section(_COACHING_RECORDING_SECTION):
            cfg.add_section(_COACHING_RECORDING_SECTION)
        cfg.set(_COACHING_RECORDING_SECTION, "coaching_storage_dir", effective_dir)
        config_dir.mkdir(parents=True, exist_ok=True)
        with user_ini.open("w", encoding="utf-8") as fh:
            user_cp.write(fh)
    except Exception as exc:
        _LOG.warning("could not persist default coaching storage dir '%s' (%s)", effective_dir, exc)


def resolve_coaching_storage_dir(
    raw_value: object | None = None,
    *,
    persist_if_user_empty: bool = False,
) -> str:
    """Resolve coaching storage dir."""
    if raw_value is None:
        raw_value = cfg_get(
            _COACHING_RECORDING_SECTION,
            "coaching_storage_dir",
            str(_COACHING_RECORDING_DEFAULTS["coaching_storage_dir"]),
        )

    raw_text = _coerce_str(raw_value, "")
    path_obj = Path(raw_text) if raw_text else _COACHING_STORAGE_DIR_FALLBACK
    effective_dir = str(path_obj)

    try:
        path_obj.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        _LOG.error("coaching storage directory unavailable '%s' (%s)", effective_dir, exc)

    if persist_if_user_empty:
        _persist_coaching_storage_dir_if_user_empty(effective_dir)

    return effective_dir


def load_coaching_recording_settings() -> dict[str, object]:
    """Load data coaching recording settings."""
    return {
        "coaching_recording_enabled": bool(
            _cfg_bool(
                _COACHING_RECORDING_SECTION,
                "coaching_recording_enabled",
                bool(_COACHING_RECORDING_DEFAULTS["coaching_recording_enabled"]),
            )
        ),
        "coaching_storage_dir": resolve_coaching_storage_dir(persist_if_user_empty=True),
        "irsdk_sample_hz": _coerce_int_in_range(
            cfg_get(
                _COACHING_RECORDING_SECTION,
                "irsdk_sample_hz",
                str(_COACHING_RECORDING_DEFAULTS["irsdk_sample_hz"]),
            ),
            int(_COACHING_RECORDING_DEFAULTS["irsdk_sample_hz"]),
            min_value=_COACHING_RECORDING_INT_RANGES["irsdk_sample_hz"][0],
            max_value=_COACHING_RECORDING_INT_RANGES["irsdk_sample_hz"][1],
        ),
        "coaching_retention_months_enabled": bool(
            _cfg_bool(
                _COACHING_RECORDING_SECTION,
                "coaching_retention_months_enabled",
                bool(_COACHING_RECORDING_DEFAULTS["coaching_retention_months_enabled"]),
            )
        ),
        "coaching_retention_months": _coerce_int_in_range(
            cfg_get(
                _COACHING_RECORDING_SECTION,
                "coaching_retention_months",
                str(_COACHING_RECORDING_DEFAULTS["coaching_retention_months"]),
            ),
            int(_COACHING_RECORDING_DEFAULTS["coaching_retention_months"]),
            min_value=_COACHING_RECORDING_INT_RANGES["coaching_retention_months"][0],
            max_value=_COACHING_RECORDING_INT_RANGES["coaching_retention_months"][1],
        ),
        "coaching_low_disk_warning_enabled": bool(
            _cfg_bool(
                _COACHING_RECORDING_SECTION,
                "coaching_low_disk_warning_enabled",
                bool(_COACHING_RECORDING_DEFAULTS["coaching_low_disk_warning_enabled"]),
            )
        ),
        "coaching_low_disk_warning_gb": _coerce_int_in_range(
            cfg_get(
                _COACHING_RECORDING_SECTION,
                "coaching_low_disk_warning_gb",
                str(_COACHING_RECORDING_DEFAULTS["coaching_low_disk_warning_gb"]),
            ),
            int(_COACHING_RECORDING_DEFAULTS["coaching_low_disk_warning_gb"]),
            min_value=_COACHING_RECORDING_INT_RANGES["coaching_low_disk_warning_gb"][0],
            max_value=_COACHING_RECORDING_INT_RANGES["coaching_low_disk_warning_gb"][1],
        ),
        "coaching_auto_delete_enabled": bool(
            _cfg_bool(
                _COACHING_RECORDING_SECTION,
                "coaching_auto_delete_enabled",
                bool(_COACHING_RECORDING_DEFAULTS["coaching_auto_delete_enabled"]),
            )
        ),
    }


def save_coaching_recording_settings(values: dict[str, object]) -> dict[str, object]:
    """Save data coaching recording settings."""
    current = load_coaching_recording_settings()
    merged: dict[str, object] = dict(current)
    incoming = values if isinstance(values, dict) else {}

    if "coaching_recording_enabled" in incoming:
        merged["coaching_recording_enabled"] = _coerce_bool(
            incoming.get("coaching_recording_enabled"),
            bool(current["coaching_recording_enabled"]),
        )
    if "coaching_storage_dir" in incoming:
        merged["coaching_storage_dir"] = _coerce_str(incoming.get("coaching_storage_dir"), str(current["coaching_storage_dir"]))
    if "irsdk_sample_hz" in incoming:
        lo, hi = _COACHING_RECORDING_INT_RANGES["irsdk_sample_hz"]
        merged["irsdk_sample_hz"] = _coerce_int_in_range(
            incoming.get("irsdk_sample_hz"),
            int(current["irsdk_sample_hz"]),
            min_value=lo,
            max_value=hi,
        )
    if "coaching_retention_months_enabled" in incoming:
        merged["coaching_retention_months_enabled"] = _coerce_bool(
            incoming.get("coaching_retention_months_enabled"),
            bool(current["coaching_retention_months_enabled"]),
        )
    if "coaching_retention_months" in incoming:
        lo, hi = _COACHING_RECORDING_INT_RANGES["coaching_retention_months"]
        merged["coaching_retention_months"] = _coerce_int_in_range(
            incoming.get("coaching_retention_months"),
            int(current["coaching_retention_months"]),
            min_value=lo,
            max_value=hi,
        )
    if "coaching_low_disk_warning_enabled" in incoming:
        merged["coaching_low_disk_warning_enabled"] = _coerce_bool(
            incoming.get("coaching_low_disk_warning_enabled"),
            bool(current["coaching_low_disk_warning_enabled"]),
        )
    if "coaching_low_disk_warning_gb" in incoming:
        lo, hi = _COACHING_RECORDING_INT_RANGES["coaching_low_disk_warning_gb"]
        merged["coaching_low_disk_warning_gb"] = _coerce_int_in_range(
            incoming.get("coaching_low_disk_warning_gb"),
            int(current["coaching_low_disk_warning_gb"]),
            min_value=lo,
            max_value=hi,
        )
    if "coaching_auto_delete_enabled" in incoming:
        merged["coaching_auto_delete_enabled"] = _coerce_bool(
            incoming.get("coaching_auto_delete_enabled"),
            bool(current["coaching_auto_delete_enabled"]),
        )
    merged["coaching_storage_dir"] = resolve_coaching_storage_dir(merged.get("coaching_storage_dir"))

    user_cp = configparser.ConfigParser()
    try:
        if user_ini.exists():
            user_cp.read(user_ini, encoding="utf-8")
    except Exception:
        pass
    if not user_cp.has_section(_COACHING_RECORDING_SECTION):
        user_cp.add_section(_COACHING_RECORDING_SECTION)

    for key, value in merged.items():
        if isinstance(value, bool):
            text = "true" if value else "false"
        else:
            text = str(value)
        try:
            user_cp.set(_COACHING_RECORDING_SECTION, str(key), text)
        except Exception:
            pass
        try:
            if not cfg.has_section(_COACHING_RECORDING_SECTION):
                cfg.add_section(_COACHING_RECORDING_SECTION)
            cfg.set(_COACHING_RECORDING_SECTION, str(key), text)
        except Exception:
            pass

    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        with user_ini.open("w", encoding="utf-8") as fh:
            user_cp.write(fh)
    except Exception:
        pass

    return merged


def load_png_view() -> dict:
    """Load data png view."""
    try:
        if png_view_file.exists():
            data = json.loads(png_view_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_png_view(data: dict) -> None:
    """Save data png view."""
    try:
        png_view_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_hud_layout() -> dict:
    """Load data hud layout."""
    try:
        if hud_layout_file.exists():
            data = json.loads(hud_layout_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_hud_layout(data: dict) -> None:
    """Save data hud layout."""
    try:
        hud_layout_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_output_format() -> dict[str, str]:
    # Reihenfolge: config/output_format.json (User-Wahl) -> defaults.ini -> Fallback
    """Load data output format."""
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
    """Save data output format."""
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
    """Implement cfg float logic."""
    try:
        v = str(cfg_get(section, key, str(fallback))).strip()
        return float(v) if v else float(fallback)
    except Exception:
        return float(fallback)


def _cfg_float_opt(section: str, key: str) -> float | None:
    """Implement cfg float opt logic."""
    try:
        v = str(cfg_get(section, key, "")).strip()
        return float(v) if v else None
    except Exception:
        return None


def _cfg_int(section: str, key: str, fallback: int) -> int:
    """Implement cfg int logic."""
    try:
        v = str(cfg_get(section, key, str(fallback))).strip()
        return int(float(v)) if v else int(fallback)
    except Exception:
        return int(fallback)


def _cfg_int_opt(section: str, key: str) -> int | None:
    """Implement cfg int opt logic."""
    try:
        v = str(cfg_get(section, key, "")).strip()
        return int(float(v)) if v else None
    except Exception:
        return None


def _append_log_line(log_file: Path | None, line: str) -> None:
    """Implement append log line logic."""
    if log_file is None:
        return
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(str(line).rstrip("\n") + "\n")
    except Exception:
        pass


def _parse_non_negative_finite_float(raw: str) -> tuple[float | None, str | None]:
    """Parse non negative finite float."""
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
    """Implement log video cut once logic."""
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
    """Load data video cut settings."""
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
