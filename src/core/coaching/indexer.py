from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any

from core.coaching.storage import ACTIVE_SESSION_LOCK_FILENAME, SESSION_FINALIZED_FILENAME


_RUN_META_RE = re.compile(r"^run_(\d{4})_meta\.json$", re.IGNORECASE)
_RUN_PARQUET_RE = re.compile(r"^run_(\d{4})\.parquet$", re.IGNORECASE)


@dataclass
class NodeSummary:
    total_time_s: float | None = None
    laps: int | None = None
    fastest_lap_s: float | None = None
    last_driven_ts: float | None = None


@dataclass
class CoachingTreeNode:
    id: str
    kind: str
    label: str
    summary: NodeSummary = field(default_factory=NodeSummary)
    path: Path | None = None
    session_path: Path | None = None
    run_id: int | None = None
    children: list["CoachingTreeNode"] = field(default_factory=list)
    can_open_folder: bool = False
    can_delete: bool = False
    delete_paths: tuple[Path, ...] = ()
    is_active_session: bool = False
    is_finalized: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class CoachingIndex:
    root_dir: Path
    tracks: list[CoachingTreeNode]
    nodes_by_id: dict[str, CoachingTreeNode]
    generated_ts: float
    session_count: int = 0
    run_count: int = 0
    lap_count: int = 0


@dataclass
class _RunScan:
    run_id: int
    parquet_path: Path | None
    meta_path: Path | None
    extra_paths: list[Path]
    meta: dict[str, Any]
    lap_segments: list[dict[str, Any]]
    summary: NodeSummary


@dataclass
class _SessionScan:
    session_dir: Path
    folder_name: str
    track: str
    car: str
    session_type: str
    session_id: str
    session_meta: dict[str, Any]
    runs: list[_RunScan]
    has_active_lock: bool
    has_finalized_marker: bool
    last_driven_ts: float | None
    parsed_folder_ts: float | None
    summary: NodeSummary


@dataclass
class _SessionCacheEntry:
    dir_mtime_ns: int
    parsed: _SessionScan


_SESSION_CACHE: dict[str, _SessionCacheEntry] = {}


def scan_storage(root_dir: Path) -> CoachingIndex:
    root = Path(root_dir)
    tracks: list[CoachingTreeNode] = []
    nodes_by_id: dict[str, CoachingTreeNode] = {}
    session_count = 0
    run_count = 0
    lap_count = 0

    if not root.exists() or not root.is_dir():
        return CoachingIndex(
            root_dir=root,
            tracks=[],
            nodes_by_id={},
            generated_ts=_safe_now_ts(),
            session_count=0,
            run_count=0,
            lap_count=0,
        )

    sessions: list[_SessionScan] = []
    live_keys: set[str] = set()
    try:
        candidates = [p for p in root.iterdir() if p.is_dir()]
    except Exception:
        candidates = []
    for session_dir in candidates:
        effective_dir = _maybe_rename_offline_testing_unknown_session_dir(session_dir)
        parsed = _scan_session_dir_cached(effective_dir)
        if parsed is None:
            continue
        key = _cache_key(effective_dir)
        live_keys.add(key)
        sessions.append(parsed)
    _prune_cache(live_keys)

    grouped: dict[str, dict[str, list[_SessionScan]]] = {}
    for session in sessions:
        grouped.setdefault(session.track, {}).setdefault(session.car, []).append(session)
        session_count += 1
        run_count += len(session.runs)
        lap_count += int(session.summary.laps or 0)

    for track_name in sorted(grouped.keys(), key=_sort_key_text):
        car_map = grouped[track_name]
        car_nodes: list[CoachingTreeNode] = []
        for car_name in sorted(car_map.keys(), key=_sort_key_text):
            session_scans = sorted(
                car_map[car_name],
                key=lambda s: (
                    -(s.summary.last_driven_ts or 0.0),
                    _sort_key_text(s.folder_name),
                ),
            )
            event_nodes: list[CoachingTreeNode] = []
            for session in session_scans:
                event_node = _build_session_event_node(session)
                event_nodes.append(event_node)
                _register_tree_nodes(nodes_by_id, event_node)
            car_summary = _aggregate_summary(event_nodes)
            car_node = CoachingTreeNode(
                id=f"car::{track_name}::{car_name}",
                kind="car",
                label=car_name,
                summary=car_summary,
                children=event_nodes,
            )
            car_nodes.append(car_node)
        track_summary = _aggregate_summary(car_nodes)
        track_node = CoachingTreeNode(
            id=f"track::{track_name}",
            kind="track",
            label=track_name,
            summary=track_summary,
            children=car_nodes,
        )
        tracks.append(track_node)

    for track_node in tracks:
        _register_tree_nodes(nodes_by_id, track_node)

    return CoachingIndex(
        root_dir=root,
        tracks=tracks,
        nodes_by_id=nodes_by_id,
        generated_ts=_safe_now_ts(),
        session_count=session_count,
        run_count=run_count,
        lap_count=lap_count,
    )


