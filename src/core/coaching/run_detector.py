"""Runtime module for core/coaching/run_detector.py."""

from __future__ import annotations

import logging
import re
from typing import Any


_LOG = logging.getLogger(__name__)


class RunDetector:
    """Container and behavior for Run Detector."""
    _INCOMPLETE_SPEED_THRESHOLD = 1.0
    STATE_IDLE = "IDLE"
    STATE_ARMED = "ARMED"
    STATE_ACTIVE = "ACTIVE"

    def __init__(self, session_type: str, *, incomplete_timeout_s: float = 25.0) -> None:
        """Implement init logic."""
        self.session_type = str(session_type or "unknown").strip().lower() or "unknown"
        self.state = self.STATE_IDLE
        self.incomplete_timeout_s = float(incomplete_timeout_s)

        self._prev_on_pit_road: bool | None = None
        self._prev_sample: dict[str, Any] | None = None
        self._unknown_logged = False
        self._missing_signal_logs: set[str] = set()

        self._active_since_ts: float | None = None
        self._last_lap_completed: int | None = None
        self._last_lap_change_ts: float | None = None
        self._teleport_candidate_ts: float | None = None

    def update(self, sample: dict[str, Any], now_ts: float) -> list[dict[str, Any]]:
        """Update."""
        sample_dict = sample if isinstance(sample, dict) else {}
        events: list[dict[str, Any]] = []

        self._apply_start_rules(sample_dict, now_ts, events)
        self._apply_end_rules(sample_dict, now_ts, events)

        self._prev_sample = sample_dict
        self._prev_on_pit_road = self._coerce_bool(self._read_value(sample_dict, "OnPitRoad"))
        return events

    def _apply_start_rules(self, sample: dict[str, Any], now_ts: float, events: list[dict[str, Any]]) -> None:
        """Apply start rules."""
        if self.state == self.STATE_ACTIVE:
            return

        if self.session_type in {"practice", "qualify"}:
            on_pit_road = self._coerce_bool(self._read_value(sample, "OnPitRoad"))
            if on_pit_road is None:
                self._log_missing_signal_once("OnPitRoad", "RunDetector start rule waiting for OnPitRoad")
                return
            if on_pit_road:
                self._arm()
                return
            if self._prev_on_pit_road is True and on_pit_road is False and self.state == self.STATE_ARMED:
                self._start(now_ts, "pit_exit", events, sample)
                return
            if self.state == self.STATE_ARMED and on_pit_road is False and self._prev_on_pit_road is None:
                # No transition seen yet (service started mid-lap); keep ARMED and wait for a clean pit cycle.
                return
            return

        if self.session_type == "race":
            flags = self._read_value(sample, "SessionFlags")
            state = self._read_value(sample, "SessionState")
            if flags is None:
                self._log_missing_signal_once("SessionFlags", "RunDetector race start waiting for SessionFlags")
                return
            if state is None:
                self._log_missing_signal_once("SessionState", "RunDetector race start waiting for SessionState")
                return
            if self._is_race_session_state_start_ok(state) and self._has_green_flag(flags):
                self._start(now_ts, "green_flag", events, sample)
            return

        if self.session_type == "unknown" and not self._unknown_logged:
            self._unknown_logged = True
            _LOG.info("RunDetector: session_type unknown; automatic run start disabled")

    def _apply_end_rules(self, sample: dict[str, Any], now_ts: float, events: list[dict[str, Any]]) -> None:
        """Apply end rules."""
        if self.state != self.STATE_ACTIVE:
            return

        lap_completed = self._coerce_int(self._read_value(sample, "LapCompleted"))
        if (
            lap_completed is not None
            and lap_completed == 0
            and self._last_lap_completed is not None
            and self._last_lap_completed > 2
        ):
            self._teleport_candidate_ts = now_ts

        if (
            self._teleport_candidate_ts is not None
            and self._coerce_bool(self._read_value(sample, "OnPitRoad")) is True
            and (now_ts - self._teleport_candidate_ts) < 3.0
        ):
            _LOG.info("RunDetector: Teleport-to-pit erkannt bei t=%.1f", now_ts)
            self._teleport_candidate_ts = None
            self._end(now_ts, "teleport_to_pit", events)
            return

        self._update_lap_progress(sample, now_ts)

        if self.session_type in {"race", "qualify"}:
            flags = self._read_value(sample, "SessionFlags")
            state = self._read_value(sample, "SessionState")
            if flags is not None and state is not None:
                if self._has_checkered_flag(flags) and self._is_race_session_state_end_ok(state):
                    self._end(now_ts, "checkered_flag", events)
                    return
            elif flags is None:
                self._log_missing_signal_once("SessionFlags:end", "RunDetector end rule waiting for SessionFlags")
            elif state is None:
                self._log_missing_signal_once("SessionState:end", "RunDetector end rule waiting for SessionState")
        if self._should_end_incomplete(sample, now_ts):
            self._end(now_ts, "incomplete_timeout", events)

    def _arm(self) -> None:
        """Implement arm logic."""
        if self.state == self.STATE_IDLE:
            self.state = self.STATE_ARMED

    def _start(self, now_ts: float, reason: str, events: list[dict[str, Any]], sample: dict[str, Any]) -> None:
        """Start."""
        if self.state == self.STATE_ACTIVE:
            return
        self.state = self.STATE_ACTIVE
        self._active_since_ts = now_ts
        lap_completed = self._coerce_int(self._read_value(sample, "LapCompleted"))
        self._last_lap_completed = lap_completed
        self._last_lap_change_ts = now_ts
        events.append(
            {
                "type": "RUN_START",
                "reason": reason,
                "timestamp": now_ts,
                "session_type": self.session_type,
            }
        )

    def _end(self, now_ts: float, reason: str, events: list[dict[str, Any]]) -> None:
        """Implement end logic."""
        if self.state != self.STATE_ACTIVE:
            return
        self.state = self.STATE_IDLE
        events.append(
            {
                "type": "RUN_END",
                "reason": reason,
                "timestamp": now_ts,
                "session_type": self.session_type,
            }
        )
        self._active_since_ts = None
        self._last_lap_completed = None
        self._last_lap_change_ts = None
        self._prev_on_pit_road = None
        self._teleport_candidate_ts = None

    @staticmethod
    def _read_value(sample: dict[str, Any], key: str) -> Any:
        """Read value."""
        if key in sample:
            return sample.get(key)
        raw = sample.get("raw")
        if isinstance(raw, dict):
            return raw.get(key)
        return None

    @staticmethod
    def _coerce_bool(value: Any) -> bool | None:
        """Coerce bool."""
        if isinstance(value, bool):
            return value
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            key = value.strip().lower()
            if key in {"true", "1", "yes", "y"}:
                return True
            if key in {"false", "0", "no", "n"}:
                return False
        return None

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

    def _log_missing_signal_once(self, key: str, message: str) -> None:
        """Implement log missing signal once logic."""
        if key in self._missing_signal_logs:
            return
        self._missing_signal_logs.add(key)
        _LOG.info(message)

    def _update_lap_progress(self, sample: dict[str, Any], now_ts: float) -> None:
        """Update lap progress."""
        lap_completed = self._coerce_int(self._read_value(sample, "LapCompleted"))
        if lap_completed is None:
            return
        if self._last_lap_completed is None:
            self._last_lap_completed = lap_completed
            self._last_lap_change_ts = now_ts
            return
        if lap_completed != self._last_lap_completed:
            self._last_lap_completed = lap_completed
            self._last_lap_change_ts = now_ts

    def _should_end_incomplete(self, sample: dict[str, Any], now_ts: float) -> bool:
        """Return whether end incomplete."""
        if self._last_lap_change_ts is None:
            if self._coerce_int(self._read_value(sample, "LapCompleted")) is None:
                self._log_missing_signal_once("LapCompleted", "RunDetector incomplete rule waiting for LapCompleted")
            return False
        if (now_ts - self._last_lap_change_ts) < self.incomplete_timeout_s:
            return False
        return self._incomplete_secondary_ok(sample)

    def _incomplete_secondary_ok(self, sample: dict[str, Any]) -> bool:
        """Implement incomplete secondary ok logic."""
        is_on_track_car = self._coerce_bool(self._read_value(sample, "IsOnTrackCar"))
        if is_on_track_car is False:
            return True
        speed = self._coerce_float(self._read_value(sample, "Speed"))
        if speed is not None and speed < self._INCOMPLETE_SPEED_THRESHOLD:
            return True
        if is_on_track_car is None and speed is None:
            self._log_missing_signal_once(
                "IncompleteSecondary",
                "RunDetector incomplete rule waiting for IsOnTrackCar/Speed secondary signals",
            )
        return False

    @classmethod
    def _has_green_flag(cls, flags_value: Any) -> bool:
        """Return whether green flag."""
        if isinstance(flags_value, int):
            return bool(flags_value & 0x00000004)
        if isinstance(flags_value, float):
            return bool(int(flags_value) & 0x00000004)
        if isinstance(flags_value, str):
            return "green" in flags_value.lower()
        if isinstance(flags_value, (list, tuple, set)):
            return any(cls._has_green_flag(item) for item in flags_value)
        return False

    @classmethod
    def _has_checkered_flag(cls, flags_value: Any) -> bool:
        """Return whether checkered flag."""
        if isinstance(flags_value, int):
            return bool(flags_value & 0x00000001)
        if isinstance(flags_value, float):
            return bool(int(flags_value) & 0x00000001)
        if isinstance(flags_value, str):
            return "checker" in flags_value.lower()
        if isinstance(flags_value, (list, tuple, set)):
            return any(cls._has_checkered_flag(item) for item in flags_value)
        return False

    @staticmethod
    def _is_race_session_state_start_ok(state_value: Any) -> bool:
        """Return whether race session state start ok."""
        if isinstance(state_value, str):
            key = re.sub(r"[^a-z0-9]+", " ", state_value.lower()).strip()
            if not key:
                return False
            if any(token in key for token in ("cool down", "checkered", "done", "finished", "end")):
                return False
            return any(token in key for token in ("race", "green", "start", "racing", "running"))
        state_int = RunDetector._coerce_int(state_value)
        if state_int is None:
            return False
        return state_int > 0

    @staticmethod
    def _is_race_session_state_end_ok(state_value: Any) -> bool:
        """Return whether race session state end ok."""
        if isinstance(state_value, str):
            key = re.sub(r"[^a-z0-9]+", " ", state_value.lower()).strip()
            if not key:
                return False
            return any(token in key for token in ("checkered", "cool down", "cooldown", "done", "finished", "end"))
        state_int = RunDetector._coerce_int(state_value)
        if state_int is None:
            return False
        return state_int > 0
