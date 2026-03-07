"""Unit tests for compact environment rendering in ui.coaching_detail."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ui.coaching_detail import _build_environment_summary  # noqa: E402


def test_build_environment_summary_compacts_track_usage() -> None:
    summary = _build_environment_summary(
        {"air_pressure_hpa": 1032.2},
        track_usage="Low Usage",
    )

    assert summary == "P: 1032.2 hPa  Usage: Low"


def test_build_environment_summary_tooltip_keeps_full_track_usage() -> None:
    summary = _build_environment_summary(
        {"air_pressure_hpa": 1032.2},
        track_usage="Moderate Usage",
        compact_track_usage=False,
    )

    assert summary == "P: 1032.2 hPa  Usage: Moderate Usage"
