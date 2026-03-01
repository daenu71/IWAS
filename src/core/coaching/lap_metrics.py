"""Runtime module for core/coaching/lap_metrics.py."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Any

"""
Lap summary rules:
- complete lap: closed by LapCompleted transition (preferred) or LapDistPct wrap fallback,
  with minimum sample/duration thresholds to avoid tiny fragments.
- lap_incomplete: lap is not complete for browser semantics (open/current lap, lap counter glitch,
  or insufficient LapDistPct coverage in fallback mode).
- lap_offtrack: lap had any off-track sample. `IsOnTrackCar` is authoritative, `IsOnTrack` is fallback.
- best lap: minimum duration among laps with `lap_incomplete=False` and `lap_offtrack=False`.
"""


@dataclass
class LapSlice:
    """Container and behavior for Lap Slice."""
    lap_no: int | None
    start_idx: int
    end_idx: int
    start_ts: float | None
    end_ts: float | None
    duration_s: float | None
    sample_count: int
    reason: str
    is_complete: bool
    is_valid: bool | None
    lap_incomplete: bool = False
    lap_offtrack: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert value to dict."""
        return {
            "lap_no": self.lap_no,
            "start_idx": self.start_idx,
            "end_idx": self.end_idx,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "duration_s": self.duration_s,
            "sample_count": self.sample_count,
            "reason": self.reason,
            "is_complete": self.is_complete,
            "is_valid": self.is_valid,
            "lap_incomplete": self.lap_incomplete,
            "lap_offtrack": self.lap_offtrack,
        }


@dataclass
class RunLapMetrics:
    """Container and behavior for Run Lap Metrics."""
    laps_completed: int = 0
    laps_including_current: int = 0
    best_valid_lap_s: float | None = None
    total_time_s: float | None = None
    last_driven_ts: float | None = None
    last_driven_source: str = "unknown"
    lap_slices: list[LapSlice] = field(default_factory=list)
    source: str = "none"


@dataclass
class _Boundary:
    """Container and behavior for Boundary."""
    idx: int
    reason: str
    lap_no: int | None = None
    lap_time_hint_s: float | None = None


