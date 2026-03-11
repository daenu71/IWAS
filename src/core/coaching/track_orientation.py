"""Track display orientation: translate/rotate/mirror xy so start/finish faces down-right."""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

DEFAULT_START_TANGENT_LOOKAHEAD_PCT: float = 0.01
DISPLAY_ORIENTATION_RULE: str = "start_finish_tangent"


@dataclass
class TrackDisplayOrientationResult:
    xy: np.ndarray
    rule: str
    transform: str
    applied: bool
    start_anchor_lap_dist_pct: float | None
    start_anchor_xy_before: tuple[float, float] | None
    start_tangent_before: tuple[float, float] | None
    rotation_deg: float | None
    mirrored: bool
    before_first: tuple[float, float] | None
    before_last: tuple[float, float] | None
    after_first: tuple[float, float] | None
    after_last: tuple[float, float] | None


def orient_track_display_frame(
    xy: np.ndarray,
    lap_dist_pct: np.ndarray,
    *,
    lookahead_pct: float = DEFAULT_START_TANGENT_LOOKAHEAD_PCT,
) -> TrackDisplayOrientationResult:
    """Orient track xy data for display.

    Translates start/finish to origin, rotates so the start tangent points
    downward (-y), and mirrors horizontally if needed so start/finish ends up
    on the right side.
    """
    xy_arr = np.asarray(xy, dtype=np.float64)
    lap_dist_arr = np.asarray(lap_dist_pct, dtype=np.float64)

    n = len(xy_arr)
    if n == 0:
        return TrackDisplayOrientationResult(
            xy=np.empty((0, 2), dtype=np.float64),
            rule=DISPLAY_ORIENTATION_RULE,
            transform="identity(empty)",
            applied=False,
            start_anchor_lap_dist_pct=None,
            start_anchor_xy_before=None,
            start_tangent_before=None,
            rotation_deg=None,
            mirrored=False,
            before_first=None,
            before_last=None,
            after_first=None,
            after_last=None,
        )

    # Find start anchor: index closest to lap_dist_pct=0
    start_anchor_index = int(np.argmin(np.abs(lap_dist_arr)))
    start_lap_dist = float(lap_dist_arr[start_anchor_index])

    # Find start tangent vector using lookahead_pct
    candidates: list[tuple[float, np.ndarray]] = []
    for idx in range(n):
        if idx == start_anchor_index:
            continue
        delta = float(lap_dist_arr[idx]) - start_lap_dist
        if delta < 0.0:
            delta += 1.0
        if delta <= 1.0e-9 or delta >= 1.0 - 1.0e-9:
            continue
        t = np.asarray(xy_arr[idx] - xy_arr[start_anchor_index], dtype=np.float64)
        if not np.all(np.isfinite(t)) or float(np.linalg.norm(t)) <= 1.0e-9:
            continue
        candidates.append((delta, t))

    tangent: np.ndarray | None = None
    if candidates:
        candidates.sort(key=lambda item: item[0])
        for delta, t in candidates:
            if delta >= lookahead_pct - 1.0e-9:
                tangent = t
                break
        if tangent is None:
            tangent = candidates[0][1]

    # Translate start anchor to origin
    translated = xy_arr - xy_arr[start_anchor_index]

    if tangent is None or not np.all(np.isfinite(tangent)) or float(np.linalg.norm(tangent)) <= 1.0e-9:
        transform_str = "translate=start_anchor_to_origin,rotate=none,mirror_x=no"
        applied = not np.allclose(xy_arr, translated, rtol=0.0, atol=1e-9)
        return TrackDisplayOrientationResult(
            xy=translated,
            rule=DISPLAY_ORIENTATION_RULE,
            transform=transform_str,
            applied=applied,
            start_anchor_lap_dist_pct=float(lap_dist_arr[start_anchor_index]),
            start_anchor_xy_before=(float(xy_arr[start_anchor_index, 0]), float(xy_arr[start_anchor_index, 1])),
            start_tangent_before=None,
            rotation_deg=None,
            mirrored=False,
            before_first=(float(xy_arr[0, 0]), float(xy_arr[0, 1])),
            before_last=(float(xy_arr[-1, 0]), float(xy_arr[-1, 1])),
            after_first=(float(translated[0, 0]), float(translated[0, 1])),
            after_last=(float(translated[-1, 0]), float(translated[-1, 1])),
        )

    # Rotate so tangent points downward (-y axis)
    current_angle = math.atan2(float(tangent[1]), float(tangent[0]))
    rotation_rad = -math.pi / 2.0 - current_angle
    cos_a = math.cos(rotation_rad)
    sin_a = math.sin(rotation_rad)
    rotation_matrix = np.asarray([[cos_a, -sin_a], [sin_a, cos_a]], dtype=np.float64)
    rotated = translated @ rotation_matrix.T

    # Mirror horizontally if start/finish is on the left (max_x > |min_x|)
    max_x = float(np.max(rotated[:, 0]))
    min_x = float(np.min(rotated[:, 0]))
    mirrored = max_x > abs(min_x) + 1.0e-9
    if mirrored:
        rotated = rotated.copy()
        rotated[:, 0] *= -1.0

    rotated[np.abs(rotated) <= 1.0e-12] = 0.0

    transform_str = (
        f"translate=start_anchor_to_origin,"
        f"rotate={math.degrees(rotation_rad):.6f}deg,"
        f"mirror_x={'yes' if mirrored else 'no'}"
    )
    applied = not np.allclose(xy_arr, rotated, rtol=0.0, atol=1e-9)

    return TrackDisplayOrientationResult(
        xy=rotated,
        rule=DISPLAY_ORIENTATION_RULE,
        transform=transform_str,
        applied=applied,
        start_anchor_lap_dist_pct=float(lap_dist_arr[start_anchor_index]),
        start_anchor_xy_before=(float(xy_arr[start_anchor_index, 0]), float(xy_arr[start_anchor_index, 1])),
        start_tangent_before=(float(tangent[0]), float(tangent[1])),
        rotation_deg=math.degrees(rotation_rad),
        mirrored=mirrored,
        before_first=(float(xy_arr[0, 0]), float(xy_arr[0, 1])),
        before_last=(float(xy_arr[-1, 0]), float(xy_arr[-1, 1])),
        after_first=(float(rotated[0, 0]), float(rotated[0, 1])),
        after_last=(float(rotated[-1, 0]), float(rotated[-1, 1])),
    )
