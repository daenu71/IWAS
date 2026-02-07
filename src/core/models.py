from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return int(default)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _to_bool(value: Any, default: bool = False) -> bool:
    try:
        return bool(value)
    except Exception:
        return bool(default)


@dataclass
class OutputFormat:
    aspect: str = ""
    preset: str = ""
    quality: str = ""
    hud_width_px: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "aspect": str(self.aspect),
            "preset": str(self.preset),
            "quality": str(self.quality),
            "hud_width_px": int(self.hud_width_px),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "OutputFormat":
        data = d if isinstance(d, dict) else {}
        return cls(
            aspect=str(data.get("aspect") or ""),
            preset=str(data.get("preset") or ""),
            quality=str(data.get("quality") or ""),
            hud_width_px=_to_int(data.get("hud_width_px", 0), 0),
        )


@dataclass
class HudBox:
    type: str = ""
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": str(self.type),
            "x": int(self.x),
            "y": int(self.y),
            "w": int(self.w),
            "h": int(self.h),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "HudBox":
        data = d if isinstance(d, dict) else {}
        return cls(
            type=str(data.get("type") or ""),
            x=_to_int(data.get("x", 0), 0),
            y=_to_int(data.get("y", 0), 0),
            w=_to_int(data.get("w", 0), 0),
            h=_to_int(data.get("h", 0), 0),
        )


@dataclass
class HudLayoutState:
    hud_layout_data: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def key_from(out_preset: str, hud_w: int) -> str:
        return f"{out_preset}|hud{hud_w}"

    def current_boxes_for_key(self, key: str) -> list[HudBox]:
        raw = self.hud_layout_data.get(key) if isinstance(self.hud_layout_data, dict) else None
        if not isinstance(raw, list):
            return []
        out: list[HudBox] = []
        for item in raw:
            if isinstance(item, dict):
                out.append(HudBox.from_dict(item))
        return out

    def set_current_boxes_for_key(self, key: str, boxes: list[HudBox]) -> None:
        if not isinstance(self.hud_layout_data, dict):
            self.hud_layout_data = {}
        out: list[dict[str, Any]] = []
        for box in boxes:
            if isinstance(box, HudBox):
                out.append(box.to_dict())
            elif isinstance(box, dict):
                out.append(dict(box))
        self.hud_layout_data[key] = out


@dataclass
class PngSideState:
    zoom: float = 1.0
    off_x: int = 0
    off_y: int = 0
    fit_to_height: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "zoom": float(self.zoom),
            "off_x": int(self.off_x),
            "off_y": int(self.off_y),
            "fit_to_height": bool(self.fit_to_height),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "PngSideState":
        data = d if isinstance(d, dict) else {}
        return cls(
            zoom=_to_float(data.get("zoom", 1.0), 1.0),
            off_x=_to_int(data.get("off_x", 0), 0),
            off_y=_to_int(data.get("off_y", 0), 0),
            fit_to_height=_to_bool(data.get("fit_to_height", False), False),
        )


@dataclass
class PngViewState:
    png_view_data: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def key_from(out_preset: str, hud_w: int) -> str:
        return f"{out_preset}|hud{hud_w}"

    def load_current(self, key: str) -> tuple[PngSideState, PngSideState]:
        d = self.png_view_data.get(key) if isinstance(self.png_view_data, dict) else None
        if not isinstance(d, dict):
            return PngSideState(), PngSideState()
        left = PngSideState(
            zoom=_to_float(d.get("zoom_l", 1.0), 1.0),
            off_x=_to_int(d.get("off_lx", 0), 0),
            off_y=_to_int(d.get("off_ly", 0), 0),
            fit_to_height=_to_bool(d.get("fit_l", False), False),
        )
        right = PngSideState(
            zoom=_to_float(d.get("zoom_r", 1.0), 1.0),
            off_x=_to_int(d.get("off_rx", 0), 0),
            off_y=_to_int(d.get("off_ry", 0), 0),
            fit_to_height=_to_bool(d.get("fit_r", False), False),
        )
        return left, right

    def save_current(self, key: str, left: PngSideState, right: PngSideState) -> None:
        if not isinstance(self.png_view_data, dict):
            self.png_view_data = {}
        self.png_view_data[key] = {
            "zoom_l": float(left.zoom),
            "off_lx": int(left.off_x),
            "off_ly": int(left.off_y),
            "fit_l": bool(left.fit_to_height),
            "zoom_r": float(right.zoom),
            "off_rx": int(right.off_x),
            "off_ry": int(right.off_y),
            "fit_r": bool(right.fit_to_height),
        }


