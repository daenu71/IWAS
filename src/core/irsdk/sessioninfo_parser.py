"""SessionInfo parsing and metadata extraction utilities."""

from __future__ import annotations

import re
from typing import Any


def extract_session_meta(
    session_info_yaml: str,
    *,
    recorder_start_ts: float | int | None = None,
    session_info_saved_ts: float | int | None = None,
) -> dict[str, Any]:
    """Extract session meta."""
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
        track_usage = _extract_track_usage(parsed, weekend_info, session_info)

        _set_if_present(meta, "DriverName", _coalesce(driver.get("UserName"), driver.get("DriverName"), driver.get("UserNameAbbrev")))
        _set_if_present(meta, "CarScreenName", _coalesce(driver.get("CarScreenName"), driver.get("CarPath")))
        _set_if_present(meta, "CarClassShortName", driver.get("CarClassShortName"))
        _set_if_present(meta, "TrackDisplayName", weekend_info.get("TrackDisplayName"))
        _set_if_present(meta, "TrackConfigName", weekend_info.get("TrackConfigName"))
        _set_if_present(meta, "TrackUsage", track_usage)
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
        environment = _extract_environment(weekend_info)
        if environment is not None:
            meta["environment"] = environment
        session_conditions = _extract_session_conditions(weekend_info)
        if session_conditions:
            meta["session_conditions"] = session_conditions
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
        _set_if_present(
            meta,
            "TrackUsage",
            _normalize_track_usage(
                _coalesce(
                    _regex_extract_scalar(text, "TrackUsage"),
                    _regex_extract_scalar(text, "SessionTrackRubberState"),
                )
            ),
        )
        raw_session_type = _coalesce(_regex_extract_scalar(text, "SessionType"), _regex_extract_scalar(text, "EventType"))
        if raw_session_type is not None:
            meta["session_type_raw"] = str(raw_session_type)
            meta["SessionType"] = normalize_session_type(raw_session_type)
        environment = extract_environment_from_session_info(text)
        if environment is not None:
            meta["environment"] = environment
        session_conditions = _extract_session_conditions(
            {
                "TrackSurfaceTemp": _regex_extract_scalar(text, "TrackSurfaceTemp"),
                "TrackTemp": _regex_extract_scalar(text, "TrackTemp"),
                "AirTemp": _regex_extract_scalar(text, "AirTemp"),
                "TrackAirTemp": _regex_extract_scalar(text, "TrackAirTemp"),
                "AirPressure": _regex_extract_scalar(text, "AirPressure"),
                "TrackAirPressure": _regex_extract_scalar(text, "TrackAirPressure"),
                "RelativeHumidity": _regex_extract_scalar(text, "RelativeHumidity"),
                "TrackRelativeHumidity": _regex_extract_scalar(text, "TrackRelativeHumidity"),
                "Humidity": _regex_extract_scalar(text, "Humidity"),
                "WindVel": _regex_extract_scalar(text, "WindVel"),
                "TrackWindVel": _regex_extract_scalar(text, "TrackWindVel"),
                "WindSpeed": _regex_extract_scalar(text, "WindSpeed"),
                "WindDir": _regex_extract_scalar(text, "WindDir"),
                "TrackWindDir": _regex_extract_scalar(text, "TrackWindDir"),
                "WindDirection": _regex_extract_scalar(text, "WindDirection"),
                "Skies": _regex_extract_scalar(text, "Skies"),
                "TrackSkies": _regex_extract_scalar(text, "TrackSkies"),
                "WeatherDeclaredWet": _regex_extract_scalar(text, "WeatherDeclaredWet"),
                "WeatherType": _regex_extract_scalar(text, "WeatherType"),
                "TrackWeatherType": _regex_extract_scalar(text, "TrackWeatherType"),
            }
        )
        if session_conditions:
            meta["session_conditions"] = session_conditions

    return _with_timestamps(meta, recorder_start_ts=recorder_start_ts, session_info_saved_ts=session_info_saved_ts)


def resolve_session_environment(
    session_meta: dict[str, Any] | None,
    *,
    session_info_yaml: str | None = None,
) -> dict[str, Any] | None:
    """Resolve environment data from recorded meta with a YAML fallback."""
    meta = session_meta if isinstance(session_meta, dict) else {}
    environment = _normalize_environment_dict(meta.get("environment"))
    track_usage = _normalize_track_usage(meta.get("TrackUsage"))
    if track_usage is None and session_info_yaml:
        extracted = extract_environment_from_session_info(session_info_yaml)
        if isinstance(extracted, dict):
            track_usage = _normalize_track_usage(extracted.get("track_usage"))
    if environment is not None:
        if track_usage and not _coerce_optional_str(environment.get("track_usage")):
            environment["track_usage"] = track_usage
        return environment

    environment = _extract_environment(meta)
    if environment is not None:
        return environment

    if session_info_yaml:
        return extract_environment_from_session_info(session_info_yaml)
    return None


