"""Runtime module for core/coaching/indexer.py."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
from typing import Any

from core.coaching.lap_segmenter import LapSegmenter
from core.coaching.lap_metrics import RunLapMetrics, compute_run_lap_metrics
from core.coaching.storage import ACTIVE_SESSION_LOCK_FILENAME, SESSION_FINALIZED_FILENAME


_RUN_META_RE = re.compile(r"^run_(\d{4})_meta\.json$", re.IGNORECASE)
_RUN_PARQUET_RE = re.compile(r"^run_(\d{4})\.parquet$", re.IGNORECASE)
_RUN_LAP_META_WITH_RUN_RE = re.compile(r"^run_(\d+)_lap_(\d+)_meta\.json$", re.IGNORECASE)
_LAP_META_RE = re.compile(r"^lap_(\d+)_meta\.json$", re.IGNORECASE)
_LOG = logging.getLogger(__name__)
_DEBUG_SAMPLES_FILENAME = "debug_samples.jsonl"
_MIN_VALID_LAP_TIME_S = 30.0
_MIN_VALID_LAP_SAMPLES = 60


@dataclass
class NodeSummary:
    """Container and behavior for Node Summary."""
    total_time_s: float | None = None
    laps: int | None = None
    laps_total_display: int | None = None
    fastest_lap_s: float | None = None
    last_driven_ts: float | None = None
    lap_incomplete: bool = False
    lap_offtrack: bool = False


@dataclass
class CoachingTreeNode:
    """Container and behavior for Coaching Tree Node."""
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
    """Container and behavior for Coaching Index."""
    root_dir: Path
    tracks: list[CoachingTreeNode]
    nodes_by_id: dict[str, CoachingTreeNode]
    generated_ts: float
    session_count: int = 0
    run_count: int = 0
    lap_count: int = 0


@dataclass
class _RunScan:
    """Container and behavior for Run Scan."""
    run_id: int
    parquet_path: Path | None
    meta_path: Path | None
    extra_paths: list[Path]
    meta: dict[str, Any]
    lap_segments: list[dict[str, Any]]
    summary: NodeSummary


@dataclass
class _SessionScan:
    """Container and behavior for Session Scan."""
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
    """Container and behavior for Session Cache Entry."""
    signature: tuple[Any, ...]
    parsed: _SessionScan


_SESSION_CACHE: dict[str, _SessionCacheEntry] = {}


def scan_storage(root_dir: Path) -> CoachingIndex:
    """Scan storage."""
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

    for track_name, car_map in grouped.items():
        car_nodes: list[CoachingTreeNode] = []
        for car_name, session_group in car_map.items():
            session_scans = sorted(
                session_group,
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
        car_nodes.sort(key=lambda node: (-(node.summary.last_driven_ts or 0.0), _sort_key_text(node.label)))
        track_summary = _aggregate_summary(car_nodes)
        track_node = CoachingTreeNode(
            id=f"track::{track_name}",
            kind="track",
            label=track_name,
            summary=track_summary,
            children=car_nodes,
        )
        tracks.append(track_node)
    tracks.sort(key=lambda node: (-(node.summary.last_driven_ts or 0.0), _sort_key_text(node.label)))

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
    """Scan session dir cached."""
    key = _cache_key(session_dir)
    signature, children = _session_cache_signature(session_dir)
    if signature is None:
        return None
    cached = _SESSION_CACHE.get(key)
    if cached is not None and cached.signature == signature:
        return cached.parsed
    parsed = _scan_session_dir_uncached(session_dir, children=children)
    if parsed is None:
        return None
    _SESSION_CACHE[key] = _SessionCacheEntry(signature=signature, parsed=parsed)
    return parsed


def _scan_session_dir_uncached(session_dir: Path, *, children: list[Path] | None = None) -> _SessionScan | None:
    """Scan session dir uncached."""
    if children is None:
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
    run_lap_meta_map: dict[int, dict[int, Path]] = {}
    shared_lap_meta_map: dict[int, Path] = {}
    run_extra_map: dict[int, list[Path]] = {}
    scan_children = sorted(children, key=lambda p: _sort_key_text(p.name))
    for child in scan_children:
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
        lap_meta_info = _parse_lap_meta_filename(name)
        if lap_meta_info is not None:
            lap_run_id, lap_seq = lap_meta_info
            if lap_run_id is None:
                shared_lap_meta_map[lap_seq] = child
            else:
                run_lap_meta_map.setdefault(lap_run_id, {})[lap_seq] = child
            continue

    known_run_ids = sorted(set(run_meta_map.keys()) | set(run_parquet_map.keys()) | set(run_lap_meta_map.keys()))
    if shared_lap_meta_map:
        if len(known_run_ids) == 1:
            only_run_id = known_run_ids[0]
            run_map = run_lap_meta_map.setdefault(only_run_id, {})
            for lap_seq, path in shared_lap_meta_map.items():
                run_map.setdefault(lap_seq, path)
        elif _is_debug_coaching_enabled() and _LOG.isEnabledFor(logging.DEBUG):
            _LOG.debug(
                "coaching.indexer lap-meta shared_ignored session=%s shared_files=%s known_runs=%s",
                session_dir.name,
                len(shared_lap_meta_map),
                known_run_ids,
            )
    for child in scan_children:
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
        run_metrics = compute_run_lap_metrics(
            parquet_path=parquet_path,
            run_meta=run_meta,
            sample_hz=sample_hz,
            fallback_last_ts=session_last_ts,
            meta_path=meta_path,
        )
        lap_segments = [seg.to_dict() for seg in run_metrics.lap_slices]
        lap_meta_paths = run_lap_meta_map.get(run_id, {})
        if _is_debug_coaching_enabled() and _LOG.isEnabledFor(logging.DEBUG):
            _LOG.debug(
                "coaching.indexer lap-meta run=%s/%04d lap_segments=%s lap_meta_files=%s",
                session_dir.name,
                run_id,
                len(lap_segments),
                len(lap_meta_paths),
            )
        lap_meta_status = _apply_lap_meta_to_segments(
            run_id=run_id,
            run_dir=session_dir,
            lap_segments=lap_segments,
            lap_meta_paths=lap_meta_paths,
        )
        _refresh_segment_validity_from_parquet(parquet_path=parquet_path, lap_segments=lap_segments)
        _enforce_unique_lap_numbers(lap_segments)
        run_meta["lap_meta_status"] = lap_meta_status
        _debug_log_run_lap_diagnostics(run_dir=session_dir, run_id=run_id, lap_segments=lap_segments)
        run_summary = _compute_run_summary(
            run_meta,
            lap_segments=lap_segments,
            run_metrics=run_metrics,
            sample_hz=sample_hz,
            fallback_last_ts=session_last_ts,
            parquet_path=parquet_path,
            meta_path=meta_path,
        )
        if _LOG.isEnabledFor(logging.DEBUG):
            _LOG.debug(
                "coaching.indexer run=%s/%04d laps_completed=%s laps_including_current=%s best_valid=%s last_driven_ts=%s source=%s",
                session_dir.name,
                run_id,
                run_summary.laps,
                run_summary.laps_total_display,
                run_summary.fastest_lap_s,
                run_summary.last_driven_ts,
                run_metrics.last_driven_source,
            )
        if _is_debug_coaching_enabled() and _LOG.isEnabledFor(logging.DEBUG):
            stats = _lap_validity_stats(lap_segments)
            _LOG.debug(
                "coaching.indexer validity run=%s/%04d laps_total=%s laps_valid=%s best_valid=%s incomplete=%s offtrack=%s",
                session_dir.name,
                run_id,
                stats["laps_total"],
                stats["laps_valid"],
                stats["best_valid_lap_s"],
                stats["laps_incomplete"],
                stats["laps_offtrack"],
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

    _augment_single_run_from_debug_samples(session_dir=session_dir, runs=runs)

    runs.sort(
        key=lambda r: (
            -(r.summary.last_driven_ts or 0.0),
            _sort_key_text(f"Run {r.run_id:04d}"),
        )
    )
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
    """Container and behavior for Parsed Folder Name."""
    track: str
    car: str
    session_type: str
    session_id: str
    folder_ts: float | None


def _parse_session_folder_name(folder_name: str) -> _ParsedFolderName:
    """Parse session folder name."""
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


def _parse_lap_meta_filename(filename: str) -> tuple[int | None, int] | None:
    """Parse lap meta filename."""
    name = str(filename or "")
    with_run = _RUN_LAP_META_WITH_RUN_RE.match(name)
    if with_run:
        try:
            run_id = int(with_run.group(1))
            lap_seq = int(with_run.group(2))
        except Exception:
            return None
        return (run_id, lap_seq)
    without_run = _LAP_META_RE.match(name)
    if without_run:
        try:
            lap_seq = int(without_run.group(1))
        except Exception:
            return None
        return (None, lap_seq)
    return None


def _maybe_rename_offline_testing_unknown_session_dir(session_dir: Path) -> Path:
    """Implement maybe rename offline testing unknown session dir logic."""
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
    """Parse folder ts."""
    try:
        dt = datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H%M%S")
    except Exception:
        return None
    try:
        return dt.timestamp()
    except Exception:
        return None


def _read_json_dict(path: Path | None) -> dict[str, Any]:
    """Read json dict."""
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
    run_metrics: RunLapMetrics,
    sample_hz: float | None,
    fallback_last_ts: float | None,
    parquet_path: Path | None,
    meta_path: Path | None,
) -> NodeSummary:
    """Compute run summary."""
    complete_durations = [
        d
        for d in (
            _lap_duration_seconds(seg)
            for seg in lap_segments
            if not _segment_lap_incomplete(seg)
        )
        if d is not None and d >= 0.0
    ]
    total_time = _duration_from_run_meta(run_meta)
    if total_time is None:
        total_time = run_metrics.total_time_s
    if total_time is None and complete_durations:
        total_time = sum(complete_durations)
    if total_time is None:
        sample_count = _coerce_optional_float(run_meta.get("sample_count"))
        if sample_count is not None and sample_hz and sample_hz > 0:
            total_time = sample_count / sample_hz

    laps_completed = _count_completed_laps(lap_segments)
    laps_total_display = max(int(run_metrics.laps_including_current), len(lap_segments), laps_completed)
    fastest_lap = _best_valid_lap_from_segments(lap_segments)
    last_driven = _max_optional(
        run_metrics.last_driven_ts,
        fallback_last_ts,
        _path_mtime_ts(parquet_path),
        _path_mtime_ts(meta_path),
    )
    return NodeSummary(
        total_time_s=total_time,
        laps=laps_completed,
        laps_total_display=laps_total_display,
        fastest_lap_s=fastest_lap,
        last_driven_ts=last_driven,
    )


def _compute_session_summary(runs: list[_RunScan], *, fallback_last_ts: float | None) -> NodeSummary:
    """Compute session summary."""
    if not runs:
        return NodeSummary(
            total_time_s=None,
            laps=0,
            laps_total_display=0,
            fastest_lap_s=None,
            last_driven_ts=fallback_last_ts,
        )
    total_time_values = [r.summary.total_time_s for r in runs if r.summary.total_time_s is not None]
    lap_counts = [int(r.summary.laps or 0) for r in runs]
    lap_total_display_counts = [
        int(r.summary.laps_total_display if r.summary.laps_total_display is not None else (r.summary.laps or 0))
        for r in runs
    ]
    fastest_values = [r.summary.fastest_lap_s for r in runs if r.summary.fastest_lap_s is not None]
    last_values = [r.summary.last_driven_ts for r in runs if r.summary.last_driven_ts is not None]
    return NodeSummary(
        total_time_s=sum(total_time_values) if total_time_values else None,
        laps=sum(lap_counts),
        laps_total_display=sum(lap_total_display_counts),
        fastest_lap_s=min(fastest_values) if fastest_values else None,
        last_driven_ts=max(last_values) if last_values else fallback_last_ts,
    )


def _augment_single_run_from_debug_samples(*, session_dir: Path, runs: list[_RunScan]) -> None:
    # Backfill for sessions where recorder produced only one run file but debug stream shows more laps.
    """Implement augment single run from debug samples logic."""
    if len(runs) != 1:
        return
    snapshot = _read_debug_lap_snapshot(session_dir)
    if not snapshot:
        return

    run = runs[0]
    observed_completed = _coerce_optional_int(snapshot.get("lap_completed"))
    observed_lap = _coerce_optional_int(snapshot.get("lap"))
    if observed_completed is None and observed_lap is None:
        return

    current_completed = int(run.summary.laps or 0)
    current_total = int(run.summary.laps_total_display if run.summary.laps_total_display is not None else (run.summary.laps or 0))
    target_completed = max(current_completed, observed_completed or 0)
    target_total = max(current_total, target_completed, observed_lap or 0)
    if target_total <= current_total and target_completed <= current_completed:
        return

    run.lap_segments = _expand_lap_segments_from_counters(
        existing_segments=run.lap_segments,
        target_completed=target_completed,
        target_total=target_total,
    )
    run.summary.laps = _count_completed_laps(run.lap_segments)
    run.summary.laps_total_display = max(target_total, len(run.lap_segments))
    run.summary.fastest_lap_s = _best_valid_lap_from_segments(run.lap_segments)


def _expand_lap_segments_from_counters(
    *,
    existing_segments: list[dict[str, Any]],
    target_completed: int,
    target_total: int,
) -> list[dict[str, Any]]:
    """Implement expand lap segments from counters logic."""
    by_lap_no: dict[int, dict[str, Any]] = {}
    for seg in existing_segments:
        if not isinstance(seg, dict):
            continue
        lap_no = _coerce_optional_int(seg.get("lap_no"))
        if lap_no is None or lap_no <= 0:
            continue
        by_lap_no.setdefault(lap_no, dict(seg))

    expanded: list[dict[str, Any]] = []
    for lap_no in range(1, target_total + 1):
        seg = by_lap_no.get(lap_no)
        if seg is None:
            expanded.append(
                {
                    "lap_no": lap_no,
                    "start_idx": None,
                    "end_idx": None,
                    "start_ts": None,
                    "end_ts": None,
                    "duration_s": None,
                    "sample_count": 0,
                    "reason": "debug_samples_counter_backfill"
                    if lap_no <= target_completed
                    else "current_incomplete(debug_samples)",
                    "is_complete": lap_no <= target_completed,
                    "is_valid": None,
                    "lap_incomplete": lap_no > target_completed,
                    "lap_offtrack": False,
                }
            )
            continue

        seg = dict(seg)
        seg["lap_no"] = lap_no
        if lap_no <= target_completed:
            seg["is_complete"] = True
            seg["lap_incomplete"] = False
            reason = str(seg.get("reason") or "")
            if reason.lower().startswith("current_incomplete"):
                seg["reason"] = "debug_samples_counter_backfill"
        else:
            seg["is_complete"] = False
            seg["lap_incomplete"] = True
            seg["reason"] = "current_incomplete(debug_samples)"
        if "lap_offtrack" not in seg:
            seg["lap_offtrack"] = False
        expanded.append(seg)
    return expanded


def _read_debug_lap_snapshot(session_dir: Path) -> dict[str, Any] | None:
    """Read debug lap snapshot."""
    path = session_dir / _DEBUG_SAMPLES_FILENAME
    if not path.exists():
        return None
    try:
        size = int(path.stat().st_size)
    except Exception:
        return None
    if size <= 0:
        return None

    for window_size in (256 * 1024, 1024 * 1024, 4 * 1024 * 1024):
        start = max(0, size - window_size)
        try:
            with path.open("rb") as fh:
                if start > 0:
                    fh.seek(start)
                raw = fh.read()
        except Exception:
            return None
        text = raw.decode("utf-8", errors="ignore")
        lines = text.splitlines()
        if start > 0 and lines:
            lines = lines[1:]
        snapshot = _extract_debug_lap_snapshot_from_lines(lines)
        if snapshot is not None:
            return snapshot
    return None


def _extract_debug_lap_snapshot_from_lines(lines: list[str]) -> dict[str, Any] | None:
    """Extract debug lap snapshot from lines."""
    for line in reversed(lines):
        text = str(line or "").strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue

        probe = obj.get("probe")
        raw = obj.get("raw")
        probe_dict = probe if isinstance(probe, dict) else {}
        raw_dict = raw if isinstance(raw, dict) else {}
        lap = _coerce_optional_int(probe_dict.get("Lap"))
        if lap is None:
            lap = _coerce_optional_int(raw_dict.get("Lap"))
        lap_completed = _coerce_optional_int(probe_dict.get("LapCompleted"))
        if lap_completed is None:
            lap_completed = _coerce_optional_int(raw_dict.get("LapCompleted"))
        if lap is None and lap_completed is None:
            continue
        lap_best = _coerce_optional_float(probe_dict.get("LapBestLapTime"))
        if lap_best is None:
            lap_best = _coerce_optional_float(raw_dict.get("LapBestLapTime"))
        return {
            "lap": lap,
            "lap_completed": lap_completed,
            "lap_best_lap_time_s": lap_best,
        }
    return None


def _build_session_event_node(session: _SessionScan) -> CoachingTreeNode:
    """Build and return session event node."""
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
        laps_total_display=session.summary.laps_total_display,
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
    """Build and return run node."""
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

    has_validity_meta = _has_valid_lap_metadata(run.lap_segments)
    best_valid_display: float | str
    if run.summary.fastest_lap_s is not None:
        best_valid_display = run.summary.fastest_lap_s
    elif has_validity_meta:
        best_valid_display = "na"
    else:
        best_valid_display = "na / unknown validity"
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
            "laps_completed": int(run.summary.laps or 0),
            "laps_including_current": int(
                run.summary.laps_total_display if run.summary.laps_total_display is not None else (run.summary.laps or 0)
            ),
            "best_valid_lap_s": best_valid_display,
            "lap_meta_available": has_validity_meta,
        },
    )


def _build_lap_node(session: _SessionScan, run: _RunScan, idx: int, segment: dict[str, Any]) -> CoachingTreeNode:
    """Build and return lap node."""
    session_id_str = _stable_path_id(session.session_dir)
    lap_no = _coerce_optional_int(segment.get("lap_no"))
    lap_incomplete = _segment_lap_incomplete(segment)
    lap_offtrack = _segment_lap_offtrack(segment)
    is_current = lap_incomplete or str(segment.get("reason") or "").lower().startswith("current_incomplete")
    if is_current and lap_no is not None:
        lap_label = f"Lap {lap_no} (current)"
    elif is_current:
        lap_label = "Current (incomplete)"
    else:
        lap_label = f"Lap {lap_no}" if lap_no is not None else f"Lap {idx + 1}"
    duration = _lap_duration_seconds(segment)
    lap_valid = _segment_lap_valid(segment)
    best_valid = duration if (duration is not None and lap_valid) else None
    summary = NodeSummary(
        total_time_s=duration,
        laps=0 if lap_incomplete else 1,
        laps_total_display=1,
        fastest_lap_s=best_valid,
        last_driven_ts=run.summary.last_driven_ts,
        lap_incomplete=lap_incomplete,
        lap_offtrack=lap_offtrack,
    )
    lap_meta = dict(segment)
    lap_meta["lap_incomplete"] = lap_incomplete
    lap_meta["lap_offtrack"] = lap_offtrack
    lap_meta["lap_summary"] = _build_normalized_lap_summary(
        segment=segment,
        lap_time_s=duration,
        lap_incomplete=lap_incomplete,
        lap_offtrack=lap_offtrack,
        lap_valid=lap_valid,
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
        meta=lap_meta,
    )


def _register_tree_nodes(nodes_by_id: dict[str, CoachingTreeNode], node: CoachingTreeNode) -> None:
    """Implement register tree nodes logic."""
    nodes_by_id[node.id] = node
    for child in node.children:
        _register_tree_nodes(nodes_by_id, child)


def _aggregate_summary(nodes: list[CoachingTreeNode]) -> NodeSummary:
    """Implement aggregate summary logic."""
    if not nodes:
        return NodeSummary()
    total_time_values = [n.summary.total_time_s for n in nodes if n.summary.total_time_s is not None]
    laps_total = sum(int(n.summary.laps or 0) for n in nodes)
    laps_total_display = sum(
        int(n.summary.laps_total_display if n.summary.laps_total_display is not None else (n.summary.laps or 0))
        for n in nodes
    )
    fastest_values = [n.summary.fastest_lap_s for n in nodes if n.summary.fastest_lap_s is not None]
    last_values = [n.summary.last_driven_ts for n in nodes if n.summary.last_driven_ts is not None]
    return NodeSummary(
        total_time_s=sum(total_time_values) if total_time_values else None,
        laps=laps_total,
        laps_total_display=laps_total_display,
        fastest_lap_s=min(fastest_values) if fastest_values else None,
        last_driven_ts=max(last_values) if last_values else None,
    )


# Coaching Browser lap validity semantics:
# - lap_incomplete: lap is not a fully committed lap for browser/best-time purposes.
# - lap_offtrack: lap had at least one offtrack sample.
# - best(valid): only laps with lap_incomplete=False and lap_offtrack=False.
def _segment_lap_incomplete(segment: dict[str, Any]) -> bool:
    """Implement segment lap incomplete logic."""
    lap_complete = _coerce_optional_bool(segment.get("lap_complete"))
    if lap_complete is not None:
        incomplete = not lap_complete
    else:
        explicit = _coerce_optional_bool(segment.get("lap_incomplete"))
        if explicit is not None:
            incomplete = explicit
        else:
            incomplete = not bool(segment.get("is_complete", True))
    if incomplete:
        return True
    return _segment_is_fragment(segment)


def _segment_lap_offtrack(segment: dict[str, Any]) -> bool:
    """Implement segment lap offtrack logic."""
    surface = _coerce_optional_bool(segment.get("offtrack_surface"))
    if surface is not None:
        return surface
    explicit = _coerce_optional_bool(segment.get("lap_offtrack"))
    if explicit is not None:
        return explicit
    # Backward-compatible fallback: if only validity exists, treat explicit invalid as offtrack.
    is_valid = _coerce_optional_bool(segment.get("is_valid"))
    return is_valid is False


def _segment_lap_valid(segment: dict[str, Any]) -> bool:
    """Implement segment lap valid logic."""
    explicit = _coerce_optional_bool(segment.get("valid_lap"))
    lap_incomplete = _segment_lap_incomplete(segment)
    lap_offtrack = _segment_lap_offtrack(segment)
    incident_delta = _coerce_optional_int(segment.get("incident_delta"))
    if incident_delta is None or incident_delta < 0:
        incident_delta = 0
    lap_no = _coerce_optional_int(segment.get("lap_no"))
    passes_sanity = _segment_passes_sanity(segment)
    if explicit is None:
        explicit = not lap_incomplete and not lap_offtrack and incident_delta == 0
    return bool(
        explicit
        and not lap_incomplete
        and not lap_offtrack
        and incident_delta == 0
        and passes_sanity
        and lap_no != 0
    )


def _segment_is_fragment(segment: dict[str, Any]) -> bool:
    """Implement segment is fragment logic."""
    duration = _lap_duration_seconds(segment)
    if duration is not None and duration < _MIN_VALID_LAP_TIME_S:
        return True
    sample_count = _segment_sample_count(segment)
    if sample_count is not None and sample_count < _MIN_VALID_LAP_SAMPLES:
        return True
    return False


def _segment_passes_sanity(segment: dict[str, Any]) -> bool:
    """Implement segment passes sanity logic."""
    duration = _lap_duration_seconds(segment)
    if duration is None or duration < _MIN_VALID_LAP_TIME_S:
        return False
    sample_count = _segment_sample_count(segment)
    if sample_count is not None and sample_count < _MIN_VALID_LAP_SAMPLES:
        return False
    return True


def _segment_sample_count(segment: dict[str, Any]) -> int | None:
    """Implement segment sample count logic."""
    sample_count = _coerce_optional_int(segment.get("sample_count"))
    if sample_count is not None and sample_count >= 0:
        return sample_count
    start_idx = _coerce_optional_int(segment.get("start_sample"))
    end_idx = _coerce_optional_int(segment.get("end_sample"))
    if start_idx is None or end_idx is None:
        start_idx = _coerce_optional_int(segment.get("start_idx"))
        end_idx = _coerce_optional_int(segment.get("end_idx"))
    if start_idx is None or end_idx is None or end_idx < start_idx:
        return None
    return int(end_idx - start_idx + 1)


def _refresh_segment_validity_from_parquet(*, parquet_path: Path | None, lap_segments: list[dict[str, Any]]) -> None:
    """Implement refresh segment validity from parquet logic."""
    if parquet_path is None or not parquet_path.exists() or not lap_segments:
        return
    try:
        import pyarrow.parquet as pq  # type: ignore
    except Exception:
        return

    try:
        table = pq.read_table(
            parquet_path,
            columns=["PlayerTrackSurface", "PlayerCarMyIncidentCount", "OnPitRoad", "IsOnTrackCar"],
        )
    except Exception:
        return

    row_count = int(table.num_rows)
    if row_count <= 0:
        return
    names = set(table.schema.names)
    track_values = table.column("PlayerTrackSurface").to_pylist() if "PlayerTrackSurface" in names else [None] * row_count
    incident_values = (
        table.column("PlayerCarMyIncidentCount").to_pylist() if "PlayerCarMyIncidentCount" in names else [None] * row_count
    )
    pit_values = table.column("OnPitRoad").to_pylist() if "OnPitRoad" in names else [None] * row_count
    on_track_values = table.column("IsOnTrackCar").to_pylist() if "IsOnTrackCar" in names else [None] * row_count

    for segment in lap_segments:
        if not isinstance(segment, dict):
            continue
        bounds = _segment_sample_bounds(segment, row_count=row_count)
        if bounds is None:
            continue
        start_idx, end_idx = bounds
        if end_idx < start_idx:
            continue
        sample_count = int(end_idx - start_idx + 1)
        segment["sample_count"] = sample_count
        slice_tracks = track_values[start_idx : end_idx + 1]
        slice_incidents = incident_values[start_idx : end_idx + 1]
        slice_pit = pit_values[start_idx : end_idx + 1]
        slice_on_track = on_track_values[start_idx : end_idx + 1]

        offtrack_surface = False
        track_min: int | None = None
        track_max: int | None = None
        track_counter: dict[int, int] = {}
        for idx, raw_surface in enumerate(slice_tracks):
            enum_value = _coerce_optional_int(raw_surface)
            on_pit_road = _coerce_optional_bool(slice_pit[idx]) if idx < len(slice_pit) else None
            is_on_track_car = _coerce_optional_bool(slice_on_track[idx]) if idx < len(slice_on_track) else None
            surface_class = LapSegmenter.classify_track_surface(
                raw_surface,
                on_pit_road=on_pit_road,
                is_on_track_car=is_on_track_car,
            )
            if surface_class == "OFF_TRACK":
                offtrack_surface = True
            if enum_value is None:
                continue
            if track_min is None or enum_value < track_min:
                track_min = enum_value
            if track_max is None or enum_value > track_max:
                track_max = enum_value
            track_counter[enum_value] = int(track_counter.get(enum_value, 0)) + 1

        incident_min: int | None = None
        incident_max: int | None = None
        for raw_incident in slice_incidents:
            incident = _coerce_optional_int(raw_incident)
            if incident is None:
                continue
            if incident_min is None or incident < incident_min:
                incident_min = incident
            if incident_max is None or incident > incident_max:
                incident_max = incident
        incident_delta = 0
        if incident_min is not None and incident_max is not None:
            incident_delta = max(0, int(incident_max - incident_min))

        segment["offtrack_surface"] = bool(offtrack_surface)
        segment["lap_offtrack"] = bool(offtrack_surface)
        segment["incident_delta"] = int(incident_delta)
        if track_min is not None:
            segment["track_surface_min"] = int(track_min)
        if track_max is not None:
            segment["track_surface_max"] = int(track_max)
        if track_counter:
            top3 = sorted(track_counter.items(), key=lambda kv: (-int(kv[1]), int(kv[0])))[:3]
            segment["track_surface_values"] = [[int(v), int(c)] for v, c in top3]
        if incident_min is not None:
            segment["incident_min"] = int(incident_min)
        if incident_max is not None:
            segment["incident_max"] = int(incident_max)

        lap_complete = _coerce_optional_bool(segment.get("lap_complete"))
        if lap_complete is None:
            lap_incomplete = _coerce_optional_bool(segment.get("lap_incomplete"))
            if lap_incomplete is not None:
                lap_complete = not lap_incomplete
        if lap_complete is None:
            lap_complete = bool(segment.get("is_complete", False))
        if bool(lap_complete) and not _segment_passes_sanity(segment):
            lap_complete = False
        segment["lap_complete"] = bool(lap_complete)
        segment["is_complete"] = bool(lap_complete)
        segment["lap_incomplete"] = not bool(lap_complete)
        valid_lap = bool(
            bool(lap_complete)
            and _segment_passes_sanity(segment)
            and not bool(offtrack_surface)
            and int(incident_delta) == 0
            and _coerce_optional_int(segment.get("lap_no")) != 0
        )
        segment["valid_lap"] = bool(valid_lap)
        segment["is_valid"] = bool(valid_lap)


def _segment_sample_bounds(segment: dict[str, Any], *, row_count: int) -> tuple[int, int] | None:
    """Implement segment sample bounds logic."""
    start_idx = _coerce_optional_int(segment.get("start_sample"))
    end_idx = _coerce_optional_int(segment.get("end_sample"))
    if start_idx is None or end_idx is None:
        start_idx = _coerce_optional_int(segment.get("start_idx"))
        end_idx = _coerce_optional_int(segment.get("end_idx"))
    if start_idx is None or end_idx is None:
        return None
    if start_idx < 0:
        start_idx = 0
    if end_idx >= row_count:
        end_idx = row_count - 1
    if end_idx < start_idx:
        return None
    return (int(start_idx), int(end_idx))


def _enforce_unique_lap_numbers(lap_segments: list[dict[str, Any]]) -> None:
    """Implement enforce unique lap numbers logic."""
    canonical_index_by_lap_no: dict[int, int] = {}
    for idx, segment in enumerate(lap_segments):
        if not isinstance(segment, dict):
            continue
        lap_no = _coerce_optional_int(segment.get("lap_no"))
        if lap_no is None:
            continue
        canonical_idx = canonical_index_by_lap_no.get(lap_no)
        if canonical_idx is None:
            canonical_index_by_lap_no[lap_no] = idx
            continue
        canonical_segment = lap_segments[canonical_idx]
        if not isinstance(canonical_segment, dict):
            canonical_index_by_lap_no[lap_no] = idx
            continue
        if _segment_duration_sort_key(segment) > _segment_duration_sort_key(canonical_segment):
            _mark_segment_duplicate_fragment(canonical_segment, lap_no=lap_no)
            canonical_index_by_lap_no[lap_no] = idx
        else:
            _mark_segment_duplicate_fragment(segment, lap_no=lap_no)


def _segment_duration_sort_key(segment: dict[str, Any]) -> tuple[float, int]:
    """Implement segment duration sort key logic."""
    duration = _lap_duration_seconds(segment)
    sample_count = _segment_sample_count(segment)
    return (float(duration) if duration is not None else -1.0, int(sample_count) if sample_count is not None else -1)


def _mark_segment_duplicate_fragment(segment: dict[str, Any], *, lap_no: int) -> None:
    """Implement mark segment duplicate fragment logic."""
    segment["lap_complete"] = False
    segment["is_complete"] = False
    segment["lap_incomplete"] = True
    segment["valid_lap"] = False
    segment["is_valid"] = False
    segment["fragment"] = True
    segment["fragment_lap_no"] = int(lap_no)
    segment.pop("lap_no", None)
    segment.pop("lap_num", None)
    reason = str(segment.get("reason") or "")
    if "fragment_duplicate_lap_no" not in reason:
        segment["reason"] = f"{reason}|fragment_duplicate_lap_no" if reason else "fragment_duplicate_lap_no"


def _count_completed_laps(lap_segments: list[dict[str, Any]]) -> int:
    """Implement count completed laps logic."""
    return sum(1 for segment in lap_segments if isinstance(segment, dict) and not _segment_lap_incomplete(segment))


def _best_valid_lap_from_segments(lap_segments: list[dict[str, Any]]) -> float | None:
    """Implement best valid lap from segments logic."""
    valid_by_lap_no: dict[int, float] = {}
    valid_without_lap_no: list[float] = []
    for segment in lap_segments:
        if not isinstance(segment, dict):
            continue
        duration = _lap_duration_seconds(segment)
        if duration is None or duration < 0.0 or not _segment_lap_valid(segment):
            continue
        lap_no = _coerce_optional_int(segment.get("lap_no"))
        if lap_no is None:
            valid_without_lap_no.append(duration)
            continue
        previous = valid_by_lap_no.get(lap_no)
        if previous is None or duration > previous:
            valid_by_lap_no[lap_no] = duration
    valid_times = list(valid_by_lap_no.values()) + valid_without_lap_no
    if not valid_times:
        return None
    return min(valid_times)


def _lap_validity_stats(lap_segments: list[dict[str, Any]]) -> dict[str, Any]:
    """Implement lap validity stats logic."""
    total = 0
    incomplete = 0
    offtrack = 0
    valid = 0
    for segment in lap_segments:
        if not isinstance(segment, dict):
            continue
        total += 1
        is_incomplete = _segment_lap_incomplete(segment)
        is_offtrack = _segment_lap_offtrack(segment)
        if is_incomplete:
            incomplete += 1
        if is_offtrack:
            offtrack += 1
        if _segment_lap_valid(segment):
            valid += 1
    return {
        "laps_total": total,
        "laps_valid": valid,
        "laps_incomplete": incomplete,
        "laps_offtrack": offtrack,
        "best_valid_lap_s": _best_valid_lap_from_segments(lap_segments) if total > 0 else None,
    }


def _has_valid_lap_metadata(lap_segments: list[dict[str, Any]]) -> bool:
    """Return whether valid lap metadata."""
    for segment in lap_segments:
        if not isinstance(segment, dict):
            continue
        if _coerce_optional_bool(segment.get("valid_lap")) is not None:
            return True
    return False


def _apply_lap_meta_to_segments(
    *,
    run_id: int,
    run_dir: Path,
    lap_segments: list[dict[str, Any]],
    lap_meta_paths: dict[int, Path],
) -> dict[str, Any]:
    """Apply lap meta to segments."""
    found_files = 0
    applied_segments = 0
    validity_applied = 0
    unresolved_files = 0
    merged_paths_by_index: dict[int, Path] = {}
    for lap_seq, path in sorted(lap_meta_paths.items(), key=lambda item: int(item[0])):
        data = _read_json_dict(path)
        if not isinstance(data, dict) or not data:
            continue
        found_files += 1
        lap_index = _resolve_lap_meta_segment_index(
            lap_segments=lap_segments,
            lap_seq=lap_seq,
            data=data,
        )
        if lap_index is None:
            unresolved_files += 1
            continue
        if lap_index < 0 or lap_index >= len(lap_segments):
            unresolved_files += 1
            continue
        segment = lap_segments[lap_index]
        if not isinstance(segment, dict):
            continue
        applied_segments += 1
        merged_paths_by_index[lap_index] = path
        segment["lap_meta_path"] = str(path)
        if _coerce_optional_int(segment.get("lap_index")) is None:
            segment["lap_index"] = int(lap_index)

        lap_num = _coerce_optional_int(data.get("lap_num"))
        if lap_num is None:
            lap_num = _coerce_optional_int(data.get("lap_no"))
        if lap_num is not None:
            if _coerce_optional_int(segment.get("lap_no")) is None:
                segment["lap_no"] = int(lap_num)
            segment["lap_num"] = int(lap_num)

        for source_key, target_key in (
            ("lap_start_sample", "start_sample"),
            ("lap_end_sample", "end_sample"),
            ("lap_start_ts", "start_ts"),
            ("lap_end_ts", "end_ts"),
        ):
            source_value = data.get(source_key)
            if source_value is not None:
                segment[target_key] = source_value

        lap_complete = _coerce_optional_bool(data.get("lap_complete"))
        offtrack_surface = _coerce_optional_bool(data.get("offtrack_surface"))
        incident_delta = _coerce_optional_int(data.get("incident_delta"))
        valid_lap = _coerce_optional_bool(data.get("valid_lap"))
        if incident_delta is not None and incident_delta < 0:
            incident_delta = 0

        if lap_complete is not None:
            segment["lap_complete"] = bool(lap_complete)
            segment["is_complete"] = bool(lap_complete)
            if _coerce_optional_bool(segment.get("lap_incomplete")) is None:
                segment["lap_incomplete"] = not bool(lap_complete)
        if offtrack_surface is not None:
            segment["offtrack_surface"] = bool(offtrack_surface)
            segment["lap_offtrack"] = bool(offtrack_surface)
        if incident_delta is not None:
            segment["incident_delta"] = int(incident_delta)
        if valid_lap is None and lap_complete is not None and offtrack_surface is not None and incident_delta is not None:
            valid_lap = bool(
                lap_complete
                and not offtrack_surface
                and incident_delta == 0
                and _segment_passes_sanity(segment)
                and _coerce_optional_int(segment.get("lap_no")) != 0
            )
        if valid_lap is not None:
            segment["valid_lap"] = bool(valid_lap)
            segment["is_valid"] = bool(valid_lap)
            validity_applied += 1
    if _is_debug_coaching_enabled() and _LOG.isEnabledFor(logging.DEBUG):
        missing_logged = 0
        for idx, segment in enumerate(lap_segments):
            has_meta = idx in merged_paths_by_index
            should_log = idx < 3
            if not has_meta and missing_logged < 3:
                should_log = True
                missing_logged += 1
            if not should_log:
                continue
            lap_no = _coerce_optional_int(segment.get("lap_no")) if isinstance(segment, dict) else None
            expected_path = merged_paths_by_index.get(idx)
            if expected_path is None:
                expected_path = run_dir / f"run_{run_id:04d}_lap_{idx + 1:04d}_meta.json"
            expected_exists = False
            try:
                expected_exists = expected_path.exists()
            except Exception:
                expected_exists = False
            present_keys: list[str] = []
            if isinstance(segment, dict):
                present_keys = [
                    key
                    for key in (
                        "duration_s",
                        "start_ts",
                        "end_ts",
                        "lap_complete",
                        "offtrack_surface",
                        "incident_delta",
                        "valid_lap",
                        "lap_incomplete",
                        "lap_offtrack",
                    )
                    if key in segment
                ]
            _LOG.debug(
                "coaching.indexer lap-meta merge run=%s/%04d lap_no=%s lap_idx=%s expected=%s exists=%s keys=%s",
                run_dir.name,
                run_id,
                lap_no,
                idx,
                str(expected_path),
                expected_exists,
                ",".join(present_keys),
            )

    return {
        "found_files": found_files,
        "applied_segments": applied_segments,
        "validity_applied": validity_applied,
        "unresolved_files": unresolved_files,
    }


def _debug_log_run_lap_diagnostics(*, run_dir: Path, run_id: int, lap_segments: list[dict[str, Any]]) -> None:
    """Implement debug log run lap diagnostics logic."""
    if not _is_debug_lap_diagnostics_enabled() or not _LOG.isEnabledFor(logging.DEBUG):
        return
    surface_counts = _collect_debug_surface_counts(run_dir)
    if surface_counts:
        values = sorted((int(value), int(count)) for value, count in surface_counts.items())
        _LOG.debug("coaching.indexer run=%s/%04d debug_samples track_surface_values=%s", run_dir.name, run_id, values)
    for idx, segment in enumerate(lap_segments):
        if not isinstance(segment, dict):
            continue
        lap_no = _coerce_optional_int(segment.get("lap_no"))
        lap_time_s = _lap_duration_seconds(segment)
        lap_complete = _coerce_optional_bool(segment.get("lap_complete"))
        offtrack_surface = _coerce_optional_bool(segment.get("offtrack_surface"))
        incident_delta = _coerce_optional_int(segment.get("incident_delta"))
        valid_lap = _coerce_optional_bool(segment.get("valid_lap"))
        sample_count = _segment_sample_count(segment)
        _LOG.debug(
            "coaching.indexer run=%s/%04d lap_idx=%s lap_no=%s lap_time_s=%s lap_complete=%s offtrack_surface=%s incident_delta=%s valid_lap=%s sample_count=%s track_min=%s track_max=%s incidents=%s/%s",
            run_dir.name,
            run_id,
            idx,
            lap_no,
            lap_time_s,
            lap_complete,
            offtrack_surface,
            incident_delta,
            valid_lap,
            sample_count,
            segment.get("track_surface_min"),
            segment.get("track_surface_max"),
            segment.get("incident_min"),
            segment.get("incident_max"),
        )


def _collect_debug_surface_counts(run_dir: Path) -> dict[int, int]:
    """Collect debug surface counts."""
    path = run_dir / _DEBUG_SAMPLES_FILENAME
    if not path.exists():
        return {}
    counts: dict[int, int] = {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                text = str(line or "").strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                raw = obj.get("raw")
                if not isinstance(raw, dict):
                    continue
                value = _coerce_optional_int(raw.get("PlayerTrackSurface"))
                if value is None:
                    continue
                counts[value] = int(counts.get(value, 0)) + 1
    except Exception:
        return {}
    return counts


def _resolve_lap_meta_segment_index(
    *,
    lap_segments: list[dict[str, Any]],
    lap_seq: int,
    data: dict[str, Any],
) -> int | None:
    """Resolve lap meta segment index."""
    explicit_index = _coerce_optional_int(data.get("lap_index"))
    if explicit_index is not None and 0 <= explicit_index < len(lap_segments):
        return explicit_index
    if explicit_index is not None and explicit_index == len(lap_segments) and len(lap_segments) > 0:
        return len(lap_segments) - 1

    candidate_lap_values: list[int] = []
    for key in ("lap_no", "lap_num"):
        value = _coerce_optional_int(data.get(key))
        if value is not None:
            candidate_lap_values.append(int(value))
    for value in candidate_lap_values:
        matched = _match_segment_index_by_lap_no(lap_segments=lap_segments, lap_value=value)
        if matched is not None:
            return matched
    for value in candidate_lap_values:
        matched = _match_segment_index_by_lap_no(lap_segments=lap_segments, lap_value=(value + 1))
        if matched is not None:
            return matched
    for value in candidate_lap_values:
        matched = _match_segment_index_by_lap_no(lap_segments=lap_segments, lap_value=(value - 1))
        if matched is not None:
            return matched

    for fallback_idx in (lap_seq - 1, lap_seq):
        if 0 <= fallback_idx < len(lap_segments):
            return fallback_idx
    return None


def _match_segment_index_by_lap_no(*, lap_segments: list[dict[str, Any]], lap_value: int) -> int | None:
    """Implement match segment index by lap no logic."""
    for idx, segment in enumerate(lap_segments):
        if not isinstance(segment, dict):
            continue
        seg_lap_no = _coerce_optional_int(segment.get("lap_no"))
        seg_lap_num = _coerce_optional_int(segment.get("lap_num"))
        if seg_lap_no == lap_value or seg_lap_num == lap_value:
            return idx
    return None


def _build_normalized_lap_summary(
    *,
    segment: dict[str, Any],
    lap_time_s: float | None,
    lap_incomplete: bool,
    lap_offtrack: bool,
    lap_valid: bool,
) -> dict[str, Any]:
    """Build and return normalized lap summary."""
    lap_complete = _coerce_optional_bool(segment.get("lap_complete"))
    offtrack_surface = _coerce_optional_bool(segment.get("offtrack_surface"))
    incident_delta = _coerce_optional_int(segment.get("incident_delta"))
    valid_lap = _coerce_optional_bool(segment.get("valid_lap"))
    if lap_complete is None:
        lap_complete = not bool(lap_incomplete)
    if offtrack_surface is None:
        offtrack_surface = bool(lap_offtrack)
    if incident_delta is None:
        incident_delta = 0
    if valid_lap is None:
        valid_lap = bool(lap_valid)
    if lap_incomplete:
        lap_complete = False
        valid_lap = False
    return {
        "lap_time_s": lap_time_s,
        "lap_complete": bool(lap_complete),
        "offtrack_surface": bool(offtrack_surface),
        "incident_delta": int(incident_delta),
        "valid_lap": bool(valid_lap),
        "incomplete": bool(lap_incomplete),
        "lap_incomplete": bool(lap_incomplete),
        "lap_offtrack": bool(lap_offtrack),
    }


def _session_label(session: _SessionScan) -> str:
    """Implement session label logic."""
    ts_text = _format_dt_short(session.parsed_folder_ts or session.last_driven_ts)
    type_text = str(session.session_type or "Unknown")
    sid = str(session.session_id or session.folder_name)
    label = f"{ts_text}  {type_text}  {sid}".strip()
    if session.has_active_lock and not session.has_finalized_marker:
        return f"{label}  [ACTIVE]"
    return label


def _format_dt_short(ts: float | None) -> str:
    """Format dt short."""
    if ts is None:
        return "Unknown time"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "Unknown time"


def _duration_from_run_meta(run_meta: dict[str, Any]) -> float | None:
    """Implement duration from run meta logic."""
    start = _coerce_optional_float(run_meta.get("start_session_time"))
    end = _coerce_optional_float(run_meta.get("end_session_time"))
    if start is None or end is None:
        return None
    delta = end - start
    if delta < 0:
        return None
    return delta


def _lap_duration_seconds(segment: dict[str, Any]) -> float | None:
    """Implement lap duration seconds logic."""
    if not isinstance(segment, dict):
        return None
    duration = _coerce_optional_float(segment.get("duration_s"))
    if duration is not None and duration >= 0.0:
        return duration
    start = _coerce_optional_float(segment.get("start_ts"))
    end = _coerce_optional_float(segment.get("end_ts"))
    if start is None or end is None:
        return None
    delta = end - start
    if delta < 0:
        return None
    return delta


def _best_effort_last_driven_ts(session_dir: Path, parsed_folder_ts: float | None) -> float | None:
    """Implement best effort last driven ts logic."""
    return _max_optional(parsed_folder_ts, _path_mtime_ts(session_dir))


def _path_mtime_ts(path: Path | None) -> float | None:
    """Implement path mtime ts logic."""
    if path is None:
        return None
    try:
        return path.stat().st_mtime
    except Exception:
        return None


def _meta_str(meta: dict[str, Any], key: str) -> str | None:
    """Implement meta str logic."""
    value = meta.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_optional_float(value: Any) -> float | None:
    """Coerce optional float."""
    try:
        return float(value)
    except Exception:
        return None


def _coerce_optional_int(value: Any) -> int | None:
    """Coerce optional int."""
    try:
        return int(value)
    except Exception:
        return None


def _coerce_optional_bool(value: Any) -> bool | None:
    """Coerce optional bool."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value == 0:
            return False
        if value == 1:
            return True
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"0", "false", "no", "n", "off"}:
            return False
        if text in {"1", "true", "yes", "y", "on"}:
            return True
    return None


