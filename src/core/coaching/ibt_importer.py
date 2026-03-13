"""IBT file import: reads one IBT telemetry file and writes coaching artefacts.

Called by the import-queue processor.  One IBT file → one session folder under
data/coaching/ with session_meta.json, session_info.yaml, run_0001.parquet and
run_0001_meta.json – mirroring the live-recording layout.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import time
from typing import Any

from core.coaching.models import SessionMeta
from core.coaching.parquet_writer import ParquetRunWriter
from core.coaching.storage import (
    ensure_session_dir,
    get_coaching_storage_dir,
    mark_session_active,
    mark_session_finalized,
)
from core.irsdk.sessioninfo_parser import extract_session_meta


_LOG = logging.getLogger(__name__)
_RUN_ID: int = 1
_IBT_FALLBACK_HZ: float = 60.0
_CHUNK_SECONDS: float = 5.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def import_ibt_queue_entry(
    queue_entry: dict[str, Any],
    *,
    storage_root: Path | None = None,
) -> Path:
    """Import one IBT file described by *queue_entry* and return the session dir.

    Raises on any error; the caller is responsible for catching and marking the
    queue entry as ``failed``.
    """
    ibt_path = Path(str(queue_entry["source_path"]))
    fingerprint = str(queue_entry.get("fingerprint") or "")
    modified_ts = float(queue_entry.get("modified_ts") or time.time())

    _LOG.info(
        "[ibt_importer] start ibt=%s fingerprint=%.16s...",
        ibt_path.name,
        fingerprint,
    )

    ibt = _open_ibt(ibt_path)
    try:
        session_dir = _run_import(
            ibt=ibt,
            ibt_path=ibt_path,
            fingerprint=fingerprint,
            modified_ts=modified_ts,
            storage_root=storage_root,
        )
    finally:
        _close_ibt(ibt)

    _LOG.info("[ibt_importer] done session_dir=%s", session_dir.name)
    return session_dir


# ---------------------------------------------------------------------------
# Core import pipeline
# ---------------------------------------------------------------------------

def _run_import(
    *,
    ibt: Any,
    ibt_path: Path,
    fingerprint: str,
    modified_ts: float,
    storage_root: Path | None,
) -> Path:
    # 1. Extract session metadata from IBT header / session YAML.
    yaml_text = _read_session_yaml(ibt)
    session_meta_raw = extract_session_meta(yaml_text, recorder_start_ts=modified_ts)

    track = (
        session_meta_raw.get("TrackDisplayName")
        or session_meta_raw.get("TrackConfigName")
        or _read_weekend_field(ibt, "TrackDisplayName")
        or _read_weekend_field(ibt, "TrackName")
        or "unknown"
    )
    car = (
        session_meta_raw.get("CarScreenName")
        or _read_weekend_field(ibt, "CarScreenName")
        or "unknown"
    )
    session_type = session_meta_raw.get("SessionType") or "unknown"
    session_id = (
        str(session_meta_raw.get("SessionUniqueID") or "")
        or _read_weekend_field(ibt, "SessionUniqueID")
        or _read_weekend_field(ibt, "SubSessionID")
        or "0"
    )

    # 2. Create session directory.
    base_dir = Path(storage_root) if storage_root is not None else get_coaching_storage_dir()
    session_dir = ensure_session_dir(
        modified_ts, track, car, session_type, session_id,
        base_dir=base_dir,
    )

    # Mark session as active (lock file present until finalization).
    mark_session_active(session_dir, payload={"import_source": "ibt"})

    try:
        # 3. Inventory available channels.
        available_channels = _describe_channels(ibt)
        dtype_decisions = _build_dtype_decisions(available_channels)
        channel_names = list(available_channels.keys())

        # 4. Read all telemetry column-by-column (list per channel).
        col_data: dict[str, list[Any]] = {}
        sample_count = 0
        for name in channel_names:
            values = _get_all(ibt, name)
            if values is not None:
                col_data[name] = values
                if len(values) > sample_count:
                    sample_count = len(values)

        # 5. Estimate sample rate from SessionTime.
        sample_hz = _estimate_sample_hz(col_data.get("SessionTime"), sample_count)

        # 6. Derive session-time and lap ranges for run_meta.
        st_values = col_data.get("SessionTime")
        start_session_time = _first_finite(st_values)
        end_session_time = _last_finite(st_values)

        lap_values = col_data.get("Lap") or col_data.get("LapCompleted")
        lap_start = _first_int(lap_values)
        lap_end = _last_int(lap_values)

        # 7. Write Parquet (run_0001.parquet).
        run_parquet_path = session_dir / f"run_{_RUN_ID:04d}.parquet"
        _write_parquet(
            path=run_parquet_path,
            col_data=col_data,
            channel_names=channel_names,
            dtype_decisions=dtype_decisions,
            sample_count=sample_count,
            modified_ts=modified_ts,
            sample_hz=sample_hz,
        )

        # 8. Write run_0001_meta.json.
        run_meta: dict[str, Any] = {
            "run_id": _RUN_ID,
            "sample_count": sample_count,
            "sample_hz": sample_hz,
            "import_source": "ibt",
        }
        if start_session_time is not None:
            run_meta["start_session_time"] = start_session_time
        if end_session_time is not None:
            run_meta["end_session_time"] = end_session_time
        if lap_start is not None:
            run_meta["lap_start"] = lap_start
        if lap_end is not None:
            run_meta["lap_end"] = lap_end
        _write_json_file(session_dir / f"run_{_RUN_ID:04d}_meta.json", run_meta)

        # 9. Write session_meta.json (same fields as live recording + source block).
        meta_obj = SessionMeta(
            recorded_channels=channel_names,
            missing_channels=[],
            sample_hz=sample_hz,
            dtype_decisions=dtype_decisions,
        )
        base: dict[str, Any] = dict(session_meta_raw)
        base["source"] = {
            "ibt_path": str(ibt_path),
            "ibt_fingerprint": fingerprint,
        }
        _write_json_file(session_dir / "session_meta.json", meta_obj.to_dict(base))

        # 10. Write session_info.yaml from IBT header YAML.
        if yaml_text:
            (session_dir / "session_info.yaml").write_text(yaml_text, encoding="utf-8")

    except Exception:
        # Active lock stays in place – the folder is visibly incomplete.
        raise

    # 11. Mark session finalized (removes lock, writes .finalized marker).
    mark_session_finalized(session_dir, remove_lock=True)

    # 12. Derive run / pit / lap index artefacts from the written Parquet.
    #     This is a separate post-processing step; failures are logged but do
    #     not roll back the already-finalized import.
    try:
        from core.coaching.ibt_session_splitter import split_session
        split_session(session_dir)
    except Exception as exc:  # pragma: no cover
        _LOG.warning("[ibt_importer] session splitter failed (non-fatal): %s", exc)

    return session_dir


# ---------------------------------------------------------------------------
# Parquet writer helper
# ---------------------------------------------------------------------------

def _write_parquet(
    *,
    path: Path,
    col_data: dict[str, list[Any]],
    channel_names: list[str],
    dtype_decisions: dict[str, str],
    sample_count: int,
    modified_ts: float,
    sample_hz: float,
) -> None:
    """Write all telemetry samples to *path* using ParquetRunWriter."""
    if sample_count == 0:
        _LOG.warning("[ibt_importer] no samples to write, skipping parquet")
        return

    st_values = col_data.get("SessionTime")
    last_session_time = _last_finite(st_values)

    writer = ParquetRunWriter(
        path,
        recorded_channels=channel_names,
        dtype_decisions=dtype_decisions,
        chunk_seconds=_CHUNK_SECONDS,
        sample_hz=sample_hz,
    )
    try:
        for i in range(sample_count):
            st_i = _coerce_float(st_values[i]) if st_values and i < len(st_values) else None

            if st_i is not None and last_session_time is not None:
                ts = modified_ts - (last_session_time - st_i)
            else:
                ts = modified_ts - (sample_count - 1 - i) / max(sample_hz, 1.0)

            monotonic_ts = st_i if st_i is not None else i / max(sample_hz, 1.0)

            row: dict[str, Any] = {"ts": ts, "monotonic_ts": monotonic_ts}
            for name in channel_names:
                col = col_data.get(name)
                row[name] = col[i] if col is not None and i < len(col) else None

            writer.append_row(row)
    finally:
        writer.close(final=True)


# ---------------------------------------------------------------------------
# IBT open / close helpers
# ---------------------------------------------------------------------------

def _open_ibt(ibt_path: Path) -> Any:
    import irsdk  # type: ignore[import]

    ibt_ctor = getattr(irsdk, "IBT", None)
    if callable(ibt_ctor):
        ibt = ibt_ctor()
        open_fn = getattr(ibt, "open", None)
        if callable(open_fn):
            open_fn(str(ibt_path))
            return ibt

    # Fallback: use IRSDK offline reader
    ir = irsdk.IRSDK()
    result = ir.startup(test_file=str(ibt_path))
    if result is False:
        raise RuntimeError(f"irsdk could not open IBT file: {ibt_path}")
    return ir


def _close_ibt(ir: Any) -> None:
    try:
        close = getattr(ir, "close", None)
        if callable(close):
            close()
            return
        shutdown = getattr(ir, "shutdown", None)
        if callable(shutdown):
            shutdown()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Session YAML / header helpers
# ---------------------------------------------------------------------------

def _read_session_yaml(ir: Any) -> str:
    for attr in ("session_info", "sessionInfo", "session_info_yaml", "sessionInfoYaml"):
        value = getattr(ir, attr, None)
        if value is None:
            continue
        text = value() if callable(value) else value
        if isinstance(text, str) and text.strip():
            return text
    try:
        raw = ir["SessionInfo"]
        if isinstance(raw, str):
            return raw
    except Exception:
        pass
    return ""


def _read_weekend_field(ir: Any, field: str) -> str | None:
    try:
        weekend = ir["WeekendInfo"]
        if isinstance(weekend, dict):
            value = weekend.get(field)
            if value is not None:
                text = str(value).strip()
                return text or None
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Channel inventory helpers
# ---------------------------------------------------------------------------

def _describe_channels(ir: Any) -> dict[str, dict[str, Any]]:
    """Return {channel_name: header_info} for all channels in *ir*."""
    result: dict[str, dict[str, Any]] = {}
    headers = getattr(ir, "_var_headers", None) or getattr(ir, "var_headers", None)
    if headers is None:
        return result
    for header in _iter_headers(headers):
        name = _header_field(header, "name", "Name", "var_name")
        if not name:
            continue
        result[str(name)] = _extract_header_info(header)
    return result


def _iter_headers(headers: Any):  # type: ignore[return]
    if isinstance(headers, (list, tuple)):
        yield from headers
        return
    if hasattr(headers, "values"):
        yield from headers.values()
        return
    try:
        yield from iter(headers)
    except TypeError:
        pass


def _header_field(header: Any, *names: str) -> Any:
    for name in names:
        value = getattr(header, name, None)
        if value is not None:
            return value
        if isinstance(header, dict):
            value = header.get(name)
            if value is not None:
                return value
    return None


def _extract_header_info(header: Any) -> dict[str, Any]:
    info: dict[str, Any] = {}
    raw_type = _header_field(header, "type", "Type", "var_type")
    if raw_type is not None:
        info["type"] = _pretty_var_type(raw_type)
    count = _coerce_int(_header_field(header, "count", "Count"))
    if count is not None:
        info["count"] = count
    return info


def _pretty_var_type(raw_type: Any) -> str | None:
    key = str(raw_type).strip().lower()
    if key in {"irsdk_char", "irsdk_bool", "irsdk_int", "irsdk_bitfield", "irsdk_float", "irsdk_double"}:
        return key.replace("irsdk_", "")
    idx = _coerce_int(raw_type)
    return {0: "char", 1: "bool", 2: "int", 3: "bitfield", 4: "float", 5: "double"}.get(idx)


def _build_dtype_decisions(available_channels: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Map channel name → Arrow dtype token understood by ParquetRunWriter."""
    decisions: dict[str, str] = {}
    for name, info in available_channels.items():
        count = info.get("count")
        if isinstance(count, int) and count > 1:
            # Array channel – store as comma-separated string.
            decisions[name] = f"string[{count}]"
            continue
        type_str = str(info.get("type") or "").lower()
        if type_str == "float":
            decisions[name] = "float32"
        elif type_str == "double":
            decisions[name] = "float64"
        elif type_str == "int":
            decisions[name] = "int32"
        elif type_str == "bitfield":
            decisions[name] = "bitfield"
        elif type_str == "bool":
            decisions[name] = "bool"
        elif type_str == "char":
            decisions[name] = "string"
    return decisions


