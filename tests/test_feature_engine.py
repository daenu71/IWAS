"""Unit tests for core.coaching.feature_engine - Story 2.4.1."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.coaching.feature_engine import extract_corner_features  # noqa: E402


_IMPLEMENTED_GROUPS = [
    "rotation_via_load",
    "friction_ellipse",
    "trail_braking",
    "downshift_stability",
    "light_hands",
    "vertical_dynamics",
    "limit_consistency",
    "exit_timing",
    "compound_strategy",
    "dynamic_balance",
]


def _feature_schema() -> dict[str, Any]:
    path = Path(__file__).resolve().parent.parent / "config" / "coaching" / "feature_schema_v1.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _make_channels(
    *,
    n: int = 240,
    include_abs: bool = True,
    include_torque: bool = True,
) -> dict[str, np.ndarray]:
    t = np.linspace(0.0, 1.0, n, endpoint=False, dtype=np.float64)
    channels: dict[str, np.ndarray] = {
        "LapDistPct": t,
        "SessionTime": np.arange(n, dtype=np.float64) * 0.01,
        "Speed": 32.0 + 6.0 * np.sin(2.0 * np.pi * t),
        "YawRate": 0.25 * np.sin(6.0 * np.pi * t) + 0.08 * np.cos(2.0 * np.pi * t),
        "LatAccel": 5.0 * np.sin(4.0 * np.pi * t) + 0.7 * np.cos(2.0 * np.pi * t),
        "LongAccel": -1.8 + 0.9 * np.cos(3.0 * np.pi * t),
        "VertAccel": 9.3 + 1.7 * np.sin(4.0 * np.pi * t),
        "Throttle": np.clip(0.15 + 0.9 * t + 0.12 * np.sin(6.0 * np.pi * t), 0.0, 1.0),
        "Brake": np.clip(1.0 - 1.1 * t + 0.08 * np.cos(8.0 * np.pi * t), 0.0, 1.0),
        "SteeringWheelAngle": 0.35 * np.sin(12.0 * np.pi * t) + 0.05 * np.sin(4.0 * np.pi * t),
        "Gear": np.full(n, 4.0, dtype=np.float64),
        "RPM": np.full(n, 6200.0, dtype=np.float64),
    }

    # Add multiple shifts in the corner entry phase to ensure shift metrics are populated.
    channels["Gear"][60:] = 3.0
    channels["Gear"][80:] = 4.0
    channels["Gear"][170:] = 5.0
    channels["RPM"] += 180.0 * np.sin(2.0 * np.pi * t) - 130.0 * (channels["Gear"] - 3.0)

    if include_torque:
        channels["SteeringWheelTorque"] = (
            0.8 * np.sin(12.0 * np.pi * t + 0.4) + 0.15 * np.cos(2.0 * np.pi * t)
        )
    if include_abs:
        channels["ABSactive"] = np.where((t > 0.30) & (t < 0.42), 1.0, 0.0)
    return channels


def _write_parquet(tmp_path: Path, channels: dict[str, np.ndarray]) -> Path:
    cols: dict[str, pa.Array] = {
        name: pa.array(values.astype(np.float64).tolist(), type=pa.float64())
        for name, values in channels.items()
    }
    out = tmp_path / "lap_resampled.parquet"
    pq.write_table(pa.table(cols), str(out))
    return out


def _extract(
    *,
    tmp_path: Path,
    channels: dict[str, np.ndarray],
    corners: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    parquet_path = _write_parquet(tmp_path, channels)
    return extract_corner_features(
        parquet_path=parquet_path,
        events={"events": {"throttle_on": {"lapdist_pct": 0.55}}},
        corner_map={"corner_map_version": 1, "corners": corners},
        run_id="run_0001",
        lap_id="lap_0001",
        track_key="Track__Test",
        car_key="Car__Test",
    )


def test_required_features_non_null_with_complete_input(tmp_path: Path) -> None:
    """All required features in implemented groups must be non-null with full input."""
    channels = _make_channels(include_abs=True, include_torque=True)
    rows = _extract(
        tmp_path=tmp_path,
        channels=channels,
        corners=[
            {
                "corner_id": 1,
                "start_lapdist_pct": 0.20,
                "end_lapdist_pct": 0.85,
                "corner_type": "compound",
                "radius_est": 55.0,
                "crest_present": True,
                "compression_present": True,
            }
        ],
    )
    assert len(rows) == 1
    row = rows[0]

    schema = _feature_schema()
    required_ids = [
        feature["id"]
        for group in _IMPLEMENTED_GROUPS
        for feature in schema["groups"][group]
        if feature.get("requires")
    ]
    missing_non_null = [feature_id for feature_id in required_ids if row.get(feature_id) is None]
    assert missing_non_null == [], (
        f"Expected required features to be non-null, but got null for: {missing_non_null}"
    )


def test_optional_features_null_when_channel_missing(tmp_path: Path) -> None:
    """Optional features must be null if their optional source channels are absent."""
    channels = _make_channels(include_abs=False, include_torque=False)
    rows = _extract(
        tmp_path=tmp_path,
        channels=channels,
        corners=[
            {
                "corner_id": 1,
                "start_lapdist_pct": 0.20,
                "end_lapdist_pct": 0.85,
                "corner_type": "single",
                "radius_est": 90.0,
                "crest_present": False,
                "compression_present": True,
            }
        ],
    )
    assert len(rows) == 1
    row = rows[0]

    assert row["abs_active_ratio"] is None
    assert row["torque_response_latency_ms"] is None
    assert row["torque_peak_during_correction"] is None


def test_output_schema_matches_feature_schema(tmp_path: Path) -> None:
    """Every feature id from schema must exist in every output row."""
    channels = _make_channels(include_abs=True, include_torque=True)
    corners = [
        {
            "corner_id": 1,
            "start_lapdist_pct": 0.15,
            "end_lapdist_pct": 0.50,
            "corner_type": "single",
            "radius_est": 80.0,
            "crest_present": False,
            "compression_present": True,
        },
        {
            "corner_id": 2,
            "start_lapdist_pct": 0.55,
            "end_lapdist_pct": 0.90,
            "corner_type": "compound",
            "radius_est": 50.0,
            "crest_present": True,
            "compression_present": True,
        },
    ]
    rows = _extract(tmp_path=tmp_path, channels=channels, corners=corners)
    assert len(rows) == len(corners)

    schema = _feature_schema()
    feature_ids = [
        feature["id"]
        for features in schema["groups"].values()
        for feature in features
    ]
    low_conf_ids = [
        feature["id"]
        for features in schema["groups"].values()
        for feature in features
        if feature.get("low_confidence")
    ]

    for row in rows:
        missing = [feature_id for feature_id in feature_ids if feature_id not in row]
        assert missing == [], f"Missing feature columns in output row: {missing}"

        missing_conf = [
            f"{feature_id}_confidence"
            for feature_id in low_conf_ids
            if f"{feature_id}_confidence" not in row
        ]
        assert missing_conf == [], f"Missing low-confidence meta columns: {missing_conf}"

        wrong_conf = [
            f"{feature_id}_confidence"
            for feature_id in low_conf_ids
            if row[f"{feature_id}_confidence"] != "low"
        ]
        assert wrong_conf == [], f"Unexpected low-confidence marker values: {wrong_conf}"
