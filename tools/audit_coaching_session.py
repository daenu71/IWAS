from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import difflib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable


WHEELS: tuple[str, ...] = ("LF", "RF", "LR", "RR")
TIRE_SEGMENTS: tuple[str, ...] = ("L", "M", "R")


def _wheel_columns(prefix: str) -> tuple[str, ...]:
    return tuple(f"{prefix}{wheel}" for wheel in WHEELS)


def _index_columns(prefix: str, count: int) -> tuple[str, ...]:
    return tuple(f"{prefix}_{idx}" for idx in range(count))


def _tire_temp_wheel_columns() -> tuple[str, ...]:
    cols: list[str] = []
    for wheel in WHEELS:
        for segment in TIRE_SEGMENTS:
            cols.append(f"TireTemp{wheel}_{segment}")
    return tuple(cols)


def _normalize_key(value: str) -> str:
    return "".join(ch.lower() for ch in str(value) if ch.isalnum())


def _normalize_session_type(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return "unknown"
    key = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    if not key:
        return "unknown"
    if any(token in key for token in ("qualify", "qualification", "qualifying")):
        return "qualify"
    if "race" in key:
        return "race"
    if any(token in key for token in ("practice", "warmup", "warm up", "test", "testing")):
        return "practice"
    return "unknown"


@dataclass(frozen=True)
class ExpectedItem:
    key: str
    category_code: str
    category_title: str
    display_name: str
    kind: str
    column_name: str | None = None
    required_any_of: tuple[tuple[str, ...], ...] = ()


@dataclass
class ItemResult:
    key: str
    category_code: str
    category_title: str
    display_name: str
    kind: str
    present_exact: bool
    details: str = ""
    near_miss_candidates: list[str] = field(default_factory=list)


def build_expected_items() -> list[ExpectedItem]:
    items: list[ExpectedItem] = []

    categories: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        ("A", "Session/System", ("SessionTime", "SessionState", "SessionUniqueID", "SessionFlags")),
        (
            "B",
            "Rundendaten",
            (
                "Lap",
                "LapCompleted",
                "LapDist",
                "LapDistPct",
                "LapCurrentLapTime",
                "LapLastLapTime",
                "LapBestLapTime",
                "LapDeltaToBestLap",
                "LapDeltaToSessionBestLap",
                "LapDeltaToSessionOptimalLap",
                "LapDeltaToOptimalLap",
            ),
        ),
        (
            "C",
            "Fahrzeugbewegung",
            (
                "Speed",
                "Yaw",
                "Pitch",
                "Roll",
                "VelocityX",
                "VelocityY",
                "VelocityZ",
                "VelocityLocalX",
                "VelocityLocalY",
                "VelocityLocalZ",
                "YawRate",
                "LatAccel",
                "LongAccel",
                "VertAccel",
            ),
        ),
        (
            "D",
            "Eingaben",
            (
                "Throttle",
                "Brake",
                "Clutch",
                "SteeringWheelAngle",
                "SteeringWheelTorque",
                "SteeringWheelPctTorque",
            ),
        ),
        ("E", "Motor/Getriebe", ("RPM", "Gear", "FuelLevel", "FuelLevelPct", "FuelUsePerHour")),
        ("G", "Elektronik", ("ABSactive", "TractionControl", "TractionControlActive", "BrakeBias")),
        ("H", "Position/Umwelt", ("Lat", "Lon", "Alt", "TrackTemp", "AirTemp")),
    )

    for category_code, category_title, names in categories:
        for name in names:
            items.append(
                ExpectedItem(
                    key=name,
                    category_code=category_code,
                    category_title=category_title,
                    display_name=name,
                    kind="parquet_column",
                    column_name=name,
                )
            )

    items.extend(
        (
            ExpectedItem(
                key="ShockDefl[4]",
                category_code="F",
                category_title="Reifen/Suspension",
                display_name="ShockDefl (alle 4)",
                kind="parquet_group",
                required_any_of=(
                    _wheel_columns("ShockDefl"),
                    _index_columns("ShockDefl", 4),
                ),
            ),
            ExpectedItem(
                key="RideHeight[4]",
                category_code="F",
                category_title="Reifen/Suspension",
                display_name="RideHeight (alle 4)",
                kind="parquet_group",
                required_any_of=(
                    _wheel_columns("RideHeight"),
                    _index_columns("RideHeight", 4),
                ),
            ),
            ExpectedItem(
                key="TireTemp[4][L/M/R]",
                category_code="F",
                category_title="Reifen/Suspension",
                display_name="TireTemp L/M/R (alle 4)",
                kind="parquet_group",
                required_any_of=(
                    _tire_temp_wheel_columns(),
                    _index_columns("TireTemp", 12),
                ),
            ),
            ExpectedItem(
                key="TirePressure[4]",
                category_code="F",
                category_title="Reifen/Suspension",
                display_name="TirePressure (alle 4)",
                kind="parquet_group",
                required_any_of=(
                    _wheel_columns("TirePressure"),
                    _index_columns("TirePressure", 4),
                ),
            ),
        )
    )

    for name in ("OnPitRoad", "IsOnTrack", "IsOnTrackCar"):
        items.append(
            ExpectedItem(
                key=name,
                category_code="I",
                category_title="Zusatzfelder",
                display_name=name,
                kind="parquet_column",
                column_name=name,
            )
        )

    items.extend(
        (
            ExpectedItem(
                key="SessionInfo.Sessions[SessionNum].SessionType",
                category_code="I",
                category_title="Zusatzfelder",
                display_name="SessionInfo.Sessions[SessionNum].SessionType",
                kind="yaml_path",
            ),
            ExpectedItem(
                key="WeekendInfo.SessionType",
                category_code="I",
                category_title="Zusatzfelder",
                display_name="WeekendInfo.SessionType",
                kind="yaml_path",
            ),
        )
    )
    return items