def compute_run_lap_metrics(
    *,
    parquet_path: Path | None,
    run_meta: dict[str, Any],
    sample_hz: float | None,
    fallback_last_ts: float | None,
    meta_path: Path | None = None,
) -> RunLapMetrics:
    """Compute run lap metrics."""
    if parquet_path is None or not parquet_path.exists():
        return _compute_from_meta_only(
            run_meta=run_meta,
            sample_hz=sample_hz,
            fallback_last_ts=fallback_last_ts,
            parquet_path=parquet_path,
            meta_path=meta_path,
        )

    try:
        data, row_count = _read_parquet_columns(parquet_path)
    except Exception:
        return _compute_from_meta_only(
            run_meta=run_meta,
            sample_hz=sample_hz,
            fallback_last_ts=fallback_last_ts,
            parquet_path=parquet_path,
            meta_path=meta_path,
        )

    if row_count <= 0:
        return _compute_from_meta_only(
            run_meta=run_meta,
            sample_hz=sample_hz,
            fallback_last_ts=fallback_last_ts,
            parquet_path=parquet_path,
            meta_path=meta_path,
        )

    ts_values = data.get("ts", [])
    session_time_values = data.get("SessionTime", [])
    monotonic_values = data.get("monotonic_ts", [])
    lap_values = data.get("Lap", [])
    lap_completed_values = data.get("LapCompleted", [])
    lap_dist_pct_values = data.get("LapDistPct", [])
    lap_last_time_values = data.get("LapLastLapTime", [])
    lap_current_time_values = data.get("LapCurrentLapTime", [])
    on_track_values = data.get("IsOnTrack", [])
    on_track_car_values = data.get("IsOnTrackCar", [])

    time_values, time_source = _select_time_series(ts_values, session_time_values, monotonic_values)

    last_driven_ts = _max_finite(ts_values)
    last_driven_source = "parquet.ts" if last_driven_ts is not None else "parquet.mtime"
    if last_driven_ts is None:
        last_driven_ts = _path_mtime_ts(parquet_path)
    if last_driven_ts is None:
        last_driven_ts = _path_mtime_ts(meta_path)
        last_driven_source = "meta.mtime"
    if last_driven_ts is None:
        last_driven_ts = fallback_last_ts
        last_driven_source = "fallback"

    boundaries_lc = _lap_completed_boundaries(
        lap_completed_values=lap_completed_values,
        lap_last_time_values=lap_last_time_values,
    )
    boundaries_wrap = _lap_dist_wrap_boundaries(
        lap_dist_pct_values=lap_dist_pct_values,
        lap_values=lap_values,
        lap_completed_values=lap_completed_values,
        lap_last_time_values=lap_last_time_values,
    )
    use_lap_completed = len(boundaries_lc) > 0
    boundaries = boundaries_lc if use_lap_completed else boundaries_wrap
    source = "parquet_lap_completed" if use_lap_completed else "parquet_lapdist_wrap"

    min_complete_samples = _min_complete_samples(sample_hz)
    start_idx = 0
    clear_start = _looks_like_lap_start(
        idx=0,
        lap_dist_pct_values=lap_dist_pct_values,
        lap_current_time_values=lap_current_time_values,
    )
    complete_slices: list[LapSlice] = []

    for boundary in boundaries:
        if boundary.idx <= start_idx:
            start_idx = boundary.idx
            clear_start = True
            continue
        end_idx = boundary.idx - 1
        seg = _build_lap_slice(
            lap_no=boundary.lap_no or _infer_lap_no_for_completed(end_idx, lap_values, lap_completed_values),
            start_idx=start_idx,
            end_idx=end_idx,
            time_values=time_values,
            time_source=time_source,
            duration_hint_s=boundary.lap_time_hint_s,
            reason=boundary.reason,
            is_complete=False,
            is_valid=None,
        )
        seg_offtrack, has_on_track_signal = _resolve_lap_offtrack(
            seg=seg,
            on_track_values=on_track_values,
            on_track_car_values=on_track_car_values,
        )
        seg.lap_offtrack = seg_offtrack
        if has_on_track_signal:
            seg.is_valid = not seg_offtrack
        else:
            seg.is_valid = _meta_lap_validity(run_meta=run_meta, lap_no=seg.lap_no, lap_index=len(complete_slices))
        seg.is_complete = clear_start and _passes_complete_threshold(seg, min_complete_samples=min_complete_samples)
        if seg.is_complete:
            complete_slices.append(seg)
        start_idx = boundary.idx
        clear_start = True

    all_slices: list[LapSlice] = list(complete_slices)
    incomplete_slice: LapSlice | None = None
    if start_idx < row_count:
        incomplete_slice = _build_lap_slice(
            lap_no=_infer_current_lap_no(lap_values=lap_values, lap_completed_values=lap_completed_values, complete_slices=complete_slices),
            start_idx=start_idx,
            end_idx=row_count - 1,
            time_values=time_values,
            time_source=time_source,
            duration_hint_s=None,
            reason="current_incomplete",
            is_complete=False,
            is_valid=None,
        )
        incomplete_offtrack, has_on_track_signal = _resolve_lap_offtrack(
            seg=incomplete_slice,
            on_track_values=on_track_values,
            on_track_car_values=on_track_car_values,
        )
        incomplete_slice.lap_offtrack = incomplete_offtrack
        if has_on_track_signal:
            incomplete_slice.is_valid = not incomplete_offtrack
        else:
            incomplete_slice.is_valid = _meta_lap_validity(
                run_meta=run_meta,
                lap_no=incomplete_slice.lap_no,
                lap_index=len(complete_slices),
            )
        if _should_show_incomplete_lap(
            seg=incomplete_slice,
            sample_hz=sample_hz,
            lap_dist_pct_values=lap_dist_pct_values,
        ):
            all_slices.append(incomplete_slice)
        else:
            incomplete_slice = None

    _apply_lap_semantics(lap_slices=all_slices, lap_dist_pct_values=lap_dist_pct_values)
    valid_complete_times = [
        seg.duration_s
        for seg in all_slices
        if not seg.lap_incomplete and not seg.lap_offtrack and seg.duration_s is not None
    ]
    total_time = _series_duration(time_values)
    laps_completed = sum(1 for seg in all_slices if not seg.lap_incomplete)
    laps_including_current = len(all_slices)
    best_valid_lap_s = min(valid_complete_times) if valid_complete_times else None

    return RunLapMetrics(
        laps_completed=laps_completed,
        laps_including_current=laps_including_current,
        best_valid_lap_s=best_valid_lap_s,
        total_time_s=total_time,
        last_driven_ts=last_driven_ts,
        last_driven_source=last_driven_source,
        lap_slices=all_slices,
        source=source,
    )


