from __future__ import annotations

from collections import deque
import json
import logging
from pathlib import Path
import threading
import time
from typing import Any

from core.coaching.models import SessionMeta
from core.irsdk.channels import REQUESTED_CHANNELS
from core.irsdk.irsdk_client import IRSDKClient


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
                    next_tick = time.monotonic()

                if not self._channels_initialized:
                    self._initialize_channels()

                sample = self._client.read_sample(fields=self._recorded_channels)
                if sample is None:
                    # Connection may have dropped in read_sample().
                    self._sleep_interruptible(0.05)
                    continue

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
                except Exception:
                    _LOG.warning("irsdk session_meta.json exists but could not be parsed; overwriting")
            meta_path.write_text(json.dumps(meta.to_dict(base), indent=2), encoding="utf-8")
        except Exception as exc:
            _LOG.warning("irsdk session meta write failed (%s)", exc)

    def _resolve_session_meta_path(self) -> Path | None:
        try:
            from core.persistence import load_coaching_recording_settings

            settings = load_coaching_recording_settings()
            storage_dir = str(settings.get("coaching_storage_dir", "") or "").strip()
            if storage_dir:
                return Path(storage_dir) / "session_meta.json"
        except Exception as exc:
            _LOG.warning("irsdk session meta path via coaching settings unavailable (%s)", exc)
        return Path.cwd() / "session_meta.json"

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
