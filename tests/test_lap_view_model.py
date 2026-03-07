"""Unit tests for core.coaching.lap_view_model (Story 3.0)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.coaching.lap_view_model import (  # noqa: E402
    CornerInfo,
    Event,
    LapMeta,
    LapViewModel,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_N = 500  # grid points per lap


def _make_session_dir(tmp_path: Path) -> Path:
    session_dir = tmp_path / "2026-03-05__100000__SpaFR__FORMULA__Practice__s1"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def _write_session_meta(session_dir: Path) -> None:
    meta = {
        "TrackDisplayName": "Spa-Francorchamps",
        "TrackConfigName": "Full",
        "CarScreenName": "Dallara Formula 3",
        "TrackUsage": "Low Usage",
        "environment": {
            "track_temp_c": 27.5,
            "air_temp_c": 19.2,
            "humidity_pct": 46.0,
            "weather_type": "Dynamic",
            "track_usage": "Low Usage",
        },
    }
    (session_dir / "session_meta.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )


def _write_session_meta_without_environment(session_dir: Path) -> None:
    meta = {
        "TrackDisplayName": "Spa-Francorchamps",
        "TrackConfigName": "Full",
        "CarScreenName": "Dallara Formula 3",
    }
    (session_dir / "session_meta.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )


def _write_session_info_yaml(session_dir: Path) -> None:
    (session_dir / "session_info.yaml").write_text(
        """
WeekendInfo:
  TrackSurfaceTemp: 27.5 C
  TrackAirTemp: 19.2 C
  TrackRelativeHumidity: 46 %
  TrackFogLevel: 0 %
  TrackWindVel: 1.8 m/s
  TrackWindDir: 3.14159265359 rad
  TrackSkies: Partly Cloudy
  TrackWeatherType: Dynamic
  TrackAirPressure: 1009.1 hPa
