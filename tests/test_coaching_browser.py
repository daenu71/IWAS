"""Unit tests for environment tooltip behavior in ui.coaching_browser."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.coaching.indexer import CoachingIndex, CoachingTreeNode, NodeSummary  # noqa: E402
from ui.coaching_browser import CoachingBrowser  # noqa: E402


def test_tooltip_environment_is_available_for_session_run_and_lap_rows() -> None:
    environment = {"track_temp_c": 26.0, "weather_type": "Static"}
    session_node = CoachingTreeNode(
        id="session",
        kind="event",
        label="Session",
        summary=NodeSummary(environment=environment),
    )
    run_node = CoachingTreeNode(
        id="run",
        kind="run",
        label="Run 0001",
        summary=NodeSummary(environment=environment),
    )
    lap_node = CoachingTreeNode(
        id="lap",
        kind="lap",
        label="Lap 1",
        summary=NodeSummary(environment=environment),
    )
    browser = CoachingBrowser.__new__(CoachingBrowser)
    browser._index = CoachingIndex(
        root_dir=Path("."),
        tracks=[],
        nodes_by_id={
            session_node.id: session_node,
            run_node.id: run_node,
            lap_node.id: lap_node,
        },
        generated_ts=0.0,
    )

    assert browser._tooltip_environment_for_iid("session") == environment
    assert browser._tooltip_environment_for_iid("run") == environment
    assert browser._tooltip_environment_for_iid("lap") == environment
    assert browser._tooltip_environment_for_iid("missing") is None
