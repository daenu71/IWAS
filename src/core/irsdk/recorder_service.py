from __future__ import annotations

from collections import deque
import json
import logging
from pathlib import Path
import threading
import time
from typing import Any

from core.coaching.models import SessionMeta
from core.coaching.run_detector import RunDetector
from core.irsdk.channels import REQUESTED_CHANNELS
from core.irsdk.irsdk_client import IRSDKClient
from core.irsdk.sessioninfo_parser import extract_session_meta


_LOG = logging.getLogger(__name__)


class RecorderService:
    def __init__(self, client: IRSDKClient | None = None) -> None:
        self._client = client or IRSDKClient()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._sample_hz = 120
        self._buffer: deque[dict[str, Any]] = deque(maxlen=self._buffer_capacity_for_hz(120))
        self._sample_count = 0
        self._recorded_channels: tuple[str, ...] = tuple(REQUESTED_CHANNELS)
        self._missing_channels: tuple[str, ...] = ()
        self._channels_initialized = False
        self._dtype_decisions: dict[str, str] = {}
        self._session_meta_written = False
        self._session_dir: Path | None = None
        self._session_start_wall_ts: float | None = None
        self._session_info_yaml_saved = False
        self._session_info_wait_logged = False
        self._session_info_artifacts_logged = False
        self._session_type: str | None = None
        self._run_detector: RunDetector | None = None
        self._run_id_seq = 0
        self._active_run_id: int | None = None
        self._run_events: deque[dict[str, Any]] = deque(maxlen=200)

    @property
    def running(self) -> bool:
        with self._lock:
            thread = self._thread
        return bool(thread is not None and thread.is_alive())

    @property
    def connected(self) -> bool:
        return self._client.is_connected

    @property
    def sample_count(self) -> int:
        with self._lock:
            return self._sample_count

    @property
    def sample_hz(self) -> int:
        with self._lock:
            return self._sample_hz

    def start(self, sample_hz: int) -> None:
        hz = self._normalize_hz(sample_hz)
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._sample_hz = hz
            self._sample_count = 0
            self._buffer = deque(maxlen=self._buffer_capacity_for_hz(hz))
            self._recorded_channels = tuple(REQUESTED_CHANNELS)
            self._missing_channels = ()
            self._channels_initialized = False
            self._dtype_decisions = {}
            self._session_meta_written = False
            self._session_dir = None
            self._session_start_wall_ts = time.time()
            self._session_info_yaml_saved = False
            self._session_info_wait_logged = False
            self._session_info_artifacts_logged = False
            self._session_type = None
            self._run_detector = None
            self._run_id_seq = 0
            self._active_run_id = None
            self._run_events = deque(maxlen=200)
            self._stop_event.clear()
            thread = threading.Thread(
                target=self._run_loop,
                name="irsdk-recorder",
                daemon=True,
            )
            self._thread = thread
        thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
        if thread is None:
            self._client.disconnect()
            return

        self._stop_event.set()
        thread.join(timeout=2.0)
        self._client.disconnect()

        with self._lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None

    def get_buffer_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._buffer)

    def _run_loop(self) -> None:
        with self._lock:
            sample_hz = self._sample_hz
            log_every_samples = max(120, sample_hz if sample_hz > 0 else 120)
        interval = (1.0 / sample_hz) if sample_hz > 0 else 0.0
        next_tick = time.monotonic()
        last_sample_log_at = 0

        try:
            while not self._stop_event.is_set():
                if not self._client.is_connected:
                    _LOG.info("irsdk reconnect attempt")
                    self._client.connect()
                    if not self._client.is_connected:
                        self._sleep_interruptible(1.0)
                        continue
                    self._initialize_channels()
                    self._try_write_session_info_yaml()
                    next_tick = time.monotonic()

                if not self._channels_initialized:
                    self._initialize_channels()
                if not self._session_info_yaml_saved:
                    self._try_write_session_info_yaml()

                sample = self._client.read_sample(fields=self._recorded_channels)
                if sample is None:
                    # Connection may have dropped in read_sample().
                    self._sleep_interruptible(0.05)
                    continue
                self._process_run_detector(sample)

                with self._lock:
                    self._buffer.append(sample)
                    self._sample_count += 1
                    count = self._sample_count

                if count == 1 or (count - last_sample_log_at) >= log_every_samples:
                    last_sample_log_at = count
                    _LOG.info("irsdk sample_count=%d", count)

                if interval > 0.0:
                    next_tick += interval
                    now = time.monotonic()
                    delay = next_tick - now
                    if delay > 0.0:
                        self._sleep_interruptible(delay)
                    else:
                        if delay < -(interval * 4.0):
                            next_tick = now
                        self._sleep_interruptible(0.0)
                else:
                    # Unthrottled mode still yields briefly so the UI thread stays responsive.
                    self._sleep_interruptible(0.001)
        finally:
            self._client.disconnect()
            with self._lock:
                if self._thread is not None and self._thread is threading.current_thread():
                    self._thread = None

    def _sleep_interruptible(self, seconds: float) -> None:
        if seconds <= 0.0:
            self._stop_event.wait(0.0)
            return
        self._stop_event.wait(seconds)

    def _initialize_channels(self) -> None:
        if self._channels_initialized:
            return

        channel_info = self._client.describe_available_channels()
        available_channels = set(channel_info.keys())
        if available_channels:
            recorded_channels = [name for name in REQUESTED_CHANNELS if name in available_channels]
            missing_channels = [name for name in REQUESTED_CHANNELS if name not in available_channels]
        else:
            # Preserve current behavior if header discovery is unavailable.
            recorded_channels = list(REQUESTED_CHANNELS)
            missing_channels = []
            _LOG.warning("irsdk channel discovery unavailable; falling back to requested channel list")

        with self._lock:
            self._recorded_channels = tuple(recorded_channels)
            self._missing_channels = tuple(missing_channels)
            self._dtype_decisions = self._build_dtype_decisions(recorded_channels, channel_info)
            self._channels_initialized = True

        if missing_channels:
            _LOG.warning("irsdk missing channels (skipping): %s", ", ".join(missing_channels))
        self._write_session_meta()

    @classmethod
    def _build_dtype_decisions(
        cls,
        recorded_channels: list[str],
        channel_info: dict[str, dict[str, Any]],
    ) -> dict[str, str]:
        decisions: dict[str, str] = {}
        for name in recorded_channels:
            info = channel_info.get(name) or {}
            decision = cls._format_dtype_decision(info)
            if decision:
                decisions[name] = decision
        return decisions

    @staticmethod
    def _format_dtype_decision(info: dict[str, Any]) -> str | None:
        var_type = info.get("type")
        count = info.get("count")
        if var_type is None and count is None:
            return None

        dtype_name = None
        if var_type is not None:
            raw = str(var_type).strip()
            key = raw.lower()
            dtype_name = {
                "irsdk_float": "float32",
                "float": "float32",
                "irsdk_double": "float64",
                "double": "float64",
                "irsdk_int": "int32",
                "int": "int32",
                "irsdk_bool": "bool",
                "bool": "bool",
                "irsdk_char": "char",
                "char": "char",
                "irsdk_bitfield": "uint32",
                "bitfield": "uint32",
            }.get(key, raw)

        if count is None:
            return dtype_name
        try:
            count_int = int(count)
        except Exception:
            count_int = None
        if count_int is None:
            return f"{dtype_name}[{count}]" if dtype_name else f"[{count}]"
        if count_int <= 1:
            return dtype_name or f"[{count_int}]"
        return f"{dtype_name}[{count_int}]" if dtype_name else f"[{count_int}]"

    def _write_session_meta(self) -> None:
        with self._lock:
            if self._session_meta_written or not self._channels_initialized:
                return
            sample_hz = self._sample_hz
            recorded_channels = list(self._recorded_channels)
            missing_channels = list(self._missing_channels)
            dtype_decisions = dict(self._dtype_decisions)
            self._session_meta_written = True

        meta = SessionMeta(
            recorded_channels=recorded_channels,
            missing_channels=missing_channels,
            sample_hz=sample_hz,
            dtype_decisions=dtype_decisions,
        )
        meta_path = self._resolve_session_meta_path()
        if meta_path is None:
            return

        try:
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            base: dict[str, Any] = {}
            if meta_path.exists():
                try:
                    existing = json.loads(meta_path.read_text(encoding="utf-8"))
                    if isinstance(existing, dict):
                        base = existing
                        self._ensure_run_detector_from_session_type(base.get("SessionType"))
                except Exception:
                    _LOG.warning("irsdk session_meta.json exists but could not be parsed; overwriting")
            meta_path.write_text(json.dumps(meta.to_dict(base), indent=2), encoding="utf-8")
        except Exception as exc:
            _LOG.warning("irsdk session meta write failed (%s)", exc)

    def _resolve_session_meta_path(self) -> Path | None:
        session_dir = self._ensure_session_dir()
        if session_dir is not None:
            return session_dir / "session_meta.json"
        return Path.cwd() / "session_meta.json"

    def _try_write_session_info_yaml(self) -> None:
        if self._session_info_yaml_saved:
            return

        getter = getattr(self._client, "get_session_info_yaml", None)
        if not callable(getter):
            if not self._session_info_wait_logged:
                self._session_info_wait_logged = True
                _LOG.info("irsdk SessionInfo raw YAML not available (client has no getter)")
            return

        try:
            raw_yaml = getter()
        except Exception as exc:
            if not self._session_info_wait_logged:
                self._session_info_wait_logged = True
                _LOG.warning("irsdk SessionInfo read failed (%s)", exc)
            return

        if not raw_yaml:
            if not self._session_info_wait_logged:
                self._session_info_wait_logged = True
                _LOG.info("irsdk SessionInfo not yet available; will retry")
            return

        yaml_path = self._resolve_session_info_yaml_path()
        if yaml_path is None:
            return
        try:
            yaml_path.parent.mkdir(parents=True, exist_ok=True)
            yaml_text = str(raw_yaml)
            yaml_path.write_text(yaml_text, encoding="utf-8")
            saved_ts = time.time()
            self._session_info_yaml_saved = True
            meta_merged = self._merge_session_info_meta_from_yaml(yaml_text, session_info_saved_ts=saved_ts)
            if meta_merged and not self._session_info_artifacts_logged:
                self._session_info_artifacts_logged = True
                _LOG.info("irsdk session_info.yaml + session_meta.json saved (%s)", yaml_path.parent)
        except Exception as exc:
            _LOG.warning("irsdk session_info.yaml write failed (%s)", exc)

    def _resolve_session_info_yaml_path(self) -> Path | None:
        session_dir = self._ensure_session_dir()
        if session_dir is None:
            return None
        return session_dir / "session_info.yaml"

    def _ensure_session_dir(self) -> Path | None:
        with self._lock:
            if self._session_dir is not None:
                return self._session_dir
            start_wall_ts = self._session_start_wall_ts or time.time()
        base_dir = self._resolve_session_root_dir()
        if base_dir is None:
            return None

        timestamp_part = time.strftime("%Y%m%d-%H%M%S", time.localtime(start_wall_ts))
        millis_part = int((start_wall_ts - int(start_wall_ts)) * 1000)
        session_dir = base_dir / f"session-{timestamp_part}-{millis_part:03d}"
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            _LOG.warning("irsdk session dir create failed (%s)", exc)
            return None

        with self._lock:
            if self._session_dir is None:
                self._session_dir = session_dir
            return self._session_dir

    def _resolve_session_root_dir(self) -> Path | None:
        try:
            from core.persistence import load_coaching_recording_settings

            settings = load_coaching_recording_settings()
            storage_dir = str(settings.get("coaching_storage_dir", "") or "").strip()
            if storage_dir:
                return Path(storage_dir)
        except Exception:
            pass
        return Path.cwd()

    def _merge_session_info_meta_from_yaml(self, yaml_text: str, *, session_info_saved_ts: float) -> bool:
        try:
            with self._lock:
                recorder_start_ts = self._session_start_wall_ts
            extracted = extract_session_meta(
                yaml_text,
                recorder_start_ts=recorder_start_ts,
                session_info_saved_ts=session_info_saved_ts,
            )
            if not extracted:
                extracted = {
                    "recorder_start_ts": recorder_start_ts,
                    "session_info_saved_ts": session_info_saved_ts,
                }
            self._ensure_run_detector_from_session_type(extracted.get("SessionType"))
            self._merge_session_meta_fields(extracted)
            return True
        except Exception as exc:
            _LOG.warning("irsdk SessionInfo meta extract/merge failed (%s)", exc)
            return False

    def _merge_session_meta_fields(self, fields: dict[str, Any]) -> None:
        meta_path = self._resolve_session_meta_path()
        if meta_path is None:
            return
        try:
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            base: dict[str, Any] = {}
            if meta_path.exists():
                try:
                    existing = json.loads(meta_path.read_text(encoding="utf-8"))
                    if isinstance(existing, dict):
                        base = existing
                except Exception:
                    _LOG.warning("irsdk session_meta.json exists but could not be parsed; overwriting")
            base.update(fields)
            meta_path.write_text(json.dumps(base, indent=2), encoding="utf-8")
        except Exception as exc:
            _LOG.warning("irsdk session meta merge failed (%s)", exc)

    def _ensure_run_detector_from_session_type(self, session_type: Any) -> None:
        if session_type is None:
            return
        normalized = str(session_type).strip().lower()
        if not normalized:
            return
        with self._lock:
            current = self._session_type
            detector = self._run_detector
            if detector is not None:
                if current != normalized:
                    _LOG.info(
                        "irsdk run detector session_type already set (%s), ignoring later value (%s)",
                        current,
                        normalized,
                    )
                return
            self._session_type = normalized
            self._run_detector = RunDetector(normalized)
        _LOG.info("irsdk run detector ready (session_type=%s)", normalized)

    def _process_run_detector(self, sample: dict[str, Any]) -> None:
        with self._lock:
            detector = self._run_detector
            session_type = self._session_type or "unknown"
        if detector is None:
            return

        try:
            now_ts = float(sample.get("timestamp_monotonic", time.monotonic()))
        except Exception:
            now_ts = time.monotonic()
        try:
            events = detector.update(sample, now_ts)
        except Exception as exc:
            _LOG.warning("irsdk run detector update failed (%s)", exc)
            return
        if not events:
            return

        for event in events:
            self._handle_run_event(event, session_type=session_type)

    def _handle_run_event(self, event: dict[str, Any], *, session_type: str) -> None:
        event_type = str(event.get("type") or "")
        reason = str(event.get("reason") or "unknown")
        ts = event.get("timestamp")

        with self._lock:
            active_run_id = self._active_run_id
            if event_type == "RUN_START":
                if active_run_id is not None:
                    return
                self._run_id_seq += 1
                active_run_id = self._run_id_seq
                self._active_run_id = active_run_id
            elif event_type == "RUN_END":
                if active_run_id is None:
                    return
                self._active_run_id = None
            else:
                return

            stored_event = dict(event)
            stored_event["run_id"] = active_run_id
            self._run_events.append(stored_event)

        _LOG.info(
            "irsdk %s run_id=%s reason=%s session_type=%s ts=%s",
            event_type.lower(),
            active_run_id,
            reason,
            session_type,
            ts,
        )

    @staticmethod
    def _buffer_capacity_for_hz(sample_hz: int) -> int:
        if sample_hz <= 0:
            return 240
        return max(1, int(sample_hz) * 2)

    @staticmethod
    def _normalize_hz(sample_hz: int) -> int:
        try:
            hz = int(sample_hz)
        except Exception:
            return 120
        if hz < 0:
            return 0
        return hz