def _compute_from_meta_only(
    *,
    run_meta: dict[str, Any],
    sample_hz: float | None,
    fallback_last_ts: float | None,
    parquet_path: Path | None,
    meta_path: Path | None,
) -> RunLapMetrics:
    """Compute from meta only."""
    lap_segments_raw = run_meta.get("lap_segments")
    lap_segments = [seg for seg in lap_segments_raw if isinstance(seg, dict)] if isinstance(lap_segments_raw, list) else []
    min_complete_samples = _min_complete_samples(sample_hz)
    min_incomplete_samples = _min_incomplete_samples(sample_hz)
    complete_slices: list[LapSlice] = []
    incomplete_slice: LapSlice | None = None
    for idx, seg_raw in enumerate(lap_segments):
        start_raw = seg_raw.get("start_sample") if "start_sample" in seg_raw else seg_raw.get("start_idx")
        end_raw = seg_raw.get("end_sample") if "end_sample" in seg_raw else seg_raw.get("end_idx")
        start_idx = _coerce_optional_int(start_raw)
        end_idx = _coerce_optional_int(end_raw)
        if start_idx is None or end_idx is None or end_idx < start_idx:
            continue
        seg = LapSlice(
            lap_no=_coerce_optional_int(seg_raw.get("lap_no")),
            start_idx=start_idx,
            end_idx=end_idx,
            start_ts=_coerce_optional_float(seg_raw.get("start_ts")),
            end_ts=_coerce_optional_float(seg_raw.get("end_ts")),
            duration_s=None,
            sample_count=(end_idx - start_idx + 1),
            reason=str(seg_raw.get("reason") or "meta"),
            is_complete=False,
            is_valid=_extract_explicit_validity(seg_raw),
        )
        explicit_offtrack = _extract_explicit_offtrack(seg_raw)
        if explicit_offtrack is not None:
            seg.lap_offtrack = explicit_offtrack
            if seg.is_valid is None:
                seg.is_valid = not explicit_offtrack
        seg.duration_s = _duration_from_bounds(seg.start_ts, seg.end_ts)
        reason_lower = str(seg.reason or "").strip().lower()
        closes_lap = reason_lower in {"counter_change", "lapcompleted", "lapdistpctwrap", "distpct_wrap"}
        seg.is_complete = closes_lap and _passes_complete_threshold(seg, min_complete_samples=min_complete_samples)
        if seg.is_complete:
            complete_slices.append(seg)
            continue
        is_plausible_current = seg.sample_count >= min_incomplete_samples
        if not is_plausible_current and seg.duration_s is not None and seg.duration_s >= 5.0:
            is_plausible_current = True
        if is_plausible_current:
            seg.reason = "current_incomplete(meta)"
            incomplete_slice = seg

    laps_completed = len(complete_slices)
    total_time = _duration_from_run_meta(run_meta)
    if total_time is None:
        total_time = sum(seg.duration_s for seg in complete_slices if seg.duration_s is not None) or None
    if total_time is None:
        sample_count = _coerce_optional_float(run_meta.get("sample_count"))
        if sample_count is not None and sample_hz and sample_hz > 0:
            total_time = sample_count / sample_hz

    last_driven_ts = _path_mtime_ts(parquet_path)
    last_driven_source = "parquet.mtime"
    if last_driven_ts is None:
        last_driven_ts = _path_mtime_ts(meta_path)
        last_driven_source = "meta.mtime"
    if last_driven_ts is None:
        last_driven_ts = fallback_last_ts
        last_driven_source = "fallback"

    lap_slices = list(complete_slices)
    laps_including_current = laps_completed
    if incomplete_slice is not None:
        lap_slices.append(incomplete_slice)
        laps_including_current += 1

    _apply_lap_semantics(lap_slices=lap_slices, lap_dist_pct_values=[])
    explicit_valid_times = [
        seg.duration_s
        for seg in lap_slices
        if not seg.lap_incomplete and not seg.lap_offtrack and seg.duration_s is not None
    ]
    laps_completed = sum(1 for seg in lap_slices if not seg.lap_incomplete)
    laps_including_current = len(lap_slices)

    return RunLapMetrics(
        laps_completed=laps_completed,
        laps_including_current=laps_including_current,
        best_valid_lap_s=min(explicit_valid_times) if explicit_valid_times else None,
        total_time_s=total_time,
        last_driven_ts=last_driven_ts,
        last_driven_source=last_driven_source,
        lap_slices=lap_slices,
        source="meta",
    )


