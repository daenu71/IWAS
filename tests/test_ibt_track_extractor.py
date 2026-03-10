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
    build_ibt_inventory_report,
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


class _FakeVarHeader:
    def __init__(
        self,
        name: str,
        *,
        type_code: int = 5,
        count: int = 1,
        unit: str = "",
        desc: str = "",
    ) -> None:
        self.name = name
        self.type = type_code
        self.count = count
        self.unit = unit
        self.desc = desc


class _FakeIBT:
    def __init__(
        self,
        channel_values: dict[str, list[object]],
        *,
        session_yaml: str = "",
        headers: list[_FakeVarHeader] | None = None,
        session_lap_count: int = 1,
    ) -> None:
        self._channel_values = {name: list(values) for name, values in channel_values.items()}
        self.session_info = session_yaml
        self.var_headers = headers or [_FakeVarHeader(name) for name in channel_values.keys()]
        record_count = max((len(values) for values in self._channel_values.values()), default=0)
        self._disk_header = types.SimpleNamespace(
            session_record_count=record_count,
            session_lap_count=session_lap_count,
        )

    def open(self, ibt_file: str) -> None:
        self._opened_file = ibt_file

    def close(self) -> None:
        pass

    def get_all(self, key: str):
        if key not in self._channel_values:
            return None
        return list(self._channel_values[key])

    def clone(self) -> "_FakeIBT":
        return _FakeIBT(
            self._channel_values,
            session_yaml=self.session_info,
            headers=list(self.var_headers),
            session_lap_count=self._disk_header.session_lap_count,
        )


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


def _make_geo_channel_values(
    samples: list[tuple[float, float, float]],
    *,
    lat_name: str = "Lat",
    lon_name: str = "Lon",
    include_alt: bool = True,
) -> dict[str, list[object]]:
    values: dict[str, list[object]] = {
        "SessionTime": [],
        "LapDistPct": [],
        lat_name: [],
        lon_name: [],
    }
    if include_alt:
        values["Alt"] = []
    for idx, (lap_dist_pct, lat, lon) in enumerate(samples):
        values["SessionTime"].append(idx / 60.0)
        values["LapDistPct"].append(lap_dist_pct)
        values[lat_name].append(lat)
        values[lon_name].append(lon)
        if include_alt:
            values["Alt"].append(8.0 + idx * 0.1)
    return values


def _fake_irsdk_module(fake_ir: _FakeIRSDK | None = None, fake_ibt: _FakeIBT | None = None) -> types.ModuleType:
    mod = types.ModuleType("irsdk")
    if fake_ir is not None:
        mod.IRSDK = MagicMock(return_value=fake_ir)  # type: ignore[attr-defined]
    if fake_ibt is not None:
        mod.IBT = MagicMock(side_effect=lambda: fake_ibt.clone())  # type: ignore[attr-defined]
    return mod


def _make_inventory_yaml(
    track_display: str = "Misano World Circuit Marco Simoncelli",
    track_config: str = "Grand Prix",
) -> str:
    return "\n".join(
        [
            "WeekendInfo:",
            f" TrackDisplayName: {track_display}",
            f" TrackConfigName: {track_config}",
            " TrackName: misano gp",
            " TrackID: 501",
            " BuildVersion: 2026.02.02.02",
            " EventType: Test",
            "DriverInfo:",
            " DriverCarIdx: 0",
            " Drivers:",
            "  - CarIdx: 0",
            "    UserName: Test Driver",
            "    CarPath: lamborghinievogt3",
            "    CarScreenName: Lamborghini Huracan GT3 EVO",
            "    CarClassShortName: GT3",
            "SessionNum: 0",
            "SessionInfo:",
            " Sessions:",
            "  - SessionNum: 0",
            "    SessionType: Open Practice",
        ]
    )


