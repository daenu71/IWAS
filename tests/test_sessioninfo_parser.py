"""Unit tests for core.irsdk.sessioninfo_parser."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.irsdk.sessioninfo_parser import (  # noqa: E402
    extract_environment_from_session_info,
    extract_session_meta,
    resolve_session_environment,
)


def test_extract_session_meta_includes_environment_dict() -> None:
    yaml_text = """
WeekendInfo:
  TrackDisplayName: Spa-Francorchamps
  TrackUsage: Moderate Usage
  TrackTemp: 28.5
  AirTemp: 19.0
  Humidity: 46
  Fog: 2
  WindSpeed: 3.5
  WindDir: 135
  Skies: Partly Cloudy
  WeatherType: Dynamic
  AirPressure: 1014.2
DriverInfo:
  DriverCarIdx: 0
  Drivers:
    - CarIdx: 0
      UserName: Driver One
"""

    meta = extract_session_meta(yaml_text)

    assert meta["TrackUsage"] == "Moderate Usage"
    assert meta["environment"] == {
        "track_temp_c": 28.5,
        "air_temp_c": 19.0,
        "humidity_pct": 46.0,
        "fog_pct": 2.0,
        "wind_speed_ms": 3.5,
        "wind_dir_deg": 135.0,
        "skies": "Partly Cloudy",
        "weather_type": "Dynamic",
        "track_usage": "Moderate Usage",
        "air_pressure_hpa": 1014.2,
    }


def test_extract_session_meta_omits_empty_environment() -> None:
    yaml_text = """
WeekendInfo:
  TrackDisplayName: Spa-Francorchamps
  TrackTemp: not-a-number
  AirTemp:
  Humidity:
  Fog:
  WindSpeed:
  WindDir:
  Skies: "   "
  WeatherType: ""
  AirPressure:
DriverInfo:
  DriverCarIdx: 0
  Drivers:
    - CarIdx: 0
      UserName: Driver One
"""

    meta = extract_session_meta(yaml_text)

    assert "environment" not in meta


def test_extract_environment_supports_modern_weekendinfo_keys_and_units() -> None:
    yaml_text = """
WeekendInfo:
  TrackUsage: High Usage
  TrackSurfaceTemp: 39.81 C
  TrackAirTemp: 25.56 C
  TrackRelativeHumidity: 45 %
  TrackFogLevel: 0 %
  TrackWindVel: 0.89 m/s
  TrackWindDir: 0.00 rad
  TrackSkies: Partly Cloudy
  TrackWeatherType: Static
  TrackAirPressure: 29.89 Hg
  WeekendOptions:
    WindDirection: N
    WindSpeed: 3.22 km/h
    WeatherTemp: 25.56 C
    RelativeHumidity: 45 %
    FogLevel: 0 %
"""

    environment = extract_environment_from_session_info(yaml_text)

    assert environment is not None
    assert environment["track_temp_c"] == pytest.approx(39.81)
    assert environment["air_temp_c"] == pytest.approx(25.56)
    assert environment["humidity_pct"] == pytest.approx(45.0)
    assert environment["fog_pct"] == pytest.approx(0.0)
    assert environment["wind_speed_ms"] == pytest.approx(0.89)
    assert environment["wind_dir_deg"] == pytest.approx(0.0)
    assert environment["skies"] == "Partly Cloudy"
    assert environment["weather_type"] == "Static"
    assert environment["track_usage"] == "High Usage"
    assert environment["air_pressure_hpa"] == pytest.approx(1012.2066093347631)


def test_extract_session_meta_reads_track_usage_from_session_track_rubber_state() -> None:
    yaml_text = """
WeekendInfo:
  TrackDisplayName: Misano
  TrackAirPressure: 30.48 Hg
SessionInfo:
  CurrentSessionNum: 0
  Sessions:
    - SessionNum: 0
      SessionType: Offline Testing
      SessionTrackRubberState: low usage
"""

    meta = extract_session_meta(yaml_text)

    assert meta["TrackUsage"] == "Low Usage"
    assert meta["environment"]["track_usage"] == "Low Usage"


def test_resolve_session_environment_merges_top_level_track_usage() -> None:
    environment = resolve_session_environment(
        {
            "TrackUsage": "Low Usage",
            "environment": {"air_pressure_hpa": 1010.5},
        }
    )

    assert environment == {
        "air_pressure_hpa": 1010.5,
        "track_usage": "Low Usage",
    }


def test_resolve_session_environment_backfills_track_usage_from_yaml_when_meta_is_old() -> None:
    environment = resolve_session_environment(
        {"environment": {"air_pressure_hpa": 1032.2}},
        session_info_yaml="""
WeekendInfo:
  TrackAirPressure: 30.48 Hg
SessionInfo:
  CurrentSessionNum: 0
  Sessions:
    - SessionNum: 0
      SessionTrackRubberState: low usage
""",
    )

    assert environment == {
        "air_pressure_hpa": 1032.2,
        "track_usage": "Low Usage",
    }
