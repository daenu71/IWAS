"""Unit tests for environment propagation in core.coaching.indexer."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.coaching.indexer import (  # noqa: E402
    NodeSummary,
    _RunScan,
    _SessionScan,
    _build_session_event_node,
)


def test_environment_inherits_from_session_to_run_and_lap(tmp_path: Path) -> None:
    session_dir = tmp_path / "2026-03-06__100000__Spa__F3__Practice__s1"
    session_dir.mkdir(parents=True, exist_ok=True)
    environment = {
        "track_temp_c": 27.0,
        "air_temp_c": 18.0,
        "humidity_pct": 40.0,
    }
    run = _RunScan(
        run_id=1,
        parquet_path=None,
        meta_path=None,
        extra_paths=[],
        meta={},
        lap_segments=[
            {
                "lap_no": 3,
                "lap_complete": True,
                "duration_s": 91.234,
            }
        ],
        summary=NodeSummary(
            total_time_s=91.234,
            laps=1,
            laps_total_display=1,
            fastest_lap_s=91.234,
            last_driven_ts=100.0,
        ),
    )
    session = _SessionScan(
        session_dir=session_dir,
        folder_name=session_dir.name,
        track="Spa",
        car="F3",
        session_type="practice",
        session_id="s1",
        session_meta={"environment": environment},
        runs=[run],
        has_active_lock=False,
        has_finalized_marker=True,
        last_driven_ts=100.0,
        parsed_folder_ts=100.0,
        summary=NodeSummary(
            total_time_s=91.234,
            laps=1,
            laps_total_display=1,
            fastest_lap_s=91.234,
            last_driven_ts=100.0,
        ),
    )

    session_node = _build_session_event_node(session)
    run_node = session_node.children[0]
    lap_node = run_node.children[0]

    assert session_node.summary.environment == environment
    assert run_node.summary.environment == environment
    assert lap_node.summary.environment == environment
    assert run_node.summary.environment is not session_node.summary.environment
    assert lap_node.summary.environment is not run_node.summary.environment


def test_run_environment_overrides_session_environment(tmp_path: Path) -> None:
    session_dir = tmp_path / "2026-03-06__100000__Spa__F3__Practice__s1"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_environment = {"track_temp_c": 30.0}
    run_environment = {"track_temp_c": 24.0, "weather_type": "Constant"}
    run = _RunScan(
        run_id=1,
        parquet_path=None,
        meta_path=None,
        extra_paths=[],
        meta={},
        lap_segments=[
            {
                "lap_no": 4,
                "lap_complete": True,
                "duration_s": 92.5,
            }
        ],
        summary=NodeSummary(
            total_time_s=92.5,
            laps=1,
            laps_total_display=1,
            fastest_lap_s=92.5,
            last_driven_ts=200.0,
            environment=run_environment,
        ),
    )
    session = _SessionScan(
        session_dir=session_dir,
        folder_name=session_dir.name,
        track="Spa",
        car="F3",
        session_type="practice",
        session_id="s1",
        session_meta={"environment": session_environment},
        runs=[run],
        has_active_lock=False,
        has_finalized_marker=True,
        last_driven_ts=200.0,
        parsed_folder_ts=200.0,
        summary=NodeSummary(
            total_time_s=92.5,
            laps=1,
            laps_total_display=1,
            fastest_lap_s=92.5,
            last_driven_ts=200.0,
        ),
    )

    session_node = _build_session_event_node(session)
    run_node = session_node.children[0]
    lap_node = run_node.children[0]

    assert session_node.summary.environment == session_environment
    assert run_node.summary.environment == run_environment
    assert lap_node.summary.environment == run_environment
