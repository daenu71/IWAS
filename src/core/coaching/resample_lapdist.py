"""Deterministischer LapDistPct Resampler – Story 2.1.1.

Pro Lap wird ein resampled Parquet-File erzeugt: alle relevanten Kanäle
auf ein gleichmäßiges LapDistPct-Grid interpoliert. Das Ergebnis ist
deterministisch (gleicher Input → gleicher Output, Byte für Byte).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

_DEFAULT_GRID_STEP = 0.0005
_COVERAGE_WARN_THRESHOLD = 0.95
# A negative jump larger than this in LapDistPct signals a 0.99→0.01 wrap.
_WRAP_DROP_THRESHOLD = -0.5


def resample_lap(
    *,
    parquet_path: Path | str,
    start_idx: int,
    end_idx: int,
    output_path: Path | str,
    grid_step: float = _DEFAULT_GRID_STEP,
) -> dict[str, Any]:
    """Resample a single lap onto a uniform LapDistPct grid.

    Reads rows [start_idx, end_idx] inclusive from *parquet_path*,
    interpolates all float channels (linear) and int/bool channels
    (nearest-neighbour) onto ``np.linspace(0.0, 1.0, n, endpoint=False)``
    where ``n = round(1 / grid_step)``, and writes the result to
    *output_path* as a Parquet file.

    Wrap-around in LapDistPct (0.99 → 0.01 in raw data) is handled by
    unwrapping before interpolation.

    Returns a metadata dict with at minimum:

    - ``coverage_pct``: fraction of grid covered by input data (0.0..1.0)
    - ``low_coverage``: True when coverage_pct < 0.95
    - ``n_input_rows``: number of input samples for this lap
    - ``n_grid_points``: number of points on the output grid
    - ``grid_step``: the grid step used
    """
    parquet_path = Path(parquet_path)
    output_path = Path(output_path)
    grid_step = float(grid_step)

    n_points = int(round(1.0 / grid_step))
    grid = np.linspace(0.0, 1.0, n_points, endpoint=False)
    n_grid = len(grid)

    table = _read_lap_slice(parquet_path, int(start_idx), int(end_idx))
    n_rows = table.num_rows

    if n_rows == 0:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        empty_table = _make_empty_output(grid)
        _write_parquet(empty_table, output_path)
        return {
            "coverage_pct": 0.0,
            "low_coverage": True,
            "n_input_rows": 0,
            "n_grid_points": int(n_grid),
            "grid_step": float(grid_step),
        }

    # --- Unwrap LapDistPct ---
    lap_dist_raw = _extract_float_column(table, "LapDistPct")
    lap_dist_unwrapped = _unwrap_lapdistpct(lap_dist_raw)

    valid_mask = np.isfinite(lap_dist_unwrapped)
    n_valid = int(np.sum(valid_mask))
    if n_valid < 2:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        empty_table = _make_empty_output(grid)
        _write_parquet(empty_table, output_path)
        return {
            "coverage_pct": 0.0,
            "low_coverage": True,
            "n_input_rows": int(n_rows),
            "n_grid_points": int(n_grid),
            "grid_step": float(grid_step),
        }

    x_valid = lap_dist_unwrapped[valid_mask]
    sort_order = np.argsort(x_valid, kind="stable")
    x_sorted = x_valid[sort_order]
    x_min = float(x_sorted[0])
    x_max = float(x_sorted[-1])

    coverage_pct = _compute_coverage(grid, x_min, x_max)

    # --- Interpolate all channels onto the grid ---
    schema = table.schema
    # column name → (float64 array of length n_grid, pa.DataType for output)
    out_columns: dict[str, tuple[np.ndarray, pa.DataType]] = {}

    # LapDistPct is the grid itself (always float32)
    out_columns["LapDistPct"] = (grid, pa.float32())

    for i in range(len(schema)):
        field = schema.field(i)
        col_name = field.name
        if col_name in ("ts", "monotonic_ts", "LapDistPct"):
            continue
        if pa.types.is_string(field.type) or pa.types.is_large_string(field.type):
            continue

        y_raw = _extract_float_column(table, col_name)
        y_valid = y_raw[valid_mask][sort_order]

        is_nearest = pa.types.is_integer(field.type) or pa.types.is_boolean(field.type)
        interp = _interpolate_on_grid(x_sorted, y_valid, grid, x_min, x_max, nearest=is_nearest)
        out_columns[col_name] = (interp, pa.float32())

    # --- Build output table with sorted column names for determinism ---
    sorted_names = sorted(out_columns.keys())
    out_arrays = []
    out_fields = []
    for name in sorted_names:
        arr, arrow_type = out_columns[name]
        # Convert to float32; NaN stays as NaN (pyarrow nullable float)
        out_arrays.append(pa.array(arr.astype(np.float32).tolist(), type=arrow_type))
        out_fields.append(pa.field(name, arrow_type))

    out_schema = pa.schema(out_fields)
    out_table = pa.Table.from_arrays(out_arrays, schema=out_schema)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_parquet(out_table, output_path)

    return {
        "coverage_pct": round(coverage_pct, 6),
        "low_coverage": bool(coverage_pct < _COVERAGE_WARN_THRESHOLD),
        "n_input_rows": int(n_rows),
        "n_grid_points": int(n_grid),
        "grid_step": float(grid_step),
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _unwrap_lapdistpct(values: np.ndarray) -> np.ndarray:
    """Unwrap LapDistPct to remove the 0.99 → 0.01 wrap-around.

    Detects large negative jumps (drop > 0.5) and adds 1.0 to all
    subsequent values so the result is monotonically non-decreasing
    in the lap-distance domain.
    """
    result = values.copy()
    offset = 0.0
    prev_raw: float | None = None
    for i in range(len(result)):
        if not math.isfinite(result[i]):
            continue
        raw = float(result[i])
        if prev_raw is not None and (raw - prev_raw) < _WRAP_DROP_THRESHOLD:
            offset += 1.0
        result[i] = raw + offset
        prev_raw = raw
    return result


def _compute_coverage(grid: np.ndarray, x_min: float, x_max: float) -> float:
    """Fraction of grid points covered by data range [x_min, x_max].

    For wrapped data (x_max > 1.0) the post-wrap portion [0.0, x_max − 1.0]
    also counts as covered (those grid points are queryable via unwrapped
    coordinates x + 1.0).
    """
    if x_max <= x_min or len(grid) == 0:
        return 0.0
    seg1_hi = min(x_max, 1.0)
    covered = np.sum((grid >= x_min) & (grid <= seg1_hi))
    if x_max > 1.0:
        covered += np.sum((grid >= 0.0) & (grid <= (x_max - 1.0)))
    return float(covered) / float(len(grid))


def _interpolate_on_grid(
    x_sorted: np.ndarray,
    y_sorted: np.ndarray,
    grid: np.ndarray,
    x_min: float,
    x_max: float,
    *,
    nearest: bool,
) -> np.ndarray:
    """Interpolate y onto grid, respecting wrap-around in unwrapped x.

    Grid points outside [x_min, min(x_max, 1.0)] ∪ [0.0, x_max − 1.0]
    are left as NaN.  NaN values in y are excluded before interpolation.
    """
    out = np.full(len(grid), np.nan, dtype=np.float64)

    y_finite = np.isfinite(y_sorted)
    if not np.any(y_finite) or int(np.sum(y_finite)) < 2:
        return out
    xs = x_sorted[y_finite]
    ys = y_sorted[y_finite]

    # Segment 1: pre-wrap portion of the grid [x_min .. min(x_max, 1.0)]
    seg1_hi = min(x_max, 1.0)
    mask1 = (grid >= x_min) & (grid <= seg1_hi)
    if np.any(mask1):
        q1 = grid[mask1]
        if nearest:
            out[mask1] = _nearest_interp(xs, ys, q1)
        else:
            out[mask1] = np.interp(q1, xs, ys, left=np.nan, right=np.nan)

    # Segment 2: post-wrap portion (only when data crosses 1.0)
    if x_max > 1.0:
        wrapped_max = x_max - 1.0
        mask2 = (grid >= 0.0) & (grid <= wrapped_max)
        if np.any(mask2):
            q2 = grid[mask2] + 1.0  # query at unwrapped coordinates
            if nearest:
                out[mask2] = _nearest_interp(xs, ys, q2)
            else:
                out[mask2] = np.interp(q2, xs, ys, left=np.nan, right=np.nan)

    return out


def _nearest_interp(x: np.ndarray, y: np.ndarray, query: np.ndarray) -> np.ndarray:
    """Nearest-neighbour interpolation using sorted x."""
    indices = np.searchsorted(x, query, side="left")
    indices = np.clip(indices, 0, len(x) - 1)
    left_idx = np.clip(indices - 1, 0, len(x) - 1)
    dist_right = np.abs(x[indices] - query)
    dist_left = np.abs(x[left_idx] - query)
    chosen = np.where(dist_left <= dist_right, left_idx, indices)
    return y[chosen].astype(np.float64)


def _read_lap_slice(parquet_path: Path, start_idx: int, end_idx: int) -> pa.Table:
    """Read rows [start_idx, end_idx] inclusive from *parquet_path*."""
    pf = pq.ParquetFile(parquet_path)
    table = pf.read()
    n_rows = table.num_rows
    lo = max(0, start_idx)
    hi = min(n_rows - 1, end_idx)
    if hi < lo:
        return table.slice(0, 0)
    return table.slice(lo, hi - lo + 1)


def _extract_float_column(table: pa.Table, col_name: str) -> np.ndarray:
    """Return column as float64 array; None/non-finite values become NaN."""
    if col_name not in table.schema.names:
        return np.full(table.num_rows, np.nan, dtype=np.float64)
    py_list = table.column(col_name).to_pylist()
    result = np.empty(len(py_list), dtype=np.float64)
    for i, v in enumerate(py_list):
        if v is None:
            result[i] = np.nan
        else:
            try:
                f = float(v)
                result[i] = f if math.isfinite(f) else np.nan
            except Exception:
                result[i] = np.nan
    return result


def _make_empty_output(grid: np.ndarray) -> pa.Table:
    """Return a minimal output table (only LapDistPct grid, all others absent)."""
    return pa.table(
        {"LapDistPct": pa.array(grid.astype(np.float32).tolist(), type=pa.float32())},
        schema=pa.schema([pa.field("LapDistPct", pa.float32())]),
    )


def _write_parquet(table: pa.Table, path: Path) -> None:
    """Write *table* to *path* deterministically.

    - ``write_statistics=False``: no min/max stats (avoids float precision diffs)
    - ``replace_schema_metadata({})``: strip pandas/arrow metadata
    - Single row group for fixed layout
    """
    clean_table = table.replace_schema_metadata({})
    pq.write_table(
        clean_table,
        str(path),
        compression="snappy",
        write_statistics=False,
        row_group_size=max(1, clean_table.num_rows),
    )
