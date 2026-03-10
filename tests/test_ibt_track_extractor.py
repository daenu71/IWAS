from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.coaching.ibt_track_extractor import (  # noqa: E402
    _compute_normals,
    _read_track_key,
    extract_track_geometry,
    read_ibt_session_metadata,
)


def _make_session_yaml(
    track_display: str = "Sebring",
    track_config: str = "Full Course",
    *,
    track_name: str | None = None,
    track_width: float | None = None,
    track_id: int | None = None,
    build_version: str | None = None,
) -> str:
    lines = [
        "WeekendInfo:",
        f" TrackDisplayName: {track_display}",
        f" TrackConfigName: {track_config}",
    ]
    if track_name is not None:
        lines.append(f" TrackName: {track_name}")
    if track_width is not None:
        lines.append(f" TrackWidth: {track_width}")
    if track_id is not None:
        lines.append(f" TrackID: {track_id}")
    if build_version is not None:
        lines.append(f" BuildVersion: {build_version}")
    return "\n".join(lines)


class _FakeIRSDK:
    def __init__(
        self,
        frames: list[dict],
        *,
        session_yaml: str = "",
        weekend_info: dict | None = None,
    ) -> None:
        self._frames = frames
        self._idx = 0
        self.session_info = session_yaml
        self._weekend_info = weekend_info or {}

    def startup(self, test_file: str | None = None, **kwargs) -> bool:
        return True

    def shutdown(self) -> None:
        pass

    def __getitem__(self, key: str):
        if key == "WeekendInfo":
            return self._weekend_info
        if self._idx >= len(self._frames):
            return None
        return self._frames[self._idx].get(key)

    def parse_to(self, time_secs: float) -> bool:
        for idx in range(self._idx + 1, len(self._frames)):
            if self._frames[idx].get("SessionTime", -1) >= time_secs:
                self._idx = idx
                return True
        return False


def _make_straight_frames(n: int = 600, vx: float = 50.0) -> list[dict]:
    dt = 1.0 / 60.0
    frames: list[dict] = []
    for idx in range(n):
        frames.append(
            {
                "SessionTime": idx * dt,
                "VelocityX": vx,
                "VelocityY": 0.0,
                "LapDistPct": idx / n,
            }
        )
    return frames


def _make_geo_frames(samples: list[tuple[float, float, float]]) -> list[dict]:
    dt = 1.0 / 60.0
    frames: list[dict] = []
    for idx, (lap_dist_pct, lat, lon) in enumerate(samples):
        frames.append(
            {
                "SessionTime": idx * dt,
                "LapDistPct": lap_dist_pct,
                "Lat": lat,
                "Lon": lon,
            }
        )
    return frames


def _fake_irsdk_module(fake_ir: _FakeIRSDK) -> types.ModuleType:
    mod = types.ModuleType("irsdk")
    mod.IRSDK = MagicMock(return_value=fake_ir)  # type: ignore[attr-defined]
    return mod


def test_extract_produces_valid_json(tmp_path: Path) -> None:
    frames = _make_geo_frames(
        [
            (0.00, 47.000000, 8.000000),
            (0.25, 47.000250, 8.000000),
            (0.50, 47.000250, 8.000350),
            (0.75, 47.000000, 8.000350),
        ]
    )
    fake_ir = _FakeIRSDK(
        frames=frames,
        session_yaml=_make_session_yaml("Sebring", "Full Course", build_version="2026.03"),
    )
    ibt_file = tmp_path / "fake.ibt"
    ibt_file.touch()

    with patch.dict(sys.modules, {"irsdk": _fake_irsdk_module(fake_ir)}):
        out = extract_track_geometry(ibt_file, tmp_path)

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["track_key"] == "Sebring__Full Course"
    assert payload["source_type"] == "ibt"
    assert payload["source"] == "ibt_telemetry"
    assert payload["source_path"] == str(ibt_file)
    assert payload["iracing_build"] == "2026.03"
    assert payload["geometry_kind"] == "centerline_only"
    assert payload["position_source"] == "latlon"
    assert payload["distance_source"] == "LapDistPct"
    assert isinstance(payload["center_line"], list) and len(payload["center_line"]) == 4
    assert payload["left_edge"] == []
    assert payload["right_edge"] == []

    first = payload["center_line"][0]
    assert set(first) >= {"lap_dist_pct", "lat", "lon", "x_m", "y_m"}
    assert first["lap_dist_pct"] == pytest.approx(0.0)
    assert first["lat"] == pytest.approx(47.0)
    assert first["lon"] == pytest.approx(8.0)
    assert first["x_m"] == pytest.approx(0.0)
    assert first["y_m"] == pytest.approx(0.0)