def extract_environment_from_session_info(session_info_yaml: str) -> dict[str, Any] | None:
    """Extract only the environment block from SessionInfo YAML text."""
    text = str(session_info_yaml or "")
    if not text.strip():
        return None
    parsed = _safe_yaml_parse(text)
    if isinstance(parsed, dict):
        return _extract_environment(
            _as_dict(parsed.get("WeekendInfo")),
            parsed=parsed,
            session_info=_as_dict(parsed.get("SessionInfo")),
        )
    return _extract_environment(
        {
            "TrackTemp": _regex_extract_scalar(text, "TrackTemp"),
            "TrackSurfaceTemp": _regex_extract_scalar(text, "TrackSurfaceTemp"),
            "AirTemp": _regex_extract_scalar(text, "AirTemp"),
            "TrackAirTemp": _regex_extract_scalar(text, "TrackAirTemp"),
            "WeatherTemp": _regex_extract_scalar(text, "WeatherTemp"),
            "Humidity": _regex_extract_scalar(text, "Humidity"),
            "TrackRelativeHumidity": _regex_extract_scalar(text, "TrackRelativeHumidity"),
            "RelativeHumidity": _regex_extract_scalar(text, "RelativeHumidity"),
            "Fog": _regex_extract_scalar(text, "Fog"),
            "TrackFogLevel": _regex_extract_scalar(text, "TrackFogLevel"),
            "FogLevel": _regex_extract_scalar(text, "FogLevel"),
            "WindSpeed": _regex_extract_scalar(text, "WindSpeed"),
            "TrackWindVel": _regex_extract_scalar(text, "TrackWindVel"),
            "WindDir": _regex_extract_scalar(text, "WindDir"),
            "TrackWindDir": _regex_extract_scalar(text, "TrackWindDir"),
            "WindDirection": _regex_extract_scalar(text, "WindDirection"),
            "Skies": _regex_extract_scalar(text, "Skies"),
            "TrackSkies": _regex_extract_scalar(text, "TrackSkies"),
            "WeatherType": _regex_extract_scalar(text, "WeatherType"),
            "TrackWeatherType": _regex_extract_scalar(text, "TrackWeatherType"),
            "AirPressure": _regex_extract_scalar(text, "AirPressure"),
            "TrackAirPressure": _regex_extract_scalar(text, "TrackAirPressure"),
            "TrackUsage": _regex_extract_scalar(text, "TrackUsage"),
            "SessionTrackRubberState": _regex_extract_scalar(text, "SessionTrackRubberState"),
        }
    )


def normalize_session_type(raw_session_type: Any) -> str:
    """Normalize session type."""
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
    """Implement safe yaml parse logic."""
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text)
    except Exception:
        return None


def _as_dict(value: Any) -> dict[str, Any]:
    """Implement as dict logic."""
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    """Implement as list logic."""
    return value if isinstance(value, list) else []


def _select_driver(driver_info: dict[str, Any]) -> dict[str, Any]:
    """Select driver."""
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
    """Extract primary session type."""
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


def _extract_primary_session_field(root: dict[str, Any], session_info: dict[str, Any], field_name: str) -> Any:
    """Extract one field from the active session, falling back to any session match."""
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
            if item_num is not None and item_num == current_session_num and item.get(field_name) is not None:
                return item.get(field_name)

    for item in sessions:
        if isinstance(item, dict) and item.get(field_name) is not None:
            return item.get(field_name)
    return session_info.get(field_name)


def _extract_track_usage(
    root: dict[str, Any] | None,
    weekend_info: dict[str, Any] | None,
    session_info: dict[str, Any] | None,
) -> str | None:
    """Extract and normalize track usage/rubber state from known SessionInfo fields."""
    root_dict = root if isinstance(root, dict) else {}
    weekend_dict = weekend_info if isinstance(weekend_info, dict) else {}
    session_dict = session_info if isinstance(session_info, dict) else {}
    raw_value = _coalesce(
        weekend_dict.get("TrackUsage"),
        weekend_dict.get("SessionTrackRubberState"),
        _extract_primary_session_field(root_dict, session_dict, "SessionTrackRubberState"),
        session_dict.get("SessionTrackRubberState"),
    )
    return _normalize_track_usage(raw_value)


def _find_first_value_for_key(value: Any, key: str) -> Any:
    """Find first value for key."""
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
    """Implement regex extract scalar logic."""
    match = re.search(rf"(?m)^\s*{re.escape(key)}\s*:\s*(.+?)\s*$", text)
    if not match:
        return None
    value = match.group(1).strip()
    if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
        value = value[1:-1]
    return value or None