def _find_near_misses(
    expected_name: str,
    actual_names: Iterable[str],
    *,
    limit: int = 8,
) -> list[str]:
    target = str(expected_name)
    target_norm = _normalize_key(target)
    scored: list[tuple[float, str]] = []
    for candidate in actual_names:
        cand = str(candidate)
        if cand == target:
            continue
        cand_norm = _normalize_key(cand)
        score = 0.0
        if target_norm and cand_norm and target_norm == cand_norm:
            score = 1.20
        else:
            ratio = difflib.SequenceMatcher(a=target_norm, b=cand_norm).ratio()
            if ratio >= 0.72:
                score = ratio
            elif target_norm and cand_norm and (target_norm in cand_norm or cand_norm in target_norm):
                score = 0.74
        if score <= 0.0:
            continue
        scored.append((score, cand))

    scored.sort(key=lambda item: (-item[0], item[1].lower(), item[1]))
    output: list[str] = []
    seen: set[str] = set()
    for _score, name in scored:
        if name in seen:
            continue
        seen.add(name)
        output.append(name)
        if len(output) >= limit:
            break
    return output


def _find_group_near_misses(expected_columns: Iterable[str], actual_columns: Iterable[str], *, limit: int = 10) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    actual_list = [str(name) for name in actual_columns]
    for expected_name in expected_columns:
        for candidate in _find_near_misses(str(expected_name), actual_list, limit=limit):
            if candidate in seen:
                continue
            seen.add(candidate)
            merged.append(candidate)
            if len(merged) >= limit:
                return merged
    return merged


