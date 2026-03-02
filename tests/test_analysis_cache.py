"""Unit tests for core.coaching.analysis_cache."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.coaching.analysis_cache import AnalysisCache  # noqa: E402
from core.coaching.analysis_contract import AnalysisContract  # noqa: E402
from core.coaching.feature_schema import FeatureSchema  # noqa: E402


def _make_lap_dir(tmp_path: Path) -> Path:
    session_dir = (
        tmp_path
        / "2026-03-02__100000__TestTrack__TestCar__Practice__session_1"
    )
    lap_dir = session_dir / "run_0001" / "laps" / "lap_0001"
    lap_dir.mkdir(parents=True, exist_ok=True)
    return lap_dir


def _status_template(cache: AnalysisCache) -> dict[str, object]:
    return {
        "status": "computed",
        "engine_version": cache.engine_version,
        "schema_hash": FeatureSchema.load().schema_hash,
        "contract_hash": AnalysisContract().contract_hash,
        "corner_map_version": 0,
        "corner_count": 0,
        "feature_count": 38,
        "partial": False,
        "missing_channels": [],
        "reasons": [],
        "artifacts": [
            "lap_resampled.parquet",
            "lap_events.json",
            "corner_features.parquet",
        ],
        "computed_at": "2026-03-02T10:00:00Z",
    }


def _write_status(lap_dir: Path, status: dict[str, object]) -> None:
    analysis_dir = lap_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    (analysis_dir / "analysis_status.json").write_text(
        json.dumps(status, indent=2),
        encoding="utf-8",
    )


def _write_flatline_lap_input(lap_dir: Path, n: int = 1200) -> None:
    ldp = np.linspace(0.0, 1.0 - 1.0 / n, n, dtype=np.float64)
    session_time = np.arange(n, dtype=np.float64) * 0.01

    cols = {
        "ts": pa.array(session_time.tolist(), type=pa.float64()),
        "monotonic_ts": pa.array(session_time.tolist(), type=pa.float64()),
        "LapDistPct": pa.array(ldp.astype(np.float32).tolist(), type=pa.float32()),
        "LapDist": pa.array((ldp * 5000.0).astype(np.float32).tolist(), type=pa.float32()),
        "SessionTime": pa.array(session_time.astype(np.float32).tolist(), type=pa.float32()),
        "Speed": pa.array((40.0 + 5.0 * np.sin(2.0 * np.pi * ldp)).astype(np.float32).tolist(), type=pa.float32()),
        "YawRate": pa.array(np.zeros(n, dtype=np.float32).tolist(), type=pa.float32()),
        "LatAccel": pa.array((2.0 * np.sin(4.0 * np.pi * ldp)).astype(np.float32).tolist(), type=pa.float32()),
        "LongAccel": pa.array((-1.0 + 0.5 * np.cos(3.0 * np.pi * ldp)).astype(np.float32).tolist(), type=pa.float32()),
        "VertAccel": pa.array((9.81 + 0.1 * np.sin(5.0 * np.pi * ldp)).astype(np.float32).tolist(), type=pa.float32()),
        "Throttle": pa.array(np.clip(ldp, 0.0, 1.0).astype(np.float32).tolist(), type=pa.float32()),
        "Brake": pa.array(np.clip(1.0 - ldp, 0.0, 1.0).astype(np.float32).tolist(), type=pa.float32()),
        "SteeringWheelAngle": pa.array((0.2 * np.sin(6.0 * np.pi * ldp)).astype(np.float32).tolist(), type=pa.float32()),
        "Gear": pa.array(np.full(n, 3, dtype=np.int32).tolist(), type=pa.int32()),
        "RPM": pa.array((6200.0 + 100.0 * np.sin(2.0 * np.pi * ldp)).astype(np.float32).tolist(), type=pa.float32()),
    }

    path = lap_dir / "lap_input.parquet"
    pq.write_table(pa.table(cols), str(path))


def test_stale_detection_engine_version_change(tmp_path: Path) -> None:
    lap_dir = _make_lap_dir(tmp_path)
    cache = AnalysisCache()
    status = _status_template(cache)
    status["engine_version"] = "0.0.9"
    _write_status(lap_dir, status)

    assert cache.is_stale(lap_dir) is True


def test_stale_detection_schema_hash_change(tmp_path: Path) -> None:
    lap_dir = _make_lap_dir(tmp_path)
    cache = AnalysisCache()
    status = _status_template(cache)
    status["schema_hash"] = "deadbeef"
    _write_status(lap_dir, status)

    assert cache.is_stale(lap_dir) is True


def test_compute_blocked_on_flatline_data(tmp_path: Path) -> None:
    lap_dir = _make_lap_dir(tmp_path)
    _write_flatline_lap_input(lap_dir)

    cache = AnalysisCache()
    result = cache.compute(lap_dir)

    assert result["status"] == "blocked"
    assert any("flatline_YawRate" in reason for reason in result["reasons"])
    assert result["partial"] is True
    assert "SteeringWheelTorque" in result["missing_channels"]

    status_path = lap_dir / "analysis" / "analysis_status.json"
    assert status_path.exists()
    loaded = json.loads(status_path.read_text(encoding="utf-8"))
    assert loaded["status"] == "blocked"