def _read_parquet_columns(path: Path) -> tuple[dict[str, list[Any]], int]:
    """Read parquet columns."""
    import pyarrow.parquet as pq  # type: ignore

    requested = [
        "ts",
        "monotonic_ts",
        "SessionTime",
        "Lap",
        "LapCompleted",
        "LapDistPct",
        "LapLastLapTime",
        "LapCurrentLapTime",
        "IsOnTrack",
        "IsOnTrackCar",
    ]
    parquet_file = pq.ParquetFile(path)
    try:
        available_names = set(parquet_file.schema_arrow.names)
    except Exception:
        available_names = set()
    selected = [name for name in requested if name in available_names]
    if selected:
        table = parquet_file.read(columns=selected)
        data = table.to_pydict()
        row_count = int(table.num_rows)
    else:
        data = {}
        meta = getattr(parquet_file, "metadata", None)
        row_count = int(getattr(meta, "num_rows", 0) or 0)
    for name in requested:
        if name not in data:
            data[name] = [None] * row_count
    return data, row_count


def _lap_completed_boundaries(
    *,
    lap_completed_values: list[Any],
    lap_last_time_values: list[Any],
) -> list[_Boundary]:
    """Implement lap completed boundaries logic."""
    boundaries: list[_Boundary] = []
    prev_value: int | None = None
    for idx, raw in enumerate(lap_completed_values):
        value = _coerce_optional_int(raw)
        if value is None:
            continue
        if prev_value is None:
            prev_value = value
            continue
        if value > prev_value:
            boundaries.append(
                _Boundary(
                    idx=idx,
                    reason="LapCompleted",
                    lap_no=value,
                    lap_time_hint_s=_clean_lap_time_hint(lap_last_time_values, idx),
                )
            )
        prev_value = value
    return boundaries


def _lap_dist_wrap_boundaries(
    *,
    lap_dist_pct_values: list[Any],
    lap_values: list[Any],
    lap_completed_values: list[Any],
    lap_last_time_values: list[Any],
) -> list[_Boundary]:
    """Implement lap dist wrap boundaries logic."""
    boundaries: list[_Boundary] = []
    cooldown_active = False
    prev_pct: float | None = None
    for idx, raw in enumerate(lap_dist_pct_values):
        pct = _coerce_optional_float(raw)
        if pct is None:
            continue
        if prev_pct is None:
            prev_pct = pct
            continue
        if cooldown_active:
            if pct > 0.10 and pct < 0.99:
                cooldown_active = False
            prev_pct = pct
            continue
        if prev_pct >= 0.99 and pct <= 0.01:
            end_idx = max(0, idx - 1)
            lap_no = _coerce_optional_int(_list_get(lap_values, end_idx))
            if lap_no is None:
                completed = _coerce_optional_int(_list_get(lap_completed_values, idx))
                if completed is not None:
                    lap_no = completed
            boundaries.append(
                _Boundary(
                    idx=idx,
                    reason="LapDistPctWrap",
                    lap_no=lap_no,
                    lap_time_hint_s=_clean_lap_time_hint(lap_last_time_values, idx),
                )
            )
            cooldown_active = True
        prev_pct = pct
    return boundaries


