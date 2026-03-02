"""Unit tests for core.coaching.resample_lapdist – Story 2.1.1."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

# ---------------------------------------------------------------------------
# Ensure src/ is importable when running from project root or tests/
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.coaching.resample_lapdist import resample_lap  # noqa: E402

_GRID_STEP = 0.0005
_N_GRID = int(round(1.0 / _GRID_STEP))  # 2000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run_parquet(tmp_path: Path, lap_dist: np.ndarray, extra: dict | None = None) -> Path:
    """Write a minimal run parquet with LapDistPct and optional extra channels."""
    n = len(lap_dist)
    cols: dict[str, pa.Array] = {
        "ts": pa.array(np.arange(n, dtype=np.float64).tolist(), type=pa.float64()),
        "monotonic_ts": pa.array(np.arange(n, dtype=np.float64).tolist(), type=pa.float64()),
        "LapDistPct": pa.array(lap_dist.astype(np.float32).tolist(), type=pa.float32()),
    }
    if extra:
        for col_name, arr in extra.items():
            if arr.dtype == bool or arr.dtype == np.bool_:
                cols[col_name] = pa.array(arr.tolist(), type=pa.bool_())
            elif np.issubdtype(arr.dtype, np.integer):
                cols[col_name] = pa.array(arr.astype(np.int32).tolist(), type=pa.int32())
            else:
                cols[col_name] = pa.array(arr.astype(np.float32).tolist(), type=pa.float32())
    table = pa.table(cols)
    out = tmp_path / "run_test.parquet"
    pq.write_table(table, str(out))
    return out


def _read_output(output_path: Path) -> dict[str, list]:
    """Return the output parquet as a column dict."""
    return pq.read_table(str(output_path)).to_pydict()


# ---------------------------------------------------------------------------
# Test 1 – Synthetic data with known grid → verify output values
# ---------------------------------------------------------------------------


def test_synthetic_linear_channel(tmp_path: Path) -> None:
    """Float channel that equals LapDistPct * 200 should be resampled correctly."""
    n = 1000
    lap_dist = np.linspace(0.0, 0.999, n, dtype=np.float64)
    speed = (lap_dist * 200.0).astype(np.float32)

    run_parquet = _make_run_parquet(tmp_path, lap_dist, {"Speed": speed})
    output_path = tmp_path / "lap_resampled.parquet"

    meta = resample_lap(
        parquet_path=run_parquet,
        start_idx=0,
        end_idx=n - 1,
        output_path=output_path,
        grid_step=_GRID_STEP,
    )

    assert output_path.exists(), "output parquet must be created"

    # Meta assertions
    assert "coverage_pct" in meta
    assert meta["coverage_pct"] > 0.95, f"coverage_pct={meta['coverage_pct']}"
    assert not meta["low_coverage"]
    assert meta["n_input_rows"] == n
    assert meta["n_grid_points"] == _N_GRID

    data = _read_output(output_path)

    # Grid shape
    assert len(data["LapDistPct"]) == _N_GRID
    assert len(data["Speed"]) == _N_GRID

    # LapDistPct output IS the grid
    grid = np.linspace(0.0, 1.0, _N_GRID, endpoint=False)
    for i, (expected, actual) in enumerate(zip(grid, data["LapDistPct"])):
        assert abs(float(actual) - expected) < 1e-5, f"grid[{i}] mismatch"

    # Speed should equal LapDistPct * 200 within interpolation tolerance
    for i, (ldp, spd) in enumerate(zip(data["LapDistPct"], data["Speed"])):
        if spd is None or ldp is None or math.isnan(float(spd)):
            continue
        expected_speed = float(ldp) * 200.0
        assert abs(float(spd) - expected_speed) < 0.5, (
            f"Speed[{i}]: expected {expected_speed:.3f}, got {float(spd):.3f}"
        )


def test_gear_channel_nearest_neighbor(tmp_path: Path) -> None:
    """Int channel (Gear) should be resampled with nearest-neighbour."""
    n = 400
    lap_dist = np.linspace(0.0, 0.999, n, dtype=np.float64)
    # Gear: 1 for first half, 2 for second half
    gear = np.where(lap_dist < 0.5, np.int32(1), np.int32(2))

    run_parquet = _make_run_parquet(tmp_path, lap_dist, {"Gear": gear})
    output_path = tmp_path / "lap_gear.parquet"

    resample_lap(
        parquet_path=run_parquet,
        start_idx=0,
        end_idx=n - 1,
        output_path=output_path,
        grid_step=_GRID_STEP,
    )

    data = _read_output(output_path)
    grid = np.linspace(0.0, 1.0, _N_GRID, endpoint=False)
    for i, (ldp, g) in enumerate(zip(grid, data["Gear"])):
        if g is None or math.isnan(float(g)):
            continue
        gv = float(g)
        # Nearest-neighbour must produce integer-like values only (1 or 2, no blending)
        assert abs(gv - round(gv)) < 0.01, (
            f"Gear[{i}] at LapDistPct={ldp:.4f}: expected integer-like value, got {gv:.3f}"
        )
        assert gv in (1.0, 2.0), f"Gear[{i}] must be 1 or 2, got {gv}"
        # Far from boundary: assert the specific value
        if ldp < 0.45:
            assert gv == 1.0, f"Gear[{i}] at LapDistPct={ldp:.4f}: expected 1, got {gv}"
        elif ldp > 0.55:
            assert gv == 2.0, f"Gear[{i}] at LapDistPct={ldp:.4f}: expected 2, got {gv}"


# ---------------------------------------------------------------------------
# Test 2 – Wrap-around (0.99 → 0.01 in raw data)
# ---------------------------------------------------------------------------


def test_wrap_around(tmp_path: Path) -> None:
    """Lap starting at 0.5, crossing finish line (0.99→0.01), ending at 0.3."""
    part1 = np.linspace(0.5, 0.99, 600, dtype=np.float64)
    part2 = np.linspace(0.01, 0.3, 400, dtype=np.float64)
    lap_dist = np.concatenate([part1, part2])
    n = len(lap_dist)

    # Speed linearly follows LapDistPct * 100 (using unwrapped domain)
    speed = (lap_dist * 100.0).astype(np.float32)
    # After the wrap, lap_dist is 0.01..0.3 (raw), but in the unwrapped domain
    # those represent 1.01..1.3, so speed values will be 1..30.

    run_parquet = _make_run_parquet(tmp_path, lap_dist, {"Speed": speed})
    output_path = tmp_path / "lap_wrap.parquet"

    meta = resample_lap(
        parquet_path=run_parquet,
        start_idx=0,
        end_idx=n - 1,
        output_path=output_path,
        grid_step=_GRID_STEP,
    )

    assert output_path.exists()
    assert "coverage_pct" in meta

    data = _read_output(output_path)
    grid = np.linspace(0.0, 1.0, _N_GRID, endpoint=False)

    # Grid points in [0.5, 0.9995] must have valid (non-NaN) Speed values
    for i, ldp in enumerate(grid):
        if 0.5 <= ldp <= 0.9995:
            spd = data["Speed"][i]
            assert spd is not None, f"Speed at LapDistPct={ldp:.4f} must not be None"
            assert not math.isnan(float(spd)), f"Speed at LapDistPct={ldp:.4f} must not be NaN"

    # Grid points in [0.01, 0.29] must have valid Speed values (from the post-wrap data)
    for i, ldp in enumerate(grid):
        if 0.012 <= ldp <= 0.288:
            spd = data["Speed"][i]
            assert spd is not None, f"Speed at LapDistPct={ldp:.4f} (post-wrap) must not be None"
            assert not math.isnan(float(spd)), f"Speed at LapDistPct={ldp:.4f} (post-wrap) must not be NaN"

    # Grid points in [0.31, 0.49] must be NaN (gap: data goes 0.3 directly to 0.5)
    for i, ldp in enumerate(grid):
        if 0.33 <= ldp <= 0.47:
            spd = data["Speed"][i]
            if spd is not None:
                assert math.isnan(float(spd)), (
                    f"Speed at LapDistPct={ldp:.4f} (gap) should be NaN, got {spd}"
                )

    # Coverage: [0.01..0.3] ≈ 29% + [0.5..1.0] ≈ 50% → ~79% total
    assert meta["coverage_pct"] > 0.6, f"coverage_pct={meta['coverage_pct']} too low"


# ---------------------------------------------------------------------------
# Test 3 – Determinism: two calls with same input → identical parquet bytes
# ---------------------------------------------------------------------------


def test_determinism(tmp_path: Path) -> None:
    """Calling resample_lap twice with the same input yields byte-identical files."""
    n = 800
    lap_dist = np.linspace(0.0, 0.999, n, dtype=np.float64)
    speed = (lap_dist * 150.0 + 20.0).astype(np.float32)
    gear = np.where(lap_dist < 0.5, np.int32(3), np.int32(4))

    run_parquet = _make_run_parquet(tmp_path, lap_dist, {"Speed": speed, "Gear": gear})

    out1 = tmp_path / "lap_det_1.parquet"
    out2 = tmp_path / "lap_det_2.parquet"

    resample_lap(
        parquet_path=run_parquet,
        start_idx=0,
        end_idx=n - 1,
        output_path=out1,
        grid_step=_GRID_STEP,
    )
    resample_lap(
        parquet_path=run_parquet,
        start_idx=0,
        end_idx=n - 1,
        output_path=out2,
        grid_step=_GRID_STEP,
    )

    assert out1.read_bytes() == out2.read_bytes(), (
        "Two identical resample_lap calls must produce byte-for-byte identical parquet files"
    )