""".strip(),
        encoding="utf-8",
    )


def _write_lap_meta(session_dir: Path, run_id: int, lap_no: int) -> None:
    meta = {
        "lap_time": 132.456,
        "lap_complete": True,
        "offtrack_surface": False,
    }
    (session_dir / f"run_{run_id:04d}_lap_{lap_no:04d}_meta.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )


def _make_lap_dir(session_dir: Path, lap_no: int) -> Path:
    lap_dir = session_dir / "laps" / f"lap_{lap_no:04d}"
    (lap_dir / "analysis").mkdir(parents=True, exist_ok=True)
    return lap_dir


def _write_resampled(lap_dir: Path, n: int = _N) -> None:
    ldp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    st = np.arange(n, dtype=np.float32) * 0.01
    vx = (10.0 * np.sin(2.0 * np.pi * ldp)).astype(np.float32)
    vy = (5.0 * np.cos(2.0 * np.pi * ldp)).astype(np.float32)
    speed = (40.0 + 5.0 * np.sin(2.0 * np.pi * ldp)).astype(np.float32)

    cols = {
        "LapDistPct": pa.array(ldp.tolist(), type=pa.float32()),
        "SessionTime": pa.array(st.tolist(), type=pa.float32()),
        "VelocityX": pa.array(vx.tolist(), type=pa.float32()),
        "VelocityY": pa.array(vy.tolist(), type=pa.float32()),
        "Speed": pa.array(speed.tolist(), type=pa.float32()),
    }
    pq.write_table(
        pa.table(cols),
        str(lap_dir / "analysis" / "lap_resampled.parquet"),
    )


def _write_corner_map(session_dir: Path) -> None:
    corner_map = {
        "track_key": "Spa-Francorchamps__Full",
        "corner_map_version": 1,
        "corners": [
            {
                "corner_id": 1,
                "start_lapdist_pct": 0.05,
                "end_lapdist_pct": 0.12,
                "corner_type": "hairpin",
                "radius_est": 25.0,
                "curvature_peak": 0.04,
                "curvature_mean": 0.03,
                "curvature_trend": "sym",
                "crest_present": None,
                "compression_present": None,
                "corner_map_version": 1,
            },
            {
                "corner_id": 2,
                "start_lapdist_pct": 0.30,
                "end_lapdist_pct": 0.40,
                "corner_type": "sweeper",
                "radius_est": 150.0,
                "curvature_peak": 0.007,
                "curvature_mean": 0.005,
                "curvature_trend": "inc",
                "crest_present": None,
                "compression_present": None,
                "corner_map_version": 1,
            },
        ],
    }
    map_dir = session_dir / "corner_maps" / "Spa-Francorchamps__Full"
    map_dir.mkdir(parents=True, exist_ok=True)
    (map_dir / "corner_map_v1.json").write_text(
        json.dumps(corner_map), encoding="utf-8"
    )


def _write_events(lap_dir: Path) -> None:
    events = {
        "meta": {"config_version": 1, "n_grid_points": _N, "missing_channels": {}},
        "events": {
            "brake_start": {
                "name": "brake_start",
                "lapdist_pct": 0.04,
                "session_time": 1.5,
                "value": None,
            },
            "turn_in": {
                "name": "turn_in",
                "lapdist_pct": 0.06,
                "session_time": 2.1,
                "value": 0.12,
            },
            "min_speed": {
                "name": "min_speed",
                "lapdist_pct": 0.09,
                "session_time": 3.0,
                "value": 38.5,
            },
            "gear_change": [
                {
                    "name": "gear_change",
                    "lapdist_pct": 0.35,
                    "session_time": 15.0,
                    "value": {"from": 4, "to": 3},
                }
            ],
            "throttle_on": None,  # null-safe: must not crash
        },
    }
    (lap_dir / "analysis" / "lap_events.json").write_text(
        json.dumps(events), encoding="utf-8"
    )


def _write_features(lap_dir: Path) -> None:
    cols = {
        "corner_id": pa.array([1, 2], type=pa.int32()),
        "run_id": pa.array(["run_0001", "run_0001"], type=pa.string()),
        "lap_id": pa.array(["lap_0001", "lap_0001"], type=pa.string()),
        "grip_usage_p95": pa.array([0.87, 0.72], type=pa.float64()),
        "brake_release_slope": pa.array([-0.42, -0.38], type=pa.float64()),
    }
    pq.write_table(
        pa.table(cols),
        str(lap_dir / "analysis" / "corner_features.parquet"),
    )


# ---------------------------------------------------------------------------
# Test 1 – full artefact set → all fields populated
# ---------------------------------------------------------------------------


def test_load_full_artefact_set(tmp_path: Path) -> None:
    session_dir = _make_session_dir(tmp_path)
    _write_session_meta(session_dir)
    _write_lap_meta(session_dir, run_id=1, lap_no=1)
    lap_dir = _make_lap_dir(session_dir, lap_no=1)
    _write_resampled(lap_dir)
    _write_corner_map(session_dir)
    _write_events(lap_dir)
    _write_features(lap_dir)

    vm = LapViewModel.load(session_dir, run_id=1, lap_no=1)

    assert vm.is_loaded()

    # meta
    assert isinstance(vm.meta, LapMeta)
    assert vm.meta.track == "Spa-Francorchamps"
    assert vm.meta.car == "Dallara Formula 3"
    assert vm.meta.lap_no == 1
    assert vm.meta.lap_time == pytest.approx(132.456)
    assert vm.meta.valid is True
    assert vm.meta.environment == {
        "track_temp_c": 27.5,
        "air_temp_c": 19.2,
        "humidity_pct": 46.0,
        "weather_type": "Dynamic",
        "track_usage": "Low Usage",
    }
    assert vm.meta.track_usage == "Low Usage"

    # geometry
    assert vm.lap_dist_pct.shape == (_N,)
    assert vm.track_xy.shape == (_N, 2)
    assert float(vm.track_xy[:, 0].min()) >= 0.0
    assert float(vm.track_xy[:, 0].max()) <= 1.0
    assert float(vm.track_xy[:, 1].min()) >= 0.0
    assert float(vm.track_xy[:, 1].max()) <= 1.0

    # corners
    assert len(vm.corners) == 2
    assert vm.corners[0].corner_id == 1
    assert vm.corners[0].corner_type == "hairpin"
    assert vm.corners[1].corner_id == 2

    # events grouped by corner
    # turn_in (0.06) and min_speed (0.09) are inside corner 1 [0.05, 0.12]
    assert 1 in vm.events
    event_types_c1 = {e.event_type for e in vm.events[1]}
    assert "turn_in" in event_types_c1
    assert "min_speed" in event_types_c1

    # brake_start (0.04) is before corner 1 → key 0
    assert 0 in vm.events
    event_types_global = {e.event_type for e in vm.events[0]}
    assert "brake_start" in event_types_global

    # gear_change (0.35) is inside corner 2 [0.30, 0.40]
    assert 2 in vm.events
    event_types_c2 = {e.event_type for e in vm.events[2]}
    assert "gear_change" in event_types_c2

    # features
    assert 1 in vm.features
    assert 2 in vm.features
    assert vm.features[1]["grip_usage_p95"] == pytest.approx(0.87)
    assert vm.features[2]["grip_usage_p95"] == pytest.approx(0.72)


# ---------------------------------------------------------------------------
# Test 2 – missing lap_events.json → no crash, events empty
# ---------------------------------------------------------------------------


def test_load_missing_events_no_crash(tmp_path: Path) -> None:
    session_dir = _make_session_dir(tmp_path)
    _write_session_meta(session_dir)
    _write_lap_meta(session_dir, run_id=1, lap_no=1)
    lap_dir = _make_lap_dir(session_dir, lap_no=1)
    _write_resampled(lap_dir)
    _write_corner_map(session_dir)
    # Intentionally do NOT write lap_events.json
    _write_features(lap_dir)

    vm = LapViewModel.load(session_dir, run_id=1, lap_no=1)

    assert vm.is_loaded()
    assert vm.events == {}  # no events, no crash
    # Other fields still populated
    assert len(vm.corners) == 2
    assert len(vm.features) == 2


# ---------------------------------------------------------------------------
# Test 3 – get_resampled_channel returns correct np.ndarray
# ---------------------------------------------------------------------------


def test_get_resampled_channel(tmp_path: Path) -> None:
    session_dir = _make_session_dir(tmp_path)
    _write_session_meta(session_dir)
    _write_lap_meta(session_dir, run_id=1, lap_no=1)
    lap_dir = _make_lap_dir(session_dir, lap_no=1)
    _write_resampled(lap_dir)
    _write_corner_map(session_dir)

    vm = LapViewModel.load(session_dir, run_id=1, lap_no=1)

    speed = vm.get_resampled_channel("Speed")
    assert isinstance(speed, np.ndarray)
    assert speed.dtype == np.float64
    assert speed.shape == (_N,)
    assert float(speed.min()) > 0.0  # sanity: positive speed values

    # Missing channel → empty array, no crash
    missing = vm.get_resampled_channel("NonExistentChannel")
    assert isinstance(missing, np.ndarray)
    assert missing.size == 0


# ---------------------------------------------------------------------------
# Test 4 – null safety: missing session_meta and lap_meta → no crash
# ---------------------------------------------------------------------------


def test_load_missing_meta_no_crash(tmp_path: Path) -> None:
    session_dir = _make_session_dir(tmp_path)
    # No session_meta.json, no lap meta, no corner map
    lap_dir = _make_lap_dir(session_dir, lap_no=2)
    _write_resampled(lap_dir)

    vm = LapViewModel.load(session_dir, run_id=1, lap_no=2)

    assert vm.is_loaded()
    assert vm.meta is not None
    assert vm.meta.track == "unknown_track"
    assert vm.meta.car == "unknown_car"
    assert vm.meta.lap_time is None
    assert vm.meta.environment is None
    assert vm.corners == []
    assert vm.events == {}
    assert vm.features == {}


def test_load_meta_falls_back_to_session_info_environment(tmp_path: Path) -> None:
    session_dir = _make_session_dir(tmp_path)
    _write_session_meta_without_environment(session_dir)
    _write_session_info_yaml(session_dir)
    _write_lap_meta(session_dir, run_id=1, lap_no=1)
    lap_dir = _make_lap_dir(session_dir, lap_no=1)
    _write_resampled(lap_dir)

    vm = LapViewModel.load(session_dir, run_id=1, lap_no=1)

    assert vm.meta is not None
    assert vm.meta.environment is not None
    assert vm.meta.environment["track_temp_c"] == pytest.approx(27.5)
    assert vm.meta.environment["air_temp_c"] == pytest.approx(19.2)
    assert vm.meta.environment["humidity_pct"] == pytest.approx(46.0)
    assert vm.meta.environment["fog_pct"] == pytest.approx(0.0)
    assert vm.meta.environment["wind_speed_ms"] == pytest.approx(1.8)
    assert vm.meta.environment["wind_dir_deg"] == pytest.approx(180.0)
    assert vm.meta.environment["skies"] == "Partly Cloudy"
    assert vm.meta.environment["weather_type"] == "Dynamic"
    assert vm.meta.environment["air_pressure_hpa"] == pytest.approx(1009.1)