def _scan_session_dir_cached(session_dir: Path) -> _SessionScan | None:
    key = _cache_key(session_dir)
    try:
        mtime_ns = session_dir.stat().st_mtime_ns
    except Exception:
        return None
    cached = _SESSION_CACHE.get(key)
    if cached is not None and cached.dir_mtime_ns == mtime_ns:
        return cached.parsed
    parsed = _scan_session_dir_uncached(session_dir)
    if parsed is None:
        return None
    _SESSION_CACHE[key] = _SessionCacheEntry(dir_mtime_ns=mtime_ns, parsed=parsed)
    return parsed


def _scan_session_dir_uncached(session_dir: Path) -> _SessionScan | None:
    try:
        children = list(session_dir.iterdir())
    except Exception:
        return None

    parsed_name = _parse_session_folder_name(session_dir.name)
    session_meta_path = session_dir / "session_meta.json"
    session_meta = _read_json_dict(session_meta_path)
    has_active_lock = (session_dir / ACTIVE_SESSION_LOCK_FILENAME).exists()
    has_finalized_marker = (session_dir / SESSION_FINALIZED_FILENAME).exists()

    run_meta_map: dict[int, Path] = {}
    run_parquet_map: dict[int, Path] = {}
    run_extra_map: dict[int, list[Path]] = {}
    for child in children:
        name = child.name
        meta_match = _RUN_META_RE.match(name)
        if meta_match:
            run_id = int(meta_match.group(1))
            run_meta_map[run_id] = child
            continue
        parquet_match = _RUN_PARQUET_RE.match(name)
        if parquet_match:
            run_id = int(parquet_match.group(1))
            run_parquet_map[run_id] = child
            continue

    known_run_ids = sorted(set(run_meta_map.keys()) | set(run_parquet_map.keys()))
    for child in children:
        for run_id in known_run_ids:
            prefix = f"run_{run_id:04d}"
            if not child.name.lower().startswith(prefix.lower()):
                continue
            if child == run_meta_map.get(run_id) or child == run_parquet_map.get(run_id):
                continue
            run_extra_map.setdefault(run_id, []).append(child)
            break

    runs: list[_RunScan] = []
    sample_hz = _coerce_optional_float(session_meta.get("sample_hz"))
    session_last_ts = _best_effort_last_driven_ts(session_dir, parsed_name.folder_ts)

    for run_id in known_run_ids:
        meta_path = run_meta_map.get(run_id)
        parquet_path = run_parquet_map.get(run_id)
        run_meta = _read_json_dict(meta_path) if meta_path is not None else {}
        lap_segments_raw = run_meta.get("lap_segments")
        lap_segments = [seg for seg in lap_segments_raw if isinstance(seg, dict)] if isinstance(lap_segments_raw, list) else []
        run_summary = _compute_run_summary(
            run_meta,
            lap_segments=lap_segments,
            sample_hz=sample_hz,
            fallback_last_ts=session_last_ts,
            parquet_path=parquet_path,
            meta_path=meta_path,
        )
        extra_paths = sorted(run_extra_map.get(run_id, []), key=lambda p: _sort_key_text(p.name))
        runs.append(
            _RunScan(
                run_id=run_id,
                parquet_path=parquet_path,
                meta_path=meta_path,
                extra_paths=extra_paths,
                meta=run_meta,
                lap_segments=lap_segments,
                summary=run_summary,
            )
        )

    runs.sort(key=lambda r: r.run_id)
    session_summary = _compute_session_summary(runs, fallback_last_ts=session_last_ts)
    return _SessionScan(
        session_dir=session_dir,
        folder_name=session_dir.name,
        track=parsed_name.track,
        car=parsed_name.car,
        session_type=_meta_str(session_meta, "SessionType") or parsed_name.session_type,
        session_id=_meta_str(session_meta, "SessionUniqueID") or parsed_name.session_id,
        session_meta=session_meta,
        runs=runs,
        has_active_lock=has_active_lock,
        has_finalized_marker=has_finalized_marker,
        last_driven_ts=session_summary.last_driven_ts or session_last_ts,
        parsed_folder_ts=parsed_name.folder_ts,
        summary=session_summary,
    )


