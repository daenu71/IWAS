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


def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return None


def _set_if_changed(target: dict[str, Any], key: str, value: Any) -> bool:
    if target.get(key) == value:
        return False
    target[key] = value
    return True


def _merge_known_keys(existing: Any, known: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    out = dict(existing) if isinstance(existing, dict) else {}
    changed = not isinstance(existing, dict)
    for k, v in known.items():
        if out.get(k) != v:
            out[k] = v
            changed = True
    return out, changed


@dataclass
class HudFrameConfig:
    orientation: str = "vertical"
    anchor: str = "center"
    frame_thickness_px: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "orientation": str(self.orientation),
            "anchor": str(self.anchor),
            "frame_thickness_px": self.frame_thickness_px if self.frame_thickness_px is None else int(self.frame_thickness_px),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "HudFrameConfig":
        data = d if isinstance(d, dict) else {}

        orientation = str(data.get("orientation") or "vertical").strip().lower()
        if orientation not in ("vertical", "horizontal"):
            orientation = "vertical"

        anchor = str(data.get("anchor") or "center").strip().lower()
        valid_anchors = ("center", "left", "right") if orientation == "vertical" else ("top", "center", "bottom", "top_bottom")
        if anchor not in valid_anchors:
            anchor = "center"

        return cls(
            orientation=orientation,
            anchor=anchor,
            frame_thickness_px=_to_int_or_none(data.get("frame_thickness_px")),
        )


@dataclass
class VideoTransformConfig:
    scale_pct: float = 100.0
    shift_x_px: int = 0
    shift_y_px: int = 0
    fit_button_mode: str = "fit_height"

    def to_dict(self) -> dict[str, Any]:
        return {
            "scale_pct": float(self.scale_pct),
            "shift_x_px": int(self.shift_x_px),
            "shift_y_px": int(self.shift_y_px),
            "fit_button_mode": str(self.fit_button_mode),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None, *, video_layout: str = "LR") -> "VideoTransformConfig":
        data = d if isinstance(d, dict) else {}
        default_fit = "fit_height" if str(video_layout) == "LR" else "fit_width"
        fit_button_mode = str(data.get("fit_button_mode") or default_fit).strip().lower()
        if fit_button_mode not in ("fit_height", "fit_width"):
            fit_button_mode = default_fit
        return cls(
            scale_pct=_to_float(data.get("scale_pct", 100.0), 100.0),
            shift_x_px=_to_int(data.get("shift_x_px", 0), 0),
            shift_y_px=_to_int(data.get("shift_y_px", 0), 0),
            fit_button_mode=fit_button_mode,
        )


@dataclass
class HudFreeConfig:
    bg_alpha: int = 255
    boxes_abs_out: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bg_alpha": int(self.bg_alpha),
            "boxes_abs_out": {
                str(k): {
                    "x": int(v.get("x", 0)),
                    "y": int(v.get("y", 0)),
                    "w": int(v.get("w", 0)),
                    "h": int(v.get("h", 0)),
                }
                for k, v in self.boxes_abs_out.items()
                if isinstance(v, dict)
            },
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "HudFreeConfig":
        data = d if isinstance(d, dict) else {}
        boxes_raw = data.get("boxes_abs_out")
        boxes: dict[str, dict[str, int]] = {}
        if isinstance(boxes_raw, dict):
            for key, box in boxes_raw.items():
                if not isinstance(box, dict):
                    continue
                boxes[str(key)] = {
                    "x": _to_int(box.get("x", 0), 0),
                    "y": _to_int(box.get("y", 0), 0),
                    "w": _to_int(box.get("w", 0), 0),
                    "h": _to_int(box.get("h", 0), 0),
                }
        return cls(
            bg_alpha=_to_int(data.get("bg_alpha", 255), 255),
            boxes_abs_out=boxes,
        )


@dataclass
class LayoutConfig:
    layout_version: int = 1
    video_layout: str = "LR"
    hud_mode: str = "frame"
    hud_frame: HudFrameConfig = field(default_factory=HudFrameConfig)
    video_transform: VideoTransformConfig = field(default_factory=VideoTransformConfig)
    hud_free: HudFreeConfig = field(default_factory=HudFreeConfig)

    def to_dict(self) -> dict[str, Any]:
        return {
            "layout_version": int(self.layout_version),
            "video_layout": str(self.video_layout),
            "hud_mode": str(self.hud_mode),
            "hud_frame": self.hud_frame.to_dict(),
            "video_transform": self.video_transform.to_dict(),
            "hud_free": self.hud_free.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "LayoutConfig":
        data = d if isinstance(d, dict) else {}

        video_layout = str(data.get("video_layout") or "LR").strip().upper()
        if video_layout not in ("LR", "TB"):
            video_layout = "LR"

        hud_mode = str(data.get("hud_mode") or "frame").strip().lower()
        if hud_mode not in ("frame", "free"):
            hud_mode = "frame"

        return cls(
            layout_version=_to_int(data.get("layout_version", 1), 1),
            video_layout=video_layout,
            hud_mode=hud_mode,
            hud_frame=HudFrameConfig.from_dict(data.get("hud_frame") if isinstance(data.get("hud_frame"), dict) else {}),
            video_transform=VideoTransformConfig.from_dict(
                data.get("video_transform") if isinstance(data.get("video_transform"), dict) else {},
                video_layout=video_layout,
            ),
            hud_free=HudFreeConfig.from_dict(data.get("hud_free") if isinstance(data.get("hud_free"), dict) else {}),
        )


def migrate_layout_contract_dict(data: dict[str, Any]) -> bool:
    if not isinstance(data, dict):
        return False

    cfg = LayoutConfig.from_dict(data)
    migrated = False

    migrated = _set_if_changed(data, "layout_version", int(cfg.layout_version)) or migrated
    migrated = _set_if_changed(data, "video_layout", str(cfg.video_layout)) or migrated
    migrated = _set_if_changed(data, "hud_mode", str(cfg.hud_mode)) or migrated

    hud_frame_value, changed = _merge_known_keys(data.get("hud_frame"), cfg.hud_frame.to_dict())
    if changed:
        data["hud_frame"] = hud_frame_value
        migrated = True

    video_transform_value, changed = _merge_known_keys(data.get("video_transform"), cfg.video_transform.to_dict())
    if changed:
        data["video_transform"] = video_transform_value
        migrated = True

    hud_free_existing = data.get("hud_free")
    hud_free_value, changed = _merge_known_keys(
        hud_free_existing,
        {
            "bg_alpha": int(cfg.hud_free.bg_alpha),
        },
    )
    if changed:
        migrated = True

    boxes_existing = None
    if isinstance(hud_free_existing, dict):
        boxes_existing = hud_free_existing.get("boxes_abs_out")
    boxes_value = dict(boxes_existing) if isinstance(boxes_existing, dict) else {}
    if not isinstance(boxes_existing, dict):
        migrated = True
    for hud_key, box in cfg.hud_free.boxes_abs_out.items():
        merged_box, box_changed = _merge_known_keys(boxes_value.get(hud_key), box)
        if box_changed:
            boxes_value[hud_key] = merged_box
            migrated = True
    hud_free_value["boxes_abs_out"] = boxes_value
    data["hud_free"] = hud_free_value

    return migrated


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
    layout_config: LayoutConfig = field(default_factory=LayoutConfig)

    def to_dict(self) -> dict[str, Any]:
        out = {
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
        out.update(self.layout_config.to_dict())
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Profile":
        data = d if isinstance(d, dict) else {}
        videos_raw = data.get("videos")
        csvs_raw = data.get("csvs")
        starts_raw = data.get("startframes")
        ends_raw = data.get("endframes")
        layout = LayoutConfig.from_dict(data)
        return cls(
            version=data.get("version", 1),
            videos=[str(v) for v in (videos_raw if isinstance(videos_raw, list) else [])],
            csvs=[str(v) for v in (csvs_raw if isinstance(csvs_raw, list) else [])],
            startframes={str(k): _to_int(v, 0) for k, v in (starts_raw.items() if isinstance(starts_raw, dict) else [])},
            endframes={str(k): _to_int(v, 0) for k, v in (ends_raw.items() if isinstance(ends_raw, dict) else [])},
            output=OutputFormat.from_dict(data.get("output") if isinstance(data.get("output"), dict) else {}),
            hud_layout_data=data.get("hud_layout_data") if isinstance(data.get("hud_layout_data"), dict) else {},
            png_view_data=data.get("png_view_data") if isinstance(data.get("png_view_data"), dict) else {},
            layout_config=layout,
        )


@dataclass
class AppModel:
    output: OutputFormat = field(default_factory=OutputFormat)
    hud_layout: HudLayoutState = field(default_factory=HudLayoutState)
    png_view: PngViewState = field(default_factory=PngViewState)
    layout_config: LayoutConfig = field(default_factory=LayoutConfig)


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
    hud_pedals: dict[str, Any] = field(
        default_factory=lambda: {
            "sample_mode": "time",
            "abs_debounce_ms": 60,
            "max_brake_delay_distance": 0.003,
            "max_brake_delay_pressure": 35.0,
        }
    )
    png_view_key: str = ""
    png_view_state: dict[str, Any] = field(default_factory=lambda: {"L": {}, "R": {}})
    hud_layout_data: dict[str, Any] = field(default_factory=dict)
    png_view_data: dict[str, Any] = field(default_factory=dict)
    layout_config: LayoutConfig = field(default_factory=LayoutConfig)

    def to_dict(self) -> dict[str, Any]:
        out = {
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
            "hud_pedals": self.hud_pedals,
            "png_view_key": self.png_view_key,
            "png_view_state": self.png_view_state,
            "hud_layout_data": self.hud_layout_data,
            "png_view_data": self.png_view_data,
        }
        out.update(self.layout_config.to_dict())
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "RenderPayload":
        data = d if isinstance(d, dict) else {}
        return cls(
            version=data.get("version", 1),
            videos=list(data.get("videos", [])) if isinstance(data.get("videos"), list) else [],
            csvs=list(data.get("csvs", [])) if isinstance(data.get("csvs"), list) else [],
            slow_video=str(data.get("slow_video") or ""),
            fast_video=str(data.get("fast_video") or ""),
            out_video=str(data.get("out_video") or ""),
            output=OutputFormat.from_dict(data.get("output") if isinstance(data.get("output"), dict) else {}),
            hud_enabled=data.get("hud_enabled") if isinstance(data.get("hud_enabled"), dict) else {},
            hud_boxes=data.get("hud_boxes") if isinstance(data.get("hud_boxes"), dict) else {},
            hud_window=data.get("hud_window") if isinstance(data.get("hud_window"), dict) else {},
            hud_speed=data.get("hud_speed") if isinstance(data.get("hud_speed"), dict) else {},
            hud_curve_points=data.get("hud_curve_points") if isinstance(data.get("hud_curve_points"), dict) else {},
            hud_gear_rpm=data.get("hud_gear_rpm") if isinstance(data.get("hud_gear_rpm"), dict) else {},
            hud_pedals=data.get("hud_pedals") if isinstance(data.get("hud_pedals"), dict) else {},
            png_view_key=str(data.get("png_view_key") or ""),
            png_view_state=data.get("png_view_state") if isinstance(data.get("png_view_state"), dict) else {"L": {}, "R": {}},
            hud_layout_data=data.get("hud_layout_data") if isinstance(data.get("hud_layout_data"), dict) else {},
            png_view_data=data.get("png_view_data") if isinstance(data.get("png_view_data"), dict) else {},
            layout_config=LayoutConfig.from_dict(data),
        )
