from __future__ import annotations

import bisect
import logging
import math
from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class FrameSegment:
    start_frame: int
    end_frame: int
    start_time_s: float
    end_time_s: float


@dataclass(frozen=True)
class CurveSegmentStats:
    merge_count: int = 0


@dataclass(frozen=True)
class FrameMappingStats:
    merge_count: int = 0


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
) -> bool:
    if end_s < start_s:
        end_s = start_s
    if not segments:
        segments.append((start_s, end_s))
        return False
    prev_start, prev_end = segments[-1]
    if (start_s - prev_end) <= min_between_curves_s:
        merged_end = end_s if end_s > prev_end else prev_end
        if merged_end < prev_start:
            merged_end = prev_start
        segments[-1] = (prev_start, merged_end)
        return True
    segments.append((start_s, end_s))
    return False


def _validate_time_segments_sorted(
    segments: Sequence[tuple[float, float]],
) -> list[tuple[float, float]]:
    normalized: list[tuple[float, float]] = []
    prev_start: float | None = None
    for idx, raw in enumerate(segments):
        if len(raw) != 2:
            raise ValueError(f"segment at index {idx} must contain (start_s, end_s).")
        start_s = float(raw[0])
        end_s = float(raw[1])
        if not math.isfinite(start_s) or not math.isfinite(end_s):
            raise ValueError(f"segment at index {idx} has non-finite time values.")
        if prev_start is not None and start_s < prev_start:
            raise ValueError(
                "segments must be sorted by start time. "
                f"index={idx} start_s={start_s} prev_start_s={prev_start}"
            )
        prev_start = start_s
        if end_s < start_s:
            end_s = start_s
        normalized.append((start_s, end_s))
    return normalized


def _append_or_merge_frame_segment(
    mapped: list[FrameSegment],
    candidate: FrameSegment,
) -> bool:
    if not mapped:
        mapped.append(candidate)
        return False

    prev = mapped[-1]
    if candidate.start_frame < prev.start_frame:
        raise ValueError(
            "mapped segments became non-monotonic by start_frame. "
            f"candidate={candidate.start_frame} prev={prev.start_frame}"
        )

    if candidate.start_frame <= prev.end_frame:
        merged = FrameSegment(
            start_frame=prev.start_frame,
            end_frame=max(prev.end_frame, candidate.end_frame),
            start_time_s=prev.start_time_s,
            end_time_s=max(prev.end_time_s, candidate.end_time_s),
        )
        mapped[-1] = merged
        return True

    mapped.append(candidate)
    return False


def map_time_segments_to_frame_indices(
    segments: Sequence[tuple[float, float]],
    frame_time_s: Sequence[float],
    logger: Any = None,
) -> list[FrameSegment]:
    mapped, _stats = map_time_segments_to_frame_indices_with_stats(
        segments=segments,
        frame_time_s=frame_time_s,
        logger=logger,
    )
    return mapped


