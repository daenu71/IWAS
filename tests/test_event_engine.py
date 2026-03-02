"""Unit tests for core.coaching.event_engine – Story 2.3.1."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.coaching.event_engine import extract_lap_events  # noqa: E402

_N = 2000  # grid size matching Story 2.1.1 default

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ldp(n: int = _N) -> np.ndarray:
    return np.linspace(0.0, 1.0 - 1.0 / n, n, dtype=np.float32)


def _write_parquet(
    tmp_path: Path,
    *,
    brake: np.ndarray | None = None,
    throttle: np.ndarray | None = None,
    speed: np.ndarray | None = None,
    gear: np.ndarray | None = None,
    steering: np.ndarray | None = None,
    yaw_rate: np.ndarray | None = None,
    vert_accel: np.ndarray | None = None,
    filename: str = "lap_resampled.parquet",
) -> Path:
    ldp = _make_ldp()
    cols: dict[str, pa.Array] = {
        "LapDistPct": pa.array(ldp.tolist(), type=pa.float32()),
    }
    for col_name, arr in [
        ("Brake", brake),
        ("Throttle", throttle),
        ("Speed", speed),
        ("Gear", gear),
        ("SteeringWheelAngle", steering),
        ("YawRate", yaw_rate),
        ("VertAccel", vert_accel),
    ]:
        if arr is not None:
            cols[col_name] = pa.array(arr.astype(np.float32).tolist(), type=pa.float32())

    out = tmp_path / filename
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(cols), str(out))
    return out


def _inline_config(tmp_path: Path, overrides: dict | None = None) -> Path:
    """Write a minimal event config to tmp_path and return its path."""
    cfg = {
        "version": 1,
        "threshold_brake_start": 0.05,
        "threshold_brake_off": 0.05,
        "threshold_steering_turn_in": 0.05,
        "threshold_throttle_on": 0.05,
        "threshold_throttle_full": 0.95,
        "threshold_yawrate_spike": 0.5,
        "threshold_oversteer_yawrate": 0.3,
        "threshold_understeer_yawrate": 0.3,
        "threshold_crest": -2.0,
        "threshold_compression": 3.0,
        "min_oversteer_samples": 3,
        "min_understeer_samples": 3,
        "min_crest_samples": 3,
        "min_compression_samples": 3,
    }
    if overrides:
        cfg.update(overrides)
    path = tmp_path / "event_config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Test 1: Synthetic lap with a clear braking event
# ---------------------------------------------------------------------------


def test_braking_events_detected(tmp_path: Path) -> None:
    """brake_start, peak_brake, brake_release_end must be present for a
    clear braking zone.  Positions must be within the expected LapDistPct
    window."""
    brake = np.zeros(_N, dtype=np.float64)
    # Braking zone: 20 % → peak at 25 % → release ends by 35 %
    ramp_up_start = int(0.20 * _N)    # 400
    peak_idx = int(0.25 * _N)          # 500
    ramp_down_end = int(0.35 * _N)    # 700

    brake[ramp_up_start:peak_idx] = np.linspace(0.1, 1.0, peak_idx - ramp_up_start)
    brake[peak_idx : ramp_down_end - 100] = 1.0
    brake[ramp_down_end - 100 : ramp_down_end] = np.linspace(1.0, 0.0, 100)

    parquet_path = _write_parquet(tmp_path, brake=brake)
    cfg_path = _inline_config(tmp_path)

    result = extract_lap_events(parquet_path=parquet_path, config_path=cfg_path)
    ev = result["events"]

    # All three key brake events must be non-null
    assert ev["brake_start"] is not None, "brake_start should be detected"
    assert ev["peak_brake"] is not None, "peak_brake should be detected"
    assert ev["brake_release_end"] is not None, "brake_release_end should be detected"

    # Positions must be in the expected window
    assert 0.19 < ev["brake_start"]["lapdist_pct"] < 0.30, (
        f"brake_start at {ev['brake_start']['lapdist_pct']!r} not in expected range"
    )
    assert 0.24 < ev["peak_brake"]["lapdist_pct"] < 0.36, (
        f"peak_brake at {ev['peak_brake']['lapdist_pct']!r} not in expected range"
    )
    assert ev["brake_release_end"]["lapdist_pct"] > ev["peak_brake"]["lapdist_pct"], (
        "brake_release_end must come after peak_brake"
    )

    # peak_brake value must be close to 1.0
    assert ev["peak_brake"]["value"] is not None
    assert ev["peak_brake"]["value"] > 0.9

    # Output must contain all required event keys
    required_keys = {
        "turn_in", "brake_start", "peak_brake", "brake_release_start",
        "brake_release_end", "min_speed", "throttle_on", "throttle_full",
        "gear_change", "oversteer_event", "understeer_event", "crest", "compression",
    }
    assert required_keys == set(ev.keys()), (
        f"Missing keys: {required_keys - set(ev.keys())}"
    )

    # meta must carry config_version
    assert result["meta"]["config_version"] == 1

    # Array events must be lists (not None) when channel present but not detected
    # (Brake was given; gear_change, oversteer etc. channels absent → None is ok)
    assert isinstance(ev["gear_change"], (list, type(None)))


def test_braking_events_write_output(tmp_path: Path) -> None:
    """extract_lap_events must write a valid JSON file when output_path is given."""
    brake = np.zeros(_N, dtype=np.float64)
    brake[400:600] = 1.0
    brake[600:700] = np.linspace(1.0, 0.0, 100)

    parquet_path = _write_parquet(tmp_path, brake=brake)
    cfg_path = _inline_config(tmp_path)
    output_path = tmp_path / "analysis" / "lap_events.json"

    extract_lap_events(
        parquet_path=parquet_path,
        config_path=cfg_path,
        output_path=output_path,
    )

    assert output_path.exists(), "lap_events.json was not created"
    loaded = json.loads(output_path.read_text(encoding="utf-8"))
    assert "events" in loaded
    assert "meta" in loaded


# ---------------------------------------------------------------------------
# Test 2: Synthetic lap without braking → all brake events null
# ---------------------------------------------------------------------------


def test_no_braking_all_brake_events_null(tmp_path: Path) -> None:
    """When the Brake channel is present but always zero, all brake-related
    events must be null (channel present, no event detected)."""
    brake = np.zeros(_N, dtype=np.float64)

    parquet_path = _write_parquet(tmp_path, brake=brake)
    cfg_path = _inline_config(tmp_path)

    result = extract_lap_events(parquet_path=parquet_path, config_path=cfg_path)
    ev = result["events"]

    brake_events = ["brake_start", "peak_brake", "brake_release_start", "brake_release_end"]
    for name in brake_events:
        assert ev[name] is None, (
            f"{name} should be null when no braking, got {ev[name]!r}"
        )

    # Brake channel was present → no missing_channel entry for brake events
    mc = result["meta"]["missing_channels"]
    for name in brake_events:
        assert name not in mc, (
            f"{name} should not appear in missing_channels when channel is present"
        )


def test_missing_brake_channel_reported_in_meta(tmp_path: Path) -> None:
    """When the Brake channel is absent entirely, brake events are null and
    the missing channel is recorded in meta."""
    # Write parquet WITHOUT Brake channel
    parquet_path = _write_parquet(tmp_path)  # only LapDistPct
    cfg_path = _inline_config(tmp_path)

    result = extract_lap_events(parquet_path=parquet_path, config_path=cfg_path)
    ev = result["events"]
    mc = result["meta"]["missing_channels"]

    assert ev["brake_start"] is None
    assert ev["peak_brake"] is None
    assert "brake_start" in mc or "peak_brake" in mc, (
        "At least one brake event must reference missing 'Brake' channel in meta"
    )
    # The reported channel must be 'Brake'
    for key in ("brake_start", "peak_brake", "brake_release_start", "brake_release_end"):
        if key in mc:
            assert mc[key] == "Brake"


# ---------------------------------------------------------------------------
# Test 3: Determinism
# ---------------------------------------------------------------------------


def test_determinism(tmp_path: Path) -> None:
    """Two calls with identical input must produce identical JSON output."""
    brake = np.zeros(_N, dtype=np.float64)
    brake[400:500] = np.linspace(0.0, 1.0, 100)
    brake[500:600] = 1.0
    brake[600:700] = np.linspace(1.0, 0.0, 100)

    gear = np.ones(_N, dtype=np.float64)
    gear[300:] = 2.0
    gear[600:] = 3.0
    gear[900:] = 4.0

    parquet_path = _write_parquet(tmp_path, brake=brake, gear=gear)
    cfg_path = _inline_config(tmp_path)

    result1 = extract_lap_events(parquet_path=parquet_path, config_path=cfg_path)
    result2 = extract_lap_events(parquet_path=parquet_path, config_path=cfg_path)

    json1 = json.dumps(result1, sort_keys=True)
    json2 = json.dumps(result2, sort_keys=True)

    assert json1 == json2, "extract_lap_events is not deterministic"


# ---------------------------------------------------------------------------
# Additional: understeer null when no corner map
# ---------------------------------------------------------------------------


def test_understeer_null_without_corner_map(tmp_path: Path) -> None:
    """understeer_event must be null when no corner_map is provided."""
    parquet_path = _write_parquet(tmp_path)
    cfg_path = _inline_config(tmp_path)

    result = extract_lap_events(
        parquet_path=parquet_path,
        config_path=cfg_path,
        corner_map=None,
    )
    assert result["events"]["understeer_event"] is None


def test_understeer_empty_list_with_corner_map_no_events(tmp_path: Path) -> None:
    """understeer_event must be [] when corner_map is provided but no understeer
    is detected (car drives faster than expected yaw rate)."""
    speed = np.full(_N, 50.0, dtype=np.float64)    # 50 m/s
    yaw_rate = np.full(_N, 5.0, dtype=np.float64)  # very high yaw → no understeer

    parquet_path = _write_parquet(tmp_path, speed=speed, yaw_rate=yaw_rate)
    cfg_path = _inline_config(tmp_path)

    ldp = _make_ldp()
    fake_corner_map = {
        "corners": [
            {
                "corner_id": 1,
                "start_lapdist_pct": 0.1,
                "end_lapdist_pct": 0.2,
                "curvature_mean": 0.05,
                "curvature_peak": 0.07,
            }
        ]
    }

    result = extract_lap_events(
        parquet_path=parquet_path,
        config_path=cfg_path,
        corner_map=fake_corner_map,
    )
    assert result["events"]["understeer_event"] == [], (
        "Expected empty list when no understeer detected"
    )