def _build_lap_slice(
    *,
    lap_no: int | None,
    start_idx: int,
    end_idx: int,
    time_values: list[Any],
    time_source: str,
    duration_hint_s: float | None,
    reason: str,
    is_complete: bool,
    is_valid: bool | None,
) -> LapSlice:
    """Build and return lap slice."""
    start_ts = _coerce_optional_float(_list_get(time_values, start_idx))
    end_ts = _coerce_optional_float(_list_get(time_values, end_idx))
    duration = _duration_from_bounds(start_ts, end_ts)
    if duration is None and duration_hint_s is not None and duration_hint_s > 0:
        duration = duration_hint_s
    suffix = f" ({time_source})" if time_source else ""
    return LapSlice(
        lap_no=lap_no,
        start_idx=start_idx,
        end_idx=end_idx,
        start_ts=start_ts,
        end_ts=end_ts,
        duration_s=duration,
        sample_count=(end_idx - start_idx + 1),
        reason=f"{reason}{suffix}",
        is_complete=is_complete,
        is_valid=is_valid,
    )


def _resolve_lap_offtrack(
    *,
    seg: LapSlice,
    on_track_values: list[Any],
    on_track_car_values: list[Any],
) -> tuple[bool, bool]:
    # Preferred source for offtrack is IsOnTrackCar; IsOnTrack is fallback when car signal is unavailable.
    """Resolve lap offtrack."""
    car_offtrack, car_seen = _scan_offtrack_signal(on_track_car_values, seg.start_idx, seg.end_idx)
    if car_seen:
        return (car_offtrack, True)
    track_offtrack, track_seen = _scan_offtrack_signal(on_track_values, seg.start_idx, seg.end_idx)
    if track_seen:
        return (track_offtrack, True)
    return (False, False)


def _scan_offtrack_signal(values: list[Any], start_idx: int, end_idx: int) -> tuple[bool, bool]:
    """Scan offtrack signal."""
    if not values:
        return (False, False)
    seen = False
    for idx in range(start_idx, end_idx + 1):
        value = _coerce_optional_bool(_list_get(values, idx))
        if value is None:
            continue
        seen = True
        if value is False:
            return (True, True)
    return (False, seen)


def _meta_lap_validity(*, run_meta: dict[str, Any], lap_no: int | None, lap_index: int) -> bool | None:
    """Implement meta lap validity logic."""
    by_no = str(lap_no) if lap_no is not None else None

    lap_validity = run_meta.get("lap_validity")
    if isinstance(lap_validity, dict):
        if by_no is not None and by_no in lap_validity:
            return _coerce_optional_bool(lap_validity.get(by_no))
        if lap_no is not None and lap_no in lap_validity:
            return _coerce_optional_bool(lap_validity.get(lap_no))

    invalid_lists = [
        run_meta.get("offtrack_laps"),
        run_meta.get("invalid_laps"),
    ]
    for items in invalid_lists:
        invalid = _coerce_int_set(items)
        if invalid is not None and lap_no is not None and lap_no in invalid:
            return False

    valid_items = run_meta.get("valid_laps")
    valid = _coerce_int_set(valid_items)
    if valid is not None and lap_no is not None:
        return lap_no in valid

    lap_segments_raw = run_meta.get("lap_segments")
    if isinstance(lap_segments_raw, list):
        candidate: dict[str, Any] | None = None
        if 0 <= lap_index < len(lap_segments_raw):
            item = lap_segments_raw[lap_index]
            if isinstance(item, dict):
                candidate = item
        if candidate is None and lap_no is not None:
            for item in lap_segments_raw:
                if not isinstance(item, dict):
                    continue
                item_lap_no = _coerce_optional_int(item.get("lap_no"))
                if item_lap_no == lap_no:
                    candidate = item
                    break
        if isinstance(candidate, dict):
            explicit = _extract_explicit_validity(candidate)
            if explicit is not None:
                return explicit

    return None