@dataclass
class _ParsedFolderName:
    track: str
    car: str
    session_type: str
    session_id: str
    folder_ts: float | None


def _parse_session_folder_name(folder_name: str) -> _ParsedFolderName:
    parts = str(folder_name or "").split("__")
    if len(parts) >= 6:
        date_part, time_part = parts[0], parts[1]
        track = parts[2] or "Unknown"
        car = parts[3] or "Unknown"
        session_type = parts[4] or "Unknown"
        session_id = "__".join(parts[5:]) or "Unknown"
        ts = _parse_folder_ts(date_part, time_part)
        return _ParsedFolderName(
            track=track,
            car=car,
            session_type=session_type,
            session_id=session_id,
            folder_ts=ts,
        )
    return _ParsedFolderName(
        track="Unknown",
        car="Unknown",
        session_type="Unknown",
        session_id=str(folder_name or "Unknown"),
        folder_ts=None,
    )


def _maybe_rename_offline_testing_unknown_session_dir(session_dir: Path) -> Path:
    path = Path(session_dir)
    parts = path.name.split("__")
    if len(parts) < 6:
        return path
    if str(parts[-1]).strip().lower() != "unknown":
        return path

    meta = _read_json_dict(path / "session_meta.json")
    raw = str(meta.get("session_type_raw") or "").strip()
    if raw.lower() != "offline testing":
        return path

    target_name = "__".join([*parts[:-1], "Offline-Testing"])
    target = path.parent / target_name
    if target.exists():
        return target
    try:
        path.rename(target)
        return target
    except Exception:
        return path


def _parse_folder_ts(date_part: str, time_part: str) -> float | None:
    try:
        dt = datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H%M%S")
    except Exception:
        return None
    try:
        return dt.timestamp()
    except Exception:
        return None


