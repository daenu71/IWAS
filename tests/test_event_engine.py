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

from core.coaching.event_engine import extract_corner_events, extract_lap_events  # noqa: E402

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
    session_time: np.ndarray | None = None,
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
        ("SessionTime", session_time),
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

    # brake_start and peak_brake are now arrays; must have at least one entry
    assert ev["brake_start"] is not None and len(ev["brake_start"]) > 0, (
        "brake_start should be detected"
    )
    assert ev["peak_brake"] is not None and len(ev["peak_brake"]) > 0, (
        "peak_brake should be detected"
    )
    assert ev["brake_release_end"] is not None, "brake_release_end should be detected"

    # Positions must be in the expected window (check first entry for each array event)
    assert 0.19 < ev["brake_start"][0]["lapdist_pct"] < 0.30, (
        f"brake_start at {ev['brake_start'][0]['lapdist_pct']!r} not in expected range"
    )
    assert 0.24 < ev["peak_brake"][0]["lapdist_pct"] < 0.36, (
        f"peak_brake at {ev['peak_brake'][0]['lapdist_pct']!r} not in expected range"
    )
    assert ev["brake_release_end"]["lapdist_pct"] > ev["peak_brake"][0]["lapdist_pct"], (
        "brake_release_end must come after peak_brake"
    )

    # peak_brake value must be close to 1.0
    assert ev["peak_brake"][0]["value"] is not None
    assert ev["peak_brake"][0]["value"] > 0.9

    # Output must contain all required event keys
    required_keys = {
        "turn_in_rate_based", "turn_in_angle_based",
        "brake_start", "peak_brake", "brake_release_start",
        "brake_release_end", "min_speed", "throttle_on", "throttle_full", "throttle_off",
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
    assert isinstance(ev["throttle_off"], (list, type(None)))


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

    # brake_start and peak_brake are now arrays: empty list when channel present but no events
    assert ev["brake_start"] == [], (
        f"brake_start should be [] when no braking, got {ev['brake_start']!r}"
    )
    assert ev["peak_brake"] == [], (
        f"peak_brake should be [] when no braking, got {ev['peak_brake']!r}"
    )
    # singular events remain null
    assert ev["brake_release_start"] is None, (
        f"brake_release_start should be null when no braking, got {ev['brake_release_start']!r}"
    )
    assert ev["brake_release_end"] is None, (
        f"brake_release_end should be null when no braking, got {ev['brake_release_end']!r}"
    )

    # Brake channel was present → no missing_channel entry for brake events
    mc = result["meta"]["missing_channels"]
    for name in ["brake_start", "peak_brake", "brake_release_start", "brake_release_end"]:
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
# Test 3: Throttle lift detection
# ---------------------------------------------------------------------------


def test_throttle_off_detected_as_array_event(tmp_path: Path) -> None:
    throttle = np.zeros(_N, dtype=np.float64)
    throttle[100:150] = 1.0
    throttle[150:220] = 0.4
    throttle[300:350] = 1.0
    throttle[350:430] = 0.2

    parquet_path = _write_parquet(tmp_path, throttle=throttle)
    cfg_path = _inline_config(tmp_path)

    result = extract_lap_events(parquet_path=parquet_path, config_path=cfg_path)
    ev = result["events"]

    assert ev["throttle_full"] is not None
    assert isinstance(ev["throttle_off"], list)
    assert len(ev["throttle_off"]) == 2
    assert [item["name"] for item in ev["throttle_off"]] == ["throttle_off", "throttle_off"]
    assert [item["value"] for item in ev["throttle_off"]] == [None, None]
    assert ev["throttle_off"][0]["lapdist_pct"] == pytest.approx(150 / _N, abs=1e-6)
    assert ev["throttle_off"][1]["lapdist_pct"] == pytest.approx(350 / _N, abs=1e-6)


def test_missing_throttle_channel_reports_throttle_off_in_meta(tmp_path: Path) -> None:
    parquet_path = _write_parquet(tmp_path)
    cfg_path = _inline_config(tmp_path)

    result = extract_lap_events(parquet_path=parquet_path, config_path=cfg_path)

    assert result["events"]["throttle_off"] is None
    assert result["meta"]["missing_channels"]["throttle_off"] == "Throttle"


def test_corner_events_include_throttle_off(tmp_path: Path) -> None:
    throttle = np.zeros(_N, dtype=np.float64)
    throttle[100:150] = 1.0
    throttle[150:220] = 0.4
    throttle[300:350] = 1.0
    throttle[350:430] = 0.2

    parquet_path = _write_parquet(tmp_path, throttle=throttle)
    cfg_path = _inline_config(tmp_path)
    corners = [
        {
            "corner_id": 7,
            "start_lapdist_pct": 0.0,
            "end_lapdist_pct": 0.3,
            "curvature_mean": 0.0,
        }
    ]

    result = extract_corner_events(
        parquet_path=parquet_path,
        corners=corners,
        config_path=cfg_path,
    )
    throttle_off = [
        item for item in result["corners"]["7"] if item.get("name") == "throttle_off"
    ]

    assert len(throttle_off) == 2
    assert throttle_off[0]["lapdist_pct"] == pytest.approx(150 / _N, abs=1e-6)
    assert throttle_off[1]["lapdist_pct"] == pytest.approx(350 / _N, abs=1e-6)


# ---------------------------------------------------------------------------
# Test 4: Determinism
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


# ---------------------------------------------------------------------------
# Test: Compound corner → two brake zones → two events per array type
# ---------------------------------------------------------------------------


def test_compound_corner_two_brake_starts(tmp_path: Path) -> None:
    """A compound corner with two separate brake zones produces two entries
    for brake_start, peak_brake, and min_speed in lap_events.json."""
    brake = np.zeros(_N, dtype=np.float64)
    speed = np.full(_N, 100.0, dtype=np.float64)

    # First brake zone: 20 % – 27 %
    z1_s = int(0.20 * _N)  # 400
    z1_e = int(0.27 * _N)  # 540
    # Second brake zone: 38 % – 45 %
    z2_s = int(0.38 * _N)  # 760
    z2_e = int(0.45 * _N)  # 900

    brake[z1_s:z1_e] = 0.8
    brake[z2_s:z2_e] = 0.7

    # Speed decreases through each brake zone to a local minimum at zone end
    speed[z1_s:z1_e] = np.linspace(100.0, 60.0, z1_e - z1_s)
    speed[z1_e : z1_e + 30] = 60.0          # apex 1
    speed[z2_s:z2_e] = np.linspace(90.0, 55.0, z2_e - z2_s)
    speed[z2_e : z2_e + 30] = 55.0          # apex 2

    parquet_path = _write_parquet(tmp_path, brake=brake, speed=speed)
    cfg_path = _inline_config(tmp_path)

    result = extract_lap_events(parquet_path=parquet_path, config_path=cfg_path)
    ev = result["events"]

    # brake_start: one rising edge per brake zone → 2 entries
    assert isinstance(ev["brake_start"], list), "brake_start must be a list"
    assert len(ev["brake_start"]) == 2, (
        f"Expected 2 brake_start events for compound corner, got {len(ev['brake_start'])}"
    )
    assert ev["brake_start"][0]["lapdist_pct"] == pytest.approx(z1_s / _N, abs=2e-3)
    assert ev["brake_start"][1]["lapdist_pct"] == pytest.approx(z2_s / _N, abs=2e-3)

    # peak_brake: one max per brake run → 2 entries
    assert isinstance(ev["peak_brake"], list), "peak_brake must be a list"
    assert len(ev["peak_brake"]) == 2, (
        f"Expected 2 peak_brake events for compound corner, got {len(ev['peak_brake'])}"
    )
    assert ev["peak_brake"][0]["value"] == pytest.approx(0.8, abs=1e-3)
    assert ev["peak_brake"][1]["value"] == pytest.approx(0.7, abs=1e-3)

    # min_speed: one minimum per brake zone window → 2 entries
    assert isinstance(ev["min_speed"], list), "min_speed must be a list"
    assert len(ev["min_speed"]) == 2, (
        f"Expected 2 min_speed events for compound corner, got {len(ev['min_speed'])}"
    )
    assert ev["min_speed"][0]["value"] == pytest.approx(60.0, abs=0.1)
    assert ev["min_speed"][1]["value"] == pytest.approx(55.0, abs=0.1)


# ---------------------------------------------------------------------------
# Tests: turn_in_rate_based and turn_in_angle_based
# ---------------------------------------------------------------------------


def _make_session_time(n: int = _N, lap_time_s: float = 90.0) -> np.ndarray:
    """Linear session time over one lap."""
    return np.linspace(0.0, lap_time_s, n, dtype=np.float64)


def _turn_in_cfg(tmp_path: Path, **extra) -> Path:
    defaults = {
        "threshold_turn_in_angle_fraction": 0.10,
        "threshold_turn_in_rate_min_rads": 0.01,
        "threshold_turn_in_confidence_dist": 0.005,
    }
    defaults.update(extra)
    return _inline_config(tmp_path, defaults)


def test_turn_in_rate_based_detected(tmp_path: Path) -> None:
    """turn_in_rate_based must be detected within the brake_peak → min_speed window
    at the sample with the highest |SteeringRate|."""
    n = _N
    brake = np.zeros(n, dtype=np.float64)
    speed = np.full(n, 100.0, dtype=np.float64)
    steering = np.zeros(n, dtype=np.float64)
    st = _make_session_time(n)

    # Brake zone: ramp up 20–22 %, peak at 22 %, ramp down 22–27 %
    b_s = int(0.20 * n)          # 400
    b_peak = int(0.22 * n)       # 440  ← brake peak
    b_e = int(0.27 * n)          # 540
    apex = int(0.31 * n)         # 620  ← speed minimum (well inside lookahead)
    brake[b_s:b_peak] = np.linspace(0.1, 0.9, b_peak - b_s)
    brake[b_peak] = 0.9
    brake[b_peak:b_e] = np.linspace(0.9, 0.0, b_e - b_peak)
    speed[b_s : apex + 1] = np.linspace(100.0, 60.0, apex + 1 - b_s)

    # Steering ramp starts just after brake_peak (23 %) → clear rate peak in window
    turn_start = int(0.23 * n)   # 460
    steering[turn_start:apex] = np.linspace(0.0, 0.6, apex - turn_start)
    steering[apex:] = 0.6

    parquet_path = _write_parquet(
        tmp_path, brake=brake, speed=speed, steering=steering, session_time=st
    )
    cfg_path = _turn_in_cfg(tmp_path)

    result = extract_lap_events(parquet_path=parquet_path, config_path=cfg_path)
    ev = result["events"]

    assert ev["turn_in_rate_based"] is not None
    assert len(ev["turn_in_rate_based"]) >= 1

    ti = ev["turn_in_rate_based"][0]
    assert ti["name"] == "turn_in_rate_based"
    assert "confidence" in ti
    assert ti["confidence"] in ("high", "low")
    assert 0.20 < ti["lapdist_pct"] < 0.40, (
        f"turn_in_rate_based at {ti['lapdist_pct']!r} not in expected range"
    )
    assert ti["value"] > 0.0


def test_turn_in_angle_based_detected(tmp_path: Path) -> None:
    """turn_in_angle_based must be detected at the first sample exceeding
    threshold_turn_in_angle_fraction × SteeringPeak within the search window."""
    n = _N
    brake = np.zeros(n, dtype=np.float64)
    speed = np.full(n, 100.0, dtype=np.float64)
    steering = np.zeros(n, dtype=np.float64)

    # Brake zone with clear peak at 22 %
    b_s = int(0.20 * n)
    b_peak = int(0.22 * n)
    b_e = int(0.27 * n)
    apex = int(0.31 * n)
    brake[b_s:b_peak] = np.linspace(0.1, 0.9, b_peak - b_s)
    brake[b_peak] = 0.9
    brake[b_peak:b_e] = np.linspace(0.9, 0.0, b_e - b_peak)
    speed[b_s : apex + 1] = np.linspace(100.0, 55.0, apex + 1 - b_s)

    # Steering ramps from 0 → 0.5 rad starting just after brake_peak
    turn_start = int(0.23 * n)
    steering[turn_start : apex + 1] = np.linspace(0.0, 0.5, apex + 1 - turn_start)
    steering[apex:] = 0.5

    parquet_path = _write_parquet(tmp_path, brake=brake, speed=speed, steering=steering)
    cfg_path = _turn_in_cfg(tmp_path)

    result = extract_lap_events(parquet_path=parquet_path, config_path=cfg_path)
    ev = result["events"]

    assert ev["turn_in_angle_based"] is not None
    assert len(ev["turn_in_angle_based"]) >= 1

    ti = ev["turn_in_angle_based"][0]
    assert ti["name"] == "turn_in_angle_based"
    assert "confidence" in ti
    assert ti["confidence"] in ("high", "low")
    # Must fall within the brake_peak-to-apex window
    assert 0.21 < ti["lapdist_pct"] < 0.35, (
        f"turn_in_angle_based at {ti['lapdist_pct']!r} not in expected range"
    )
    assert ti["value"] > 0.0


def test_turn_in_confidence_high_when_close(tmp_path: Path) -> None:
    """When rate-based and angle-based turn-in positions agree within
    threshold_turn_in_confidence_dist, confidence must be 'high'."""
    n = _N
    brake = np.zeros(n, dtype=np.float64)
    speed = np.full(n, 100.0, dtype=np.float64)
    steering = np.zeros(n, dtype=np.float64)
    st = _make_session_time(n)

    b_s = int(0.20 * n)
    b_peak = int(0.22 * n)
    b_e = int(0.27 * n)
    apex = int(0.31 * n)
    brake[b_s:b_peak] = np.linspace(0.1, 0.9, b_peak - b_s)
    brake[b_peak] = 0.9
    brake[b_peak:b_e] = np.linspace(0.9, 0.0, b_e - b_peak)
    speed[b_s : apex + 1] = np.linspace(100.0, 60.0, apex + 1 - b_s)

    turn_start = int(0.23 * n)
    steering[turn_start : apex + 1] = np.linspace(0.0, 0.5, apex + 1 - turn_start)
    steering[apex:] = 0.5

    parquet_path = _write_parquet(
        tmp_path, brake=brake, speed=speed, steering=steering, session_time=st
    )
    # Use a generous confidence window (5 % lapdist) so this test is robust
    cfg_path = _turn_in_cfg(tmp_path, threshold_turn_in_confidence_dist=0.05)

    result = extract_lap_events(parquet_path=parquet_path, config_path=cfg_path)
    ev = result["events"]

    rate_evs = ev["turn_in_rate_based"]
    angle_evs = ev["turn_in_angle_based"]

    assert rate_evs is not None and len(rate_evs) >= 1
    assert angle_evs is not None and len(angle_evs) >= 1

    assert rate_evs[0]["confidence"] == "high", (
        f"Expected high confidence but got {rate_evs[0]['confidence']!r}"
    )
    assert angle_evs[0]["confidence"] == "high", (
        f"Expected high confidence but got {angle_evs[0]['confidence']!r}"
    )


def test_turn_in_no_steering_channel(tmp_path: Path) -> None:
    """When SteeringWheelAngle channel is absent, both turn-in events must be null."""
    parquet_path = _write_parquet(tmp_path)  # only LapDistPct
    cfg_path = _inline_config(tmp_path)

    result = extract_lap_events(parquet_path=parquet_path, config_path=cfg_path)
    ev = result["events"]

    assert ev["turn_in_rate_based"] is None
    assert ev["turn_in_angle_based"] is None
    mc = result["meta"]["missing_channels"]
    assert "turn_in_rate_based" in mc or "turn_in_angle_based" in mc


def test_turn_in_compound_corner_two_zones(tmp_path: Path) -> None:
    """Compound corner with two brake zones produces two turn-in events per definition."""
    n = _N
    brake = np.zeros(n, dtype=np.float64)
    speed = np.full(n, 100.0, dtype=np.float64)
    steering = np.zeros(n, dtype=np.float64)

    z1_s, z1_e = int(0.20 * n), int(0.27 * n)
    z2_s, z2_e = int(0.38 * n), int(0.45 * n)
    apex1, apex2 = int(0.30 * n), int(0.48 * n)

    brake[z1_s:z1_e] = 0.8
    brake[z2_s:z2_e] = 0.7
    speed[z1_s : apex1 + 1] = np.linspace(100.0, 60.0, apex1 + 1 - z1_s)
    speed[z2_s : apex2 + 1] = np.linspace(90.0, 55.0, apex2 + 1 - z2_s)

    ti1, ti2 = int(0.24 * n), int(0.42 * n)
    steering[ti1 : apex1 + 1] = np.linspace(0.0, 0.4, apex1 + 1 - ti1)
    steering[ti2 : apex2 + 1] = np.linspace(0.0, 0.35, apex2 + 1 - ti2)

    parquet_path = _write_parquet(tmp_path, brake=brake, speed=speed, steering=steering)
    cfg_path = _turn_in_cfg(tmp_path, threshold_turn_in_rate_min_rads=0.001)

    result = extract_lap_events(parquet_path=parquet_path, config_path=cfg_path)
    ev = result["events"]

    assert isinstance(ev["turn_in_rate_based"], list)
    assert isinstance(ev["turn_in_angle_based"], list)
    assert len(ev["turn_in_rate_based"]) == 2, (
        f"Expected 2 rate-based turn-in events, got {len(ev['turn_in_rate_based'])}"
    )
    assert len(ev["turn_in_angle_based"]) == 2, (
        f"Expected 2 angle-based turn-in events, got {len(ev['turn_in_angle_based'])}"
    )
    assert ev["turn_in_rate_based"][0]["lapdist_pct"] < ev["turn_in_rate_based"][1]["lapdist_pct"]
    assert ev["turn_in_angle_based"][0]["lapdist_pct"] < ev["turn_in_angle_based"][1]["lapdist_pct"]
