from __future__ import annotations

import re
from typing import Any


def extract_session_meta(
    session_info_yaml: str,
    *,
    recorder_start_ts: float | int | None = None,
    session_info_saved_ts: float | int | None = None,
) -> dict[str, Any]:
    text = str(session_info_yaml or "")
    meta: dict[str, Any] = {}
    if not text.strip():
        return _with_timestamps(meta, recorder_start_ts=recorder_start_ts, session_info_saved_ts=session_info_saved_ts)

    parsed = _safe_yaml_parse(text)

    if isinstance(parsed, dict):
        weekend_info = _as_dict(parsed.get("WeekendInfo"))
        driver_info = _as_dict(parsed.get("DriverInfo"))
        session_info = _as_dict(parsed.get("SessionInfo"))
        driver = _select_driver(driver_info)

        _set_if_present(meta, "DriverName", _coalesce(driver.get("UserName"), driver.get("DriverName"), driver.get("UserNameAbbrev")))
        _set_if_present(meta, "CarScreenName", _coalesce(driver.get("CarScreenName"), driver.get("CarPath")))
        _set_if_present(meta, "CarClassShortName", driver.get("CarClassShortName"))
        _set_if_present(meta, "TrackDisplayName", weekend_info.get("TrackDisplayName"))
        _set_if_present(meta, "TrackConfigName", weekend_info.get("TrackConfigName"))
        _set_if_present(
            meta,
            "SessionUniqueID",
            _coalesce(
                weekend_info.get("SessionUniqueID"),
                _find_first_value_for_key(parsed, "SessionUniqueID"),
                weekend_info.get("SubSessionID"),
                weekend_info.get("SessionID"),
            ),
        )

        primary_session_type = _extract_primary_session_type(parsed, session_info)
        fallback_session_type = _coalesce(weekend_info.get("SessionType"), weekend_info.get("EventType"))
        raw_session_type = _coalesce(primary_session_type, fallback_session_type)
        if raw_session_type is not None:
            meta["session_type_raw"] = str(raw_session_type)
            meta["SessionType"] = normalize_session_type(raw_session_type)
    else:
        regex_key_map = {
            "DriverName": ("DriverName", "UserName"),
            "CarScreenName": ("CarScreenName",),
            "CarClassShortName": ("CarClassShortName",),
            "TrackDisplayName": ("TrackDisplayName",),
            "TrackConfigName": ("TrackConfigName",),
            "SessionUniqueID": ("SessionUniqueID", "SubSessionID", "SessionID"),
        }
        for meta_key, raw_keys in regex_key_map.items():
            _set_if_present(meta, meta_key, _coalesce(*(_regex_extract_scalar(text, raw_key) for raw_key in raw_keys)))
        raw_session_type = _coalesce(_regex_extract_scalar(text, "SessionType"), _regex_extract_scalar(text, "EventType"))
        if raw_session_type is not None:
            meta["session_type_raw"] = str(raw_session_type)
            meta["SessionType"] = normalize_session_type(raw_session_type)

    return _with_timestamps(meta, recorder_start_ts=recorder_start_ts, session_info_saved_ts=session_info_saved_ts)


def normalize_session_type(raw_session_type: Any) -> str:
    raw = str(raw_session_type or "").strip()
    if not raw:
        return "unknown"

    key = re.sub(r"[^a-z0-9]+", " ", raw.lower()).strip()
    if not key:
        return "unknown"
    if any(token in key for token in ("qualify", "qualification", "qualifying")):
        return "qualify"
    if "race" in key:
        return "race"
    if any(token in key for token in ("practice", "warmup", "warm up", "test")):
        return "practice"
    return "unknown"


def _safe_yaml_parse(text: str) -> Any:
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text)
    except Exception:
        return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _select_driver(driver_info: dict[str, Any]) -> dict[str, Any]:
    drivers = _as_list(driver_info.get("Drivers"))
    driver_car_idx = driver_info.get("DriverCarIdx")
    try:
        driver_car_idx_int = int(driver_car_idx) if driver_car_idx is not None else None
    except Exception:
        driver_car_idx_int = None

    for item in drivers:
        if not isinstance(item, dict):
            continue
        if driver_car_idx_int is None:
            break
        try:
            if int(item.get("CarIdx")) == driver_car_idx_int:
                return item
        except Exception:
            continue

    for item in drivers:
        if not isinstance(item, dict):
            continue
        try:
            if int(item.get("IsSpectator", 0)) == 0:
                return item
        except Exception:
            return item
    return drivers[0] if drivers and isinstance(drivers[0], dict) else {}


def _extract_primary_session_type(root: dict[str, Any], session_info: dict[str, Any]) -> Any:
    current_session_num = _coerce_int(
        _coalesce(
            root.get("SessionNum"),
            session_info.get("CurrentSessionNum"),
            _find_first_value_for_key(session_info, "SessionNum"),
        )
    )
    sessions = _as_list(session_info.get("Sessions"))
    if current_session_num is not None:
        for item in sessions:
            if not isinstance(item, dict):
                continue
            item_num = _coerce_int(item.get("SessionNum"))
            if item_num is not None and item_num == current_session_num and item.get("SessionType") is not None:
                return item.get("SessionType")

    for item in sessions:
        if isinstance(item, dict) and item.get("SessionType") is not None:
            return item.get("SessionType")
    return session_info.get("SessionType")


def _find_first_value_for_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value.get(key)
        for child in value.values():
            found = _find_first_value_for_key(child, key)
            if found is not None:
                return found
        return None
    if isinstance(value, list):
        for item in value:
            found = _find_first_value_for_key(item, key)
            if found is not None:
                return found
    return None


def _regex_extract_scalar(text: str, key: str) -> str | None:
    match = re.search(rf"(?m)^\s*{re.escape(key)}\s*:\s*(.+?)\s*$", text)
    if not match:
        return None
    value = match.group(1).strip()
    if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
        value = value[1:-1]
    return value or None


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _set_if_present(target: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    target[key] = value


def _with_timestamps(
    meta: dict[str, Any],
    *,
    recorder_start_ts: float | int | None,
    session_info_saved_ts: float | int | None,
) -> dict[str, Any]:
    if recorder_start_ts is not None:
        meta["recorder_start_ts"] = recorder_start_ts
    if session_info_saved_ts is not None:
        meta["session_info_saved_ts"] = session_info_saved_ts
    return meta
