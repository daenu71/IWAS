"""Runtime module for core/coaching/lap_segmenter.py."""

from __future__ import annotations

from collections import Counter
import logging
import os
from typing import Any


_LOG = logging.getLogger(__name__)

_TRACK_SURFACE_ON_TRACK = "ON_TRACK"
_TRACK_SURFACE_OFF_TRACK = "OFF_TRACK"
_TRACK_SURFACE_PIT = "PIT"
_TRACK_SURFACE_UNKNOWN = "UNKNOWN"

_TRACK_SURFACE_OFFTRACK_NAMES: set[str] = {
    "offtrack",
    "off",
    "offworld",
}
_TRACK_SURFACE_PIT_NAMES: set[str] = {
    "pitroad",
    "pitstall",
    "inpits",
    "inpitstall",
    "approachingpits",
}
_TRACK_SURFACE_ON_TRACK_NAMES: set[str] = {
    "ontrack",
    "track",
}


class LapSegmenter:
    """Container and behavior for Lap Segmenter."""
    def __init__(
        self,
        *,
        use_ontrack_gate: bool = True,
        wrap_hi: float = 0.99,
        wrap_lo: float = 0.01,
        min_valid_lap_time_s: float = 30.0,
        min_valid_lap_samples: int = 60,
    ) -> None:
        """Implement init logic."""
        self.use_ontrack_gate = bool(use_ontrack_gate)
        self.wrap_hi = float(wrap_hi)
        self.wrap_lo = float(wrap_lo)
        self.min_valid_lap_time_s = max(0.0, float(min_valid_lap_time_s))
        self.min_valid_lap_samples = max(1, int(min_valid_lap_samples))
        self._debug_enabled = self._is_debug_enabled()
        self.reset()

    def reset(self, run_id: int | None = None) -> None:
        """Implement reset logic."""
        self.run_id = run_id
        self.last_lap: int | None = None
        self.last_lapdistpct: float | None = None
        self.current_lap_start_index: int | None = None
        self.current_lap_start_ts: float | None = None
        self.current_lap_no: int | None = None
        self.current_offtrack_incident = False
        self.current_sample_count = 0
        self.current_track_surface_min: int | None = None
        self.current_track_surface_max: int | None = None
        self.current_track_surface_values: Counter[int] = Counter()
        self.current_incident_min: int | None = None
        self.current_incident_max: int | None = None
        self._prev_incident_count: int | None = None
        self.segments: list[dict[str, Any]] = []
        self._observed_track_surface_values: Counter[int] = Counter()
        self._debug_surface_values_logged = False

        self._counter_change_seen = False
        self._wrap_cooldown_active = False

    def update(
        self,
        sample: dict[str, Any],
        sample_index: int,
        now_ts: float | None = None,
    ) -> list[dict[str, Any]]:
        """Update."""
        sample_dict = sample if isinstance(sample, dict) else {}
        events: list[dict[str, Any]] = []

        counter_key, counter_value = self._select_lap_counter(sample_dict)
        lapdistpct = self._coerce_float(self._read_value(sample_dict, "LapDistPct"))
        is_on_track = self._coerce_bool(self._read_value(sample_dict, "IsOnTrackCar"))
        on_pit_road = self._coerce_bool(self._read_value(sample_dict, "OnPitRoad"))
        track_surface = self._read_value(sample_dict, "PlayerTrackSurface")
        incident_count = self._coerce_int(self._read_value(sample_dict, "PlayerCarMyIncidentCount"))

        if self.current_lap_start_index is None:
            self._start_segment(sample_index, now_ts, lap_no=counter_value)

        handled_by_counter = False
        if counter_value is not None:
            if self.last_lap is None:
                self.last_lap = counter_value
                if self.current_lap_no is None:
                    self.current_lap_no = counter_value
            elif counter_value != self.last_lap:
                self._counter_change_seen = True
                handled_by_counter = True
                close_event = self._close_segment(
                    end_sample_index=sample_index - 1,
                    end_ts=now_ts,
                    reason="counter_change",
                )
                if close_event is not None:
                    events.append(close_event)
                self._start_segment(sample_index, now_ts, lap_no=counter_value)
                self.last_lap = counter_value
            else:
                self.last_lap = counter_value

        if not handled_by_counter and self._should_consider_wrap(counter_key, counter_value):
            if self._wrap_cooldown_active and self._is_wrap_cooldown_released(lapdistpct):
                self._wrap_cooldown_active = False
            if self._should_trigger_wrap(lapdistpct, is_on_track):
                close_event = self._close_segment(
                    end_sample_index=sample_index - 1,
                    end_ts=now_ts,
                    reason="distpct_wrap",
                )
                if close_event is not None:
                    events.append(close_event)
                next_lap_no = self._derive_next_lap_no(counter_value)
                self._start_segment(sample_index, now_ts, lap_no=next_lap_no)
                self._wrap_cooldown_active = True

        self._accumulate_current_lap_sample(
            track_surface=track_surface,
            incident_count=incident_count,
            is_on_track_car=is_on_track,
            on_pit_road=on_pit_road,
        )
        self.last_lapdistpct = lapdistpct
        return events

    def finalize(self, last_sample_index: int, now_ts: float | None = None) -> list[dict[str, Any]]:
        """Implement finalize logic."""
        close_event = self._close_segment(
            end_sample_index=last_sample_index,
            end_ts=now_ts,
            reason="run_end",
        )
        self._debug_log_surface_values_once()
        if close_event is None:
            return []
        return [close_event]

    def _should_consider_wrap(self, counter_key: str | None, counter_value: int | None) -> bool:
        """Return whether consider wrap."""
        if counter_key is None or counter_value is None:
            return True
        return not self._counter_change_seen

    def _should_trigger_wrap(self, lapdistpct: float | None, is_on_track: bool | None) -> bool:
        """Return whether trigger wrap."""
        if lapdistpct is None:
            return False
        if self._wrap_cooldown_active:
            return False
        prev = self.last_lapdistpct
        if prev is None:
            return False
        if not (prev >= self.wrap_hi and lapdistpct <= self.wrap_lo):
            return False
        if self.use_ontrack_gate and is_on_track is not None and is_on_track is not True:
            return False
        return True

    def _is_wrap_cooldown_released(self, lapdistpct: float | None) -> bool:
        """Return whether wrap cooldown released."""
        if lapdistpct is None:
            return False
        if lapdistpct <= 0.1:
            return False
        return lapdistpct < self.wrap_hi

    def _derive_next_lap_no(self, counter_value: int | None) -> int | None:
        """Implement derive next lap no logic."""
        if counter_value is not None:
            if self.current_lap_no is not None and counter_value == self.current_lap_no:
                return self.current_lap_no + 1
            return counter_value
        if self.current_lap_no is None:
            return None
        return self.current_lap_no + 1

    def _start_segment(self, sample_index: int, now_ts: float | None, lap_no: int | None) -> None:
        """Start segment."""
        self.current_lap_start_index = int(sample_index)
        self.current_lap_start_ts = now_ts
        self.current_lap_no = lap_no
        self._reset_current_lap_meta()

    def _close_segment(
        self,
        *,
        end_sample_index: int,
        end_ts: float | None,
        reason: str,
    ) -> dict[str, Any] | None:
        """Close segment."""
        start_index = self.current_lap_start_index
        if start_index is None:
            return None
        if end_sample_index < start_index:
            return None

        sample_count = max(0, int(end_sample_index - start_index + 1))
        lap_time_s = self._compute_lap_time_s(self.current_lap_start_ts, end_ts)
        structural_complete = reason in {"counter_change", "distpct_wrap"}
        lap_complete = bool(structural_complete and self._passes_lap_sanity(lap_time_s=lap_time_s, sample_count=sample_count))
        incident_delta = 0
        if self.current_incident_min is not None and self.current_incident_max is not None:
            incident_delta = max(0, int(self.current_incident_max) - int(self.current_incident_min))
        offtrack_surface = bool(self.current_offtrack_incident)
        valid_lap = bool(
            lap_complete
            and self._passes_lap_sanity(lap_time_s=lap_time_s, sample_count=sample_count)
            and not offtrack_surface
            and incident_delta == 0
        )
        lap_index = len(self.segments)
        segment: dict[str, Any] = {
            "lap_index": int(lap_index),
            "start_idx": int(start_index),
            "end_idx": int(end_sample_index),
            "reason": str(reason),
            "lap_time_s": lap_time_s,
            "sample_count": int(sample_count),
            "lap_complete": bool(lap_complete),
            "offtrack_surface": offtrack_surface,
            "incident_delta": int(incident_delta),
            "valid_lap": valid_lap,
            "is_complete": bool(lap_complete),
            "lap_incomplete": not bool(lap_complete),
            "lap_offtrack": offtrack_surface,
            "is_valid": valid_lap,
        }
        if self.current_lap_no is not None:
            segment["lap_no"] = int(self.current_lap_no)
        if self.current_lap_start_ts is not None:
            segment["start_ts"] = float(self.current_lap_start_ts)
        if end_ts is not None:
            segment["end_ts"] = float(end_ts)
        if self._debug_enabled:
            segment["track_surface_min"] = self.current_track_surface_min
            segment["track_surface_max"] = self.current_track_surface_max
            if self.current_track_surface_values:
                segment["track_surface_values"] = [
                    [int(value), int(count)] for value, count in self.current_track_surface_values.most_common(3)
                ]
            if self.current_incident_min is not None:
                segment["incident_min"] = int(self.current_incident_min)
            if self.current_incident_max is not None:
                segment["incident_max"] = int(self.current_incident_max)

        self._apply_duplicate_lap_policy(segment)
        self.segments.append(segment)

        lap_complete = bool(segment.get("lap_complete"))
        valid_lap = bool(segment.get("valid_lap"))
        event: dict[str, Any] = {
            "type": "LAP_END",
            "reason": str(reason),
            "start_sample_index": int(start_index),
            "end_sample_index": int(end_sample_index),
            "lap_index": self.current_lap_no,
            "lap_complete": bool(lap_complete),
            "offtrack_surface": offtrack_surface,
            "incident_delta": int(incident_delta),
            "valid_lap": valid_lap,
        }
        if self.current_lap_start_ts is not None:
            event["start_ts"] = float(self.current_lap_start_ts)
        if end_ts is not None:
            event["end_ts"] = float(end_ts)
        if lap_time_s is not None:
            event["lap_time_s"] = float(lap_time_s)
        event["sample_count"] = int(sample_count)
        if self._debug_enabled and _LOG.isEnabledFor(logging.DEBUG):
            lap_no = self._coerce_int(segment.get("lap_no"))
            _LOG.debug(
                "coaching.lap_segmenter run=%s lap_no=%s reason=%s lap_time_s=%s sample_count=%s lap_complete=%s offtrack=%s incident_delta=%s valid=%s track_min=%s track_max=%s uniq=%s incidents=%s/%s",
                self.run_id,
                lap_no,
                reason,
                lap_time_s,
                sample_count,
                lap_complete,
                offtrack_surface,
                incident_delta,
                valid_lap,
                self.current_track_surface_min,
                self.current_track_surface_max,
                len(self.current_track_surface_values),
                self.current_incident_min,
                self.current_incident_max,
            )
        self.current_lap_start_index = None
        self.current_lap_start_ts = None
        self.current_lap_no = None
        self._reset_current_lap_meta()
        return event

    def _accumulate_current_lap_sample(
        self,
        *,
        track_surface: Any,
        incident_count: int | None,
        is_on_track_car: bool | None,
        on_pit_road: bool | None,
    ) -> None:
        """Implement accumulate current lap sample logic."""
        if self.current_lap_start_index is None:
            return
        self.current_sample_count += 1
        enum_value = self._coerce_int(track_surface)
        if enum_value is not None:
            if self.current_track_surface_min is None or enum_value < self.current_track_surface_min:
                self.current_track_surface_min = enum_value
            if self.current_track_surface_max is None or enum_value > self.current_track_surface_max:
                self.current_track_surface_max = enum_value
            self.current_track_surface_values[int(enum_value)] += 1
            self._observed_track_surface_values[int(enum_value)] += 1

        if incident_count is None:
            return
        if self._prev_incident_count is not None:
            step = incident_count - self._prev_incident_count
            if step in (1, 2):
                self.current_offtrack_incident = True
        self._prev_incident_count = incident_count
        if self.current_incident_min is None or incident_count < self.current_incident_min:
            self.current_incident_min = int(incident_count)
        if self.current_incident_max is None or incident_count > self.current_incident_max:
            self.current_incident_max = int(incident_count)

    def _reset_current_lap_meta(self) -> None:
        """Implement reset current lap meta logic."""
        self.current_offtrack_incident = False
        self.current_sample_count = 0
        self.current_track_surface_min = None
        self.current_track_surface_max = None
        self.current_track_surface_values = Counter()
        self.current_incident_min = None
        self.current_incident_max = None
        self._prev_incident_count = None

    @staticmethod
    def _read_value(sample: dict[str, Any], key: str) -> Any:
        """Read value."""
        raw = sample.get("raw")
        if isinstance(raw, dict) and key in raw:
            return raw.get(key)
        return sample.get(key)

    @staticmethod
    def _select_lap_counter(sample: dict[str, Any]) -> tuple[str | None, int | None]:
        """Select lap counter."""
        lap = LapSegmenter._coerce_int(LapSegmenter._read_value(sample, "Lap"))
        if lap is not None:
            return ("Lap", lap)
        lap_completed = LapSegmenter._coerce_int(LapSegmenter._read_value(sample, "LapCompleted"))
        if lap_completed is not None:
            return ("LapCompleted", lap_completed)
        return (None, None)

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        """Coerce int."""
        try:
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _coerce_float(value: Any) -> float | None:
        """Coerce float."""
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _coerce_bool(value: Any) -> bool | None:
        """Coerce bool."""
        if isinstance(value, bool):
            return value
        if value is None:
            return None
        try:
            return bool(value)
        except Exception:
            return None

    def _passes_lap_sanity(self, *, lap_time_s: float | None, sample_count: int) -> bool:
        """Implement passes lap sanity logic."""
        if lap_time_s is None or lap_time_s < self.min_valid_lap_time_s:
            return False
        if sample_count < self.min_valid_lap_samples:
            return False
        return True

    @staticmethod
    def _compute_lap_time_s(start_ts: float | None, end_ts: float | None) -> float | None:
        """Compute lap time s."""
        if start_ts is None or end_ts is None:
            return None
        lap_time_s = float(end_ts) - float(start_ts)
        if lap_time_s < 0.0:
            return None
        return float(lap_time_s)

    @classmethod
    def classify_track_surface(
        cls,
        value: Any,
        *,
        is_on_track_car: bool | None = None,
        on_pit_road: bool | None = None,
    ) -> str:
        """Implement classify track surface logic."""
        normalized = cls._normalize_enum_text(value)
        if normalized:
            if normalized in _TRACK_SURFACE_OFFTRACK_NAMES:
                return _TRACK_SURFACE_OFF_TRACK
            if normalized in _TRACK_SURFACE_PIT_NAMES:
                return _TRACK_SURFACE_PIT
            if normalized in _TRACK_SURFACE_ON_TRACK_NAMES:
                return _TRACK_SURFACE_ON_TRACK

        enum_value = cls._coerce_int(value)
        if enum_value is not None:
            if enum_value < 0:
                if on_pit_road is True:
                    return _TRACK_SURFACE_PIT
                if is_on_track_car is True:
                    return _TRACK_SURFACE_OFF_TRACK
                return _TRACK_SURFACE_UNKNOWN
            if enum_value == 0:
                return _TRACK_SURFACE_OFF_TRACK
            if enum_value == 1:
                return _TRACK_SURFACE_PIT
            if enum_value == 2:
                if on_pit_road is True:
                    return _TRACK_SURFACE_PIT
                if is_on_track_car is True:
                    return _TRACK_SURFACE_OFF_TRACK
                return _TRACK_SURFACE_UNKNOWN
            if enum_value == 3:
                return _TRACK_SURFACE_ON_TRACK
            if on_pit_road is True:
                return _TRACK_SURFACE_PIT
            if is_on_track_car is False:
                return _TRACK_SURFACE_OFF_TRACK
            if is_on_track_car is True:
                return _TRACK_SURFACE_ON_TRACK
            # Unknown future positive codes are safer as offtrack for lap validity.
            return _TRACK_SURFACE_OFF_TRACK

        if on_pit_road is True:
            return _TRACK_SURFACE_PIT
        if is_on_track_car is True:
            return _TRACK_SURFACE_ON_TRACK
        return _TRACK_SURFACE_UNKNOWN

    def _apply_duplicate_lap_policy(self, new_segment: dict[str, Any]) -> None:
        """Apply duplicate lap policy."""
        lap_no = self._coerce_int(new_segment.get("lap_no"))
        if lap_no is None:
            return
        for existing in self.segments:
            existing_lap_no = self._coerce_int(existing.get("lap_no"))
            if existing_lap_no != lap_no:
                continue
            if self._segment_duration_sort_key(existing) >= self._segment_duration_sort_key(new_segment):
                self._mark_as_fragment(new_segment, lap_no=lap_no, clear_lap_no=True)
            else:
                self._mark_as_fragment(existing, lap_no=lap_no, clear_lap_no=True)

    @classmethod
    def _segment_duration_sort_key(cls, segment: dict[str, Any]) -> tuple[float, int]:
        """Implement segment duration sort key logic."""
        lap_time_s = cls._coerce_float(segment.get("lap_time_s"))
        if lap_time_s is None:
            start_ts = cls._coerce_float(segment.get("start_ts"))
            end_ts = cls._coerce_float(segment.get("end_ts"))
            if start_ts is not None and end_ts is not None and end_ts >= start_ts:
                lap_time_s = end_ts - start_ts
        sample_count = cls._coerce_int(segment.get("sample_count"))
        if sample_count is None:
            start_idx = cls._coerce_int(segment.get("start_idx"))
            end_idx = cls._coerce_int(segment.get("end_idx"))
            if start_idx is not None and end_idx is not None and end_idx >= start_idx:
                sample_count = int(end_idx - start_idx + 1)
        return (float(lap_time_s) if lap_time_s is not None else -1.0, int(sample_count) if sample_count is not None else -1)

    @staticmethod
    def _mark_as_fragment(segment: dict[str, Any], *, lap_no: int | None = None, clear_lap_no: bool = False) -> None:
        """Implement mark as fragment logic."""
        if lap_no is None:
            lap_no = LapSegmenter._coerce_int(segment.get("lap_no"))
        segment["lap_complete"] = False
        segment["is_complete"] = False
        segment["lap_incomplete"] = True
        segment["valid_lap"] = False
        segment["is_valid"] = False
        segment["fragment"] = True
        if lap_no is not None:
            segment["fragment_lap_no"] = int(lap_no)
        if clear_lap_no:
            segment.pop("lap_no", None)
            segment.pop("lap_num", None)
        reason = str(segment.get("reason") or "")
        if "fragment_duplicate_lap_no" not in reason:
            segment["reason"] = f"{reason}|fragment_duplicate_lap_no" if reason else "fragment_duplicate_lap_no"

    def _debug_log_surface_values_once(self) -> None:
        """Implement debug log surface values once logic."""
        if not self._debug_enabled or self._debug_surface_values_logged:
            return
        self._debug_surface_values_logged = True
        if not _LOG.isEnabledFor(logging.DEBUG):
            return
        values = sorted((int(value), int(count)) for value, count in self._observed_track_surface_values.items())
        _LOG.debug("coaching.lap_segmenter run=%s track_surface_values=%s", self.run_id, values)

    @staticmethod
    def _is_debug_enabled() -> bool:
        """Return whether debug enabled."""
        value = os.getenv("IWAS_COACHING_DEBUG_LAP_SEGMENTER", "")
        return value.strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _normalize_enum_text(value: Any) -> str:
        """Normalize enum text."""
        if value is None:
            return ""
        return "".join(ch.lower() for ch in str(value) if ch.isalnum())