def _sort_key_text(value: Any) -> str:
    """Sort key text."""
    return str(value or "").lower()


def _session_cache_signature(session_dir: Path) -> tuple[tuple[Any, ...] | None, list[Path]]:
    """Implement session cache signature logic."""
    try:
        children = list(session_dir.iterdir())
        dir_stat = session_dir.stat()
    except Exception:
        return (None, [])

    relevant_entries: list[tuple[str, int, int]] = []
    for child in children:
        name = str(child.name or "")
        lower = name.lower()
        if not (
            _RUN_META_RE.match(name)
            or _RUN_PARQUET_RE.match(name)
            or _parse_lap_meta_filename(name) is not None
            or lower == "session_meta.json"
            or lower == _DEBUG_SAMPLES_FILENAME
            or lower == ACTIVE_SESSION_LOCK_FILENAME.lower()
            or lower == SESSION_FINALIZED_FILENAME.lower()
        ):
            continue
        try:
            stat = child.stat()
            relevant_entries.append((name, int(stat.st_mtime_ns), int(stat.st_size)))
        except Exception:
            relevant_entries.append((name, 0, 0))
    relevant_entries.sort(key=lambda item: _sort_key_text(item[0]))
    signature: tuple[Any, ...] = (
        int(dir_stat.st_mtime_ns),
        tuple(relevant_entries),
    )
    return (signature, children)


