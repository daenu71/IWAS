from __future__ import annotations

from typing import Any


_TRACK_SURFACE_OFFTRACK_ENUMS: set[int] = {0, 1}
_TRACK_SURFACE_OFFTRACK_NAMES: set[str] = {
    "offtrack",
    "notinworld",
    "off",
    "offworld",
}


class LapSegmenter:
    def __init__(
        self,
        *,
        use_ontrack_gate: bool = True,
        wrap_hi: float = 0.99,
        wrap_lo: float = 0.01,
    ) -> None:
        self.use_ontrack_gate = bool(use_ontrack_gate)
        self.wrap_hi = float(wrap_hi)
        self.wrap_lo = float(wrap_lo)
        self.reset()

    def reset(self, run_id: int | None = None) -> None:
        self.run_id = run_id
        self.last_lap: int | None = None
        self.last_lapdistpct: float | None = None
        self.current_lap_start_index: int | None = None
        self.current_lap_start_ts: float | None = None
        self.current_lap_no: int | None = None
        self.current_offtrack_surface = False
        self.current_incident_min: int | None = None
        self.current_incident_max: int | None = None
        self.segments: list[dict[str, Any]] = []

        self._counter_change_seen = False
        self._wrap_cooldown_active = False

    def update(
        self,
        sample: dict[str, Any],
        sample_index: int,
        now_ts: float | None = None,
    ) -> list[dict[str, Any]]:
        sample_dict = sample if isinstance(sample, dict) else {}
        events: list[dict[str, Any]] = []

        counter_key, counter_value = self._select_lap_counter(sample_dict)
        lapdistpct = self._coerce_float(self._read_value(sample_dict, "LapDistPct"))
        is_on_track = self._coerce_bool(self._read_value(sample_dict, "IsOnTrackCar"))
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

        self._accumulate_current_lap_sample(track_surface=track_surface, incident_count=incident_count)
        self.last_lapdistpct = lapdistpct
        return events

    def finalize(self, last_sample_index: int, now_ts: float | None = None) -> list[dict[str, Any]]:
        close_event = self._close_segment(
            end_sample_index=last_sample_index,
            end_ts=now_ts,
            reason="run_end",
        )
        if close_event is None:
            return []
        return [close_event]

    def _should_consider_wrap(self, counter_key: str | None, counter_value: int | None) -> bool:
        if counter_key is None or counter_value is None:
            return True
        return not self._counter_change_seen

    def _should_trigger_wrap(self, lapdistpct: float | None, is_on_track: bool | None) -> bool:
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
        if lapdistpct is None:
            return False
        if lapdistpct <= 0.1:
            return False
        return lapdistpct < self.wrap_hi

    def _derive_next_lap_no(self, counter_value: int | None) -> int | None:
        if counter_value is not None:
            if self.current_lap_no is not None and counter_value == self.current_lap_no:
                return self.current_lap_no + 1
            return counter_value
        if self.current_lap_no is None:
            return None
        return self.current_lap_no + 1

    def _start_segment(self, sample_index: int, now_ts: float | None, lap_no: int | None) -> None:
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
        start_index = self.current_lap_start_index
        if start_index is None:
            return None
        if end_sample_index < start_index:
            return None

        lap_complete = reason in {"counter_change", "distpct_wrap"}
        incident_delta = 0
        if self.current_incident_min is not None and self.current_incident_max is not None:
            incident_delta = max(0, int(self.current_incident_max) - int(self.current_incident_min))
        offtrack_surface = bool(self.current_offtrack_surface)
        valid_lap = bool(lap_complete and not offtrack_surface and incident_delta == 0)
        lap_index = len(self.segments)
        segment: dict[str, Any] = {
            "lap_index": int(lap_index),
            "start_idx": int(start_index),
            "end_idx": int(end_sample_index),
            "reason": str(reason),
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
        self.segments.append(segment)

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
        self.current_lap_start_index = None
        self.current_lap_start_ts = None
        self.current_lap_no = None
        self._reset_current_lap_meta()
        return event

    def _accumulate_current_lap_sample(self, *, track_surface: Any, incident_count: int | None) -> None:
        if self.current_lap_start_index is None:
            return
        if self._is_offtrack_track_surface(track_surface):
            self.current_offtrack_surface = True
        if incident_count is None:
            return
        if self.current_incident_min is None or incident_count < self.current_incident_min:
            self.current_incident_min = int(incident_count)
        if self.current_incident_max is None or incident_count > self.current_incident_max:
            self.current_incident_max = int(incident_count)

    def _reset_current_lap_meta(self) -> None:
        self.current_offtrack_surface = False
        self.current_incident_min = None
        self.current_incident_max = None

    @staticmethod
    def _read_value(sample: dict[str, Any], key: str) -> Any:
        raw = sample.get("raw")
        if isinstance(raw, dict) and key in raw:
            return raw.get(key)
        return sample.get(key)

    @staticmethod
    def _select_lap_counter(sample: dict[str, Any]) -> tuple[str | None, int | None]:
        lap = LapSegmenter._coerce_int(LapSegmenter._read_value(sample, "Lap"))
        if lap is not None:
            return ("Lap", lap)
        lap_completed = LapSegmenter._coerce_int(LapSegmenter._read_value(sample, "LapCompleted"))
        if lap_completed is not None:
            return ("LapCompleted", lap_completed)
        return (None, None)

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        try:
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _coerce_float(value: Any) -> float | None:
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _coerce_bool(value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        if value is None:
            return None
        try:
            return bool(value)
        except Exception:
            return None

    @classmethod
    def _is_offtrack_track_surface(cls, value: Any) -> bool:
        normalized = cls._normalize_enum_text(value)
        if normalized and normalized in _TRACK_SURFACE_OFFTRACK_NAMES:
            return True
        enum_value = cls._coerce_int(value)
        if enum_value is None:
            return False
        return enum_value in _TRACK_SURFACE_OFFTRACK_ENUMS

    @staticmethod
    def _normalize_enum_text(value: Any) -> str:
        if value is None:
            return ""
        return "".join(ch.lower() for ch in str(value) if ch.isalnum())
