from __future__ import annotations

import logging
import math
from typing import Any, Sequence


def _log_line(logger: Any, level: str, message: str) -> None:
    if logger is not None:
        try:
            fn = getattr(logger, level, None)
            if callable(fn):
                fn(message)
                return
        except Exception:
            pass
        try:
            fn = getattr(logger, "msg", None)
            if callable(fn):
                fn(message)
                return
        except Exception:
            pass
    py_logger = logging.getLogger(__name__)
    try:
        fn = getattr(py_logger, level, None)
        if callable(fn):
            fn(message)
            return
    except Exception:
        pass
    py_logger.info(message)


def _detect_full_throttle_threshold(throttle: Sequence[float]) -> float:
    max_value = float("-inf")
    for raw in throttle:
        try:
            value = float(raw)
        except Exception:
            continue
        if not math.isfinite(value):
            continue
        if value > max_value:
            max_value = value
    if max_value > 1.5:
        return 99.9
    return 0.999


def _append_or_merge_segment(
    segments: list[tuple[float, float]],
    start_s: float,
    end_s: float,
    min_between_curves_s: float,
) -> None:
    if end_s < start_s:
        end_s = start_s
    if not segments:
        segments.append((start_s, end_s))
        return
    prev_start, prev_end = segments[-1]
    if (start_s - prev_end) <= min_between_curves_s:
        merged_end = end_s if end_s > prev_end else prev_end
        if merged_end < prev_start:
            merged_end = prev_start
        segments[-1] = (prev_start, merged_end)
        return
    segments.append((start_s, end_s))


def detect_curve_segments(
    time_s: Sequence[float],
    throttle: Sequence[float],
    brake: Sequence[float],
    before_brake_s: float,
    after_full_throttle_s: float,
    min_between_curves_s: float,
    logger: Any = None,
) -> list[tuple[float, float]]:
    n = len(time_s)
    if len(throttle) != n or len(brake) != n:
        raise ValueError("time_s, throttle und brake muessen gleich lang sein.")
    if n <= 0:
        _log_line(logger, "info", "cut_events: Cut hat nichts gefunden (0 Segmente)")
        return []

    first_t = float(time_s[0])
    last_t = float(time_s[-1])
    before_s = max(0.0, float(before_brake_s))
    after_s = max(0.0, float(after_full_throttle_s))
    min_between_s = max(0.0, float(min_between_curves_s))
    full_threshold = _detect_full_throttle_threshold(throttle)

    segments: list[tuple[float, float]] = []
    seen_full_throttle_section = False
    armed_for_brake = False
    in_curve = False
    pending_start = first_t
    brake_start_idx = -1

    for i in range(n):
        t_now = float(time_s[i])
        throttle_now = float(throttle[i])
        brake_now = float(brake[i])

        is_full = throttle_now >= full_threshold
        brake_active = brake_now > 0.0

        if is_full:
            seen_full_throttle_section = True
            if not in_curve:
                armed_for_brake = True

        if in_curve:
            if i > brake_start_idx and is_full:
                t_end = t_now + after_s
                if t_end > last_t:
                    t_end = last_t
                _append_or_merge_segment(
                    segments=segments,
                    start_s=pending_start,
                    end_s=t_end,
                    min_between_curves_s=min_between_s,
                )
                in_curve = False
                brake_start_idx = -1
                seen_full_throttle_section = True
                armed_for_brake = True
            continue

        if seen_full_throttle_section and armed_for_brake and brake_active:
            t_start = t_now - before_s
            if t_start < first_t:
                t_start = first_t
            pending_start = t_start
            in_curve = True
            brake_start_idx = i
            armed_for_brake = False

    if in_curve:
        _append_or_merge_segment(
            segments=segments,
            start_s=pending_start,
            end_s=last_t,
            min_between_curves_s=min_between_s,
        )

    full_duration = max(0.0, last_t - first_t)
    total_cut_duration = 0.0
    for seg_start, seg_end in segments:
        total_cut_duration += max(0.0, float(seg_end) - float(seg_start))

    _log_line(
        logger,
        "debug",
        (
            f"cut_events: n_segments={len(segments)} "
            f"full_duration={full_duration:.3f}s "
            f"total_cut_duration={total_cut_duration:.3f}s"
        ),
    )
    for idx, (seg_start, seg_end) in enumerate(segments[:3]):
        _log_line(
            logger,
            "debug",
            f"cut_events: #{idx}: start={seg_start:.3f}s end={seg_end:.3f}s",
        )
    if not segments:
        _log_line(logger, "info", "cut_events: Cut hat nichts gefunden (0 Segmente)")

    return segments


def _selftest() -> None:
    seg1 = detect_curve_segments(
        time_s=[0, 1, 2, 3, 4, 5, 6],
        throttle=[100, 100, 20, 10, 30, 100, 100],
        brake=[0, 0, 0, 0.5, 0, 0, 0],
        before_brake_s=1.0,
        after_full_throttle_s=1.0,
        min_between_curves_s=2.0,
    )
    assert seg1 == [(2.0, 6.0)], f"unexpected seg1: {seg1}"

    seg2 = detect_curve_segments(
        time_s=[0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
        throttle=[1, 1, 0.5, 0.2, 1, 1, 0.3, 0.2, 1, 1],
        brake=[0, 0, 0.4, 0, 0, 0, 0.6, 0, 0, 0],
        before_brake_s=0.0,
        after_full_throttle_s=0.0,
        min_between_curves_s=2.0,
    )
    assert seg2 == [(2.0, 8.0)], f"unexpected seg2: {seg2}"

    seg3 = detect_curve_segments(
        time_s=[0, 1, 2, 3, 4],
        throttle=[1, 1, 0.7, 1, 1],
        brake=[0, 0, 0, 0, 0],
        before_brake_s=1.0,
        after_full_throttle_s=1.0,
        min_between_curves_s=1.0,
    )
    assert seg3 == [], f"unexpected seg3: {seg3}"

    print("cut_events selftest: OK")


if __name__ == "__main__":
    _selftest()
