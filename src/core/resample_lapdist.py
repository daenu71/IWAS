"""Lap-distance resampling and interpolation utilities."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResampledRun:
    """Container and behavior for Resampled Run."""
    lapdist_grid: list[float]
    channels: dict[str, list[float]]  # alle resample-ten Kanäle als float
    n_out: int


def build_lapdist_grid(lapdist_a: list[float], lapdist_b: list[float], step: float) -> list[float]:
    """Build and return lapdist grid."""
    if step <= 0:
        raise ValueError("step muss > 0 sein")

    a_min, a_max = _min_max_finite(lapdist_a)
    b_min, b_max = _min_max_finite(lapdist_b)

    # Ueberlappung nehmen, damit beide Runs Werte haben
    lo = max(a_min, b_min)
    hi = min(a_max, b_max)

    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        raise ValueError("LapDist Ueberlappung ist ungueltig (hi<=lo).")

    # Grid erzeugen
    grid: list[float] = []
    x = lo
    # Schutz gegen Endlos-Schleife
    max_n = int((hi - lo) / step) + 5_000_000
    n = 0
    while x <= hi + 1e-12:
        grid.append(float(x))
        x += step
        n += 1
        if n > max_n:
            raise ValueError("Grid zu gross (step zu klein oder Werte ungueltig).")

    return grid


def resample_run_linear(
    lapdist_in: list[float],
    channels_in: dict[str, list[Any]],
    lapdist_grid: list[float],
    channel_names: list[str],
) -> ResampledRun:
    """Resample run linear."""
    if len(lapdist_in) < 2:
        raise ValueError("lapdist_in hat zu wenig Samples.")
    for name in channel_names:
        if name not in channels_in:
            raise KeyError(f"Spalte fehlt: {name}")
        if len(channels_in[name]) != len(lapdist_in):
            raise ValueError(f"Laenge mismatch: {name}")

    # LapDist muss monoton steigen (wie in deinem RVA nach unwrap/resample)
    if _count_non_increasing(lapdist_in) > 0:
        raise ValueError("lapdist_in ist nicht streng steigend. (Unwrap/Sort fehlt)")

    out: dict[str, list[float]] = {name: [] for name in channel_names}

    # Pointer im Input
    j = 0
    n_in = len(lapdist_in)

    for x in lapdist_grid:
        # j so weit schieben, dass lapdist_in[j] <= x <= lapdist_in[j+1]
        while j < n_in - 2 and lapdist_in[j + 1] < x:
            j += 1

        x0 = float(lapdist_in[j])
        x1 = float(lapdist_in[j + 1])

        # Rand: wenn x ausserhalb, clamp auf Randwerte
        if x <= x0:
            for name in channel_names:
                out[name].append(float(_to_float(channels_in[name][j])))
            continue
        if x >= x1 and j >= n_in - 2:
            for name in channel_names:
                out[name].append(float(_to_float(channels_in[name][j + 1])))
            continue

        # Linear interpolation
        if x1 == x0:
            t = 0.0
        else:
            t = (float(x) - x0) / (x1 - x0)

        for name in channel_names:
            v0 = float(_to_float(channels_in[name][j]))
            v1 = float(_to_float(channels_in[name][j + 1]))
            out[name].append(_lerp(v0, v1, t))

    return ResampledRun(lapdist_grid=lapdist_grid, channels=out, n_out=len(lapdist_grid))


def _lerp(a: float, b: float, t: float) -> float:
    """Implement lerp logic."""
    return (1.0 - t) * a + t * b


def _to_float(v: Any) -> float:
    """Convert value to float."""
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s == "" or s.lower() == "nan":
        return float("nan")
    try:
        return float(s)
    except Exception:
        return float("nan")


def _min_max_finite(xs: list[float]) -> tuple[float, float]:
    """Implement min max finite logic."""
    mn = float("inf")
    mx = float("-inf")
    for v in xs:
        if not math.isfinite(v):
            continue
        if v < mn:
            mn = v
        if v > mx:
            mx = v
    if mn == float("inf") or mx == float("-inf"):
        raise ValueError("Keine finite LapDist-Werte gefunden.")
    return mn, mx


def _count_non_increasing(xs: list[float]) -> int:
    """Implement count non increasing logic."""
    bad = 0
    prev = xs[0]
    for i in range(1, len(xs)):
        cur = xs[i]
        if cur <= prev:
            bad += 1
        prev = cur
    return bad
