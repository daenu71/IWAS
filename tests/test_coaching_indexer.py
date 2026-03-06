"""Unit tests for environment propagation in core.coaching.indexer."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.coaching.indexer import (  # noqa: E402
    NodeSummary,
    _RunScan,
    _SessionScan,
    _build_session_event_node,
    _scan_session_dir_uncached,
)


def test_environment_inherits_from_session_to_run_and_lap(tmp_path: Path) -> None:
    session_dir = tmp_path / "2026-03-06__100000__Spa__F3__Practice__s1"
    session_dir.mkdir(parents=True, exist_ok=True)
    environment = {
        "track_temp_c": 27.0,
        "air_temp_c": 18.0,
        "humidity_pct": 40.0,
    }
    run = _RunScan(
        run_id=1,
        parquet_path=None,
        meta_path=None,
        extra_paths=[],
        meta={},
        lap_segments=[
            {
                "lap_no": 3,
                "lap_complete": True,
                "duration_s": 91.234,
            }
        ],
        summary=NodeSummary(
            total_time_s=91.234,
            laps=1,
            laps_total_display=1,
            fastest_lap_s=91.234,
            last_driven_ts=100.0,
        ),
    )
    session = _SessionScan(
        session_dir=session_dir,
        folder_name=session_dir.name,
        track="Spa",
        car="F3",
        session_type="practice",
        session_id="s1",
        session_meta={"environment": environment},
        runs=[run],
        has_active_lock=False,
        has_finalized_marker=True,
        last_driven_ts=100.0,
        parsed_folder_ts=100.0,
        summary=NodeSummary(
            total_time_s=91.234,
            laps=1,
            laps_total_display=1,
            fastest_lap_s=91.234,
            last_driven_ts=100.0,
        ),
    )

    session_node = _build_session_event_node(session)
    run_node = session_node.children[0]
    lap_node = run_node.children[0]

    assert session_node.summary.environment == environment
    assert run_node.summary.environment == environment
    assert lap_node.summary.environment == environment
    assert run_node.summary.environment is not session_node.summary.environment
    assert lap_node.summary.environment is not run_node.summary.environment


def test_run_environment_overrides_session_environment(tmp_path: Path) -> None:
    session_dir = tmp_path / "2026-03-06__100000__Spa__F3__Practice__s1"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_environment = {"track_temp_c": 30.0}
    run_environment = {"track_temp_c": 24.0, "weather_type": "Constant"}
    run = _RunScan(
        run_id=1,
        parquet_path=None,
        meta_path=None,
        extra_paths=[],
        meta={},
        lap_segments=[
            {
                "lap_no": 4,
                "lap_complete": True,
                "duration_s": 92.5,
            }
        ],
        summary=NodeSummary(
            total_time_s=92.5,
            laps=1,
            laps_total_display=1,
            fastest_lap_s=92.5,
            last_driven_ts=200.0,
            environment=run_environment,
        ),
    )
    session = _SessionScan(
        session_dir=session_dir,
        folder_name=session_dir.name,
        track="Spa",
        car="F3",
        session_type="practice",
        session_id="s1",
        session_meta={"environment": session_environment},
        runs=[run],
        has_active_lock=False,
        has_finalized_marker=True,
        last_driven_ts=200.0,
        parsed_folder_ts=200.0,
        summary=NodeSummary(
            total_time_s=92.5,
            laps=1,
            laps_total_display=1,
            fastest_lap_s=92.5,
            last_driven_ts=200.0,
        ),
    )

    session_node = _build_session_event_node(session)
    run_node = session_node.children[0]
    lap_node = run_node.children[0]

    assert session_node.summary.environment == session_environment
    assert run_node.summary.environment == run_environment
    assert lap_node.summary.environment == run_environment


def test_scan_session_dir_resolves_environment_from_session_info_yaml(tmp_path: Path) -> None:
    session_dir = tmp_path / "2026-03-06__100000__Spa__F3__Practice__s1"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "session_meta.json").write_text(
        json.dumps(
            {
                "TrackDisplayName": "Spa-Francorchamps",
                "CarScreenName": "Dallara Formula 3",
            }
        ),
        encoding="utf-8",
    )
    (session_dir / "session_info.yaml").write_text(
        """
WeekendInfo:
  TrackSurfaceTemp: 31.5 C
  TrackAirTemp: 20.0 C
  TrackRelativeHumidity: 48 %
  TrackFogLevel: 1 %
  TrackWindVel: 1.5 m/s
  TrackWindDir: 1.57079632679 rad
  TrackSkies: Mostly Cloudy
  TrackWeatherType: Dynamic
  TrackAirPressure: 1008.4 hPa
""".strip(),
        encoding="utf-8",
    )

    scanned = _scan_session_dir_uncached(session_dir, children=list(session_dir.iterdir()))

    assert scanned is not None
    environment = scanned.session_meta["environment"]
    assert environment["track_temp_c"] == pytest.approx(31.5)
    assert environment["air_temp_c"] == pytest.approx(20.0)
    assert environment["humidity_pct"] == pytest.approx(48.0)
    assert environment["fog_pct"] == pytest.approx(1.0)
    assert environment["wind_speed_ms"] == pytest.approx(1.5)
    assert environment["wind_dir_deg"] == pytest.approx(90.0)
    assert environment["skies"] == "Mostly Cloudy"
    assert environment["weather_type"] == "Dynamic"
    assert environment["air_pressure_hpa"] == pytest.approx(1008.4)
