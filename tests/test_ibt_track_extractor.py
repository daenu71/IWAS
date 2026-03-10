from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.coaching.ibt_track_extractor import (  # noqa: E402
    _compute_normals,
    _read_track_key,
    extract_track_geometry,
    read_ibt_session_metadata,
)


def _make_session_yaml(
    track_display: str = "Sebring",
    track_config: str = "Full Course",
    *,
    track_name: str | None = None,
    track_width: float | None = None,
    track_id: int | None = None,
    build_version: str | None = None,
) -> str:
    lines = [
        "WeekendInfo:",
        f" TrackDisplayName: {track_display}",
        f" TrackConfigName: {track_config}",
    ]
    if track_name is not None:
        lines.append(f" TrackName: {track_name}")
    if track_width is not None:
        lines.append(f" TrackWidth: {track_width}")
    if track_id is not None:
        lines.append(f" TrackID: {track_id}")
    if build_version is not None:
        lines.append(f" BuildVersion: {build_version}")
    return "\n".join(lines)


class _FakeIRSDK:
    def __init__(
        self,
        frames: list[dict],
        *,
        session_yaml: str = "",
        weekend_info: dict | None = None,
    ) -> None:
        self._frames = frames
        self._idx = 0
        self.session_info = session_yaml
        self._weekend_info = weekend_info or {}

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
        for idx in range(self._idx + 1, len(self._frames)):
            if self._frames[idx].get("SessionTime", -1) >= time_secs:
                self._idx = idx
                return True
        return False


def _make_straight_frames(n: int = 600, vx: float = 50.0) -> list[dict]:
    dt = 1.0 / 60.0
    frames: list[dict] = []
    for idx in range(n):
        frames.append(
            {
                "SessionTime": idx * dt,
                "VelocityX": vx,
                "VelocityY": 0.0,
                "LapDistPct": idx / n,
            }
        )
    return frames


def _make_oval_frames(n: int = 600) -> list[dict]:
    dt = 1.0 / 60.0
    frames: list[dict] = []
    for idx in range(n):
        angle = 2 * np.pi * idx / n
        frames.append(
            {
                "SessionTime": idx * dt,
                "VelocityX": float(50.0 * np.cos(angle)),
                "VelocityY": float(50.0 * np.sin(angle)),
                "LapDistPct": idx / n,
            }
        )
    return frames


def _fake_irsdk_module(fake_ir: _FakeIRSDK) -> types.ModuleType:
    mod = types.ModuleType("irsdk")
    mod.IRSDK = MagicMock(return_value=fake_ir)  # type: ignore[attr-defined]
    return mod


def test_extract_produces_valid_json(tmp_path: Path) -> None:
    fake_ir = _FakeIRSDK(
        frames=_make_straight_frames(),
        session_yaml=_make_session_yaml("Sebring", "Full Course", build_version="2026.03"),
    )
    ibt_file = tmp_path / "fake.ibt"
    ibt_file.touch()

    with patch.dict(sys.modules, {"irsdk": _fake_irsdk_module(fake_ir)}):
        out = extract_track_geometry(ibt_file, tmp_path)

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["track_key"] == "Sebring__Full Course"
    assert payload["source_type"] == "ibt"
    assert payload["source"] == "ibt_telemetry"
    assert payload["source_path"] == str(ibt_file)
    assert payload["iracing_build"] == "2026.03"
    assert isinstance(payload["center_line"], list) and len(payload["center_line"]) >= 2
    assert isinstance(payload["left_edge"], list) and len(payload["left_edge"]) >= 2
    assert isinstance(payload["right_edge"], list) and len(payload["right_edge"]) >= 2


def test_velocity_fallback_when_no_header_geometry(tmp_path: Path) -> None:
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
    xs = [pt[0] for pt in center]
    assert len(center) >= 2
    assert max(xs) - min(xs) > 0.01


def test_edge_offset_fallback_when_no_track_width(tmp_path: Path) -> None:
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
    assert len(payload["left_edge"]) >= 2
    assert len(payload["right_edge"]) >= 2


def test_read_ibt_session_metadata_exposes_track_identifiers(tmp_path: Path) -> None:
    fake_ir = _FakeIRSDK(
        frames=[],
        session_yaml=_make_session_yaml(
            "Road Atlanta",
            "Full Course",
            track_name="road_atlanta",
            track_id=123,
            build_version="2026.02",
        ),
    )
    ibt_file = tmp_path / "road_atlanta.ibt"
    ibt_file.touch()

    with patch.dict(sys.modules, {"irsdk": _fake_irsdk_module(fake_ir)}):
        metadata = read_ibt_session_metadata(ibt_file)

    assert metadata.track_key == "Road Atlanta__Full Course"
    assert metadata.track_display_name == "Road Atlanta"
    assert metadata.track_config_name == "Full Course"
    assert metadata.track_name == "road_atlanta"
    assert metadata.track_id == 123
    assert metadata.iracing_build == "2026.02"


def test_track_key_extraction() -> None:
    fake_ir = _FakeIRSDK(
        frames=[],
        session_yaml=_make_session_yaml("Sebring", "Full Course"),
    )
    assert _read_track_key(fake_ir) == "Sebring__Full Course"


def test_track_key_no_config() -> None:
    fake_ir = _FakeIRSDK(
        frames=[],
        session_yaml=_make_session_yaml("Road Atlanta", ""),
    )
    assert _read_track_key(fake_ir) == "Road Atlanta"


def test_normals_orthogonal_to_tangents() -> None:
    pts = np.column_stack([np.linspace(0, 100, 100), np.zeros(100)])
    normals = _compute_normals(pts)
    np.testing.assert_allclose(normals[:, 0], 0.0, atol=1e-10)
    np.testing.assert_allclose(normals[:, 1], 1.0, atol=1e-10)


def test_empty_frames_produce_empty_geometry(tmp_path: Path) -> None:
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
