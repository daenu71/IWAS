from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SyncMap:
    # pro slow frame index: fast index (nearest)
    slow_to_fast_idx: list[int]
    # pro slow frame: lapdist, damit du es debuggen kannst
    slow_lapdist: list[float]


def build_sync_map_by_lapdist(
    slow_lapdist_by_frame: list[float],
    fast_lapdist_samples: list[float],
) -> SyncMap:
    """
    slow_lapdist_by_frame: LapDistPct pro slow-Frame (gleiche Laenge wie slow frames)
    fast_lapdist_samples: LapDistPct Samples (monoton steigend) fuer fast
    """
    if len(slow_lapdist_by_frame) < 1:
        raise ValueError("slow_lapdist_by_frame ist leer")
    if len(fast_lapdist_samples) < 2:
        raise ValueError("fast_lapdist_samples hat zu wenig Samples")

    if _count_non_increasing(fast_lapdist_samples) > 0:
        raise ValueError("fast_lapdist_samples ist nicht streng steigend")

    out_idx: list[int] = []
    out_ld: list[float] = []

    j = 0
    n_fast = len(fast_lapdist_samples)

    for ld in slow_lapdist_by_frame:
        x = float(ld)
        if not math.isfinite(x):
            # fallback: gleich lassen
            out_idx.append(j)
            out_ld.append(x)
            continue

        # clamp
        if x <= fast_lapdist_samples[0]:
            out_idx.append(0)
            out_ld.append(x)
            continue
        if x >= fast_lapdist_samples[-1]:
            out_idx.append(n_fast - 1)
            out_ld.append(x)
            continue

        # j so weit schieben, dass fast[j] <= x <= fast[j+1]
        while j < n_fast - 2 and fast_lapdist_samples[j + 1] < x:
            j += 1

        # nearest zwischen j und j+1
        a = fast_lapdist_samples[j]
        b = fast_lapdist_samples[j + 1]
        if abs(b - x) < abs(x - a):
            out_idx.append(j + 1)
        else:
            out_idx.append(j)

        out_ld.append(x)

    return SyncMap(slow_to_fast_idx=out_idx, slow_lapdist=out_ld)


def _count_non_increasing(xs: list[float]) -> int:
    bad = 0
    prev = xs[0]
    for i in range(1, len(xs)):
        cur = xs[i]
        if cur <= prev:
            bad += 1
        prev = cur
    return bad