def test_extract_sorts_centerline_by_lap_dist_pct(tmp_path: Path) -> None:
    fake_ir = _FakeIRSDK(
        frames=_make_geo_frames(
            [
                (0.60, 47.000600, 8.000000),
                (0.20, 47.000200, 8.000000),
                (0.80, 47.000800, 8.000000),
                (0.40, 47.000400, 8.000000),
            ]
        ),
        session_yaml=_make_session_yaml("Daytona", "Road"),
    )
    ibt_file = tmp_path / "fake.ibt"
    ibt_file.touch()

    with patch.dict(sys.modules, {"irsdk": _fake_irsdk_module(fake_ir)}):
        out = extract_track_geometry(ibt_file, tmp_path)

    payload = json.loads(out.read_text(encoding="utf-8"))
    lap_dist_pct = [point["lap_dist_pct"] for point in payload["center_line"]]
    latitudes = [point["lat"] for point in payload["center_line"]]

    assert lap_dist_pct == pytest.approx(sorted(lap_dist_pct))
    assert latitudes == pytest.approx([47.000200, 47.000400, 47.000600, 47.000800])


def test_read_ibt_session_metadata_exposes_track_identifiers(tmp_path: Path) -> None:
    fake_ir = _FakeIRSDK(
        frames=[],
        session_yaml=_make_session_yaml(
            "Road Atlanta",
            "Full Course",
            track_name="road_atlanta",
            track_id=123,
            build_version="2026.02",
        ),
    )
    ibt_file = tmp_path / "road_atlanta.ibt"
    ibt_file.touch()

    with patch.dict(sys.modules, {"irsdk": _fake_irsdk_module(fake_ir)}):
        metadata = read_ibt_session_metadata(ibt_file)

    assert metadata.track_key == "Road Atlanta__Full Course"
    assert metadata.track_display_name == "Road Atlanta"
    assert metadata.track_config_name == "Full Course"
    assert metadata.track_name == "road_atlanta"
    assert metadata.track_id == 123
    assert metadata.iracing_build == "2026.02"


def test_track_key_extraction() -> None:
    fake_ir = _FakeIRSDK(
        frames=[],
        session_yaml=_make_session_yaml("Sebring", "Full Course"),
    )
    assert _read_track_key(fake_ir) == "Sebring__Full Course"


def test_track_key_no_config() -> None:
    fake_ir = _FakeIRSDK(
        frames=[],
        session_yaml=_make_session_yaml("Road Atlanta", ""),
    )
    assert _read_track_key(fake_ir) == "Road Atlanta"


def test_normals_orthogonal_to_tangents() -> None:
    pts = np.column_stack([np.linspace(0, 100, 100), np.zeros(100)])
    normals = _compute_normals(pts)
    np.testing.assert_allclose(normals[:, 0], 0.0, atol=1e-10)
    np.testing.assert_allclose(normals[:, 1], 1.0, atol=1e-10)


def test_missing_latlon_skips_geometry_export(tmp_path: Path) -> None:
    fake_ir = _FakeIRSDK(
        frames=_make_straight_frames(),
        session_yaml=_make_session_yaml("Unknown", ""),
    )
    ibt_file = tmp_path / "fake.ibt"
    ibt_file.touch()

    with patch.dict(sys.modules, {"irsdk": _fake_irsdk_module(fake_ir)}):
        with pytest.raises(RuntimeError, match="Lat/Lon \\+ LapDistPct"):
            extract_track_geometry(ibt_file, tmp_path)

    assert list(tmp_path.rglob("track_road_geometry.json")) == []
