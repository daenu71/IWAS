from __future__ import annotations

import importlib
import logging
import threading
import time
from typing import Any, Sequence

from core.irsdk.channels import REQUESTED_CHANNELS


_LOG = logging.getLogger(__name__)

_DEFAULT_SAMPLE_FIELDS = tuple(REQUESTED_CHANNELS)


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

    def read_sample(self, fields: Sequence[str] | None = None) -> dict[str, Any] | None:
        with self._lock:
            ir = self._ir
        if ir is None:
            return None
        if not self._runtime_is_connected(ir):
            self.disconnect()
            return None

        try:
            raw: dict[str, Any] = {}
            for field in (tuple(fields) if fields is not None else _DEFAULT_SAMPLE_FIELDS):
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

    def describe_available_channels(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            ir = self._ir
        if ir is None:
            return {}
        try:
            return self._describe_available_channels_from_ir(ir)
        except Exception:
            return {}

    def get_session_info_yaml(self) -> str | None:
        with self._lock:
            ir = self._ir
        if ir is None:
            return None
        try:
            return self._get_session_info_yaml_from_ir(ir)
        except Exception:
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

    @classmethod
    def _describe_available_channels_from_ir(cls, ir: Any) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}

        for attr_name in ("var_headers", "varHeaders"):
            headers = cls._get_ir_attr_value(ir, attr_name)
            if headers is None:
                continue
            for header in cls._iter_ir_headers(headers):
                name = cls._header_field(header, "name", "Name", "var_name", "varName")
                if not name:
                    continue
                info: dict[str, Any] = {}
                var_type = cls._header_field(header, "type", "Type", "var_type", "varType")
                count = cls._header_field(header, "count", "Count")
                if var_type is not None:
                    info["type"] = str(var_type)
                if count is not None:
                    try:
                        info["count"] = int(count)
                    except Exception:
                        info["count"] = count
                result[str(name)] = info
            if result:
                return result

        for attr_name in ("var_headers_names", "varHeaderNames", "var_names", "varNames"):
            names = cls._get_ir_attr_value(ir, attr_name)
            if names is None:
                continue
            try:
                for name in names:
                    if name is None:
                        continue
                    result[str(name)] = {}
            except Exception:
                continue
            if result:
                return result

        # Fallback: probe only requested channels so unsupported header APIs do not break recording.
        for name in REQUESTED_CHANNELS:
            try:
                ir[name]
            except Exception:
                continue
            result[name] = {}
        return result

    @staticmethod
    def _get_ir_attr_value(ir: Any, attr_name: str) -> Any:
        if not hasattr(ir, attr_name):
            return None
        try:
            attr = getattr(ir, attr_name)
            return attr() if callable(attr) else attr
        except Exception:
            return None

    @staticmethod
    def _iter_ir_headers(headers: Any) -> list[Any]:
        try:
            return list(headers)
        except Exception:
            return []

    @staticmethod
    def _header_field(header: Any, *names: str) -> Any:
        for name in names:
            if isinstance(header, dict) and name in header:
                return header.get(name)
            if hasattr(header, name):
                try:
                    value = getattr(header, name)
                    return value() if callable(value) else value
                except Exception:
                    continue
        return None

    @classmethod
    def _get_session_info_yaml_from_ir(cls, ir: Any) -> str | None:
        for attr_name in (
            "session_info",
            "sessionInfo",
            "session_info_yaml",
            "sessionInfoYaml",
            "session_info_str",
            "sessionInfoStr",
            "_session_info",
            "_sessionInfo",
            "_IRSDK__session_info",
        ):
            value = cls._get_ir_attr_value(ir, attr_name)
            text = cls._coerce_text(value)
            if text and text.strip():
                return text

        try:
            for attr_name, attr_value in vars(ir).items():
                if "session" not in attr_name.lower() or "info" not in attr_name.lower():
                    continue
                text = cls._coerce_text(attr_value)
                if text and text.strip():
                    return text
        except Exception:
            pass
        return None

    @staticmethod
    def _coerce_text(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8", errors="replace")
            except Exception:
                return None
        if isinstance(value, str):
            return value
        return None

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
