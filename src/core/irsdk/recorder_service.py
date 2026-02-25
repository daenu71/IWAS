from __future__ import annotations

from collections import deque
import logging
import threading
import time
from typing import Any

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
                    next_tick = time.monotonic()

                sample = self._client.read_sample()
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