def map_time_segments_to_frame_indices_with_stats(
    segments: Sequence[tuple[float, float]],
    frame_time_s: Sequence[float],
    logger: Any = None,
) -> tuple[list[FrameSegment], FrameMappingStats]:
    """
    Mappt Zeitsegmente deterministisch auf Frame-Indizes (end_frame inklusiv).

    Konvention (entspricht floor/ceil-1 auf Zeitbasis):
    - start_frame = letzter Frame mit frame_time <= t_start
    - end_frame   = letzter Frame mit frame_time <  t_end
    - falls durch Rundung leer: end_frame = start_frame
    """
    if not frame_time_s:
        return [], FrameMappingStats(merge_count=0)

    frame_times: list[float] = []
    prev_t: float | None = None
    for idx, raw_t in enumerate(frame_time_s):
        t = float(raw_t)
        if not math.isfinite(t):
            raise ValueError(f"frame_time_s at index {idx} is non-finite.")
        if prev_t is not None and t < prev_t:
            raise ValueError(
                "frame_time_s must be sorted ascending. "
                f"index={idx} time_s={t} prev_time_s={prev_t}"
            )
        prev_t = t
        frame_times.append(t)

    n_frames = len(frame_times)
    max_frame = n_frames - 1
    normalized = _validate_time_segments_sorted(segments)
    mapped: list[FrameSegment] = []
    merge_count = 0

    for start_s, end_s in normalized:
        start_frame = bisect.bisect_right(frame_times, start_s) - 1
        if start_frame < 0:
            start_frame = 0
        if start_frame > max_frame:
            start_frame = max_frame

        end_frame = bisect.bisect_left(frame_times, end_s) - 1
        if end_frame < start_frame:
            end_frame = start_frame
        if end_frame > max_frame:
            end_frame = max_frame

        if end_frame < start_frame:
            end_frame = start_frame

        if _append_or_merge_frame_segment(
            mapped,
            FrameSegment(
                start_frame=int(start_frame),
                end_frame=int(end_frame),
                start_time_s=float(start_s),
                end_time_s=float(end_s),
            ),
        ):
            merge_count += 1

    _log_line(
        logger,
        "debug",
        f"cut_events: mapped_segments={len(mapped)} (time->frame via frame_time_s)",
    )
    for idx, seg in enumerate(mapped[:3]):
        _log_line(
            logger,
            "debug",
            (
                f"cut_events: mapped #{idx}: frames={seg.start_frame}..{seg.end_frame} "
                f"time={seg.start_time_s:.3f}s..{seg.end_time_s:.3f}s"
            ),
        )
    return mapped, FrameMappingStats(merge_count=merge_count)


def map_time_segments_to_frames(
    segments: Sequence[tuple[float, float]],
    fps: float,
    num_frames: int | None = None,
    logger: Any = None,
) -> list[FrameSegment]:
    mapped, _stats = map_time_segments_to_frames_with_stats(
        segments=segments,
        fps=fps,
        num_frames=num_frames,
        logger=logger,
    )
    return mapped


def map_time_segments_to_frames_with_stats(
    segments: Sequence[tuple[float, float]],
    fps: float,
    num_frames: int | None = None,
    logger: Any = None,
) -> tuple[list[FrameSegment], FrameMappingStats]:
    """
    Mappt Zeitsegmente deterministisch auf Frame-Indizes (end_frame inklusiv).

    Konvention:
    - start_frame = floor(t_start * fps)
    - end_frame   = ceil(t_end * fps) - 1
    - falls durch Rundung leer: end_frame = start_frame
    """
    fps_safe = float(fps)
    if not math.isfinite(fps_safe) or fps_safe <= 0.0:
        raise ValueError("fps must be finite and > 0.")

    max_frame: int | None = None
    if num_frames is not None:
        n_frames = int(num_frames)
        if n_frames <= 0:
            return [], FrameMappingStats(merge_count=0)
        max_frame = n_frames - 1

    normalized = _validate_time_segments_sorted(segments)
    mapped: list[FrameSegment] = []
    merge_count = 0

    for start_s, end_s in normalized:
        start_frame = max(0, int(math.floor(float(start_s) * fps_safe)))
        end_frame = max(start_frame, int(math.ceil(float(end_s) * fps_safe)) - 1)

        if max_frame is not None:
            if start_frame > max_frame:
                start_frame = max_frame
            if end_frame > max_frame:
                end_frame = max_frame

        if end_frame < start_frame:
            end_frame = start_frame

        if _append_or_merge_frame_segment(
            mapped,
            FrameSegment(
                start_frame=int(start_frame),
                end_frame=int(end_frame),
                start_time_s=float(start_s),
                end_time_s=float(end_s),
            ),
        ):
            merge_count += 1

    _log_line(
        logger,
        "debug",
        f"cut_events: mapped_segments={len(mapped)} (time->frame via fps={fps_safe:.6f})",
    )
    for idx, seg in enumerate(mapped[:3]):
        _log_line(
            logger,
            "debug",
            (
                f"cut_events: mapped #{idx}: frames={seg.start_frame}..{seg.end_frame} "
                f"time={seg.start_time_s:.3f}s..{seg.end_time_s:.3f}s"
            ),
        )
    return mapped, FrameMappingStats(merge_count=merge_count)


