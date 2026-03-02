"""Analysis cache manager for lap-level coaching artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from .analysis_contract import AnalysisContract
from .corner_map import build_corner_map, load_corner_map
from .event_engine import extract_lap_events
from .feature_engine import extract_corner_features
from .feature_schema import FeatureSchema
from .resample_lapdist import resample_lap
from .storage import sanitize_name


_ENGINE_VERSION = "0.1.0"
_STATUS_FILENAME = "analysis_status.json"
_COVERAGE_THRESHOLD = 0.95
_FLATLINE_THRESHOLD_S = 5.0
_FLATLINE_EPS = 1e-7
_RUN_ID_RE = re.compile(r"run_(\d+)", re.IGNORECASE)
_LAP_ID_RE = re.compile(r"lap_(\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class _SourceSlice:
    parquet_path: Path
    start_idx: int
    end_idx: int


class AnalysisCache:
    """Orchestrates analysis artifacts and cache freshness per lap."""

    def __init__(
        self,
        *,
        engine_version: str = _ENGINE_VERSION,
        contract_path: Path | str | None = None,
        schema_path: Path | str | None = None,
        event_config_path: Path | str | None = None,
        coverage_threshold: float = _COVERAGE_THRESHOLD,
        flatline_threshold_s: float = _FLATLINE_THRESHOLD_S,
    ) -> None:
        self._engine_version = str(engine_version)
        self._contract_path = Path(contract_path) if contract_path is not None else None
        self._schema_path = Path(schema_path) if schema_path is not None else None
        self._event_config_path = (
            Path(event_config_path) if event_config_path is not None else None
        )
        self._coverage_threshold = float(coverage_threshold)
        self._flatline_threshold_s = float(flatline_threshold_s)

    @property
    def engine_version(self) -> str:
        return self._engine_version

    def is_stale(self, lap_path: Path | str) -> bool:
        lap_dir, _ = self._normalize_lap_path(lap_path)
        status = self._read_status(lap_dir)
        if not status:
            return True
        if str(status.get("status", "not_computed")) in {"not_computed", "stale"}:
            return True

        current_schema_hash = FeatureSchema.load(self._schema_path).schema_hash
        current_contract_hash = AnalysisContract(self._contract_path).contract_hash
        current_corner_map_version = self._current_corner_map_version(lap_dir)

        if str(status.get("engine_version", "")) != self._engine_version:
            return True
        if str(status.get("schema_hash", "")) != current_schema_hash:
            return True
        if str(status.get("contract_hash", "")) != current_contract_hash:
            return True
        if int(_coerce_optional_int(status.get("corner_map_version")) or 0) != int(
            current_corner_map_version
        ):
            return True
        return False

    def compute(self, lap_path: Path | str) -> dict[str, Any]:
        lap_dir, explicit_source = self._normalize_lap_path(lap_path)
        analysis_dir = lap_dir / "analysis"
        analysis_dir.mkdir(parents=True, exist_ok=True)

        schema = FeatureSchema.load(self._schema_path)
        contract = AnalysisContract(self._contract_path)
        status = self._empty_status(
            lap_dir=lap_dir,
            schema_hash=schema.schema_hash,
            contract_hash=contract.contract_hash,
        )

        lap_validity_reasons, lap_validity_flag = self._lap_validity_meta(lap_dir)
        status["reasons"].extend(lap_validity_reasons)

        try:
            source = self._resolve_source_slice(
                lap_dir=lap_dir,
                explicit_source=explicit_source,
            )
            channels = _read_parquet_columns(source.parquet_path)
            contract_result = contract.check(channels)

            status["missing_channels"] = sorted(contract_result.missing_optional)
            status["partial"] = bool(status["missing_channels"])

            if not contract_result.can_compute:
                missing_required = ",".join(contract_result.missing_required)
                status["status"] = "blocked"
                status["reasons"].append(f"contract_missing_required:{missing_required}")
                return self._write_status(lap_dir, status)

            resampled_path = analysis_dir / "lap_resampled.parquet"
            resample_meta = resample_lap(
                parquet_path=source.parquet_path,
                start_idx=source.start_idx,
                end_idx=source.end_idx,
                output_path=resampled_path,
            )
            status["artifacts"].append(resampled_path.name)

            coverage_pct = float(resample_meta.get("coverage_pct", 0.0))
            if coverage_pct < self._coverage_threshold:
                status["status"] = "blocked"
                status["reasons"].append(
                    f"coverage_below_threshold:{coverage_pct:.6f}<{self._coverage_threshold:.2f}"
                )
                return self._write_status(lap_dir, status)

            flatline_reasons = self._flatline_reasons(resampled_path)
            if flatline_reasons:
                status["status"] = "blocked"
                status["reasons"].extend(flatline_reasons)
                return self._write_status(lap_dir, status)

            storage_root = self._infer_session_dir(lap_dir)
            track_key = self._infer_track_key(lap_dir)
            car_key = self._infer_car_key(lap_dir)
            run_id = self._infer_run_id(lap_dir)
            lap_id = self._infer_lap_id(lap_dir)

            corner_map = load_corner_map(storage_root=storage_root, track_key=track_key)
            if corner_map is None:
                corner_map = build_corner_map(
                    parquet_path=resampled_path,
                    storage_root=storage_root,
                    track_key=track_key,
                )
            status["corner_map_version"] = int(
                _coerce_optional_int(corner_map.get("corner_map_version")) or 0
            )
            status["corner_count"] = len(corner_map.get("corners", []))

            events_path = analysis_dir / "lap_events.json"
            events = extract_lap_events(
                parquet_path=resampled_path,
                output_path=events_path,
                config_path=self._event_config_path,
                corner_map=corner_map,
            )
            status["artifacts"].append(events_path.name)

            features_path = analysis_dir / "corner_features.parquet"
            snapshots_dir = analysis_dir / "snapshots"
            extract_corner_features(
                parquet_path=resampled_path,
                events=events,
                corner_map=corner_map,
                run_id=run_id,
                lap_id=lap_id,
                track_key=track_key,
                car_key=car_key,
                engine_version=self._engine_version,
                schema_path=self._schema_path,
                contract_path=self._contract_path,
                lap_validity_flag=lap_validity_flag,
                output_path=features_path,
                snapshots_dir=snapshots_dir,
            )
            if features_path.exists():
                status["artifacts"].append(features_path.name)

            status["feature_count"] = self._schema_feature_count()
            status["status"] = "partial" if status["partial"] else "computed"
            return self._write_status(lap_dir, status)
        except Exception as exc:
            status["status"] = "blocked"
            status["reasons"].append(f"pipeline_error:{exc.__class__.__name__}:{exc}")
            return self._write_status(lap_dir, status)

    def _empty_status(
        self,
        *,
        lap_dir: Path,
        schema_hash: str,
        contract_hash: str,
    ) -> dict[str, Any]:
        return {
            "status": "not_computed",
            "engine_version": self._engine_version,
            "schema_hash": schema_hash,
            "contract_hash": contract_hash,
            "corner_map_version": self._current_corner_map_version(lap_dir),
            "corner_count": 0,
            "feature_count": 0,
            "partial": False,
            "missing_channels": [],
            "reasons": [],
            "artifacts": [],
            "computed_at": _utc_now_iso(),
        }

    def _normalize_lap_path(self, lap_path: Path | str) -> tuple[Path, Path | None]:
        raw = Path(lap_path)
        if raw.suffix.lower() == ".parquet":
            return raw.parent, raw
        return raw, None

    def _status_path(self, lap_dir: Path) -> Path:
        return lap_dir / "analysis" / _STATUS_FILENAME

    def _read_status(self, lap_dir: Path) -> dict[str, Any]:
        path = self._status_path(lap_dir)
        if not path.exists():
            return {}
        data = _read_json_dict(path)
        if not isinstance(data, dict):
            return {}
        return data

    def _write_status(self, lap_dir: Path, status: dict[str, Any]) -> dict[str, Any]:
        path = self._status_path(lap_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        status["computed_at"] = _utc_now_iso()
        path.write_text(json.dumps(status, indent=2), encoding="utf-8")
        return status

    def _current_corner_map_version(self, lap_dir: Path) -> int:
        storage_root = self._infer_session_dir(lap_dir)
        track_key = self._infer_track_key(lap_dir)
        current = load_corner_map(storage_root=storage_root, track_key=track_key)
        if current is None:
            return 0
        return int(_coerce_optional_int(current.get("corner_map_version")) or 0)

    def _resolve_source_slice(
        self,
        *,
        lap_dir: Path,
        explicit_source: Path | None,
    ) -> _SourceSlice:
        lap_meta = self._read_lap_meta(lap_dir)
        source = (
            self._resolve_explicit_source(lap_dir, explicit_source, lap_meta)
            or self._find_lap_local_parquet(lap_dir)
            or self._find_run_level_parquet(lap_dir, lap_meta)
        )
        if source is None:
            raise FileNotFoundError(f"No source parquet found for lap at {lap_dir}")

        row_count = _parquet_row_count(source)
        if row_count <= 0:
            raise ValueError(f"Source parquet has no rows: {source}")

        start_idx = self._resolve_start_idx(lap_meta)
        end_idx = self._resolve_end_idx(lap_meta)
        if start_idx is None:
            start_idx = 0
        if end_idx is None:
            end_idx = row_count - 1

        start_idx = max(0, int(start_idx))
        end_idx = min(int(end_idx), row_count - 1)
        if end_idx < start_idx:
            raise ValueError(
                f"Invalid source slice start_idx={start_idx} end_idx={end_idx}"
            )
        return _SourceSlice(parquet_path=source, start_idx=start_idx, end_idx=end_idx)

    def _resolve_explicit_source(
        self,
        lap_dir: Path,
        explicit_source: Path | None,
        lap_meta: dict[str, Any],
    ) -> Path | None:
        candidates: list[Path] = []
        if explicit_source is not None:
            candidates.append(explicit_source)

        for key in (
            "parquet_path",
            "source_parquet",
            "run_parquet_path",
            "input_parquet",
        ):
            value = lap_meta.get(key)
            if isinstance(value, str) and value.strip():
                parsed = Path(value.strip())
                if not parsed.is_absolute():
                    parsed = lap_dir / parsed
                candidates.append(parsed)

        for candidate in candidates:
            if candidate.exists() and candidate.suffix.lower() == ".parquet":
                return candidate
        return None

    def _find_lap_local_parquet(self, lap_dir: Path) -> Path | None:
        preferred = [
            lap_dir / "lap_input.parquet",
            lap_dir / "lap_raw.parquet",
            lap_dir / "lap.parquet",
            lap_dir / "lap_data.parquet",
            lap_dir / "telemetry.parquet",
        ]
        for path in preferred:
            if path.exists():
                return path

        if not lap_dir.exists():
            return None
        parquet_files = sorted(
            p
            for p in lap_dir.glob("*.parquet")
            if p.name != "lap_resampled.parquet"
        )
        if parquet_files:
            return parquet_files[0]
        return None

    def _find_run_level_parquet(
        self, lap_dir: Path, lap_meta: dict[str, Any]
    ) -> Path | None:
        run_dir = self._infer_run_dir(lap_dir)
        if not run_dir.exists():
            return None

        run_id = _extract_number(_RUN_ID_RE, run_dir.name)
        if run_id is not None:
            direct = run_dir / f"run_{run_id:04d}.parquet"
            if direct.exists():
                return direct

        candidates = sorted(run_dir.glob("run_*.parquet"))
        if candidates:
            return candidates[0]

        value = lap_meta.get("run_parquet")
        if isinstance(value, str) and value.strip():
            parsed = Path(value.strip())
            if not parsed.is_absolute():
                parsed = run_dir / parsed
            if parsed.exists():
                return parsed
        return None

    def _resolve_start_idx(self, lap_meta: dict[str, Any]) -> int | None:
        for key in ("start_idx", "lap_start_idx", "start_sample", "start_row"):
            value = _coerce_optional_int(lap_meta.get(key))
            if value is not None:
                return value
        return None

    def _resolve_end_idx(self, lap_meta: dict[str, Any]) -> int | None:
        for key in ("end_idx", "lap_end_idx", "end_sample", "end_row"):
            value = _coerce_optional_int(lap_meta.get(key))
            if value is not None:
                return value
        return None

    def _flatline_reasons(self, parquet_path: Path) -> list[str]:
        columns = _read_parquet_columns(parquet_path)
        wanted = ["SessionTime", "YawRate", "LatAccel", "Speed"]
        present = [c for c in wanted if c in columns]
        if len(present) < 2:
            return []

        data = pq.read_table(str(parquet_path), columns=present).to_pydict()
        session_time = _to_float_array(data.get("SessionTime"))
        dt = _estimate_sample_dt(session_time)
        if dt <= 0.0:
            return []

        reasons: list[str] = []
        for channel in ("YawRate", "LatAccel", "Speed"):
            values = _to_float_array(data.get(channel))
            if values is None:
                continue
            duration = _longest_constant_duration_s(values, dt)
            if duration > self._flatline_threshold_s:
                reasons.append(f"flatline_{channel}:{duration:.3f}s")
        return reasons

    def _read_lap_meta(self, lap_dir: Path) -> dict[str, Any]:
        for path in self._lap_meta_candidates(lap_dir):
            if path.exists():
                data = _read_json_dict(path)
                if isinstance(data, dict) and data:
                    return data
        return {}

    def _lap_meta_candidates(self, lap_dir: Path) -> list[Path]:
        run_dir = self._infer_run_dir(lap_dir)
        lap_no = _extract_number(_LAP_ID_RE, lap_dir.name)
        run_no = _extract_number(_RUN_ID_RE, run_dir.name)

        candidates = [
            lap_dir / "lap_meta.json",
            lap_dir / "meta.json",
        ]
        if run_no is not None and lap_no is not None:
            candidates.append(run_dir / f"run_{run_no:04d}_lap_{lap_no:04d}_meta.json")
        if lap_no is not None:
            candidates.append(run_dir / f"lap_{lap_no:04d}_meta.json")
        return candidates

    def _lap_validity_meta(self, lap_dir: Path) -> tuple[list[str], bool]:
        meta = self._read_lap_meta(lap_dir)
        lap_summary = meta.get("lap_summary")
        summary = lap_summary if isinstance(lap_summary, dict) else {}

        incomplete = _first_bool(
            [
                meta.get("incomplete"),
                meta.get("lap_incomplete"),
                summary.get("incomplete"),
                summary.get("lap_incomplete"),
            ]
        )
        offtrack = _first_bool(
            [
                meta.get("offtrack"),
                meta.get("lap_offtrack"),
                meta.get("offtrack_surface"),
                summary.get("offtrack"),
                summary.get("lap_offtrack"),
                summary.get("offtrack_surface"),
            ]
        )

        reasons: list[str] = []
        if incomplete is True:
            reasons.append("lap_incomplete_meta")
        if offtrack is True:
            reasons.append("lap_offtrack_meta")
        return reasons, not (incomplete is True or offtrack is True)

    def _infer_run_dir(self, lap_dir: Path) -> Path:
        if lap_dir.parent.name.lower() == "laps":
            return lap_dir.parent.parent
        return lap_dir.parent

    def _infer_session_dir(self, lap_dir: Path) -> Path:
        run_dir = self._infer_run_dir(lap_dir)
        if run_dir.parent != run_dir:
            return run_dir.parent
        return run_dir

    def _infer_run_id(self, lap_dir: Path) -> str:
        run_dir = self._infer_run_dir(lap_dir)
        run_no = _extract_number(_RUN_ID_RE, run_dir.name)
        if run_no is not None:
            return f"run_{run_no:04d}"
        return sanitize_name(run_dir.name) or "run_0000"

    def _infer_lap_id(self, lap_dir: Path) -> str:
        lap_no = _extract_number(_LAP_ID_RE, lap_dir.name)
        if lap_no is not None:
            return f"lap_{lap_no:04d}"
        return sanitize_name(lap_dir.name) or "lap_0000"

    def _infer_track_key(self, lap_dir: Path) -> str:
        session_dir = self._infer_session_dir(lap_dir)
        meta = _read_json_dict(session_dir / "session_meta.json")
        parts = session_dir.name.split("__")

        track_name = (
            _coerce_optional_str(meta.get("TrackDisplayName"))
            or _coerce_optional_str(meta.get("TrackName"))
            or (parts[2] if len(parts) >= 6 else None)
            or "unknown_track"
        )
        config_name = (
            _coerce_optional_str(meta.get("TrackConfigName"))
            or _coerce_optional_str(meta.get("TrackConfig"))
            or "unknown_config"
        )
        return f"{sanitize_name(track_name)}__{sanitize_name(config_name)}"

    def _infer_car_key(self, lap_dir: Path) -> str:
        session_dir = self._infer_session_dir(lap_dir)
        meta = _read_json_dict(session_dir / "session_meta.json")
        parts = session_dir.name.split("__")

        car_name = (
            _coerce_optional_str(meta.get("CarScreenName"))
            or _coerce_optional_str(meta.get("DriverCarName"))
            or _coerce_optional_str(meta.get("CarClassShortName"))
            or (parts[3] if len(parts) >= 6 else None)
            or "unknown_car"
        )
        return sanitize_name(car_name)

    def _schema_feature_count(self) -> int:
        schema_obj = FeatureSchema.load(self._schema_path)
        count = 0
        for group in schema_obj._groups.values():
            count += len(group)
        return int(count)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _read_parquet_columns(path: Path) -> list[str]:
    try:
        return list(pq.ParquetFile(str(path)).schema_arrow.names)
    except Exception:
        return []


def _parquet_row_count(path: Path) -> int:
    return int(pq.ParquetFile(str(path)).metadata.num_rows)


def _extract_number(pattern: re.Pattern[str], text: str) -> int | None:
    m = pattern.search(text or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _coerce_optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _coerce_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_optional_bool(value: Any) -> bool | None:
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
        v = value.strip().lower()
        if v in {"0", "false", "no", "off"}:
            return False
        if v in {"1", "true", "yes", "on"}:
            return True
    return None


def _first_bool(values: list[Any]) -> bool | None:
    for value in values:
        parsed = _coerce_optional_bool(value)
        if parsed is not None:
            return parsed
    return None


def _to_float_array(values: Any) -> np.ndarray | None:
    if values is None:
        return None
    if isinstance(values, np.ndarray):
        arr = values.astype(np.float64, copy=False)
        return arr if arr.size > 0 else None
    if not isinstance(values, list):
        return None
    out = np.empty(len(values), dtype=np.float64)
    for i, value in enumerate(values):
        if value is None:
            out[i] = np.nan
            continue
        try:
            number = float(value)
        except Exception:
            out[i] = np.nan
            continue
        out[i] = number if math.isfinite(number) else np.nan
    return out if out.size > 0 else None


def _estimate_sample_dt(session_time: np.ndarray | None) -> float:
    if session_time is None or session_time.size < 2:
        return 0.0
    finite = session_time[np.isfinite(session_time)]
    if finite.size < 2:
        return 0.0
    diffs = np.diff(finite)
    diffs = diffs[np.isfinite(diffs) & (diffs > 0.0)]
    if diffs.size == 0:
        return 0.0
    return float(np.median(diffs))


def _longest_constant_duration_s(values: np.ndarray, dt_s: float) -> float:
    if values.size < 2 or dt_s <= 0.0:
        return 0.0

    best = 1
    run = 1
    for idx in range(1, values.size):
        a = values[idx - 1]
        b = values[idx]
        if np.isfinite(a) and np.isfinite(b) and abs(float(b - a)) <= _FLATLINE_EPS:
            run += 1
            if run > best:
                best = run
        else:
            run = 1
    return max(0.0, float(best - 1) * float(dt_s))
