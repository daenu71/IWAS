from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path

APP_VERSION = "0.1.0"

APP_NAME = "iWAS"


@dataclass(frozen=True)
class Cfg:
    root: Path
    config_file: Path
    hud_width_px: int


def load_cfg(project_root: str | Path, config_file: str | Path = "config/defaults.ini") -> Cfg:
    root = Path(project_root).resolve()
    cfg_path = (root / config_file).resolve()

    cp = configparser.ConfigParser()
    cp.read(cfg_path, encoding="utf-8")

    hud_width_px = _get_int(cp, "video_compare", "hud_width_px", 640)

    return Cfg(
        root=root,
        config_file=cfg_path,
        hud_width_px=hud_width_px,
    )


def _get_int(cp: configparser.ConfigParser, section: str, key: str, default: int) -> int:
    try:
        return int(cp.get(section, key, fallback=str(default)).strip())
    except Exception:
        return int(default)


def _get_float(cp: configparser.ConfigParser, section: str, key: str, default: float) -> float:
    try:
        return float(cp.get(section, key, fallback=str(default)).strip())
    except Exception:
        return float(default)


def _get_str(cp: configparser.ConfigParser, section: str, key: str, default: str) -> str:
    try:
        return str(cp.get(section, key, fallback=default)).strip()
    except Exception:
        return str(default)
