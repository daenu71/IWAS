from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.irsdk.irsdk_client import IRSDKClient  # noqa: E402
from core.irsdk.recorder_service import RecorderService  # noqa: E402


class _FakeIR:
    def __init__(self, headers: list[dict[str, object]]) -> None:
        self.var_headers = headers


class _StubDiscoveryClient:
    def describe_available_channels(self) -> dict[str, dict[str, object]]:
        return {
            "SessionTime": {"type": "float"},
            "Lat": {"type": "float"},
            "Lon": {"type": "float"},
            "Alt": {"type": "float", "unit": "m"},
        }

    def build_live_channel_report(self, _target_specs) -> dict[str, object]:
        return {
            "discovery_source": "var_headers",
            "discovery_available": True,
            "available_count": 4,
            "available_channels": {
                "SessionTime": {"type": "float"},
                "Alt": {"type": "float", "unit": "m"},
                "LatAccel": {"type": "float"},
                "LongAccel": {"type": "float"},
            },
            "targets": {
                "Lat": {
                    "request_spec": "Lat",
                    "available": False,
                    "resolved_name": None,
                    "type": None,
                    "count": None,
                    "unit": None,
                    "desc": None,
                    "aliases_checked": ["Lat", "Latitude"],
                    "similar_names": [{"name": "LatAccel", "type": "float"}],
                },
                "Lon": {
                    "request_spec": "Lon",
                    "available": False,
                    "resolved_name": None,
                    "type": None,
                    "count": None,
                    "unit": None,
                    "desc": None,
                    "aliases_checked": ["Lon", "Longitude"],
                    "similar_names": [{"name": "LongAccel", "type": "float"}],
                },
                "Alt": {
                    "request_spec": "Alt",
                    "available": True,
                    "resolved_name": "Alt",
                    "type": "float",
                    "count": 1,
                    "unit": "m",
                    "desc": "Altitude",
                    "aliases_checked": ["Alt", "Altitude"],
                    "similar_names": [{"name": "Alt", "type": "float", "unit": "m"}],
                },
            },
            "filtered_available_names": {
                "Lat": [{"name": "LatAccel", "type": "float"}],
                "Lon": [{"name": "LongAccel", "type": "float"}],
                "Alt": [{"name": "Alt", "type": "float", "unit": "m"}],
            },
        }

    def resolve_requested_channels(self, _request_specs) -> dict[str, object]:
        return {
            "channel_info": {
                "SessionTime": {"type": "float", "source_name": "SessionTime"},
                "Lat": {"type": "float", "source_name": "Lat"},
                "Lon": {"type": "float", "source_name": "Lon"},
                "Alt": {"type": "float", "source_name": "Alt", "unit": "m"},
            },
            "recorded_channels": ["SessionTime", "Lat", "Lon", "Alt"],
            "missing_channels": [],
        }


def test_build_live_channel_report_uses_strict_directory_and_similar_names() -> None:
    client = IRSDKClient()
    client._ir = _FakeIR(  # type: ignore[attr-defined]
        [
            {"name": "LatAccel", "type": 4, "count": 1},
            {"name": "LongAccel", "type": 4, "count": 1},
            {"name": "Longitude", "type": 4, "count": 1, "unit": "rad"},
            {"name": "Alt", "type": 4, "count": 1, "unit": "m"},
        ]
    )
    client._state = "connected"  # type: ignore[attr-defined]

    report = client.build_live_channel_report(("Lat", "Lon", "Alt"))

    assert report["discovery_source"] == "var_headers"
    assert report["discovery_available"] is True
    assert report["available_count"] == 4
    assert report["targets"]["Lat"]["available"] is False
    assert report["targets"]["Lon"]["available"] is True
    assert report["targets"]["Lon"]["resolved_name"] == "Longitude"
    assert report["targets"]["Alt"]["resolved_name"] == "Alt"
    assert [item["name"] for item in report["targets"]["Lat"]["similar_names"]] == ["LatAccel"]
    assert [item["name"] for item in report["filtered_available_names"]["Lon"]] == ["LongAccel", "Longitude"]