def _extract_explicit_validity(item: dict[str, Any]) -> bool | None:
    """Extract explicit validity."""
    if not isinstance(item, dict):
        return None
    for key in ("is_valid", "valid", "lap_valid"):
        if key in item:
            return _coerce_optional_bool(item.get(key))
    for key in ("offtrack", "is_offtrack", "lap_offtrack"):
        if key in item:
            value = _coerce_optional_bool(item.get(key))
            if value is not None:
                return not value
    if "on_track" in item:
        return _coerce_optional_bool(item.get("on_track"))
    return None


def _extract_explicit_offtrack(item: dict[str, Any]) -> bool | None:
    """Extract explicit offtrack."""
    if not isinstance(item, dict):
        return None
    for key in ("lap_offtrack", "is_offtrack", "offtrack"):
        if key in item:
            return _coerce_optional_bool(item.get(key))
    for key in ("is_valid", "valid", "lap_valid"):
        if key in item:
            value = _coerce_optional_bool(item.get(key))
            if value is not None:
                return not value
    if "on_track" in item:
        value = _coerce_optional_bool(item.get("on_track"))
        if value is not None:
            return not value
    return None


def _select_time_series(
    ts_values: list[Any],
    session_time_values: list[Any],
    monotonic_values: list[Any],
) -> tuple[list[Any], str]:
    """Select time series."""
    if _count_finite(ts_values) >= 2:
        return ts_values, "ts"
    if _count_finite(session_time_values) >= 2:
        return session_time_values, "SessionTime"
    if _count_finite(monotonic_values) >= 2:
        return monotonic_values, "monotonic_ts"
    if len(ts_values) > 0:
        return ts_values, "ts"
    if len(session_time_values) > 0:
        return session_time_values, "SessionTime"
    return monotonic_values, "monotonic_ts"


def _looks_like_lap_start(
    *,
    idx: int,
    lap_dist_pct_values: list[Any],
    lap_current_time_values: list[Any],
) -> bool:
    """Implement looks like lap start logic."""
    pct = _coerce_optional_float(_list_get(lap_dist_pct_values, idx))
    if pct is not None and 0.0 <= pct <= 0.08:
        return True
    lap_time = _coerce_optional_float(_list_get(lap_current_time_values, idx))
    if lap_time is not None and 0.0 <= lap_time <= 2.0:
        return True
    return False


def _infer_lap_no_for_completed(end_idx: int, lap_values: list[Any], lap_completed_values: list[Any]) -> int | None:
    """Implement infer lap no for completed logic."""
    lap_no = _coerce_optional_int(_list_get(lap_values, end_idx))
    if lap_no is not None:
        return lap_no
    completed = _coerce_optional_int(_list_get(lap_completed_values, end_idx + 1))
    if completed is not None:
        return completed
    completed_prev = _coerce_optional_int(_list_get(lap_completed_values, end_idx))
    if completed_prev is not None:
        return completed_prev
    return None


def _infer_current_lap_no(
    *,
    lap_values: list[Any],
    lap_completed_values: list[Any],
    complete_slices: list[LapSlice],
) -> int | None:
    """Implement infer current lap no logic."""
    for raw in reversed(lap_values):
        lap_no = _coerce_optional_int(raw)
        if lap_no is not None:
            return lap_no
    for raw in reversed(lap_completed_values):
        completed = _coerce_optional_int(raw)
        if completed is not None:
            return completed + 1
    if complete_slices:
        last_lap_no = complete_slices[-1].lap_no
        if last_lap_no is not None:
            return last_lap_no + 1
    return None


