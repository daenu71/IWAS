"""Unit tests for core.irsdk.sessioninfo_parser."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.irsdk.sessioninfo_parser import extract_session_meta  # noqa: E402


def test_extract_session_meta_includes_environment_dict() -> None:
    yaml_text = """
WeekendInfo:
  TrackDisplayName: Spa-Francorchamps
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

    assert meta["environment"] == {
        "track_temp_c": 28.5,
        "air_temp_c": 19.0,
        "humidity_pct": 46.0,
        "fog_pct": 2.0,
        "wind_speed_ms": 3.5,
        "wind_dir_deg": 135.0,
        "skies": "Partly Cloudy",
        "weather_type": "Dynamic",
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