def _read_json_dict(path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    if isinstance(raw, dict):
        return raw, None
    return {}, "JSON root is not an object"


def _parse_session_yaml_text_fallback(text: str) -> dict[str, Any]:
    lines = str(text).splitlines()

    def extract_block_lines(top_key: str) -> list[tuple[int, str]]:
        block: list[tuple[int, str]] = []
        start = None
        key_re = re.compile(rf"^{re.escape(top_key)}\s*:\s*(.*?)\s*$")
        for idx, line in enumerate(lines):
            if key_re.match(line):
                start = idx + 1
                break
        if start is None:
            return block
        for idx in range(start, len(lines)):
            raw = lines[idx]
            if raw and not raw.startswith(" "):
                break
            indent = len(raw) - len(raw.lstrip(" "))
            block.append((indent, raw))
        return block

    session_block = extract_block_lines("SessionInfo")
    weekend_block = extract_block_lines("WeekendInfo")

    result: dict[str, Any] = {
        "parser_mode": "text_fallback",
        "session_info_current_session_num": None,
        "session_info_sessions": [],
        "weekend_info_session_type": None,
        "known_yaml_paths": [],
        "parser_notes": ["PyYAML nicht verfuegbar, Fallback-Parser genutzt."],
    }

    weekend_keys: dict[str, str] = {}
    for indent, raw in weekend_block:
        if indent < 1:
            continue
        match = re.match(r"^\s+([A-Za-z0-9_]+)\s*:\s*(.*?)\s*$", raw)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        weekend_keys[key] = value
        result["known_yaml_paths"].append(f"WeekendInfo.{key}")
    result["weekend_info_session_type"] = weekend_keys.get("SessionType")

    current_session_num: int | None = None
    sessions_indent: int | None = None
    sessions_start: int | None = None
    for idx, (indent, raw) in enumerate(session_block):
        if indent == 1:
            match = re.match(r"^\s+([A-Za-z0-9_]+)\s*:\s*(.*?)\s*$", raw)
            if not match:
                continue
            key, value = match.group(1), match.group(2)
            result["known_yaml_paths"].append(f"SessionInfo.{key}")
            if key == "CurrentSessionNum":
                try:
                    current_session_num = int(str(value).strip())
                except Exception:
                    current_session_num = None
            if key == "Sessions":
                sessions_indent = indent
                sessions_start = idx + 1
                break
    result["session_info_current_session_num"] = current_session_num

    sessions: list[dict[str, Any]] = []
    if sessions_start is not None and sessions_indent is not None:
        idx = sessions_start
        while idx < len(session_block):
            indent, raw = session_block[idx]
            if indent <= sessions_indent:
                break
            item_match = re.match(r"^\s*-\s*(.*?)\s*$", raw)
            if item_match:
                item_indent = len(raw) - len(raw.lstrip(" "))
                item: dict[str, Any] = {}
                inline = item_match.group(1).strip()
                if inline and ":" in inline:
                    k, v = inline.split(":", 1)
                    item[k.strip()] = v.strip()
                idx += 1
                while idx < len(session_block):
                    sub_indent, sub_raw = session_block[idx]
                    if sub_indent <= sessions_indent:
                        break
                    if re.match(r"^\s*-\s+", sub_raw) and sub_indent == item_indent:
                        break
                    if sub_indent == item_indent + 2:
                        kv = re.match(r"^\s+([A-Za-z0-9_]+)\s*:\s*(.*?)\s*$", sub_raw)
                        if kv:
                            key, value = kv.group(1), kv.group(2)
                            item[key] = value
                            if "SessionNum" in item:
                                result["known_yaml_paths"].append(
                                    f"SessionInfo.Sessions[{item.get('SessionNum')}].{key}"
                                )
                    idx += 1
                sessions.append(item)
                continue
            idx += 1
    result["session_info_sessions"] = sessions
    return result


def _parse_session_yaml(yaml_text: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore

        parsed = yaml.safe_load(yaml_text)
    except Exception:
        return _parse_session_yaml_text_fallback(yaml_text)

    if not isinstance(parsed, dict):
        return {
            "parser_mode": "pyyaml",
            "session_info_current_session_num": None,
            "session_info_sessions": [],
            "weekend_info_session_type": None,
            "known_yaml_paths": [],
            "parser_notes": ["YAML geparst, aber Root ist kein Mapping."],
        }

    weekend_info = parsed.get("WeekendInfo")
    session_info = parsed.get("SessionInfo")
    weekend_dict = weekend_info if isinstance(weekend_info, dict) else {}
    session_dict = session_info if isinstance(session_info, dict) else {}

    known_paths: list[str] = []
    for key in weekend_dict.keys():
        known_paths.append(f"WeekendInfo.{key}")
    for key in session_dict.keys():
        known_paths.append(f"SessionInfo.{key}")

    sessions_raw = session_dict.get("Sessions")
    sessions: list[dict[str, Any]] = []
    if isinstance(sessions_raw, list):
        for index, item in enumerate(sessions_raw):
            if not isinstance(item, dict):
                continue
            sessions.append(item)
            session_num_hint = item.get("SessionNum", index)
            for key in item.keys():
                known_paths.append(f"SessionInfo.Sessions[{session_num_hint}].{key}")

    result = {
        "parser_mode": "pyyaml",
        "session_info_current_session_num": session_dict.get("CurrentSessionNum"),
        "session_info_sessions": sessions,
        "weekend_info_session_type": weekend_dict.get("SessionType"),
        "known_yaml_paths": known_paths,
        "parser_notes": [],
    }
    return result


def _resolve_yaml_session_type_checks(parsed_yaml: dict[str, Any]) -> dict[str, Any]:
    sessions = parsed_yaml.get("session_info_sessions")
    if not isinstance(sessions, list):
        sessions = []
    current_raw = parsed_yaml.get("session_info_current_session_num")
    current_num: int | None
    try:
        current_num = int(current_raw) if current_raw is not None else None
    except Exception:
        current_num = None

    primary_value: Any = None
    primary_found = False
    primary_index: int | None = None
    uncertainty_notes: list[str] = []

    if current_num is not None:
        for idx, item in enumerate(sessions):
            if not isinstance(item, dict):
                continue
            try:
                item_num = int(item.get("SessionNum"))
            except Exception:
                item_num = None
            if item_num == current_num:
                primary_index = idx
                if item.get("SessionType") not in (None, ""):
                    primary_found = True
                    primary_value = item.get("SessionType")
                break
        if primary_index is None and sessions:
            uncertainty_notes.append(
                "CurrentSessionNum in YAML hat keinen passenden SessionNum-Eintrag; Session 0 als Fallback pruefen."
            )
            first = sessions[0]
            if isinstance(first, dict):
                primary_index = 0
                if first.get("SessionType") not in (None, ""):
                    primary_found = True
                    primary_value = first.get("SessionType")
    else:
        if sessions:
            uncertainty_notes.append("CurrentSessionNum fehlt in YAML; Session 0 als Fallback verwendet.")
            first = sessions[0]
            if isinstance(first, dict):
                primary_index = 0
                if first.get("SessionType") not in (None, ""):
                    primary_found = True
                    primary_value = first.get("SessionType")
        else:
            uncertainty_notes.append("SessionInfo.Sessions fehlt oder ist leer.")

    weekend_value = parsed_yaml.get("weekend_info_session_type")
    weekend_found = weekend_value not in (None, "")
    if parsed_yaml.get("weekend_info_session_type") in (None, ""):
        uncertainty_notes.append("WeekendInfo.SessionType fehlt oder ist leer.")

    path_value = ""
    if primary_index is not None:
        path_value = f"SessionInfo.Sessions[{primary_index}].SessionType"

    return {
        "primary_found": primary_found,
        "primary_value": primary_value,
        "primary_path_used": path_value,
        "weekend_found": weekend_found,
        "weekend_value": weekend_value,
        "uncertainty_notes": uncertainty_notes,
    }


def _read_parquet_schema_columns(parquet_path: Path) -> tuple[list[str], str | None]:
    try:
        import pyarrow.parquet as pq  # type: ignore
    except Exception as exc:
        return [], f"pyarrow import failed: {type(exc).__name__}: {exc}"

    try:
        parquet_file = pq.ParquetFile(parquet_path)
        schema = parquet_file.schema_arrow
        names = [str(name) for name in schema.names]
        return names, None
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


def _markdown_escape(value: str) -> str:
    return str(value).replace("|", "\\|")


def _generate_markdown_report(
    *,
    session_dir: Path,
    summary: dict[str, Any],
    artifact_report: dict[str, Any],
    results: list[ItemResult],
    near_miss_map: dict[str, list[str]],
    yaml_checks: dict[str, Any],
    meta_cross_check: dict[str, Any],
) -> str:
    lines: list[str] = []
    lines.append("# Coaching Session Audit Report")
    lines.append("")
    lines.append(f"- Session: `{session_dir}`")
    lines.append(f"- Generated (UTC): `{summary.get('generated_at_utc', '')}`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Expected variables: **{summary.get('expected_count', 0)}**")
    lines.append(f"- Present exact: **{summary.get('present_count', 0)}**")
    lines.append(f"- Missing exact: **{summary.get('missing_count', 0)}**")
    lines.append(f"- Exit code: **{summary.get('exit_code', '')}**")
    lines.append("")
    lines.append("## Artifact Check")
    lines.append("")
    lines.append("| Artifact | Status | Details |")
    lines.append("|---|---|---|")
    for item in artifact_report.get("checks", []):
        status = "OK" if item.get("present") else "MISSING"
        lines.append(
            f"| {_markdown_escape(str(item.get('artifact', '')))} | {status} | {_markdown_escape(str(item.get('details', '')))} |"
        )

    missing_artifacts = artifact_report.get("missing_artifacts", [])
    lines.append("")
    lines.append("## Missing Artifacts")
    lines.append("")
    if missing_artifacts:
        for name in missing_artifacts:
            lines.append(f"- MISSING `{name}`")
    else:
        lines.append("- None")

    lines.append("")
    lines.append("## Variables by Category")
    lines.append("")
    grouped: dict[tuple[str, str], list[ItemResult]] = {}
    for item in results:
        grouped.setdefault((item.category_code, item.category_title), []).append(item)

    for (code, title) in sorted(grouped.keys(), key=lambda kv: kv[0]):
        lines.append(f"### {code}) {title}")
        lines.append("")
        lines.append("| Variable | Status | Details |")
        lines.append("|---|---|---|")
        for item in grouped[(code, title)]:
            status = "OK" if item.present_exact else "MISSING"
            lines.append(
                f"| {_markdown_escape(item.display_name)} | {status} | {_markdown_escape(item.details or '')} |"
            )
        lines.append("")

    lines.append("## Near Misses")
    lines.append("")
    if near_miss_map:
        for key in sorted(near_miss_map.keys()):
            candidates = near_miss_map.get(key) or []
            if not candidates:
                lines.append(f"- `{key}`: none")
            else:
                lines.append(f"- `{key}`: {', '.join(f'`{c}`' for c in candidates)}")
    else:
        lines.append("- None")

    lines.append("")
    lines.append("## YAML SessionType Checks")
    lines.append("")
    lines.append(
        f"- `SessionInfo.Sessions[SessionNum].SessionType`: "
        f"{'OK' if yaml_checks.get('primary_found') else 'MISSING'}"
        f"{' (`' + str(yaml_checks.get('primary_value')) + '`, path=' + str(yaml_checks.get('primary_path_used')) + ')' if yaml_checks.get('primary_found') else ''}"
    )
    lines.append(
        f"- `WeekendInfo.SessionType`: "
        f"{'OK' if yaml_checks.get('weekend_found') else 'MISSING'}"
        f"{' (`' + str(yaml_checks.get('weekend_value')) + '`)' if yaml_checks.get('weekend_found') else ''}"
    )
    for note in yaml_checks.get("uncertainty_notes", []):
        lines.append(f"- Note: {note}")

    lines.append("")
    lines.append("## session_meta.json Cross-Check")
    lines.append("")
    for line in meta_cross_check.get("lines", []):
        lines.append(f"- {line}")
    if not meta_cross_check.get("lines"):
        lines.append("- No cross-check information available.")

    return "\n".join(lines).strip() + "\n"


def run_audit(session_dir: Path, output_dir: Path, *, json_name: str, md_name: str) -> int:
    session_dir = session_dir.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    session_info_path = session_dir / "session_info.yaml"
    session_meta_path = session_dir / "session_meta.json"
    run_parquets = sorted(session_dir.glob("run_*.parquet"))
    run_meta_files = sorted(session_dir.glob("run_*_meta.json"))
    log_txt_path = session_dir / "log.txt"

    artifact_checks = [
        {
            "artifact": "session_info.yaml",
            "present": session_info_path.exists(),
            "details": str(session_info_path) if session_info_path.exists() else "missing",
            "required": True,
        },
        {
            "artifact": "session_meta.json",
            "present": session_meta_path.exists(),
            "details": str(session_meta_path) if session_meta_path.exists() else "missing",
            "required": True,
        },
        {
            "artifact": "run_*.parquet",
            "present": len(run_parquets) > 0,
            "details": f"{len(run_parquets)} file(s)",
            "required": True,
        },
        {
            "artifact": "run_*_meta.json",
            "present": len(run_meta_files) > 0,
            "details": f"{len(run_meta_files)} file(s)",
            "required": False,
        },
        {
            "artifact": "log.txt",
            "present": log_txt_path.exists(),
            "details": str(log_txt_path) if log_txt_path.exists() else "missing",
            "required": False,
        },
    ]
    missing_artifacts = [item["artifact"] for item in artifact_checks if not item["present"]]

    central_artifact_missing = (not session_info_path.exists()) or (len(run_parquets) == 0)
    files_checked = [str(session_info_path), str(session_meta_path), str(log_txt_path)] + [str(p) for p in run_parquets]
    runs_found = [p.name for p in run_parquets]

    union_columns: set[str] = set()
    parquet_errors: list[str] = []
    run_columns: dict[str, list[str]] = {}
    for parquet_path in run_parquets:
        columns, error = _read_parquet_schema_columns(parquet_path)
        if error:
            parquet_errors.append(f"{parquet_path.name}: {error}")
            continue
        run_columns[parquet_path.name] = columns
        union_columns.update(columns)

    yaml_text = ""
    yaml_error: str | None = None
    if session_info_path.exists():
        try:
            yaml_text = session_info_path.read_text(encoding="utf-8")
        except Exception as exc:
            yaml_error = f"{type(exc).__name__}: {exc}"

    parsed_yaml: dict[str, Any] = {
        "parser_mode": "unavailable",
        "session_info_current_session_num": None,
        "session_info_sessions": [],
        "weekend_info_session_type": None,
        "known_yaml_paths": [],
        "parser_notes": [],
    }
    yaml_checks: dict[str, Any] = {
        "primary_found": False,
        "primary_value": None,
        "primary_path_used": "",
        "weekend_found": False,
        "weekend_value": None,
        "uncertainty_notes": [],
    }
    if yaml_text and yaml_error is None:
        parsed_yaml = _parse_session_yaml(yaml_text)
        yaml_checks = _resolve_yaml_session_type_checks(parsed_yaml)
        for note in parsed_yaml.get("parser_notes", []):
            yaml_checks["uncertainty_notes"].append(str(note))
    elif yaml_error:
        yaml_checks["uncertainty_notes"].append(f"session_info.yaml konnte nicht gelesen werden: {yaml_error}")

    session_meta, session_meta_error = ({}, None)
    if session_meta_path.exists():
        session_meta, session_meta_error = _read_json_dict(session_meta_path)

    meta_cross_lines: list[str] = []
    if session_meta_error:
        meta_cross_lines.append(f"FAIL session_meta.json parse failed: {session_meta_error}")
    elif session_meta:
        meta_session_type = session_meta.get("SessionType")
        meta_session_type_raw = session_meta.get("session_type_raw")
        meta_cross_lines.append(
            f"session_meta.SessionType: `{meta_session_type}`" if meta_session_type is not None else "FAIL session_meta.SessionType missing"
        )
        if meta_session_type_raw is not None:
            meta_cross_lines.append(f"session_meta.session_type_raw: `{meta_session_type_raw}`")

        yaml_base_for_normalize = yaml_checks.get("primary_value") or yaml_checks.get("weekend_value")
        if yaml_base_for_normalize not in (None, "") and meta_session_type not in (None, ""):
            normalized_yaml = _normalize_session_type(yaml_base_for_normalize)
            if str(meta_session_type) == normalized_yaml:
                meta_cross_lines.append(
                    f"OK YAML->normalized `{normalized_yaml}` matches session_meta.SessionType."
                )
            else:
                meta_cross_lines.append(
                    f"FAIL YAML->normalized `{normalized_yaml}` differs from session_meta.SessionType `{meta_session_type}`."
                )
        else:
            meta_cross_lines.append("No SessionType normalization comparison possible.")
    else:
        meta_cross_lines.append("session_meta.json not available.")

    expected_items = build_expected_items()
    results: list[ItemResult] = []
    near_miss_map: dict[str, list[str]] = {}
    known_yaml_paths = [str(path) for path in parsed_yaml.get("known_yaml_paths", [])]

    for item in expected_items:
        if item.kind == "parquet_column":
            assert item.column_name is not None
            present = item.column_name in union_columns
            details = f"column `{item.column_name}` found in union schema" if present else f"column `{item.column_name}` missing in union schema"
            near = [] if present else _find_near_misses(item.column_name, union_columns)
            if not present:
                near_miss_map[item.key] = near
            results.append(
                ItemResult(
                    key=item.key,
                    category_code=item.category_code,
                    category_title=item.category_title,
                    display_name=item.display_name,
                    kind=item.kind,
                    present_exact=present,
                    details=details,
                    near_miss_candidates=near,
                )
            )
            continue

        if item.kind == "parquet_group":
            matched_variant: tuple[str, ...] | None = None
            for variant in item.required_any_of:
                missing_cols = [col for col in variant if col not in union_columns]
                if not missing_cols:
                    matched_variant = variant
                    break

            present = matched_variant is not None
            if present:
                details = f"all columns present (variant: {', '.join(matched_variant or ())})"
                near = []
            else:
                variant_notes: list[str] = []
                for idx, variant in enumerate(item.required_any_of):
                    missing_cols = [col for col in variant if col not in union_columns]
                    variant_notes.append(
                        f"variant#{idx + 1} missing {len(missing_cols)}/{len(variant)}: {', '.join(missing_cols)}"
                    )
                details = " / ".join(variant_notes)
                all_expected_columns = [col for variant in item.required_any_of for col in variant]
                near = _find_group_near_misses(all_expected_columns, union_columns)
                near_miss_map[item.key] = near

            results.append(
                ItemResult(
                    key=item.key,
                    category_code=item.category_code,
                    category_title=item.category_title,
                    display_name=item.display_name,
                    kind=item.kind,
                    present_exact=present,
                    details=details,
                    near_miss_candidates=near,
                )
            )
            continue

        if item.kind == "yaml_path":
            if item.key == "SessionInfo.Sessions[SessionNum].SessionType":
                present = bool(yaml_checks.get("primary_found"))
                details = (
                    f"path found: `{yaml_checks.get('primary_path_used')}` value=`{yaml_checks.get('primary_value')}`"
                    if present
                    else "path/value missing in session_info.yaml"
                )
                near = [] if present else _find_near_misses(item.key, known_yaml_paths)
                if not present:
                    near_miss_map[item.key] = near
            elif item.key == "WeekendInfo.SessionType":
                present = bool(yaml_checks.get("weekend_found"))
                details = (
                    f"path found: value=`{yaml_checks.get('weekend_value')}`"
                    if present
                    else "path/value missing in session_info.yaml"
                )
                near = [] if present else _find_near_misses(item.key, known_yaml_paths)
                if not present:
                    near_miss_map[item.key] = near
            else:
                present = False
                details = "unsupported yaml path checker"
                near = []

            results.append(
                ItemResult(
                    key=item.key,
                    category_code=item.category_code,
                    category_title=item.category_title,
                    display_name=item.display_name,
                    kind=item.kind,
                    present_exact=present,
                    details=details,
                    near_miss_candidates=near,
                )
            )
            continue

        results.append(
            ItemResult(
                key=item.key,
                category_code=item.category_code,
                category_title=item.category_title,
                display_name=item.display_name,
                kind=item.kind,
                present_exact=False,
                details="unsupported item kind",
            )
        )

    present_exact = [result.key for result in results if result.present_exact]
    missing_exact = [result.key for result in results if not result.present_exact]

    exit_code = 0
    if central_artifact_missing:
        exit_code = 3
    elif missing_exact:
        exit_code = 2

    generated_at_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary = {
        "generated_at_utc": generated_at_utc,
        "expected_count": len(expected_items),
        "present_count": len(present_exact),
        "missing_count": len(missing_exact),
        "exit_code": exit_code,
    }

    json_payload = {
        "session_dir": str(session_dir),
        "generated_at_utc": generated_at_utc,
        "summary": summary,
        "artifact_report": {
            "checks": artifact_checks,
            "missing_artifacts": missing_artifacts,
            "central_artifact_missing": central_artifact_missing,
        },
        "files_checked": files_checked,
        "runs_found": runs_found,
        "run_columns": run_columns,
        "parquet_union_columns": sorted(union_columns),
        "parquet_errors": parquet_errors,
        "yaml_checks": {
            "parser_mode": parsed_yaml.get("parser_mode"),
            "known_yaml_paths": known_yaml_paths,
            "primary_found": yaml_checks.get("primary_found"),
            "primary_path_used": yaml_checks.get("primary_path_used"),
            "primary_value": yaml_checks.get("primary_value"),
            "weekend_found": yaml_checks.get("weekend_found"),
            "weekend_value": yaml_checks.get("weekend_value"),
            "uncertainty_notes": yaml_checks.get("uncertainty_notes", []),
        },
        "meta_cross_check": {
            "lines": meta_cross_lines,
        },
        "expected": [item.key for item in expected_items],
        "expected_details": [
            {
                "key": item.key,
                "category_code": item.category_code,
                "category_title": item.category_title,
                "display_name": item.display_name,
                "kind": item.kind,
                "column_name": item.column_name,
                "required_any_of": [list(group) for group in item.required_any_of],
            }
            for item in expected_items
        ],
        "present_exact": present_exact,
        "missing_exact": missing_exact,
        "near_miss_candidates": near_miss_map,
        "results": [
            {
                "key": item.key,
                "category_code": item.category_code,
                "category_title": item.category_title,
                "display_name": item.display_name,
                "kind": item.kind,
                "present_exact": item.present_exact,
                "details": item.details,
                "near_miss_candidates": item.near_miss_candidates,
            }
            for item in results
        ],
    }

    json_path = output_dir / json_name
    md_path = output_dir / md_name
    json_path.write_text(json.dumps(json_payload, indent=2, ensure_ascii=False), encoding="utf-8")

    yaml_payload = json_payload["yaml_checks"]
    markdown = _generate_markdown_report(
        session_dir=session_dir,
        summary=summary,
        artifact_report=json_payload["artifact_report"],
        results=results,
        near_miss_map=near_miss_map,
        yaml_checks=yaml_payload,
        meta_cross_check=json_payload["meta_cross_check"],
    )
    md_path.write_text(markdown, encoding="utf-8")

    print("=== Coaching Session Audit Summary ===")
    print(f"Session: {session_dir}")
    print(f"Expected: {summary['expected_count']}")
    print(f"Present exact: {summary['present_count']}")
    print(f"Missing exact: {summary['missing_count']}")
    print(f"Runs found: {len(runs_found)}")
    print(f"Report JSON: {json_path}")
    print(f"Report Markdown: {md_path}")
    if parquet_errors:
        print("Parquet schema errors:")
        for err in parquet_errors:
            print(f"- {err}")
    if yaml_payload.get("uncertainty_notes"):
        print("YAML notes:")
        for note in yaml_payload["uncertainty_notes"]:
            print(f"- {note}")
    print(f"Exit code: {exit_code}")
    return exit_code


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Auditiert einen Coaching-Session-Ordner gegen Sprint-1 Vollumfang "
            "(Parquet-Spalten + YAML SessionType-Pfade) und schreibt JSON/Markdown-Reports."
        )
    )
    parser.add_argument(
        "session_dir",
        help="Pfad zum Session-Ordner (enthaelt session_info.yaml, session_meta.json, run_*.parquet).",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Ausgabeordner fuer audit_report.json/.md (Default: session_dir).",
    )
    parser.add_argument("--json-name", default="audit_report.json", help="Dateiname fuer den JSON-Report.")
    parser.add_argument("--md-name", default="audit_report.md", help="Dateiname fuer den Markdown-Report.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    session_dir = Path(args.session_dir).expanduser()
    if not session_dir.exists():
        print(f"Session directory not found: {session_dir}", file=sys.stderr)
        return 3
    output_dir = Path(args.output_dir).expanduser() if args.output_dir else session_dir
    try:
        return run_audit(
            session_dir=session_dir,
            output_dir=output_dir,
            json_name=str(args.json_name),
            md_name=str(args.md_name),
        )
    except Exception as exc:
        print(f"Audit failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