def _passes_complete_threshold(seg: LapSlice, *, min_complete_samples: int) -> bool:
    """Implement passes complete threshold logic."""
    if seg.sample_count < max(3, min_complete_samples):
        return False
    if seg.duration_s is not None and seg.duration_s < 1.0:
        return False
    return True


def _should_show_incomplete_lap(
    *,
    seg: LapSlice,
    sample_hz: float | None,
    lap_dist_pct_values: list[Any],
) -> bool:
    """Return whether show incomplete lap."""
    min_samples = _min_incomplete_samples(sample_hz)
    if seg.sample_count >= min_samples:
        return True
    if seg.duration_s is not None and seg.duration_s >= 5.0:
        return True
    pct_range = _lap_dist_pct_range(lap_dist_pct_values, seg.start_idx, seg.end_idx)
    if pct_range is not None and pct_range >= 0.05:
        return True
    return False


def _apply_lap_semantics(*, lap_slices: list[LapSlice], lap_dist_pct_values: list[Any]) -> None:
    # Source-of-truth flags used by the coaching browser:
    # - lap_incomplete: incomplete lap counter/coverage semantics.
    # - lap_offtrack: any in-lap offtrack sample (if signal exists), otherwise explicit metadata only.
    """Apply lap semantics."""
    for seg in lap_slices:
        seg.lap_incomplete = not bool(seg.is_complete)
        seg.lap_offtrack = bool(seg.lap_offtrack)

    # Counter glitches often produce repeated lap_no fragments (e.g. lap N, lap 0, lap N).
    for idx, seg in enumerate(lap_slices):
        if seg.lap_incomplete:
            continue
        lap_no = seg.lap_no
        if lap_no is None or lap_no <= 0:
            seg.lap_incomplete = True
            continue
        for later in lap_slices[idx + 1 :]:
            if later.lap_no == lap_no:
                seg.lap_incomplete = True
                break

    # Coverage fallback for non-LapCompleted closures (or very short laps):
    # a complete lap should either show a wrap or a broad 0..1 LapDistPct coverage.
    for seg in lap_slices:
        if seg.lap_incomplete:
            continue
        coverage = _lap_dist_pct_coverage(lap_dist_pct_values, seg.start_idx, seg.end_idx)
        if coverage is None:
            continue
        min_pct, max_pct, span, has_wrap = coverage
        has_broad_coverage = (
            min_pct is not None
            and max_pct is not None
            and min_pct <= 0.10
            and max_pct >= 0.90
            and span is not None
            and span >= 0.60
        )
        has_full_coverage = has_wrap or has_broad_coverage
        reason_lower = str(seg.reason or "").strip().lower()
        closes_by_lap_completed = "lapcompleted" in reason_lower
        is_extremely_short = seg.duration_s is not None and seg.duration_s < 25.0
        if (not closes_by_lap_completed and not has_full_coverage) or (is_extremely_short and not has_full_coverage):
            seg.lap_incomplete = True


def _lap_dist_pct_coverage(
    values: list[Any],
    start_idx: int,
    end_idx: int,
) -> tuple[float | None, float | None, float | None, bool] | None:
    """Implement lap dist pct coverage logic."""
    if not values or start_idx > end_idx:
        return None
    lo: float | None = None
    hi: float | None = None
    prev_pct: float | None = None
    has_wrap = False
    for idx in range(start_idx, end_idx + 1):
        pct = _coerce_optional_float(_list_get(values, idx))
        if pct is None:
            continue
        lo = pct if lo is None else min(lo, pct)
        hi = pct if hi is None else max(hi, pct)
        if prev_pct is not None and prev_pct >= 0.99 and pct <= 0.01:
            has_wrap = True
        prev_pct = pct
    if lo is None or hi is None:
        return None
    return (lo, hi, hi - lo, has_wrap)


