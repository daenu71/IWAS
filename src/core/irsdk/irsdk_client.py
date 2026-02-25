from __future__ import annotations

import importlib
import logging
import threading
import time
from typing import Any


_LOG = logging.getLogger(__name__)

_DEFAULT_SAMPLE_FIELDS = (
    "SessionTime",
    "Lap",
    "LapDistPct",
    "Speed",
    "RPM",
    "Gear",
    "Throttle",
    "Brake",
    "SteeringWheelAngle",
)


class IRSDKClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = "disconnected"
        self._irsdk_module: Any | None = None
        self._ir: Any | None = None
        self._last_error_key: str | None = None

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def is_connected(self) -> bool:
        with self._lock:
            ir = self._ir
            state = self._state
        return bool(ir is not None and state == "connected" and self._runtime_is_connected(ir))

    def connect(self) -> bool:
        with self._lock:
            existing = self._ir
        if existing is not None and self._runtime_is_connected(existing):
            with self._lock:
                self._state = "connected"
            return True

        try:
            module = self._irsdk_module
            if module is None:
                module = importlib.import_module("irsdk")
                self._irsdk_module = module

            irsdk_ctor = getattr(module, "IRSDK", None)
            if not callable(irsdk_ctor):
                raise RuntimeError("irsdk.IRSDK missing")

            ir = irsdk_ctor()
            startup = getattr(ir, "startup", None)
            startup_result = True
            if callable(startup):
                startup_result = bool(startup())

            if startup_result is False or not self._runtime_is_connected(ir):
                self._safe_shutdown(ir)
                with self._lock:
                    self._ir = None
                    self._state = "disconnected"
                return False

            with self._lock:
                self._ir = ir
                self._state = "connected"
            self._last_error_key = None
            _LOG.info("irsdk connect")
            return True
        except Exception as exc:
            self._log_connect_error_once(exc)
            with self._lock:
                self._ir = None
                self._state = "disconnected"
            return False

    def disconnect(self) -> None:
        with self._lock:
            ir = self._ir
            was_connected = self._state == "connected"
            self._ir = None
            self._state = "disconnected"
        if ir is not None:
            self._safe_shutdown(ir)
        if was_connected:
            _LOG.info("irsdk disconnect")

    def read_sample(self) -> dict[str, Any] | None:
        with self._lock:
            ir = self._ir
        if ir is None:
            return None
        if not self._runtime_is_connected(ir):
            self.disconnect()
            return None

        try:
            raw: dict[str, Any] = {}
            for field in _DEFAULT_SAMPLE_FIELDS:
                try:
                    value = ir[field]
                except Exception:
                    continue
                raw[field] = self._to_simple_value(value)
            return {
                "timestamp_monotonic": time.monotonic(),
                "timestamp_wall": time.time(),
                "raw": raw,
            }
        except Exception:
            self.disconnect()
            return None

    def _runtime_is_connected(self, ir: Any) -> bool:
        checks = (
            ("is_connected", True),
            ("isConnected", True),
            ("connected", True),
            ("is_initialized", True),
            ("isInitialized", True),
        )
        for attr_name, _truthy in checks:
            if not hasattr(ir, attr_name):
                continue
            try:
                attr_value = getattr(ir, attr_name)
                value = attr_value() if callable(attr_value) else attr_value
                return bool(value)
            except Exception:
                continue
        return True

    def _safe_shutdown(self, ir: Any) -> None:
        try:
            shutdown = getattr(ir, "shutdown", None)
            if callable(shutdown):
                shutdown()
        except Exception:
            pass

    def _log_connect_error_once(self, exc: Exception) -> None:
        key = f"{type(exc).__name__}:{exc}"
        if key == self._last_error_key:
            return
        self._last_error_key = key
        _LOG.info("irsdk connect failed (%s); staying disconnected", exc)

    @staticmethod
    def _to_simple_value(value: Any) -> Any:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        try:
            if hasattr(value, "item"):
                return value.item()
        except Exception:
            pass
        try:
            return float(value)
        except Exception:
            return str(value)