def _read_json_dict(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        return {}
    return {}


def _compute_run_summary(
    run_meta: dict[str, Any],
    *,
    lap_segments: list[dict[str, Any]],
    sample_hz: float | None,
    fallback_last_ts: float | None,
    parquet_path: Path | None,
    meta_path: Path | None,
) -> NodeSummary:
    durations = [d for d in (_lap_duration_seconds(seg) for seg in lap_segments) if d is not None and d >= 0.0]
    total_time = _duration_from_run_meta(run_meta)
    if total_time is None and durations:
        total_time = sum(durations)
    if total_time is None:
        sample_count = _coerce_optional_float(run_meta.get("sample_count"))
        if sample_count is not None and sample_hz and sample_hz > 0:
            total_time = sample_count / sample_hz

    laps = len(lap_segments)
    fastest_lap = min(durations) if durations else None
    last_driven = _max_optional(
        fallback_last_ts,
        _path_mtime_ts(parquet_path),
        _path_mtime_ts(meta_path),
    )
    return NodeSummary(
        total_time_s=total_time,
        laps=laps,
        fastest_lap_s=fastest_lap,
        last_driven_ts=last_driven,
    )


def _compute_session_summary(runs: list[_RunScan], *, fallback_last_ts: float | None) -> NodeSummary:
    if not runs:
        return NodeSummary(
            total_time_s=None,
            laps=0,
            fastest_lap_s=None,
            last_driven_ts=fallback_last_ts,
        )
    total_time_values = [r.summary.total_time_s for r in runs if r.summary.total_time_s is not None]
    lap_counts = [int(r.summary.laps or 0) for r in runs]
    fastest_values = [r.summary.fastest_lap_s for r in runs if r.summary.fastest_lap_s is not None]
    last_values = [r.summary.last_driven_ts for r in runs if r.summary.last_driven_ts is not None]
    return NodeSummary(
        total_time_s=sum(total_time_values) if total_time_values else None,
        laps=sum(lap_counts),
        fastest_lap_s=min(fastest_values) if fastest_values else None,
        last_driven_ts=max(last_values) if last_values else fallback_last_ts,
    )


def _build_session_event_node(session: _SessionScan) -> CoachingTreeNode:
    session_path = session.session_dir
    session_id_str = _stable_path_id(session_path)
    run_nodes: list[CoachingTreeNode] = []
    for run in session.runs:
        run_node = _build_run_node(session, run)
        run_nodes.append(run_node)
    event_label = _session_label(session)
    event_summary = NodeSummary(
        total_time_s=session.summary.total_time_s,
        laps=session.summary.laps,
        fastest_lap_s=session.summary.fastest_lap_s,
        last_driven_ts=session.summary.last_driven_ts or session.last_driven_ts,
    )
    return CoachingTreeNode(
        id=f"session::{session_id_str}",
        kind="event",
        label=event_label,
        summary=event_summary,
        path=session_path,
        session_path=session_path,
        children=run_nodes,
        can_open_folder=True,
        can_delete=True,
        delete_paths=(session_path,),
        is_active_session=session.has_active_lock and not session.has_finalized_marker,
        is_finalized=session.has_finalized_marker,
        meta={
            "folder_name": session.folder_name,
            "track": session.track,
            "car": session.car,
            "session_type": session.session_type,
            "session_id": session.session_id,
            "run_count": len(session.runs),
        },
    )


def _build_run_node(session: _SessionScan, run: _RunScan) -> CoachingTreeNode:
    session_id_str = _stable_path_id(session.session_dir)
    lap_nodes: list[CoachingTreeNode] = []
    for idx, segment in enumerate(run.lap_segments):
        lap_nodes.append(_build_lap_node(session, run, idx, segment))

    delete_paths: list[Path] = []
    if run.parquet_path is not None:
        delete_paths.append(run.parquet_path)
    if run.meta_path is not None:
        delete_paths.append(run.meta_path)
    delete_paths.extend(run.extra_paths)

    return CoachingTreeNode(
        id=f"run::{session_id_str}::{run.run_id:04d}",
        kind="run",
        label=f"Run {run.run_id:04d}",
        summary=run.summary,
        path=run.parquet_path or session.session_dir,
        session_path=session.session_dir,
        run_id=run.run_id,
        children=lap_nodes,
        can_open_folder=True,
        can_delete=bool(delete_paths),
        delete_paths=tuple(delete_paths),
        is_active_session=session.has_active_lock and not session.has_finalized_marker,
        is_finalized=session.has_finalized_marker,
        meta={
            "meta_path": str(run.meta_path) if run.meta_path else "",
            "parquet_path": str(run.parquet_path) if run.parquet_path else "",
            "extra_count": len(run.extra_paths),
        },
    )


def _build_lap_node(session: _SessionScan, run: _RunScan, idx: int, segment: dict[str, Any]) -> CoachingTreeNode:
    session_id_str = _stable_path_id(session.session_dir)
    lap_no = _coerce_optional_int(segment.get("lap_no"))
    lap_label = f"Lap {lap_no}" if lap_no is not None else f"Lap {idx + 1}"
    duration = _lap_duration_seconds(segment)
    summary = NodeSummary(
        total_time_s=duration,
        laps=1,
        fastest_lap_s=duration,
        last_driven_ts=run.summary.last_driven_ts,
    )
    return CoachingTreeNode(
        id=f"lap::{session_id_str}::{run.run_id:04d}::{idx}",
        kind="lap",
        label=lap_label,
        summary=summary,
        session_path=session.session_dir,
        run_id=run.run_id,
        can_open_folder=False,
        can_delete=False,
        meta=dict(segment),
    )


def _register_tree_nodes(nodes_by_id: dict[str, CoachingTreeNode], node: CoachingTreeNode) -> None:
    nodes_by_id[node.id] = node
    for child in node.children:
        _register_tree_nodes(nodes_by_id, child)


def _aggregate_summary(nodes: list[CoachingTreeNode]) -> NodeSummary:
    if not nodes:
        return NodeSummary()
    total_time_values = [n.summary.total_time_s for n in nodes if n.summary.total_time_s is not None]
    laps_total = sum(int(n.summary.laps or 0) for n in nodes)
    fastest_values = [n.summary.fastest_lap_s for n in nodes if n.summary.fastest_lap_s is not None]
    last_values = [n.summary.last_driven_ts for n in nodes if n.summary.last_driven_ts is not None]
    return NodeSummary(
        total_time_s=sum(total_time_values) if total_time_values else None,
        laps=laps_total,
        fastest_lap_s=min(fastest_values) if fastest_values else None,
        last_driven_ts=max(last_values) if last_values else None,
    )


def _session_label(session: _SessionScan) -> str:
    ts_text = _format_dt_short(session.parsed_folder_ts or session.last_driven_ts)
    type_text = str(session.session_type or "Unknown")
    sid = str(session.session_id or session.folder_name)
    label = f"{ts_text}  {type_text}  {sid}".strip()
    if session.has_active_lock and not session.has_finalized_marker:
        return f"{label}  [ACTIVE]"
    return label


def _format_dt_short(ts: float | None) -> str:
    if ts is None:
        return "Unknown time"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "Unknown time"


def _duration_from_run_meta(run_meta: dict[str, Any]) -> float | None:
    start = _coerce_optional_float(run_meta.get("start_session_time"))
    end = _coerce_optional_float(run_meta.get("end_session_time"))
    if start is None or end is None:
        return None
    delta = end - start
    if delta < 0:
        return None
    return delta


def _lap_duration_seconds(segment: dict[str, Any]) -> float | None:
    if not isinstance(segment, dict):
        return None
    start = _coerce_optional_float(segment.get("start_ts"))
    end = _coerce_optional_float(segment.get("end_ts"))
    if start is None or end is None:
        return None
    delta = end - start
    if delta < 0:
        return None
    return delta


def _best_effort_last_driven_ts(session_dir: Path, parsed_folder_ts: float | None) -> float | None:
    return _max_optional(parsed_folder_ts, _path_mtime_ts(session_dir))


def _path_mtime_ts(path: Path | None) -> float | None:
    if path is None:
        return None
    try:
        return path.stat().st_mtime
    except Exception:
        return None


def _meta_str(meta: dict[str, Any], key: str) -> str | None:
    value = meta.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _coerce_optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _sort_key_text(value: Any) -> str:
    return str(value or "").lower()


def _stable_path_id(path: Path) -> str:
    try:
        return str(path.resolve())
    except Exception:
        return str(path)


def _cache_key(path: Path) -> str:
    return _stable_path_id(path)


def _prune_cache(live_keys: set[str]) -> None:
    stale = [key for key in _SESSION_CACHE.keys() if key not in live_keys]
    for key in stale:
        _SESSION_CACHE.pop(key, None)


def _max_optional(*values: float | None) -> float | None:
    valid = [v for v in values if v is not None]
    if not valid:
        return None
    return max(valid)


def _safe_now_ts() -> float:
    try:
        return datetime.now().timestamp()
    except Exception:
        return 0.0