def _lap_dist_pct_range(values: list[Any], start_idx: int, end_idx: int) -> float | None:
    """Implement lap dist pct range logic."""
    if start_idx > end_idx:
        return None
    lo: float | None = None
    hi: float | None = None
    for idx in range(start_idx, end_idx + 1):
        pct = _coerce_optional_float(_list_get(values, idx))
        if pct is None:
            continue
        lo = pct if lo is None else min(lo, pct)
        hi = pct if hi is None else max(hi, pct)
    if lo is None or hi is None:
        return None
    return hi - lo


def _min_complete_samples(sample_hz: float | None) -> int:
    """Implement min complete samples logic."""
    if sample_hz is None or sample_hz <= 0:
        return 10
    return max(10, int(round(sample_hz * 0.75)))


def _min_incomplete_samples(sample_hz: float | None) -> int:
    """Implement min incomplete samples logic."""
    if sample_hz is None or sample_hz <= 0:
        return 25
    return max(25, int(round(sample_hz * 3.0)))


def _clean_lap_time_hint(values: list[Any], idx: int) -> float | None:
    """Implement clean lap time hint logic."""
    value = _coerce_optional_float(_list_get(values, idx))
    if value is None:
        return None
    if value <= 0 or not math.isfinite(value):
        return None
    return value


def _series_duration(values: list[Any]) -> float | None:
    """Implement series duration logic."""
    finite = [v for raw in values if (v := _coerce_optional_float(raw)) is not None and math.isfinite(v)]
    if len(finite) < 2:
        return None
    delta = max(finite) - min(finite)
    if delta < 0:
        return None
    return delta


def _max_finite(values: list[Any]) -> float | None:
    """Implement max finite logic."""
    finite = [v for raw in values if (v := _coerce_optional_float(raw)) is not None and math.isfinite(v)]
    if not finite:
        return None
    return max(finite)


def _duration_from_run_meta(run_meta: dict[str, Any]) -> float | None:
    """Implement duration from run meta logic."""
    start = _coerce_optional_float(run_meta.get("start_session_time"))
    end = _coerce_optional_float(run_meta.get("end_session_time"))
    return _duration_from_bounds(start, end)


def _duration_from_bounds(start: float | None, end: float | None) -> float | None:
    """Implement duration from bounds logic."""
    if start is None or end is None:
        return None
    delta = end - start
    if delta < 0:
        return None
    return delta


def _count_finite(values: list[Any]) -> int:
    """Implement count finite logic."""
    count = 0
    for raw in values:
        value = _coerce_optional_float(raw)
        if value is not None and math.isfinite(value):
            count += 1
    return count


def _coerce_int_set(value: Any) -> set[int] | None:
    """Coerce int set."""
    if not isinstance(value, (list, tuple, set)):
        return None
    result: set[int] = set()
    for item in value:
        parsed = _coerce_optional_int(item)
        if parsed is not None:
            result.add(parsed)
    return result


def _list_get(values: list[Any], idx: int) -> Any:
    """Implement list get logic."""
    if idx < 0:
        return None
    if idx >= len(values):
        return None
    return values[idx]


def _path_mtime_ts(path: Path | None) -> float | None:
    """Implement path mtime ts logic."""
    if path is None:
        return None
    try:
        return path.stat().st_mtime
    except Exception:
        return None


def _coerce_optional_float(value: Any) -> float | None:
    """Coerce optional float."""
    try:
        result = float(value)
    except Exception:
        return None
    if not math.isfinite(result):
        return None
    return result


def _coerce_optional_int(value: Any) -> int | None:
    """Coerce optional int."""
    try:
        return int(value)
    except Exception:
        return None


def _coerce_optional_bool(value: Any) -> bool | None:
    """Coerce optional bool."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value == 0:
            return False
        if value == 1:
            return True
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"0", "false", "no", "n", "off"}:
            return False
        if text in {"1", "true", "yes", "y", "on"}:
            return True
    return None
