"""Targeted tests for road-geometry rendering in track_geometry.py."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.coaching.lap_view_model import CornerInfo  # noqa: E402
from core.coaching.track_geometry import (  # noqa: E402
    _line_flat,
    _transform_zoom,
    render_corner_zoom,
    render_trackmap,
)


class FakeCanvas:
    def __init__(self) -> None:
        self.items: list[tuple[str, tuple, dict]] = []
        self.bindings: list[tuple[str, str]] = []
        self._fit_context = None
        self._sprite_refs = []

    def delete(self, *args) -> None:
        self.items.append(("delete", args, {}))

    def create_line(self, *args, **kwargs) -> int:
        self.items.append(("line", args, kwargs))
        return len(self.items)

    def create_polygon(self, *args, **kwargs) -> int:
        self.items.append(("polygon", args, kwargs))
        return len(self.items)

    def create_oval(self, *args, **kwargs) -> int:
        self.items.append(("oval", args, kwargs))
        return len(self.items)

    def create_text(self, *args, **kwargs) -> int:
        self.items.append(("text", args, kwargs))
        return len(self.items)

    def create_image(self, *args, **kwargs) -> int:
        self.items.append(("image", args, kwargs))
        return len(self.items)

    def tag_bind(self, tag, event, _callback) -> None:
        self.bindings.append((str(tag), str(event)))

    def bind(self, event, _callback) -> None:
        self.bindings.append(("canvas", str(event)))

    def cget(self, key: str) -> str:
        if key == "background":
            return "#000000"
        return ""

    def winfo_rgb(self, color: str) -> tuple[int, int, int]:
        if color == "#000000":
            return (0, 0, 0)
        return (65535, 65535, 65535)


def _sample_xy() -> np.ndarray:
    return np.array(
        [
            [0.10, 0.50],
            [0.30, 0.65],
            [0.50, 0.72],
            [0.70, 0.65],
            [0.90, 0.50],
        ],
        dtype=np.float64,
    )


def _sample_road_geometry(
    *,
    source_type: str = "ibt",
    center_line_only: bool = False,
    center_line: np.ndarray | None = None,
    center_line_as_dicts: bool = False,
) -> dict[str, object]:
    xy = _sample_xy() if center_line is None else np.asarray(center_line, dtype=np.float64)
    return {
        "track_key": "TestTrack__Full__unknown_class",
        "source_type": source_type,
        "center_line": _ibt_center_line_points(xy) if center_line_as_dicts else xy.tolist(),
        "left_edge": [] if center_line_only else (xy + np.array([0.0, 0.05])).tolist(),
        "right_edge": [] if center_line_only else (xy - np.array([0.0, 0.05])).tolist(),
    }


def _ibt_center_line_points(xy: np.ndarray) -> list[dict[str, float]]:
    count = len(xy)
    denom = max(count - 1, 1)
    payload: list[dict[str, float]] = []
    for idx, (x_m, y_m) in enumerate(xy.tolist()):
        payload.append(
            {
                "lap_dist_pct": idx / denom,
                "lat": 47.0 + idx * 1.0e-4,
                "lon": 8.0 + idx * 1.0e-4,
                "x_m": x_m,
                "y_m": y_m,
            }
        )
    return payload


def _line_item(canvas: FakeCanvas, tag: str) -> tuple[tuple, dict]:
    for item_type, args, kwargs in canvas.items:
        if item_type == "line" and tag in kwargs.get("tags", ()):
            return args, kwargs
    raise AssertionError(f"missing canvas line item for tag={tag}")


def test_render_trackmap_with_road_geometry_draws_extra_canvas_items() -> None:
    xy = _sample_xy()
    lap_dist_pct = np.linspace(0.0, 1.0, len(xy))
    corner = CornerInfo(
        corner_id=1,
        start_lapdist_pct=0.20,
        end_lapdist_pct=0.80,
        corner_type="sweeper",
    )

    canvas_without_road = FakeCanvas()
    render_trackmap(
        canvas=canvas_without_road,
        xy=xy,
        corners=[corner],
        selected_corner_id=1,
        width=320,
        height=240,
        lap_dist_pct=lap_dist_pct,
    )

    canvas_with_road = FakeCanvas()
    render_trackmap(
        canvas=canvas_with_road,
        xy=xy,
        corners=[corner],
        selected_corner_id=1,
        width=320,
        height=240,
        road_geometry=_sample_road_geometry(),
        lap_dist_pct=lap_dist_pct,
    )

    assert any(
        item_type == "polygon" and "trackmap_road_band" in kwargs.get("tags", ())
        for item_type, _args, kwargs in canvas_with_road.items
    )
    assert len(canvas_with_road.items) > len(canvas_without_road.items)


def test_render_trackmap_respects_open_geometry_flag() -> None:
    xy = _sample_xy()
    canvas = FakeCanvas()

    render_trackmap(
        canvas=canvas,
        xy=xy,
        corners=[],
        selected_corner_id=None,
        width=320,
        height=240,
        is_closed=False,
    )

    line_items = [
        (args, kwargs)
        for item_type, args, kwargs in canvas.items
        if item_type == "line" and kwargs.get("tags") in (("track",), ("lapline",))
    ]

    assert len(line_items) == 2
    assert all(len(args[0]) == 2 * len(xy) for args, _kwargs in line_items)


def test_render_corner_zoom_with_road_geometry_draws_extra_canvas_items() -> None:
    xy = _sample_xy()
    lap_dist_pct = np.linspace(0.0, 1.0, len(xy))
    corner = CornerInfo(
        corner_id=1,
        start_lapdist_pct=0.20,
        end_lapdist_pct=0.80,
        corner_type="sweeper",
    )

    canvas_without_road = FakeCanvas()
    render_corner_zoom(
        canvas=canvas_without_road,
        xy=xy,
        corner=corner,
        events=[],
        width=320,
        height=240,
        lap_dist_pct=lap_dist_pct,
        lo=0.20,
        hi=0.80,
    )

    canvas_with_road = FakeCanvas()
    render_corner_zoom(
        canvas=canvas_with_road,
        xy=xy,
        corner=corner,
        events=[],
        width=320,
        height=240,
        road_geometry=_sample_road_geometry(),
        lap_dist_pct=lap_dist_pct,
        lo=0.20,
        hi=0.80,
    )

    assert any(
        item_type == "polygon" and "zoom_road_band" in kwargs.get("tags", ())
        for item_type, _args, kwargs in canvas_with_road.items
    )
    assert len(canvas_with_road.items) > len(canvas_without_road.items)


def test_render_corner_zoom_preserves_input_segment_without_extra_rotation() -> None:
    xy = _sample_xy()
    lap_dist_pct = np.linspace(0.0, 1.0, len(xy))
    corner = CornerInfo(
        corner_id=1,
        start_lapdist_pct=0.20,
        end_lapdist_pct=0.80,
        corner_type="sweeper",
    )
    canvas = FakeCanvas()

    render_corner_zoom(
        canvas=canvas,
        xy=xy,
        corner=corner,
        events=[],
        width=320,
        height=240,
        lap_dist_pct=lap_dist_pct,
        lo=0.20,
        hi=0.80,
    )

    lap_args, _lap_kwargs = _line_item(canvas, "zoom_lapline")
    indices = np.where((lap_dist_pct >= 0.20) & (lap_dist_pct <= 0.80))[0]
    seg_xy = xy[indices]
    x_min = float(np.min(seg_xy[:, 0]))
    y_min = float(np.min(seg_xy[:, 1]))
    seg_scale = max(float(np.max(seg_xy[:, 0]) - x_min), float(np.max(seg_xy[:, 1]) - y_min)) or 1.0
    seg_norm = np.column_stack(
        [
            (seg_xy[:, 0] - x_min) / seg_scale,
            (seg_xy[:, 1] - y_min) / seg_scale,
        ]
    )
    expected = _transform_zoom(seg_norm, 320, 240, 1.0, (0.0, 0.0), fit_context=canvas._fit_context).flatten().tolist()

    assert np.allclose(lap_args[0], expected)


def test_render_trackmap_accepts_center_line_only_road_geometry_and_logs_bbox(
    caplog,
) -> None:
    xy = _sample_xy()
    center_line = xy + np.array([0.02, 0.10], dtype=np.float64)
    lap_dist_pct = np.linspace(0.0, 1.0, len(xy))
    canvas = FakeCanvas()

    caplog.set_level("DEBUG", logger="core.coaching.track_geometry")
    render_trackmap(
        canvas=canvas,
        xy=xy,
        corners=[],
        selected_corner_id=None,
        width=320,
        height=240,
        road_geometry=_sample_road_geometry(
            center_line_only=True,
            center_line=center_line,
            center_line_as_dicts=True,
        ),
        lap_dist_pct=lap_dist_pct,
    )

    track_args, track_kwargs = _line_item(canvas, "track")
    lap_args, lap_kwargs = _line_item(canvas, "lapline")

    expected_track = _line_flat(
        _transform_zoom(center_line, 320, 240, 1.0, (0.0, 0.0), fit_context=canvas._fit_context),
        is_closed=True,
    )
    expected_lap = _line_flat(
        _transform_zoom(xy, 320, 240, 1.0, (0.0, 0.0), fit_context=canvas._fit_context),
        is_closed=True,
    )

    assert np.allclose(track_args[0], expected_track)
    assert np.allclose(lap_args[0], expected_lap)
    assert not np.allclose(track_args[0], lap_args[0])
    assert track_kwargs["width"] > lap_kwargs["width"]
    assert not any(
        item_type == "polygon" and "trackmap_road_band" in kwargs.get("tags", ())
        for item_type, _args, kwargs in canvas.items
    )
    assert "[trackmap_render]" in caplog.text
    assert "road_geometry_mode=center_line_only" in caplog.text
    assert "road_geometry_transform=as_saved" in caplog.text
    assert "consumer_rotation=none" in caplog.text
    assert "lap_points=5" in caplog.text
    assert "road_center_line_points=5" in caplog.text
    assert "road_center_line_min_x=" in caplog.text
    assert "bbox_width=" in caplog.text
    assert "canvas_target_rect=" in caplog.text