def detect_curve_segments(
    time_s: Sequence[float],
    throttle: Sequence[float],
    brake: Sequence[float],
    before_brake_s: float,
    after_full_throttle_s: float,
    min_between_curves_s: float,
    logger: Any = None,
) -> list[tuple[float, float]]:
    segments, _stats = detect_curve_segments_with_stats(
        time_s=time_s,
        throttle=throttle,
        brake=brake,
        before_brake_s=before_brake_s,
        after_full_throttle_s=after_full_throttle_s,
        min_between_curves_s=min_between_curves_s,
        logger=logger,
    )
    return segments


def detect_curve_segments_with_stats(
    time_s: Sequence[float],
    throttle: Sequence[float],
    brake: Sequence[float],
    before_brake_s: float,
    after_full_throttle_s: float,
    min_between_curves_s: float,
    logger: Any = None,
) -> tuple[list[tuple[float, float]], CurveSegmentStats]:
    n = len(time_s)
    if len(throttle) != n or len(brake) != n:
        raise ValueError("time_s, throttle und brake muessen gleich lang sein.")
    if n <= 0:
        _log_line(logger, "info", "cut_events: Cut hat nichts gefunden (0 Segmente)")
        return [], CurveSegmentStats(merge_count=0)

    first_t = float(time_s[0])
    last_t = float(time_s[-1])
    before_s = max(0.0, float(before_brake_s))
    after_s = max(0.0, float(after_full_throttle_s))
    min_between_s = max(0.0, float(min_between_curves_s))
    full_threshold = _detect_full_throttle_threshold(throttle)

    segments: list[tuple[float, float]] = []
    merge_count = 0
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
                if _append_or_merge_segment(
                    segments=segments,
                    start_s=pending_start,
                    end_s=t_end,
                    min_between_curves_s=min_between_s,
                ):
                    merge_count += 1
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
        if _append_or_merge_segment(
            segments=segments,
            start_s=pending_start,
            end_s=last_t,
            min_between_curves_s=min_between_s,
        ):
            merge_count += 1

    full_duration = max(0.0, last_t - first_t)
    total_cut_duration = 0.0
    for seg_start, seg_end in segments:
        total_cut_duration += max(0.0, float(seg_end) - float(seg_start))

    _log_line(
        logger,
        "debug",
        (
            f"cut_events: n_segments={len(segments)} "
            f"merges={merge_count} "
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

    return segments, CurveSegmentStats(merge_count=merge_count)


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

    mapped1 = map_time_segments_to_frames([(2.0, 6.0)], fps=60.0)
    assert mapped1 == [
        FrameSegment(start_frame=120, end_frame=359, start_time_s=2.0, end_time_s=6.0)
    ], f"unexpected mapped1: {mapped1}"

    mapped2 = map_time_segments_to_frames([(2.0, 2.0)], fps=60.0)
    assert mapped2 == [
        FrameSegment(start_frame=120, end_frame=120, start_time_s=2.0, end_time_s=2.0)
    ], f"unexpected mapped2: {mapped2}"

    mapped3 = map_time_segments_to_frames(
        [(0.0, 1.01), (1.011, 1.02)],
        fps=60.0,
    )
    assert mapped3 == [
        FrameSegment(start_frame=0, end_frame=61, start_time_s=0.0, end_time_s=1.02)
    ], f"unexpected mapped3: {mapped3}"

    time_axis = [float(i) / 60.0 for i in range(600)]
    mapped4 = map_time_segments_to_frame_indices([(2.0, 6.0)], frame_time_s=time_axis)
    assert mapped4 == [
        FrameSegment(start_frame=120, end_frame=359, start_time_s=2.0, end_time_s=6.0)
    ], f"unexpected mapped4: {mapped4}"

    print("cut_events selftest: OK")


if __name__ == "__main__":
    _selftest()