def test_initialize_channels_logs_and_persists_geo_channel_availability(tmp_path: Path) -> None:
    session_dir = tmp_path / "session_001"
    session_dir.mkdir(parents=True, exist_ok=True)

    service = RecorderService(client=_StubDiscoveryClient())
    service._session_dir = session_dir  # type: ignore[attr-defined]
    service._session_start_wall_ts = 123.0  # type: ignore[attr-defined]

    service._initialize_channels()

    assert tuple(service._recorded_channels) == ("SessionTime", "Alt", "SessionUniqueID")  # type: ignore[attr-defined]
    assert any(
        isinstance(item, dict) and item.get("request_spec") == "Lat" and item.get("reason") == "not_in_live_irsdk_header"
        for item in service._missing_channels  # type: ignore[attr-defined]
    )
    assert any(
        isinstance(item, dict) and item.get("request_spec") == "Lon" and item.get("reason") == "not_in_live_irsdk_header"
        for item in service._missing_channels  # type: ignore[attr-defined]
    )

    debug_log = (session_dir / "debug_recorder.log").read_text(encoding="utf-8")
    assert "Lat: unavailable" in debug_log
    assert "Lon: unavailable" in debug_log
    assert "Alt: available name=Alt type=float unit=m" in debug_log

    vars_dump = json.loads((session_dir / "vars_dump.json").read_text(encoding="utf-8"))
    assert vars_dump["channel_directory_source"] == "var_headers"
    assert vars_dump["geo_channel_report"]["Lat"]["available"] is False
    assert vars_dump["geo_channel_report"]["Alt"]["resolved_name"] == "Alt"
    assert vars_dump["filtered_available_names"]["Lon"][0]["name"] == "LongAccel"

    session_meta = json.loads((session_dir / "session_meta.json").read_text(encoding="utf-8"))
    assert session_meta["irsdk_channel_directory_source"] == "var_headers"
    assert session_meta["irsdk_live_geo_channels"]["Lat"]["available"] is False
    assert session_meta["irsdk_live_geo_channels"]["Alt"]["resolved_name"] == "Alt"


def test_recorded_geo_channels_are_persisted_to_run_parquet(tmp_path: Path) -> None:
    pq = pytest.importorskip("pyarrow.parquet")

    session_dir = tmp_path / "session_002"
    session_dir.mkdir(parents=True, exist_ok=True)

    service = RecorderService()
    service._session_dir = session_dir  # type: ignore[attr-defined]
    service._recorded_channels = ("SessionTime", "Lat", "Lon", "Alt")  # type: ignore[attr-defined]
    service._dtype_decisions = {  # type: ignore[attr-defined]
        "SessionTime": "float32",
        "Lat": "float64",
        "Lon": "float64",
        "Alt": "float32",
    }
    service._sample_hz = 1  # type: ignore[attr-defined]
    service._active_run_id = 1  # type: ignore[attr-defined]

    service._start_run_storage(1, start_reason="test", start_ts=1.0)
    service._append_active_run_sample(
        {
            "timestamp_wall": 10.0,
            "timestamp_monotonic": 20.0,
            "raw": {
                "SessionTime": 1.25,
                "Lat": 47.225,
                "Lon": 8.816,
                "Alt": 511.5,
                "Lap": 1,
                "PlayerTrackSurface": 0,
                "PlayerCarMyIncidentCount": 0,
            },
        }
    )
    service._finalize_run(1, reason="test_done")

    table = pq.read_table(session_dir / "run_0001.parquet")
    data = table.to_pydict()

    assert data["Lat"] == [47.225]
    assert data["Lon"] == [8.816]
    assert data["Alt"] == [511.5]