def _stable_path_id(path: Path) -> str:
    """Implement stable path id logic."""
    try:
        return str(path.resolve())
    except Exception:
        return str(path)


def _cache_key(path: Path) -> str:
    """Implement cache key logic."""
    return _stable_path_id(path)


def _prune_cache(live_keys: set[str]) -> None:
    """Implement prune cache logic."""
    stale = [key for key in _SESSION_CACHE.keys() if key not in live_keys]
    for key in stale:
        _SESSION_CACHE.pop(key, None)


def _max_optional(*values: float | None) -> float | None:
    """Implement max optional logic."""
    valid = [v for v in values if v is not None]
    if not valid:
        return None
    return max(valid)


def _safe_now_ts() -> float:
    """Implement safe now ts logic."""
    try:
        return datetime.now().timestamp()
    except Exception:
        return 0.0


def _is_debug_coaching_enabled() -> bool:
    """Return whether debug coaching enabled."""
    raw = os.environ.get("IWAS_DEBUG_COACHING")
    if raw is None:
        return False
    text = str(raw).strip().lower()
    return text in {"1", "true", "yes", "on"}


def _is_debug_lap_diagnostics_enabled() -> bool:
    """Return whether debug lap diagnostics enabled."""
    raw = os.environ.get("IWAS_COACHING_DEBUG_LAP_DIAGNOSTICS")
    if raw is None:
        return False
    text = str(raw).strip().lower()
    return text in {"1", "true", "yes", "on"}