def test_extract_produces_valid_json(tmp_path: Path) -> None:
    channel_values = _make_geo_channel_values(
        [
            (0.00, 47.000000, 8.000000),
            (0.25, 47.000250, 8.000000),
            (0.50, 47.000250, 8.000350),
            (0.75, 47.000000, 8.000350),
        ]
    )
    fake_ibt = _FakeIBT(
        channel_values,
        session_yaml=_make_session_yaml("Sebring", "Full Course", build_version="2026.03"),
    )
    ibt_file = tmp_path / "fake.ibt"
    ibt_file.touch()

    with patch.dict(sys.modules, {"irsdk": _fake_irsdk_module(fake_ibt=fake_ibt)}):
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
    fake_ibt = _FakeIBT(
        _make_geo_channel_values(
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

    with patch.dict(sys.modules, {"irsdk": _fake_irsdk_module(fake_ibt=fake_ibt)}):
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
    fake_ibt = _FakeIBT(
        {
            "SessionTime": [0.0, 1.0, 2.0],
            "LapDistPct": [0.0, 0.4, 0.8],
            "VelocityX": [50.0, 50.0, 50.0],
        },
        session_yaml=_make_session_yaml("Unknown", ""),
    )
    ibt_file = tmp_path / "fake.ibt"
    ibt_file.touch()

    with patch.dict(sys.modules, {"irsdk": _fake_irsdk_module(fake_ibt=fake_ibt)}):
        with pytest.raises(RuntimeError, match="missing required channels"):
            extract_track_geometry(ibt_file, tmp_path)

    assert list(tmp_path.rglob("track_road_geometry.json")) == []


def test_extract_logs_geo_pipeline_counts_and_debug_samples(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    samples = [
        (0.00, 43.9600, 12.6830),
        (0.25, 43.9605, 12.6833),
        (0.50, 43.9610, 12.6838),
        (0.75, 43.9604, 12.6842),
        (0.99, 43.9599, 12.6834),
        (0.01, 43.9601, 12.6831),
        (0.26, 43.9606, 12.6834),
        (0.51, 43.9611, 12.6839),
        (0.76, 43.9605, 12.6843),
    ]
    fake_ibt = _FakeIBT(
        _make_geo_channel_values(samples),
        session_yaml=_make_session_yaml("Misano World Circuit Marco Simoncelli", "Grand Prix"),
    )
    ibt_file = tmp_path / "misano.ibt"
    ibt_file.touch()

    caplog.set_level("DEBUG", logger="core.coaching.ibt_track_extractor")
    with patch.dict(sys.modules, {"irsdk": _fake_irsdk_module(fake_ibt=fake_ibt)}):
        out = extract_track_geometry(ibt_file, tmp_path)

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["center_line"]
    info_messages = [record.getMessage() for record in caplog.records if record.levelname == "INFO"]
    debug_messages = [record.getMessage() for record in caplog.records if record.levelname == "DEBUG"]
    assert any("joint_valid_geo_sample_count=9" in message for message in info_messages)
    assert any(
        "after_sort_count=5" in message
        and "after_dedupe_count=5" in message
        and "after_resample_count=5" in message
        and "before_json_write_count=5" in message
        for message in info_messages
    )
    assert any("valid_geo_samples_head=" in message for message in debug_messages)
    assert any("valid_geo_samples_tail=" in message for message in debug_messages)


def test_extract_skips_with_reason_when_geo_samples_are_not_joint_valid(tmp_path: Path) -> None:
    fake_ibt = _FakeIBT(
        {
            "SessionTime": [0.0, 1.0, 2.0, 3.0],
            "LapDistPct": [0.0, 0.2, 0.4, 0.6],
            "Lat": [0.0, 0.0, 0.0, 0.0],
            "Lon": [0.0, 0.0, 0.0, 0.0],
        },
        session_yaml=_make_session_yaml("Nowhere", ""),
    )
    ibt_file = tmp_path / "invalid_geo.ibt"
    ibt_file.touch()

    with patch.dict(sys.modules, {"irsdk": _fake_irsdk_module(fake_ibt=fake_ibt)}):
        with pytest.raises(RuntimeError, match="no samples contain valid Lat \\+ Lon \\+ LapDistPct together"):
            extract_track_geometry(ibt_file, tmp_path)


def test_build_ibt_inventory_report_marks_latlon_centerline_viable(tmp_path: Path) -> None:
    ibt_file = tmp_path / "inventory_ok.ibt"
    ibt_file.touch()
    fake_ibt = _FakeIBT(
        {
            "SessionTime": [0.0, 1.0, 2.0, 3.0],
            "LapDistPct": [0.0, 0.33, 0.66, 0.99],
            "Lat": [43.9600, 43.9605, 43.9610, 43.9615],
            "Lon": [12.6830, 12.6835, 12.6840, 12.6845],
            "Alt": [8.9, 9.0, 9.1, 9.2],
            "Yaw": [0.0, 0.1, 0.2, 0.3],
            "Speed": [10.0, 20.0, 30.0, 40.0],
            "VelocityX": [1.0, 1.1, 1.2, 1.3],
            "VelocityY": [0.1, 0.2, 0.3, 0.4],
            "VelocityZ": [0.0, 0.0, 0.0, 0.0],
        },
        session_yaml=_make_inventory_yaml(),
        session_lap_count=2,
    )

    with patch.dict(sys.modules, {"irsdk": _fake_irsdk_module(fake_ibt=fake_ibt)}):
        report = build_ibt_inventory_report(ibt_file)

    assert report["file_summary"]["track_display_name"] == "Misano World Circuit Marco Simoncelli"
    assert report["file_summary"]["track_config_name"] == "Grand Prix"
    assert report["file_summary"]["track_id"] == 501
    assert report["file_summary"]["session_type"] == "practice"
    assert report["file_summary"]["iracing_build"] == "2026.02.02.02"
    assert report["file_summary"]["session_lap_count"] == 2
    assert "Lat" in report["available_channels"]
    assert report["candidate_channel_report"]["Lat"]["present"] is True
    assert report["candidate_channel_report"]["Lat"]["finite_count"] == 4
    assert report["candidate_channel_report"]["VelocityZ"]["nonzero_count"] == 0
    assert report["lat_lon_centerline_viability"]["status"] == "ok"
    assert report["lat_lon_centerline_viability"]["joint_valid_sample_count"] == 4
    assert report["lat_lon_centerline_viability"]["pipeline_counts"]["before_json_write_count"] == 4
    assert report["recommendation"]["summary"] == "Lat/Lon + LapDistPct nutzbar"


def test_build_ibt_inventory_report_surfaces_missing_exact_channel_names(tmp_path: Path) -> None:
    ibt_file = tmp_path / "inventory_missing_exact.ibt"
    ibt_file.touch()
    fake_ibt = _FakeIBT(
        {
            "SessionTime": [0.0, 1.0, 2.0, 3.0],
            "LapDistPct": [0.1, 0.2, 0.3, 0.4],
            "Latitude": [43.96, 43.97, 43.98, 43.99],
            "Longitude": [12.68, 12.69, 12.70, 12.71],
            "LatAccel": [0.0, 0.1, 0.2, 0.3],
            "VelocityX": [1.0, 1.0, 1.0, 1.0],
            "VelocityY": [0.0, 0.1, 0.2, 0.3],
        },
        session_yaml=_make_inventory_yaml("Road Atlanta", "Full Course"),
    )

    with patch.dict(sys.modules, {"irsdk": _fake_irsdk_module(fake_ibt=fake_ibt)}):
        report = build_ibt_inventory_report(ibt_file)

    assert report["candidate_channel_report"]["Lat"]["present"] is False
    assert report["candidate_channel_report"]["Lon"]["present"] is False
    assert [item["name"] for item in report["candidate_channel_report"]["Lat"]["similar_names"]] == ["LatAccel", "Latitude"]
    assert [item["name"] for item in report["candidate_channel_report"]["Lon"]["similar_names"]] == ["Longitude"]
    assert report["lat_lon_centerline_viability"]["status"] == "ok"
    assert report["lat_lon_centerline_viability"]["source_channels"]["Lat"] == "Latitude"
    assert report["lat_lon_centerline_viability"]["source_channels"]["Lon"] == "Longitude"
    assert report["recommendation"]["status"] == "usable"