def _extract_environment(
    weekend_info: dict[str, Any],
    *,
    parsed: dict[str, Any] | None = None,
    session_info: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Extract environment metadata from WeekendInfo."""
    weekend_options = _as_dict(weekend_info.get("WeekendOptions"))
    environment = {
        "track_temp_c": _coerce_temperature_c(
            _coalesce(
                weekend_info.get("TrackTemp"),
                weekend_info.get("TrackSurfaceTemp"),
                weekend_options.get("WeatherTemp"),
            )
        ),
        "air_temp_c": _coerce_temperature_c(
            _coalesce(
                weekend_info.get("AirTemp"),
                weekend_info.get("TrackAirTemp"),
                weekend_options.get("WeatherTemp"),
            )
        ),
        "humidity_pct": _coerce_percentage(
            _coalesce(
                weekend_info.get("Humidity"),
                weekend_info.get("TrackRelativeHumidity"),
                weekend_options.get("RelativeHumidity"),
            )
        ),
        "fog_pct": _coerce_percentage(
            _coalesce(
                weekend_info.get("Fog"),
                weekend_info.get("TrackFogLevel"),
                weekend_options.get("FogLevel"),
            )
        ),
        "wind_speed_ms": _coerce_speed_ms(
            _coalesce(
                weekend_info.get("WindSpeed"),
                weekend_info.get("TrackWindVel"),
                weekend_options.get("WindSpeed"),
            )
        ),
        "wind_dir_deg": _coerce_direction_deg(
            _coalesce(
                weekend_info.get("WindDir"),
                weekend_info.get("TrackWindDir"),
                weekend_options.get("WindDirection"),
            )
        ),
        "skies": _coerce_optional_str(_coalesce(weekend_info.get("Skies"), weekend_info.get("TrackSkies"), weekend_options.get("Skies"))),
        "weather_type": _coerce_optional_str(
            _coalesce(
                weekend_info.get("WeatherType"),
                weekend_info.get("TrackWeatherType"),
                weekend_options.get("WeatherType"),
            )
        ),
        "track_usage": _extract_track_usage(parsed, weekend_info, session_info),
        "air_pressure_hpa": _coerce_pressure_hpa(
            _coalesce(
                weekend_info.get("AirPressure"),
                weekend_info.get("TrackAirPressure"),
            )
        ),
    }
    if any(value is not None for value in environment.values()):
        return environment
    return None


def _extract_session_conditions(weekend_info: dict[str, Any]) -> dict[str, Any]:
    """Extract filterable session-condition fields from WeekendInfo."""
    conditions = {
        "track_temp_c": _coerce_temperature_c(_coalesce(weekend_info.get("TrackSurfaceTemp"), weekend_info.get("TrackTemp"))),
        "air_temp_c": _coerce_temperature_c(_coalesce(weekend_info.get("AirTemp"), weekend_info.get("TrackAirTemp"))),
        "air_pressure_hpa": _coerce_pressure_hpa(_coalesce(weekend_info.get("AirPressure"), weekend_info.get("TrackAirPressure"))),
        "humidity_pct": _coerce_percentage(
            _coalesce(
                weekend_info.get("RelativeHumidity"),
                weekend_info.get("TrackRelativeHumidity"),
                weekend_info.get("Humidity"),
            )
        ),
        "wind_speed_ms": _coerce_speed_ms(_coalesce(weekend_info.get("WindVel"), weekend_info.get("TrackWindVel"), weekend_info.get("WindSpeed"))),
        "wind_dir_deg": _coerce_direction_deg(_coalesce(weekend_info.get("WindDir"), weekend_info.get("TrackWindDir"), weekend_info.get("WindDirection"))),
        "skies": _coerce_optional_str(_coalesce(weekend_info.get("Skies"), weekend_info.get("TrackSkies"))),
        "weather_wet": _coerce_optional_bool(weekend_info.get("WeatherDeclaredWet")),
        "weather_type": _coerce_optional_str(_coalesce(weekend_info.get("WeatherType"), weekend_info.get("TrackWeatherType"))),
    }
    return {key: value for key, value in conditions.items() if value is not None}


def _coerce_int(value: Any) -> int | None:
    """Coerce int."""
    try:
        return int(value)
    except Exception:
        return None


def _coerce_optional_float(value: Any) -> float | None:
    """Coerce optional float."""
    try:
        return float(value)
    except Exception:
        parsed, _unit = _parse_number_and_unit(value)
        return parsed


def _coerce_optional_str(value: Any) -> str | None:
    """Coerce optional string."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_track_usage(value: Any) -> str | None:
    """Normalize known track-usage labels to a stable display form."""
    text = _coerce_optional_str(value)
    if text is None:
        return None
    compact = re.sub(r"\s+", " ", text).strip()
    lowered = compact.casefold()
    known = {
        "low usage": "Low Usage",
        "moderate usage": "Moderate Usage",
        "high usage": "High Usage",
        "moderately high usage": "Moderately High Usage",
        "moderately low usage": "Moderately Low Usage",
        "low": "Low Usage",
        "moderate": "Moderate Usage",
        "high": "High Usage",
    }
    if lowered in known:
        return known[lowered]
    return compact.title()


def _coerce_optional_bool(value: Any) -> bool | None:
    """Coerce optional bool."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if not text:
        return None
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _coalesce(*values: Any) -> Any:
    """Implement coalesce logic."""
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _normalize_environment_dict(value: Any) -> dict[str, Any] | None:
    """Return a shallow-copied environment dict or None."""
    if not isinstance(value, dict):
        return None
    copied = dict(value)
    return copied or None


def _coerce_temperature_c(value: Any) -> float | None:
    number, unit = _parse_number_and_unit(value)
    if number is None:
        return None
    normalized_unit = unit.replace("°", "")
    if normalized_unit in {"", "c", "deg c"}:
        return number
    if normalized_unit in {"f", "deg f"}:
        return (number - 32.0) * (5.0 / 9.0)
    if normalized_unit in {"k", "deg k"}:
        return number - 273.15
    return number


def _coerce_percentage(value: Any) -> float | None:
    number, _unit = _parse_number_and_unit(value)
    return number


def _coerce_speed_ms(value: Any) -> float | None:
    number, unit = _parse_number_and_unit(value)
    if number is None:
        return None
    if unit in {"", "m/s", "ms", "meter/s", "meters/s"}:
        return number
    if unit in {"km/h", "kph", "kmh"}:
        return number / 3.6
    if unit in {"mph"}:
        return number * 0.44704
    if unit in {"kt", "kts", "kn", "knot", "knots"}:
        return number * 0.514444
    return number


def _coerce_direction_deg(value: Any) -> float | None:
    text = _coerce_optional_str(value)
    if text is None:
        return None
    cardinal = _cardinal_direction_deg(text)
    if cardinal is not None:
        return cardinal
    number, unit = _parse_number_and_unit(text)
    if number is None:
        return None
    normalized_unit = unit.replace("°", "")
    if normalized_unit in {"", "deg", "degree", "degrees"}:
        return number % 360.0
    if normalized_unit in {"rad", "radian", "radians"}:
        return (number * 180.0 / 3.141592653589793) % 360.0
    return number % 360.0


def _coerce_pressure_hpa(value: Any) -> float | None:
    number, unit = _parse_number_and_unit(value)
    if number is None:
        return None
    if unit in {"", "hpa", "mbar", "mb"}:
        return number
    if unit in {"kpa"}:
        return number * 10.0
    if unit in {"bar"}:
        return number * 1000.0
    if unit in {"hg", "inhg"}:
        return number * 33.8638866667
    if unit in {"psi"}:
        return number * 68.9475729
    return number


def _parse_number_and_unit(value: Any) -> tuple[float | None, str]:
    """Extract a leading numeric value and normalized unit suffix."""
    if value is None:
        return None, ""
    if isinstance(value, bool):
        return None, ""
    if isinstance(value, (int, float)):
        return float(value), ""
    text = str(value).strip()
    if not text:
        return None, ""
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None, text.lower()
    try:
        number = float(match.group(0))
    except Exception:
        return None, ""
    unit = text[match.end():].strip().lower()
    return number, unit


def _cardinal_direction_deg(value: str) -> float | None:
    """Convert a compass direction like NE or SSW into degrees."""
    lookup = {
        "n": 0.0,
        "nne": 22.5,
        "ne": 45.0,
        "ene": 67.5,
        "e": 90.0,
        "ese": 112.5,
        "se": 135.0,
        "sse": 157.5,
        "s": 180.0,
        "ssw": 202.5,
        "sw": 225.0,
        "wsw": 247.5,
        "w": 270.0,
        "wnw": 292.5,
        "nw": 315.0,
        "nnw": 337.5,
    }
    return lookup.get(str(value or "").strip().lower())


def _set_if_present(target: dict[str, Any], key: str, value: Any) -> None:
    """Implement set if present logic."""
    if value is None:
        return
    target[key] = value


def _with_timestamps(
    meta: dict[str, Any],
    *,
    recorder_start_ts: float | int | None,
    session_info_saved_ts: float | int | None,
) -> dict[str, Any]:
    """Implement with timestamps logic."""
    if recorder_start_ts is not None:
        meta["recorder_start_ts"] = recorder_start_ts
    if session_info_saved_ts is not None:
        meta["session_info_saved_ts"] = session_info_saved_ts
    return meta
