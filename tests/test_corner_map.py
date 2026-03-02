"""Unit tests for core.coaching.corner_map – Story 2.2.1."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.coaching.corner_map import build_corner_map, load_corner_map  # noqa: E402

_N = 2000  # match the default grid from Story 2.1.1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_lap_parquet(
    tmp_path: Path,
    *,
    yaw_rate: np.ndarray,
    speed: np.ndarray,
    vert_accel: np.ndarray | None = None,
    filename: str = "lap_resampled.parquet",
) -> Path:
    """Write a minimal resampled-lap parquet with YawRate and Speed columns."""
    n = len(yaw_rate)
    lap_dist = np.linspace(0.0, 1.0 - 1.0 / n, n, dtype=np.float32)

    cols: dict[str, pa.Array] = {
        "LapDistPct": pa.array(lap_dist.tolist(), type=pa.float32()),
        "YawRate": pa.array(yaw_rate.astype(np.float32).tolist(), type=pa.float32()),
        "Speed": pa.array(speed.astype(np.float32).tolist(), type=pa.float32()),
    }
    if vert_accel is not None:
        cols["VertAccel"] = pa.array(
            vert_accel.astype(np.float32).tolist(), type=pa.float32()
        )

    table = pa.table(cols)
    out = tmp_path / filename
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, str(out))
    return out


# ---------------------------------------------------------------------------
# Test 1 – Straight track → 0 corners
# ---------------------------------------------------------------------------


def test_straight_track_no_corners(tmp_path: Path) -> None:
    """A straight lap with zero yaw rate must produce an empty corner list."""
    yaw_rate = np.zeros(_N, dtype=np.float64)
    speed = np.full(_N, 50.0, dtype=np.float64)  # constant 50 m/s

    parquet = _write_lap_parquet(tmp_path, yaw_rate=yaw_rate, speed=speed)
    corner_map = build_corner_map(
        parquet_path=parquet,
        storage_root=tmp_path,
        track_key="TestTrack__Straight",
    )

    assert isinstance(corner_map, dict)
    assert corner_map["corners"] == [], (
        f"Expected 0 corners on a straight, got {len(corner_map['corners'])}: "
        f"{corner_map['corners']}"
    )
    assert corner_map["corner_map_version"] == 1

    # File must have been written
    map_file = tmp_path / "corner_maps" / "TestTrack__Straight" / "corner_map_v1.json"
    assert map_file.exists()


# ---------------------------------------------------------------------------
# Test 2 – Synthetic single corner → 1 corner, plausible radius
# ---------------------------------------------------------------------------


def test_single_corner_radius_plausible(tmp_path: Path) -> None:
    """A single Gaussian curvature bump should yield exactly 1 corner
    with a radius estimate close to the synthetic ground truth."""
    lap_dist = np.linspace(0.0, 1.0 - 1.0 / _N, _N, dtype=np.float64)
    speed = np.full(_N, 30.0, dtype=np.float64)  # 30 m/s

    # Synthetic medium corner: κ_peak = 1/50 m ≈ 0.020 1/m → radius ≈ 50 m
    # Gaussian profile centred at lap_dist 0.5, σ = 0.05 LapDistPct
    kappa_peak = 1.0 / 50.0
    sigma = 0.05
    center = 0.5
    curvature = kappa_peak * np.exp(-0.5 * ((lap_dist - center) / sigma) ** 2)
    yaw_rate = curvature * speed  # YawRate = κ × Speed

    parquet = _write_lap_parquet(tmp_path, yaw_rate=yaw_rate, speed=speed)
    corner_map = build_corner_map(
        parquet_path=parquet,
        storage_root=tmp_path,
        track_key="TestTrack__SingleCorner",
    )

    corners = corner_map["corners"]
    assert len(corners) == 1, (
        f"Expected 1 corner, got {len(corners)}: {corners}"
    )

    c = corners[0]

    # Corner ID must be 1
    assert c["corner_id"] == 1

    # Radius should be within ±30 % of the synthetic 50 m
    r = c["radius_est"]
    assert r is not None, "radius_est must not be None"
    assert 35.0 < r < 65.0, f"radius_est={r:.1f} m is outside expected range 35–65 m"

    # Corner type: radius 35–65 m falls in 'medium' (30–80 m)
    assert c["corner_type"] == "medium", (
        f"Expected 'medium', got '{c['corner_type']}'"
    )

    # LapDistPct bounds must span the centre of the corner
    assert c["start_lapdist_pct"] < center < c["end_lapdist_pct"], (
        f"Corner [{c['start_lapdist_pct']:.4f}, {c['end_lapdist_pct']:.4f}] "
        f"does not span centre {center}"
    )

    # curvature_peak should be close to kappa_peak (within smoothing tolerance)
    assert c["curvature_peak"] > 0.010, (
        f"curvature_peak={c['curvature_peak']:.4f} seems too low"
    )


# ---------------------------------------------------------------------------
# Test 3 – Reproducibility: same input → same corner IDs
# ---------------------------------------------------------------------------


def test_reproducibility(tmp_path: Path) -> None:
    """Two build_corner_map calls on identical input must produce identical
    corner IDs, types, and positional bounds (version may differ)."""
    lap_dist = np.linspace(0.0, 1.0 - 1.0 / _N, _N, dtype=np.float64)
    speed = np.full(_N, 25.0, dtype=np.float64)

    # Two corners:
    #   corner A at lap_dist ~0.25 (hairpin, κ ~ 1/20 = 0.05)
    #   corner B at lap_dist ~0.70 (sweeper, κ ~ 1/120 ≈ 0.0083)
    kappa_a = 1.0 / 20.0
    kappa_b = 1.0 / 120.0
    curv_a = kappa_a * np.exp(-0.5 * ((lap_dist - 0.25) / 0.04) ** 2)
    curv_b = kappa_b * np.exp(-0.5 * ((lap_dist - 0.70) / 0.06) ** 2)
    curvature = curv_a + curv_b
    yaw_rate = curvature * speed

    # Use two separate storage roots so versions start at 1 independently
    root_a = tmp_path / "run_a"
    root_b = tmp_path / "run_b"

    parquet_a = _write_lap_parquet(root_a, yaw_rate=yaw_rate, speed=speed)
    parquet_b = _write_lap_parquet(root_b, yaw_rate=yaw_rate, speed=speed)

    track_key = "TestTrack__TwoCorners"
    map_a = build_corner_map(
        parquet_path=parquet_a, storage_root=root_a, track_key=track_key
    )
    map_b = build_corner_map(
        parquet_path=parquet_b, storage_root=root_b, track_key=track_key
    )

    corners_a = map_a["corners"]
    corners_b = map_b["corners"]

    assert len(corners_a) == len(corners_b), (
        f"Corner counts differ: {len(corners_a)} vs {len(corners_b)}"
    )
    assert len(corners_a) >= 2, (
        f"Expected at least 2 corners, got {len(corners_a)}"
    )

    # Compare IDs, positions, and types (version is allowed to differ)
    _IGNORED = {"corner_map_version"}
    for i, (ca, cb) in enumerate(zip(corners_a, corners_b)):
        for key in ca:
            if key in _IGNORED:
                continue
            va, vb = ca[key], cb[key]
            assert va == vb, (
                f"Corner {i} key '{key}' differs: {va!r} vs {vb!r}"
            )

    # load_corner_map round-trip
    loaded = load_corner_map(storage_root=root_a, track_key=track_key)
    assert loaded is not None
    assert loaded["corners"] == corners_a


# ---------------------------------------------------------------------------
# Test 4 – Missing Z channel → crest/compression = None, no crash
# ---------------------------------------------------------------------------


def test_no_crash_without_z_axis(tmp_path: Path) -> None:
    """build_corner_map must not crash when VertAccel is absent;
    crest_present and compression_present must be None."""
    lap_dist = np.linspace(0.0, 1.0 - 1.0 / _N, _N, dtype=np.float64)
    speed = np.full(_N, 40.0, dtype=np.float64)
    kappa = 1.0 / 60.0
    curvature = kappa * np.exp(-0.5 * ((lap_dist - 0.5) / 0.05) ** 2)
    yaw_rate = curvature * speed

    # No vert_accel column
    parquet = _write_lap_parquet(tmp_path, yaw_rate=yaw_rate, speed=speed)
    corner_map = build_corner_map(
        parquet_path=parquet,
        storage_root=tmp_path,
        track_key="TestTrack__NoZ",
    )

    assert len(corner_map["corners"]) >= 1
    for c in corner_map["corners"]:
        assert c["crest_present"] is None, (
            f"crest_present should be None without Z data, got {c['crest_present']}"
        )
        assert c["compression_present"] is None, (
            f"compression_present should be None without Z data, "
            f"got {c['compression_present']}"
        )
