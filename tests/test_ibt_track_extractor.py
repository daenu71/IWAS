"""Unit tests for core.coaching.ibt_track_extractor – Story 3.3a."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# Ensure src/ is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.coaching.ibt_track_extractor import (  # noqa: E402
    _compute_normals,
    _extract_centerline_m,
    _integrate_velocity,
    _read_track_key,
    _to_normalised_list,
    extract_track_geometry,
)
from core.coaching.track_geometry import _normalise_xy  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers – synthetic IRSDK mock
# ---------------------------------------------------------------------------


def _make_session_yaml(
    track_display: str = "Sebring",
    track_config: str = "Full Course",
    track_width: float | None = None,
) -> str:
    lines = [
        "WeekendInfo:",
        f" TrackDisplayName: {track_display}",
        f" TrackConfigName: {track_config}",
    ]
    if track_width is not None:
        lines.append(f" TrackWidth: {track_width}")
    return "\n".join(lines)


class _FakeIRSDK:
    """Minimal mock of pyirsdk.IRSDK backed by a frame list.

    Each frame is a dict mapping field names to values. The mock exposes
    ``startup()``, ``shutdown()``, ``parse_to()``, ``__getitem__()``,
    and a ``session_info`` attribute.
    """

    def __init__(
        self,
        frames: list[dict],
        session_yaml: str = "",
        weekend_info: dict | None = None,
    ) -> None:
        self._frames = frames
        self._idx = 0
        self.session_info = session_yaml
        self._weekend_info = weekend_info or {}

    # ---- irsdk-like interface ----

    def startup(self, test_file: str | None = None, **kwargs) -> bool:
        return True

    def shutdown(self) -> None:
        pass

    def __getitem__(self, key: str):
        if key == "WeekendInfo":
            return self._weekend_info
        if self._idx >= len(self._frames):
            return None
        return self._frames[self._idx].get(key)

    def parse_to(self, time_secs: float) -> bool:
        for i in range(self._idx + 1, len(self._frames)):
            if self._frames[i].get("SessionTime", -1) >= time_secs:
                self._idx = i
                return True
        return False


def _make_straight_frames(n: int = 600, vx: float = 50.0) -> list[dict]:
    """Create *n* frames driving straight along X at *vx* m/s at 60 Hz."""
    dt = 1.0 / 60.0
    frames = []
    for i in range(n):
        lap_dist = i / n
        frames.append(
            {
                "SessionTime": i * dt,
                "VelocityX": vx,
                "VelocityY": 0.0,
                "LapDistPct": lap_dist,
            }
        )
    return frames


def _make_oval_frames(n: int = 600) -> list[dict]:
    """Create *n* frames following an oval (VelocityX/Y vary sinusoidally)."""
    dt = 1.0 / 60.0
    frames = []
    for i in range(n):
        angle = 2 * np.pi * i / n
        vx = 50.0 * np.cos(angle)
        vy = 50.0 * np.sin(angle)
        lap_dist = i / n
        frames.append(
            {
                "SessionTime": i * dt,
                "VelocityX": float(vx),
                "VelocityY": float(vy),
                "LapDistPct": lap_dist,
            }
        )
    return frames


def _fake_irsdk_module(fake_ir: _FakeIRSDK) -> types.ModuleType:
    """Return a fake ``irsdk`` module whose IRSDK() returns *fake_ir*."""
    mod = types.ModuleType("irsdk")
    mod.IRSDK = MagicMock(return_value=fake_ir)  # type: ignore[attr-defined]
    return mod


# ---------------------------------------------------------------------------
# Test 1 – Synthetic IBT-like input → valid JSON output
# ---------------------------------------------------------------------------


def test_extract_produces_valid_json(tmp_path: Path) -> None:
    """Synthetic IBT with straight-line motion → JSON with all required keys."""
    fake_ir = _FakeIRSDK(
        frames=_make_straight_frames(),
        session_yaml=_make_session_yaml("Sebring", "Full Course"),
    )
    ibt_file = tmp_path / "fake.ibt"
    ibt_file.touch()

    with patch.dict(sys.modules, {"irsdk": _fake_irsdk_module(fake_ir)}):
        out = extract_track_geometry(ibt_file, tmp_path)

    assert out.exists(), "JSON file was not written"

    payload = json.loads(out.read_text(encoding="utf-8"))

    assert "track_key" in payload
    assert "center_line" in payload
    assert "left_edge" in payload
    assert "right_edge" in payload
    assert "source" in payload

    center = payload["center_line"]
    left = payload["left_edge"]
    right = payload["right_edge"]

    assert isinstance(center, list) and len(center) >= 2
    assert isinstance(left, list) and len(left) >= 2
    assert isinstance(right, list) and len(right) >= 2

    # Every element must be a 2-element list of floats
    for pt in center + left + right:
        assert len(pt) == 2
        assert all(isinstance(v, (int, float)) for v in pt)


# ---------------------------------------------------------------------------
# Test 2 – No geometry field in header → VelocityX/VelocityY fallback, no crash
# ---------------------------------------------------------------------------


def test_velocity_fallback_when_no_header_geometry(tmp_path: Path) -> None:
    """No TrackCenterLine in WeekendInfo → VelocityX/Y fallback, still writes JSON."""
    # weekend_info has no geometry fields
    fake_ir = _FakeIRSDK(
        frames=_make_oval_frames(),
        session_yaml=_make_session_yaml("Daytona", "Oval"),
        weekend_info={"TrackDisplayName": "Daytona", "TrackConfigName": "Oval"},
    )
    ibt_file = tmp_path / "fake.ibt"
    ibt_file.touch()

    with patch.dict(sys.modules, {"irsdk": _fake_irsdk_module(fake_ir)}):
        out = extract_track_geometry(ibt_file, tmp_path)

    payload = json.loads(out.read_text(encoding="utf-8"))
    center = payload["center_line"]
    assert len(center) >= 2, "center_line must have data via velocity fallback"

    # Verify integration result is non-trivial (oval should produce spread)
    xs = [pt[0] for pt in center]
    assert max(xs) - min(xs) > 0.01, "center_line should span >1% of [0,1] in X"


# ---------------------------------------------------------------------------
# Test 3 – Missing track width → normal-vector offset fallback, no crash
# ---------------------------------------------------------------------------


def test_edge_offset_fallback_when_no_track_width(tmp_path: Path) -> None:
    """No TrackWidth in session YAML → ±5 m normal-offset fallback for edges."""
    # session YAML has no TrackWidth
    fake_ir = _FakeIRSDK(
        frames=_make_straight_frames(),
        session_yaml=_make_session_yaml("Spa", "GP"),
        weekend_info={},
    )
    ibt_file = tmp_path / "fake.ibt"
    ibt_file.touch()

    with patch.dict(sys.modules, {"irsdk": _fake_irsdk_module(fake_ir)}):
        out = extract_track_geometry(ibt_file, tmp_path)

    payload = json.loads(out.read_text(encoding="utf-8"))
    left = payload["left_edge"]
    right = payload["right_edge"]

    # Both edges must be present (not empty)
    assert len(left) >= 2, "left_edge must not be empty"
    assert len(right) >= 2, "right_edge must not be empty"


# ---------------------------------------------------------------------------
# Test 4 – Normalisation identical to track_geometry._normalise_xy
# ---------------------------------------------------------------------------


def test_normalisation_consistent_with_reconstruct_xy() -> None:
    """_to_normalised_list produces the exact same result as _normalise_xy."""
    # Arbitrary metre-scale XY points
    x = np.array([0.0, 100.0, 200.0, 150.0, 50.0], dtype=np.float64)
    y = np.array([0.0, 80.0, 0.0, -50.0, -30.0], dtype=np.float64)
    pts_m = np.column_stack([x, y])

    expected = _normalise_xy(x.copy(), y.copy())
    actual = np.array(_to_normalised_list(pts_m), dtype=np.float64)

    np.testing.assert_allclose(
        actual,
        expected,
        rtol=1e-10,
        atol=1e-12,
        err_msg="_to_normalised_list deviates from _normalise_xy",
    )


# ---------------------------------------------------------------------------
# Additional robustness tests
# ---------------------------------------------------------------------------


def test_track_key_extraction() -> None:
    """Track key is built from TrackDisplayName and TrackConfigName."""
    fake_ir = _FakeIRSDK(
        frames=[],
        session_yaml=_make_session_yaml("Sebring", "Full Course"),
    )
    key = _read_track_key(fake_ir)
    assert key == "Sebring__Full Course"


def test_track_key_no_config() -> None:
    """Track key omits config part when TrackConfigName is empty."""
    fake_ir = _FakeIRSDK(
        frames=[],
        session_yaml=_make_session_yaml("Road Atlanta", ""),
    )
    key = _read_track_key(fake_ir)
    assert key == "Road Atlanta"


def test_normals_orthogonal_to_tangents() -> None:
    """Computed normals are unit vectors perpendicular to the tangent."""
    # Straight line along X axis
    n = 100
    pts = np.column_stack([np.linspace(0, 100, n), np.zeros(n)])
    normals = _compute_normals(pts)

    # Each normal should be (0, 1) for a horizontal tangent (rot. 90° CCW)
    np.testing.assert_allclose(normals[:, 0], 0.0, atol=1e-10)
    np.testing.assert_allclose(normals[:, 1], 1.0, atol=1e-10)


def test_empty_frames_produce_empty_geometry(tmp_path: Path) -> None:
    """An IBT with no usable frames → empty lists in JSON, no crash."""
    fake_ir = _FakeIRSDK(
        frames=[],
        session_yaml=_make_session_yaml("Unknown", ""),
    )
    ibt_file = tmp_path / "fake.ibt"
    ibt_file.touch()

    with patch.dict(sys.modules, {"irsdk": _fake_irsdk_module(fake_ir)}):
        out = extract_track_geometry(ibt_file, tmp_path)

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["center_line"] == []
    assert payload["left_edge"] == []
    assert payload["right_edge"] == []
