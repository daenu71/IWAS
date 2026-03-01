"""Output geometry calculation for video/hud layout composition."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

from core.models import LayoutConfig


@dataclass(frozen=True)
class Rect:
    """Container and behavior for Rect."""
    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class OutputGeometry:
    """Container and behavior for Output Geometry."""
    W: int
    H: int
    hud: int
    left_w: int
    right_w: int
    left_x: int
    left_y: int
    fast_out_x: int
    fast_out_y: int
    video_slow_rect: Rect
    video_fast_rect: Rect
    hud_rects: tuple[Rect, ...]
    video_layout: str
    hud_mode: str


def geometry_signature(geom: OutputGeometry) -> tuple[Any, ...]:
    """Implement geometry signature logic."""
    hud_items: list[tuple[int, int, int, int]] = []
    for r in tuple(geom.hud_rects):
        hud_items.append((int(r.x), int(r.y), int(r.w), int(r.h)))
    return (
        int(geom.W),
        int(geom.H),
        str(geom.video_layout),
        str(geom.hud_mode),
        int(geom.video_slow_rect.x),
        int(geom.video_slow_rect.y),
        int(geom.video_slow_rect.w),
        int(geom.video_slow_rect.h),
        int(geom.video_fast_rect.x),
        int(geom.video_fast_rect.y),
        int(geom.video_fast_rect.w),
        int(geom.video_fast_rect.h),
        tuple(hud_items),
    )


def format_output_geometry_dump(geom: OutputGeometry) -> str:
    """Format output geometry dump."""
    def _fmt_rect(r: Rect) -> str:
        """Implement fmt rect logic."""
        return f"(x={int(r.x)},y={int(r.y)},w={int(r.w)},h={int(r.h)})"

    if not tuple(geom.hud_rects):
        hud_s = "none"
    else:
        hud_s = ",".join(_fmt_rect(r) for r in tuple(geom.hud_rects))

    return (
        f"[geom] out={int(geom.W)}x{int(geom.H)} "
        f"video_layout={str(geom.video_layout)} hud_mode={str(geom.hud_mode)} "
        f"slow={_fmt_rect(geom.video_slow_rect)} "
        f"fast={_fmt_rect(geom.video_fast_rect)} "
        f"hud={hud_s}"
    )


def _parse_video_layout(layout_cfg: LayoutConfig | None) -> str:
    """Parse video layout."""
    raw = ""
    if isinstance(layout_cfg, LayoutConfig):
        raw = str(layout_cfg.video_layout or "")
    video_layout = raw.strip().upper() or "LR"
    if video_layout not in ("LR", "TB"):
        video_layout = "LR"
    return video_layout


def _parse_hud_mode(layout_cfg: LayoutConfig | None) -> str:
    """Parse hud mode."""
    raw = ""
    if isinstance(layout_cfg, LayoutConfig):
        raw = str(layout_cfg.hud_mode or "")
    hud_mode = raw.strip().lower() or "frame"
    if hud_mode not in ("frame", "free"):
        hud_mode = "frame"
    return hud_mode


def _parse_hud_frame(layout_cfg: LayoutConfig | None) -> tuple[str, str, int | None]:
    """Parse hud frame."""
    orientation = "vertical"
    anchor = "center"
    thickness: int | None = None
    if isinstance(layout_cfg, LayoutConfig):
        try:
            orientation = str(layout_cfg.hud_frame.orientation or "vertical").strip().lower()
        except Exception:
            orientation = "vertical"
        if orientation not in ("vertical", "horizontal"):
            orientation = "vertical"

        default_anchor = "center" if orientation == "vertical" else "bottom"
        try:
            anchor = str(layout_cfg.hud_frame.anchor or default_anchor).strip().lower()
        except Exception:
            anchor = default_anchor
        if orientation == "vertical":
            if anchor not in ("left", "center", "right"):
                anchor = "center"
        else:
            if anchor not in ("top", "center", "bottom", "top_bottom"):
                anchor = "bottom"

        raw_thickness = getattr(layout_cfg.hud_frame, "frame_thickness_px", None)
        if raw_thickness is not None:
            try:
                thickness = int(raw_thickness)
            except Exception:
                thickness = None
    return orientation, anchor, thickness


def _split_video_rects(video_layout: str, area: Rect) -> tuple[Rect, Rect]:
    """Implement split video rects logic."""
    if video_layout == "TB":
        slow_h = int(area.h) // 2
        fast_h = int(area.h) - slow_h
        if slow_h <= 10 or fast_h <= 10:
            raise RuntimeError("oben/unten sind zu klein. preset vergroessern.")
        return (
            Rect(int(area.x), int(area.y), int(area.w), int(slow_h)),
            Rect(int(area.x), int(area.y) + int(slow_h), int(area.w), int(fast_h)),
        )

    slow_w = int(area.w) // 2
    fast_w = int(area.w) - slow_w
    if slow_w <= 10 or fast_w <= 10:
        raise RuntimeError("links/rechts sind zu klein. preset vergroessern.")
    return (
        Rect(int(area.x), int(area.y), int(slow_w), int(area.h)),
        Rect(int(area.x) + int(slow_w), int(area.y), int(fast_w), int(area.h)),
    )


def _geom_debug_enabled() -> bool:
    """Implement geom debug enabled logic."""
    raw = str(os.environ.get("IRVC_DEBUG") or os.environ.get("IRVC_DEBUG_SWALLOWED") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _rect_dbg(r: Rect) -> str:
    """Implement rect dbg logic."""
    return f"(x={int(r.x)},y={int(r.y)},w={int(r.w)},h={int(r.h)})"


def _validate_rect_inside_output(name: str, r: Rect, W: int, H: int) -> None:
    """Validate rect inside output."""
    if int(r.w) <= 0 or int(r.h) <= 0:
        raise RuntimeError(f"{name} ungueltig: w/h <= 0")
    x0 = int(r.x)
    y0 = int(r.y)
    x1 = int(r.x) + int(r.w)
    y1 = int(r.y) + int(r.h)
    if x0 < 0 or y0 < 0 or x1 > int(W) or y1 > int(H):
        raise RuntimeError(f"{name} ungueltig: ausserhalb Output")


def _validate_rects_no_overlap(name_a: str, a: Rect, name_b: str, b: Rect) -> None:
    """Validate rects no overlap."""
    ax0 = int(a.x)
    ay0 = int(a.y)
    ax1 = int(a.x) + int(a.w)
    ay1 = int(a.y) + int(a.h)
    bx0 = int(b.x)
    by0 = int(b.y)
    bx1 = int(b.x) + int(b.w)
    by1 = int(b.y) + int(b.h)
    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)
    if ix1 > ix0 and iy1 > iy0:
        raise RuntimeError(f"{name_a}/{name_b} ungueltig: ueberlappen")


def split_weighted_lengths(total: int, weights: list[float]) -> list[int]:
    """Implement split weighted lengths logic."""
    t = max(0, int(total))
    n = len(weights)
    if n <= 0:
        return []
    norm: list[float] = []
    for w in weights:
        ww = float(w)
        if ww <= 0.0:
            ww = 1.0
        norm.append(ww)
    w_sum = float(sum(norm))
    if w_sum <= 0.0:
        norm = [1.0] * n
        w_sum = float(n)
    out = [int(float(t) * (w / w_sum)) for w in norm]
    remaining = int(t - sum(out))
    if remaining > 0:
        for i in range(remaining):
            out[i % n] += 1
    return out


def _hud_type_from_box(box: dict[str, Any]) -> str:
    """Implement hud type from box logic."""
    try:
        return str(box.get("type") or "")
    except Exception:
        return ""


def vertical_fit_weight_for_hud_key(hud_key: str) -> float:
    """Implement vertical fit weight for hud key logic."""
    if hud_key == "Throttle / Brake":
        return 1.5
    elif hud_key in ("Speed", "Gear & RPM"):
        return 0.5
    else:
        return 1.0


def _layout_horizontal_frame_row(
    items: list[dict[str, Any]],
    row_rect: Rect,
) -> list[tuple[dict[str, Any], Rect]]:
    """Implement layout horizontal frame row logic."""
    if not items:
        return []

    speed_idx = -1
    gear_idx = -1
    for idx, b in enumerate(items):
        key = _hud_type_from_box(b)
        if key == "Speed" and speed_idx < 0:
            speed_idx = int(idx)
        elif key == "Gear & RPM" and gear_idx < 0:
            gear_idx = int(idx)

    can_group = (speed_idx >= 0) and (gear_idx >= 0)
    pair_inserted = False
    columns: list[tuple[list[dict[str, Any]], float]] = []
    for b in items:
        key = _hud_type_from_box(b)
        if can_group and key in ("Speed", "Gear & RPM"):
            if not pair_inserted:
                if speed_idx <= gear_idx:
                    top_item = items[speed_idx]
                    bottom_item = items[gear_idx]
                else:
                    top_item = items[gear_idx]
                    bottom_item = items[speed_idx]
                columns.append(([top_item, bottom_item], 1.0))
                pair_inserted = True
            continue
        if key in ("Speed", "Gear & RPM"):
            columns.append(([b], 0.5))
        else:
            columns.append(([b], 1.0))

    widths = split_weighted_lengths(int(row_rect.w), [float(w) for _, w in columns])
    cur_x = int(row_rect.x)
    row_y = int(row_rect.y)
    row_h = int(row_rect.h)
    placed: list[tuple[dict[str, Any], Rect]] = []
    for i, (col_items, _weight) in enumerate(columns):
        col_w = int(widths[i]) if i < len(widths) else 0
        if len(col_items) == 2:
            h_top = int(row_h) // 2
            h_bottom = int(row_h) - int(h_top)
            placed.append((col_items[0], Rect(int(cur_x), int(row_y), int(col_w), int(h_top))))
            placed.append((col_items[1], Rect(int(cur_x), int(row_y) + int(h_top), int(col_w), int(h_bottom))))
        else:
            placed.append((col_items[0], Rect(int(cur_x), int(row_y), int(col_w), int(row_h))))
        cur_x += int(col_w)
    return placed


def split_horizontal_top_bottom_rows(
    active_boxes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Implement split horizontal top bottom rows logic."""
    items = list(active_boxes or [])
    if not items:
        return [], []

    speed_idx = -1
    gear_idx = -1
    for idx, b in enumerate(items):
        key = _hud_type_from_box(b)
        if key == "Speed" and speed_idx < 0:
            speed_idx = int(idx)
        elif key == "Gear & RPM" and gear_idx < 0:
            gear_idx = int(idx)

    can_group = (speed_idx >= 0) and (gear_idx >= 0)
    pair_first_idx = min(speed_idx, gear_idx) if can_group else -1
    pair_second_idx = max(speed_idx, gear_idx) if can_group else -1

    # Jede Unit entspricht genau einer horizontalen Spalte.
    units: list[list[dict[str, Any]]] = []
    for idx, b in enumerate(items):
        if can_group and idx == pair_first_idx:
            units.append([items[pair_first_idx], items[pair_second_idx]])
            continue
        if can_group and idx == pair_second_idx:
            continue
        units.append([b])

    top_units: list[list[dict[str, Any]]] = []
    bottom_units: list[list[dict[str, Any]]] = []
    for unit in units:
        if len(top_units) < len(bottom_units):
            top_units.append(unit)
        elif len(bottom_units) < len(top_units):
            bottom_units.append(unit)
        else:
            top_units.append(unit)

    def _expand(row_units: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
        """Implement expand logic."""
        row: list[dict[str, Any]] = []
        for unit in row_units:
            row.extend(unit)
        return row

    return _expand(top_units), _expand(bottom_units)


def layout_horizontal_frame_hud_boxes(
    *,
    active_boxes: list[dict[str, Any]],
    frame_rects: tuple[Rect, ...],
    anchor: str,
) -> list[tuple[dict[str, Any], Rect]]:
    """Implement layout horizontal frame hud boxes logic."""
    rects = tuple(frame_rects or ())
    if not active_boxes or not rects:
        return []

    rect_items = sorted(rects, key=lambda r: (int(r.y), int(r.x)))
    if str(anchor) == "top_bottom" and len(rect_items) >= 2:
        top_items, bottom_items = split_horizontal_top_bottom_rows(active_boxes)
        placed: list[tuple[dict[str, Any], Rect]] = []
        placed.extend(_layout_horizontal_frame_row(top_items, rect_items[0]))
        placed.extend(_layout_horizontal_frame_row(bottom_items, rect_items[1]))
        return placed

    return _layout_horizontal_frame_row(active_boxes, rect_items[0])


def _debug_geometry_dump(
    *,
    W: int,
    H: int,
    hud_mode: str,
    video_layout: str,
    orientation: str,
    anchor: str,
    hud_px: int,
    video_area_rect: Rect | None,
    video_slow_rect: Rect,
    video_fast_rect: Rect,
    hud_rects: tuple[Rect, ...],
) -> None:
    """Implement debug geometry dump logic."""
    if not _geom_debug_enabled():
        return
    hud_s = "none" if not hud_rects else ",".join(_rect_dbg(r) for r in hud_rects)
    video_area_s = "none" if video_area_rect is None else _rect_dbg(video_area_rect)
    print(
        "[geomdbg] "
        f"out={int(W)}x{int(H)} mode={str(hud_mode)} layout={str(video_layout)} "
        f"orientation={str(orientation)} anchor={str(anchor)} hud_px={int(hud_px)} "
        f"video_area={video_area_s} "
        f"slow={_rect_dbg(video_slow_rect)} fast={_rect_dbg(video_fast_rect)} hud={hud_s}",
        flush=True,
    )


def build_output_geometry_for_size(
    out_w: int,
    out_h: int,
    hud_width_px: int,
    layout_config: LayoutConfig | None = None,
) -> OutputGeometry:
    """Build and return output geometry for size."""
    W = int(out_w)
    H = int(out_h)
    if W <= 0 or H <= 0:
        raise RuntimeError("output size ungueltig.")

    video_layout = _parse_video_layout(layout_config)
    hud_mode = _parse_hud_mode(layout_config)
    orientation = "free"
    anchor = "free"
    hud_px = int(hud_width_px)
    video_area_rect: Rect | None = Rect(0, 0, W, H)

    if hud_mode == "free":
        video_slow_rect, video_fast_rect = _split_video_rects(video_layout, video_area_rect)
        hud_rects: tuple[Rect, ...] = ()
    else:
        orientation, anchor, frame_thickness_px = _parse_hud_frame(layout_config)
        hud = int(frame_thickness_px if frame_thickness_px is not None else hud_width_px)
        if hud < 0:
            hud = 0
        hud_px = int(hud)

        if hud <= 0:
            video_area_rect = Rect(0, 0, W, H)
            video_slow_rect, video_fast_rect = _split_video_rects(video_layout, video_area_rect)
            hud_rects = ()
        elif orientation == "horizontal" and anchor == "top_bottom":
            if (2 * hud) >= H - 2:
                raise RuntimeError("hud_frame.frame_thickness_px ist zu gross fuer top_bottom.")
            hud_rects = (
                Rect(0, 0, W, hud),
                Rect(0, H - hud, W, hud),
            )
            video_area_rect = Rect(0, hud, W, H - (2 * hud))
            video_slow_rect, video_fast_rect = _split_video_rects(video_layout, video_area_rect)
        elif orientation == "vertical" and anchor == "center" and video_layout == "LR":
            if hud >= W - 2:
                raise RuntimeError("hud_frame.frame_thickness_px ist zu gross fuer vertical/center.")
            left_w = (W - hud) // 2
            right_w = (W - hud) - left_w
            if left_w <= 10 or right_w <= 10:
                raise RuntimeError("links/rechts sind zu klein. hud/frame reduzieren oder preset vergroessern.")
            video_slow_rect = Rect(0, 0, left_w, H)
            video_fast_rect = Rect(left_w + hud, 0, right_w, H)
            hud_rects = (Rect(left_w, 0, hud, H),)
            video_area_rect = None
        elif orientation == "horizontal" and anchor == "center" and video_layout == "TB":
            if hud >= H - 2:
                raise RuntimeError("hud_frame.frame_thickness_px ist zu gross fuer horizontal/center.")
            top_h = (H - hud) // 2
            bottom_h = (H - hud) - top_h
            if top_h <= 10 or bottom_h <= 10:
                raise RuntimeError("oben/unten sind zu klein. hud/frame reduzieren oder preset vergroessern.")
            video_slow_rect = Rect(0, 0, W, top_h)
            video_fast_rect = Rect(0, top_h + hud, W, bottom_h)
            hud_rects = (Rect(0, top_h, W, hud),)
            video_area_rect = None
        elif orientation == "vertical":
            if hud >= W - 2:
                raise RuntimeError("hud_frame.frame_thickness_px ist zu gross fuer vertical.")
            hud_x = 0
            content_x = hud
            content_w = W - hud
            if anchor == "right":
                hud_x = W - hud
                content_x = 0
            elif anchor == "center":
                # In Kombination mit TB waere die Restflaeche nicht zusammenhaengend.
                hud_x = W - hud
                content_x = 0
            hud_rects = (Rect(hud_x, 0, hud, H),)
            video_area_rect = Rect(content_x, 0, content_w, H)
            video_slow_rect, video_fast_rect = _split_video_rects(video_layout, video_area_rect)
        else:
            if hud >= H - 2:
                raise RuntimeError("hud_frame.frame_thickness_px ist zu gross fuer horizontal.")
            hud_y = 0
            content_y = hud
            content_h = H - hud
            if anchor == "bottom":
                hud_y = H - hud
                content_y = 0
            elif anchor == "center":
                # In Kombination mit LR waere die Restflaeche nicht zusammenhaengend.
                hud_y = H - hud
                content_y = 0
            hud_rects = (Rect(0, hud_y, W, hud),)
            video_area_rect = Rect(0, content_y, W, content_h)
            video_slow_rect, video_fast_rect = _split_video_rects(video_layout, video_area_rect)

    _validate_rect_inside_output("video_slow_rect", video_slow_rect, W, H)
    _validate_rect_inside_output("video_fast_rect", video_fast_rect, W, H)
    _validate_rects_no_overlap("video_slow_rect", video_slow_rect, "video_fast_rect", video_fast_rect)
    for i, hr in enumerate(tuple(hud_rects)):
        _validate_rect_inside_output(f"hud_rect[{int(i)}]", hr, W, H)

    _debug_geometry_dump(
        W=W,
        H=H,
        hud_mode=hud_mode,
        video_layout=video_layout,
        orientation=orientation,
        anchor=anchor,
        hud_px=hud_px,
        video_area_rect=video_area_rect,
        video_slow_rect=video_slow_rect,
        video_fast_rect=video_fast_rect,
        hud_rects=hud_rects,
    )

    legacy_hud = 0
    if len(hud_rects) == 1:
        hr = hud_rects[0]
        if int(hr.h) == H:
            legacy_hud = int(hr.w)

    return OutputGeometry(
        W=W,
        H=H,
        hud=int(legacy_hud),
        left_w=int(video_slow_rect.w),
        right_w=int(video_fast_rect.w),
        left_x=int(video_slow_rect.x),
        left_y=int(video_slow_rect.y),
        fast_out_x=int(video_fast_rect.x),
        fast_out_y=int(video_fast_rect.y),
        video_slow_rect=video_slow_rect,
        video_fast_rect=video_fast_rect,
        hud_rects=hud_rects,
        video_layout=video_layout,
        hud_mode=hud_mode,
    )