@dataclass
class Profile:
    version: int | str = 1
    videos: list[str] = field(default_factory=list)
    csvs: list[str] = field(default_factory=list)
    startframes: dict[str, int] = field(default_factory=dict)
    endframes: dict[str, int] = field(default_factory=dict)
    output: OutputFormat = field(default_factory=OutputFormat)
    hud_layout_data: dict[str, Any] = field(default_factory=dict)
    png_view_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "videos": [str(v) for v in self.videos],
            "csvs": [str(v) for v in self.csvs],
            "startframes": {str(k): _to_int(v, 0) for k, v in self.startframes.items()},
            "endframes": {str(k): _to_int(v, 0) for k, v in self.endframes.items()},
            "output": {
                "aspect": str(self.output.aspect),
                "preset": str(self.output.preset),
                "quality": str(self.output.quality),
                "hud_width_px": str(_to_int(self.output.hud_width_px, 0)),
            },
            "hud_layout_data": self.hud_layout_data,
            "png_view_data": self.png_view_data,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Profile":
        data = d if isinstance(d, dict) else {}
        videos_raw = data.get("videos")
        csvs_raw = data.get("csvs")
        starts_raw = data.get("startframes")
        ends_raw = data.get("endframes")
        return cls(
            version=data.get("version", 1),
            videos=[str(v) for v in (videos_raw if isinstance(videos_raw, list) else [])],
            csvs=[str(v) for v in (csvs_raw if isinstance(csvs_raw, list) else [])],
            startframes={str(k): _to_int(v, 0) for k, v in (starts_raw.items() if isinstance(starts_raw, dict) else [])},
            endframes={str(k): _to_int(v, 0) for k, v in (ends_raw.items() if isinstance(ends_raw, dict) else [])},
            output=OutputFormat.from_dict(data.get("output") if isinstance(data.get("output"), dict) else {}),
            hud_layout_data=data.get("hud_layout_data") if isinstance(data.get("hud_layout_data"), dict) else {},
            png_view_data=data.get("png_view_data") if isinstance(data.get("png_view_data"), dict) else {},
        )


@dataclass
class AppModel:
    output: OutputFormat = field(default_factory=OutputFormat)
    hud_layout: HudLayoutState = field(default_factory=HudLayoutState)
    png_view: PngViewState = field(default_factory=PngViewState)


@dataclass
class RenderPayload:
    version: int | str = 1
    videos: list[str] = field(default_factory=list)
    csvs: list[str] = field(default_factory=list)
    slow_video: str = ""
    fast_video: str = ""
    out_video: str = ""
    output: OutputFormat = field(default_factory=OutputFormat)
    hud_enabled: dict[str, bool] = field(default_factory=dict)
    hud_boxes: dict[str, Any] = field(default_factory=dict)
    hud_window: dict[str, Any] = field(default_factory=lambda: {"default_before_s": 0.0, "default_after_s": 0.0, "overrides": {}})
    hud_speed: dict[str, Any] = field(default_factory=lambda: {"units": "kmh", "update_hz": 60})
    hud_curve_points: dict[str, Any] = field(default_factory=lambda: {"default": 180, "overrides": {}})
    hud_gear_rpm: dict[str, Any] = field(default_factory=lambda: {"update_hz": 60})
    png_view_key: str = ""
    png_view_state: dict[str, Any] = field(default_factory=lambda: {"L": {}, "R": {}})
    hud_layout_data: dict[str, Any] = field(default_factory=dict)
    png_view_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "videos": list(self.videos),
            "csvs": list(self.csvs),
            "slow_video": self.slow_video,
            "fast_video": self.fast_video,
            "out_video": self.out_video,
            "output": {
                "aspect": self.output.aspect,
                "preset": self.output.preset,
                "quality": self.output.quality,
                "hud_width_px": self.output.hud_width_px,
            },
            "hud_enabled": self.hud_enabled,
            "hud_boxes": self.hud_boxes,
            "hud_window": self.hud_window,
            "hud_speed": self.hud_speed,
            "hud_curve_points": self.hud_curve_points,
            "hud_gear_rpm": self.hud_gear_rpm,
            "png_view_key": self.png_view_key,
            "png_view_state": self.png_view_state,
            "hud_layout_data": self.hud_layout_data,
            "png_view_data": self.png_view_data,
        }
