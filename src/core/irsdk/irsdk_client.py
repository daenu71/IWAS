from __future__ import annotations

import importlib
import logging
import re
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
        self._last_session_info_source: str | None = None
        self._last_session_info_len: int | None = None
        self._resolved_field_reads: dict[str, tuple[str, int | None]] = {}

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
            self._resolved_field_reads = {}
        if ir is not None:
            self._safe_shutdown(ir)
        if was_connected:
            _LOG.info("irsdk disconnect")

    def read_sample(self, fields: Sequence[str] | None = None) -> dict[str, Any] | None:
        with self._lock:
            ir = self._ir
            resolved_field_reads = dict(self._resolved_field_reads)
        if ir is None:
            return None
        if not self._runtime_is_connected(ir):
            self.disconnect()
            return None

        try:
            raw: dict[str, Any] = {}
            field_list = tuple(fields) if fields is not None else _DEFAULT_SAMPLE_FIELDS
            source_cache: dict[str, Any] = {}
            missing_sources: set[str] = set()
            for field in field_list:
                source_name = field
                source_index: int | None = None
                resolved = resolved_field_reads.get(field)
                if resolved is not None:
                    source_name, source_index = resolved
                if source_name in missing_sources:
                    continue
                try:
                    if source_name in source_cache:
                        value = source_cache[source_name]
                    else:
                        value = ir[source_name]
                        source_cache[source_name] = value
                except Exception:
                    missing_sources.add(source_name)
                    continue
                if source_index is not None:
                    try:
                        value = self._extract_indexed_value(value, source_index)
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

    def list_available_vars(self) -> list[dict[str, Any]]:
        channel_info = self.describe_available_channels()
        items: list[dict[str, Any]] = []
        for name, info in channel_info.items():
            item = {"name": str(name)}
            if isinstance(info, dict):
                if "type" in info:
                    item["type"] = info.get("type")
                if "count" in info:
                    item["count"] = info.get("count")
            items.append(item)
        return items

    def resolve_requested_channels(self, request_specs: Sequence[str]) -> dict[str, Any]:
        channel_info = self.describe_available_channels()
        resolved = self._resolve_requested_channels_from_available(request_specs, channel_info)
        with self._lock:
            self._resolved_field_reads = dict(resolved.get("read_field_map") or {})
        resolved.pop("read_field_map", None)
        return resolved

    def get_session_info_yaml(self) -> str | None:
        with self._lock:
            ir = self._ir
        if ir is None:
            return None
        try:
            text, source = self._get_session_info_yaml_with_source_from_ir(ir)
            with self._lock:
                self._last_session_info_source = source
                self._last_session_info_len = len(text) if isinstance(text, str) else None
            if text and source:
                _LOG.info("irsdk SessionInfo raw YAML source=%s", source)
            return text
        except Exception:
            return None

    def get_last_session_info_source(self) -> str | None:
        with self._lock:
            return self._last_session_info_source

    def get_debug_snapshot(self) -> dict[str, Any]:
        with self._lock:
            ir = self._ir
            state = self._state
        snapshot: dict[str, Any] = {
            "state": state,
            "has_ir_object": ir is not None,
        }
        with self._lock:
            snapshot["last_session_info_source"] = self._last_session_info_source
            snapshot["last_session_info_len"] = self._last_session_info_len
        if ir is None:
            return snapshot

        snapshot["ir_class"] = f"{type(ir).__module__}.{type(ir).__name__}"
        try:
            snapshot["runtime_connected"] = bool(self._runtime_is_connected(ir))
        except Exception:
            snapshot["runtime_connected"] = None

        known_attrs: list[dict[str, Any]] = []
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
            "session_info_update",
            "var_headers_names",
            "_header",
            "_shared_mem",
        ):
            item: dict[str, Any] = {"name": attr_name, "present": hasattr(ir, attr_name)}
            if item["present"]:
                try:
                    raw = getattr(ir, attr_name)
                    value = raw() if callable(raw) else raw
                    item.update(self._summarize_debug_value(value))
                except Exception as exc:
                    item["error"] = f"{type(exc).__name__}: {exc}"
            known_attrs.append(item)
        snapshot["known_attrs"] = known_attrs

        header_info: dict[str, Any] = {}
        header = self._get_ir_attr_value(ir, "_header")
        if header is not None:
            for name in ("session_info_update", "session_info_offset", "session_info_len", "status", "version"):
                try:
                    value = getattr(header, name)
                    header_info[name] = value() if callable(value) else value
                except Exception:
                    continue
        if header_info:
            snapshot["header"] = header_info

        try:
            matches: list[dict[str, Any]] = []
            for attr_name, attr_value in vars(ir).items():
                key = attr_name.lower()
                if "session" not in key and "info" not in key:
                    continue
                item = {"name": attr_name}
                item.update(self._summarize_debug_value(attr_value))
                matches.append(item)
            snapshot["vars_session_related"] = sorted(matches, key=lambda x: str(x.get("name", "")))[:40]
        except Exception as exc:
            snapshot["vars_session_related_error"] = f"{type(exc).__name__}: {exc}"
        return snapshot

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

    @classmethod
    def _resolve_requested_channels_from_available(
        cls,
        request_specs: Sequence[str],
        available_channels: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "recorded_channels": [],
            "missing_channels": [],
            "channel_info": {},
            "read_field_map": {},
        }
        available = dict(available_channels or {})
        discovery_available = bool(available)

        for raw_spec in request_specs:
            spec = str(raw_spec)
            if spec == "ShockDefl[4]":
                cls._resolve_wheel_group_request_spec(
                    result,
                    spec=spec,
                    output_prefix="ShockDefl",
                    available=available,
                    aliases=("ShockDefl",),
                    expected_wheels=("LF", "RF", "LR", "RR"),
                    discovery_available=discovery_available,
                )
                continue
            if spec == "RideHeight[4]":
                cls._resolve_wheel_group_request_spec(
                    result,
                    spec=spec,
                    output_prefix="RideHeight",
                    available=available,
                    aliases=("RideHeight",),
                    expected_wheels=("LF", "RF", "LR", "RR"),
                    discovery_available=discovery_available,
                )
                continue
            if spec == "TirePressure[4]":
                cls._resolve_wheel_group_request_spec(
                    result,
                    spec=spec,
                    output_prefix="TirePressure",
                    available=available,
                    aliases=("TirePressure", "Pressure"),
                    expected_wheels=("LF", "RF", "LR", "RR"),
                    discovery_available=discovery_available,
                )
                continue
            if spec == "TireTemp[4][L/M/R]":
                cls._resolve_tire_temp_group_request_spec(
                    result,
                    spec=spec,
                    available=available,
                    discovery_available=discovery_available,
                )
                continue

            info = available.get(spec)
            if info is None:
                result["missing_channels"].append(
                    cls._build_missing_spec_entry(
                        spec,
                        "not_found" if discovery_available else "channel_discovery_unavailable",
                    )
                )
                continue
            cls._register_concrete_channel(
                result,
                column_name=spec,
                source_name=spec,
                source_index=None,
                source_info=info,
            )

        return result

    @classmethod
    def _resolve_wheel_group_request_spec(
        cls,
        result: dict[str, Any],
        *,
        spec: str,
        output_prefix: str,
        available: dict[str, dict[str, Any]],
        aliases: Sequence[str],
        expected_wheels: Sequence[str],
        discovery_available: bool,
    ) -> None:
        exact_expanded = cls._try_expand_exact_array_header(
            result,
            spec=spec,
            available=available,
            base_names=(output_prefix,),
            output_prefix=output_prefix,
            expected_count=len(tuple(expected_wheels)),
        )
        if exact_expanded:
            return

        winners = cls._select_best_wheel_scalar_sources(available, aliases=aliases)
        missing_wheels: list[str] = []
        for wheel in expected_wheels:
            picked = winners.get(wheel)
            if picked is None:
                missing_wheels.append(str(wheel))
                continue
            source_name, source_info = picked
            cls._register_concrete_channel(
                result,
                column_name=f"{output_prefix}{wheel}",
                source_name=source_name,
                source_index=None,
                source_info=source_info,
            )
        if missing_wheels:
            entry = cls._build_missing_spec_entry(
                spec,
                "partial_match" if winners else ("not_found" if discovery_available else "channel_discovery_unavailable"),
                missing_wheels=missing_wheels,
            )
            if winners:
                entry["matched_wheels"] = [wheel for wheel in expected_wheels if wheel not in missing_wheels]
            result["missing_channels"].append(entry)

    @classmethod
    def _resolve_tire_temp_group_request_spec(
        cls,
        result: dict[str, Any],
        *,
        spec: str,
        available: dict[str, dict[str, Any]],
        discovery_available: bool,
    ) -> None:
        exact_expanded = cls._try_expand_exact_array_header(
            result,
            spec=spec,
            available=available,
            base_names=("TireTemp", "TireTemps"),
            output_prefix="TireTemp",
            expected_count=12,
        )
        if exact_expanded:
            return

        wheel_order = ("LF", "RF", "LR", "RR")
        segment_order = ("L", "M", "R")
        winners = cls._select_best_tire_temp_scalar_sources(available)
        missing_pairs: list[str] = []
        for wheel in wheel_order:
            for segment in segment_order:
                picked = winners.get((wheel, segment))
                if picked is None:
                    missing_pairs.append(f"{wheel}:{segment}")
                    continue
                source_name, source_info = picked
                cls._register_concrete_channel(
                    result,
                    column_name=f"TireTemp{wheel}_{segment}",
                    source_name=source_name,
                    source_index=None,
                    source_info=source_info,
                )
        if missing_pairs:
            entry = cls._build_missing_spec_entry(
                spec,
                "partial_match" if winners else ("not_found" if discovery_available else "channel_discovery_unavailable"),
                missing_components=missing_pairs,
            )
            if winners:
                entry["matched_components"] = len(winners)
            result["missing_channels"].append(entry)

    @classmethod
    def _try_expand_exact_array_header(
        cls,
        result: dict[str, Any],
        *,
        spec: str,
        available: dict[str, dict[str, Any]],
        base_names: Sequence[str],
        output_prefix: str,
        expected_count: int,
    ) -> bool:
        for base_name in base_names:
            info = available.get(base_name)
            if info is None:
                continue
            count = cls._coerce_int((info or {}).get("count"))
            if count is not None and count < expected_count:
                result["missing_channels"].append(
                    cls._build_missing_spec_entry(
                        spec,
                        "array_too_short",
                        source_name=base_name,
                        expected_count=expected_count,
                        actual_count=count,
                    )
                )
                return True

            for index in range(expected_count):
                cls._register_concrete_channel(
                    result,
                    column_name=f"{output_prefix}_{index}",
                    source_name=base_name,
                    source_index=index,
                    source_info=info,
                )
            return True
        return False

    @classmethod
    def _select_best_wheel_scalar_sources(
        cls,
        available: dict[str, dict[str, Any]],
        *,
        aliases: Sequence[str],
    ) -> dict[str, tuple[str, dict[str, Any]]]:
        alias_keys = [cls._normalize_var_key(alias) for alias in aliases if str(alias).strip()]
        best: dict[str, tuple[int, str, dict[str, Any]]] = {}
        for name, info in available.items():
            wheel, remainder = cls._wheel_parts_from_var_name(name)
            if wheel is None:
                continue
            full_key = cls._normalize_var_key(name)
            score = -1
            for alias in alias_keys:
                if not alias:
                    continue
                if remainder == alias:
                    score = max(score, 300)
                elif alias in remainder:
                    score = max(score, 240)
                elif alias in full_key:
                    score = max(score, 180)
            if score < 0:
                continue
            count = cls._coerce_int((info or {}).get("count"))
            if count is None or count == 1:
                score += 10
            else:
                score -= 40
            score -= len(remainder)
            current = best.get(wheel)
            if current is None or score > current[0]:
                best[wheel] = (score, str(name), dict(info or {}))
        return {wheel: (name, info) for wheel, (_score, name, info) in best.items()}

    @classmethod
    def _select_best_tire_temp_scalar_sources(
        cls,
        available: dict[str, dict[str, Any]],
    ) -> dict[tuple[str, str], tuple[str, dict[str, Any]]]:
        best: dict[tuple[str, str], tuple[int, str, dict[str, Any]]] = {}
        for name, info in available.items():
            wheel, remainder = cls._wheel_parts_from_var_name(name)
            if wheel is None:
                continue
            full_key = cls._normalize_var_key(name)
            if "temp" not in remainder and "temp" not in full_key:
                continue
            segment, seg_score = cls._tire_temp_segment_from_remainder(remainder)
            if segment is None:
                continue
            score = 150 + int(seg_score)
            if "temp" in remainder:
                score += 40
            count = cls._coerce_int((info or {}).get("count"))
            if count is None or count == 1:
                score += 10
            else:
                score -= 40
            score -= len(remainder)
            key = (wheel, segment)
            current = best.get(key)
            if current is None or score > current[0]:
                best[key] = (score, str(name), dict(info or {}))
        return {key: (name, info) for key, (_score, name, info) in best.items()}

    @classmethod
    def _register_concrete_channel(
        cls,
        result: dict[str, Any],
        *,
        column_name: str,
        source_name: str,
        source_index: int | None,
        source_info: dict[str, Any] | None,
    ) -> None:
        concrete_name = str(column_name)
        if concrete_name in result["channel_info"]:
            return
        result["recorded_channels"].append(concrete_name)
        result["read_field_map"][concrete_name] = (str(source_name), source_index)
        info = cls._copy_concrete_channel_info(source_info or {}, source_name=source_name, source_index=source_index)
        result["channel_info"][concrete_name] = info

    @classmethod
    def _copy_concrete_channel_info(
        cls,
        source_info: dict[str, Any],
        *,
        source_name: str,
        source_index: int | None,
    ) -> dict[str, Any]:
        info: dict[str, Any] = {}
        if "type" in source_info:
            info["type"] = source_info.get("type")
        count = source_info.get("count")
        if source_index is None:
            if count is not None:
                info["count"] = count
        else:
            info["count"] = 1
            info["source_index"] = int(source_index)
        info["source_name"] = str(source_name)
        return info

    @staticmethod
    def _build_missing_spec_entry(spec: str, reason: str, **extra: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {"request_spec": str(spec), "reason": str(reason)}
        for key, value in extra.items():
            entry[key] = value
        return entry

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        try:
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _normalize_var_key(value: Any) -> str:
        return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())

    @classmethod
    def _wheel_parts_from_var_name(cls, name: Any) -> tuple[str | None, str]:
        norm = cls._normalize_var_key(name)
        if not norm:
            return None, ""
        wheel_tokens = (
            ("lf", "LF"),
            ("rf", "RF"),
            ("lr", "LR"),
            ("rr", "RR"),
            ("leftfront", "LF"),
            ("rightfront", "RF"),
            ("leftrear", "LR"),
            ("rightrear", "RR"),
        )
        for token, wheel in wheel_tokens:
            if norm.startswith(token):
                return wheel, norm[len(token) :]
        for token, wheel in wheel_tokens:
            if norm.endswith(token):
                return wheel, norm[: -len(token)]
        return None, norm

    @classmethod
    def _tire_temp_segment_from_remainder(cls, remainder: str) -> tuple[str | None, int]:
        key = cls._normalize_var_key(remainder)
        if "temp" not in key:
            return None, 0
        suffix_map: tuple[tuple[str, str, int], ...] = (
            ("tempcl", "L", 140),
            ("tempcm", "M", 140),
            ("tempcr", "R", 140),
            ("templ", "L", 120),
            ("tempm", "M", 120),
            ("tempr", "R", 120),
            ("inner", "L", 90),
            ("middle", "M", 90),
            ("mid", "M", 85),
            ("center", "M", 90),
            ("centre", "M", 90),
            ("outer", "R", 90),
            ("left", "L", 85),
            ("right", "R", 85),
            ("cl", "L", 70),
            ("cm", "M", 70),
            ("cr", "R", 70),
        )
        for suffix, segment, score in suffix_map:
            if key.endswith(suffix):
                return segment, score
        match = re.search(r"temp[a-z0-9]*([lmr])$", key)
        if match:
            return str(match.group(1)).upper(), 60
        return None, 0

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
    def _get_session_info_yaml_with_source_from_ir(cls, ir: Any) -> tuple[str | None, str | None]:
        text, source = cls._get_session_info_yaml_primary_from_ir(ir)
        if text and source:
            return text, source
        text = cls._get_session_info_yaml_shared_mem_fallback_from_ir(ir)
        if text:
            return text, "fallback_shared_mem"
        return None, None

    @classmethod
    def _get_session_info_yaml_from_ir(cls, ir: Any) -> str | None:
        text, _source = cls._get_session_info_yaml_with_source_from_ir(ir)
        return text

    @classmethod
    def _get_session_info_yaml_primary_from_ir(cls, ir: Any) -> tuple[str | None, str | None]:
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
                return text, f"primary_attr:{attr_name}"

        try:
            for attr_name, attr_value in vars(ir).items():
                if "session" not in attr_name.lower() or "info" not in attr_name.lower():
                    continue
                text = cls._coerce_text(attr_value)
                if text and text.strip():
                    return text, f"primary_vars_scan:{attr_name}"
        except Exception:
            pass
        return None, None

    @classmethod
    def _get_session_info_yaml_shared_mem_fallback_from_ir(cls, ir: Any) -> str | None:
        header = cls._get_ir_attr_value(ir, "_header")
        shared_mem = cls._get_ir_attr_value(ir, "_shared_mem")
        if header is None or shared_mem is None:
            return None

        try:
            offset = getattr(header, "session_info_offset")
            length = getattr(header, "session_info_len")
            offset = int(offset() if callable(offset) else offset)
            length = int(length() if callable(length) else length)
        except Exception:
            return None
        if offset < 0 or length <= 0:
            return None

        end = offset + length
        try:
            chunk = shared_mem[offset:end]
        except Exception:
            try:
                chunk = bytes(shared_mem)[offset:end]
            except Exception:
                return None
        try:
            if isinstance(chunk, memoryview):
                data = chunk.tobytes()
            elif isinstance(chunk, bytearray):
                data = bytes(chunk)
            elif isinstance(chunk, bytes):
                data = chunk
            else:
                data = bytes(chunk)
        except Exception:
            return None
        if not data:
            return None

        # iRacing session info is a NUL-terminated YAML blob inside the shared memory segment.
        data = data.split(b"\x00", 1)[0]
        if not data:
            return None
        text = cls._decode_text_best_effort(data)
        if not text or not text.strip():
            return None
        if ":" not in text:
            return None
        return text

    @staticmethod
    def _decode_text_best_effort(data: bytes) -> str | None:
        if not isinstance(data, (bytes, bytearray)):
            return None
        raw = bytes(data)
        # Prefer strict decoding first so we do not silently inject replacement characters
        # and miss a later codec (e.g. pyirsdk uses cp1252 for SessionInfo text).
        for encoding in ("utf-8", "cp1252", "latin-1"):
            try:
                return raw.decode(encoding)
            except Exception:
                continue
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
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
    def _summarize_debug_value(value: Any) -> dict[str, Any]:
        info: dict[str, Any] = {"type": type(value).__name__}
        try:
            if isinstance(value, (str, bytes, bytearray)):
                info["len"] = len(value)
            elif isinstance(value, (list, tuple, set, dict)):
                info["len"] = len(value)
        except Exception:
            pass
        text = IRSDKClient._coerce_text(value)
        if text is not None:
            info["text_len"] = len(text)
            info["text_preview"] = text[:200]
        else:
            try:
                info["repr"] = repr(value)[:200]
            except Exception:
                pass
        return info

    @staticmethod
    def _extract_indexed_value(value: Any, index: int) -> Any:
        if index < 0:
            raise IndexError(index)
        if isinstance(value, (list, tuple)):
            return value[index]
        try:
            return value[index]  # type: ignore[index]
        except Exception:
            pass
        try:
            seq = list(value)
        except Exception as exc:
            raise TypeError("value is not indexable") from exc
        return seq[index]

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