# ---------------------------------------------------------------------------
# IBT get_all wrapper
# ---------------------------------------------------------------------------

def _get_all(ir: Any, name: str) -> list[Any] | None:
    getter = getattr(ir, "get_all", None)
    if callable(getter):
        try:
            values = getter(name)
            return values if isinstance(values, list) else None
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------

def _estimate_sample_hz(session_time_values: list[Any] | None, sample_count: int) -> float:
    if not session_time_values or len(session_time_values) < 2:
        return _IBT_FALLBACK_HZ
    first = _coerce_float(session_time_values[0])
    last = _coerce_float(session_time_values[-1])
    if first is None or last is None or last <= first:
        return _IBT_FALLBACK_HZ
    estimated = (len(session_time_values) - 1) / (last - first)
    if 55.0 <= estimated <= 65.0:
        return 60.0
    if 115.0 <= estimated <= 125.0:
        return 120.0
    if 25.0 <= estimated <= 35.0:
        return 30.0
    return max(1.0, round(estimated, 1))


def _first_finite(values: list[Any] | None) -> float | None:
    for v in (values or []):
        f = _coerce_float(v)
        if f is not None:
            return f
    return None


def _last_finite(values: list[Any] | None) -> float | None:
    result: float | None = None
    for v in (values or []):
        f = _coerce_float(v)
        if f is not None:
            result = f
    return result


def _first_int(values: list[Any] | None) -> int | None:
    for v in (values or []):
        i = _coerce_int(v)
        if i is not None:
            return i
    return None


def _last_int(values: list[Any] | None) -> int | None:
    result: int | None = None
    for v in (values or []):
        i = _coerce_int(v)
        if i is not None:
            result = i
    return result


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        import math
        f = float(value)
        return f if math.isfinite(f) else None
    except Exception:
        return None


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# JSON helper
# ---------------------------------------------------------------------------

def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
