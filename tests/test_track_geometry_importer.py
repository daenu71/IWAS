from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import core.coaching.track_geometry_importer as importer  # noqa: E402
from core.coaching.ibt_track_extractor import TrackSessionMetadata  # noqa: E402


def _metadata(
    track_display_name: str,
    track_config_name: str = "",
    *,
    track_name: str | None = None,
    track_id: int | None = None,
) -> TrackSessionMetadata:
    track_key = f"{track_display_name}__{track_config_name}" if track_config_name else track_display_name
    return TrackSessionMetadata(
        track_key=track_key,
        track_display_name=track_display_name,
        track_config_name=track_config_name,
        track_name=track_name or track_display_name,
        track_id=track_id,
        iracing_build="2026.03",
    )


def _write_geometry(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_find_matching_ibt_files_uses_metadata_not_filename(tmp_path: Path, monkeypatch) -> None:
    telemetry_dir = tmp_path / "telemetry"
    telemetry_dir.mkdir()
    newer = telemetry_dir / "totally_unrelated_name.ibt"
    older = telemetry_dir / "also_unrelated.ibt"
    newer.touch()
    older.touch()
    older_ts = newer.stat().st_mtime - 20
    newer_ts = newer.stat().st_mtime + 20

    path_to_metadata = {
        str(newer): _metadata("Spa-Francorchamps", "Grand Prix", track_name="spa"),
        str(older): _metadata("Okayama", "Full Course"),
    }

    monkeypatch.setattr(importer, "read_ibt_session_metadata", lambda path: path_to_metadata[str(path)])
    newer.touch()
    older.touch()
    older.touch()
    import os

    os.utime(older, (older_ts, older_ts))
    os.utime(newer, (newer_ts, newer_ts))

    matches = importer.find_matching_ibt_files("Spa-Francorchamps__Grand Prix", telemetry_dir=telemetry_dir)

    assert len(matches) == 1
    assert matches[0].ibt_path == newer
    assert matches[0].metadata.track_key == "Spa-Francorchamps__Grand Prix"


def test_import_skips_existing_real_geometry(tmp_path: Path, monkeypatch) -> None:
    storage_root = tmp_path / "storage"
    geometry_path = storage_root / "track_geometries" / "Spa__GP" / "track_road_geometry.json"
    _write_geometry(
        geometry_path,
        {
            "track_key": "Spa__GP",
            "source_type": "ibt",
            "center_line": [[0.0, 0.0], [1.0, 1.0]],
            "left_edge": [[0.0, 0.1], [1.0, 1.1]],
            "right_edge": [[0.0, -0.1], [1.0, 0.9]],
        },
    )

    def _unexpected(*args, **kwargs):
        raise AssertionError("extract_track_geometry must not be called")

    monkeypatch.setattr(importer, "extract_track_geometry", _unexpected)

    result = importer.import_track_geometry_from_telemetry(
        "Spa__GP",
        telemetry_dir=tmp_path / "missing_telemetry",
        storage_root=storage_root,
    )

    assert result.status == "skipped_existing"
    assert result.existing_source_type == "ibt"
    assert result.geometry_path == geometry_path


def test_import_replaces_legacy_fallback_geometry(tmp_path: Path, monkeypatch) -> None:
    storage_root = tmp_path / "storage"
    telemetry_dir = tmp_path / "telemetry"
    telemetry_dir.mkdir()
    ibt_path = telemetry_dir / "spa_session.ibt"
    ibt_path.touch()
    geometry_path = storage_root / "track_geometries" / "Spa__GP" / "track_road_geometry.json"
    _write_geometry(
        geometry_path,
        {
            "track_key": "Spa__GP",
            "source": "parquet_fallback",
            "center_line": [[0.0, 0.0], [1.0, 1.0]],
            "left_edge": [[0.0, 0.1], [1.0, 1.1]],
            "right_edge": [[0.0, -0.1], [1.0, 0.9]],
        },
    )

    candidate = importer.IbtTrackCandidate(
        ibt_path=ibt_path,
        metadata=_metadata("Spa", "GP"),
        modified_ts=ibt_path.stat().st_mtime,
    )
    monkeypatch.setattr(importer, "find_matching_ibt_files", lambda *args, **kwargs: [candidate])

    def _fake_extract(found_ibt: Path, storage_root_arg: Path) -> Path:
        assert found_ibt == ibt_path
        out_path = storage_root_arg / "track_geometries" / "Spa__GP" / "track_road_geometry.json"
        _write_geometry(
            out_path,
            {
                "track_key": "Spa__GP",
                "source_type": "ibt",
                "source": "ibt_telemetry",
                "source_path": str(found_ibt),
                "geometry_kind": "centerline_only",
                "position_source": "latlon",
                "distance_source": "LapDistPct",
                "center_line": [
                    {"lap_dist_pct": 0.0, "lat": 47.0, "lon": 8.0, "x_m": 0.0, "y_m": 0.0},
                    {"lap_dist_pct": 0.5, "lat": 47.0002, "lon": 8.0002, "x_m": 15.0, "y_m": 22.0},
                ],
                "left_edge": [],
                "right_edge": [],
            },
        )
        return out_path

    monkeypatch.setattr(importer, "extract_track_geometry", _fake_extract)

    result = importer.import_track_geometry_from_telemetry(
        "Spa__GP",
        telemetry_dir=telemetry_dir,
        storage_root=storage_root,
    )

    saved = json.loads(geometry_path.read_text(encoding="utf-8"))
    assert result.status == "imported"
    assert result.matched_ibt_path == ibt_path
    assert result.existing_source_type == "fallback"
    assert saved["source_type"] == "ibt"
    assert saved["source_path"] == str(ibt_path)
    assert saved["geometry_kind"] == "centerline_only"
    assert saved["left_edge"] == []
    assert saved["right_edge"] == []


def test_import_returns_no_match_when_scan_finds_nothing(tmp_path: Path, monkeypatch) -> None:
    telemetry_dir = tmp_path / "telemetry"
    telemetry_dir.mkdir()
    monkeypatch.setattr(importer, "find_matching_ibt_files", lambda *args, **kwargs: [])

    result = importer.import_track_geometry_from_telemetry(
        "Monza",
        telemetry_dir=telemetry_dir,
        storage_root=tmp_path / "storage",
    )

    assert result.status == "no_matching_ibt"


def test_import_skips_invalid_ibt_geometry(tmp_path: Path, monkeypatch) -> None:
    storage_root = tmp_path / "storage"
    telemetry_dir = tmp_path / "telemetry"
    telemetry_dir.mkdir()
    ibt_path = telemetry_dir / "spa_session.ibt"
    ibt_path.touch()

    candidate = importer.IbtTrackCandidate(
        ibt_path=ibt_path,
        metadata=_metadata("Spa", "GP"),
        modified_ts=ibt_path.stat().st_mtime,
    )
    monkeypatch.setattr(importer, "find_matching_ibt_files", lambda *args, **kwargs: [candidate])

    def _invalid_extract(*args, **kwargs):
        raise RuntimeError("missing valid Lat/Lon + LapDistPct centerline in IBT")

    monkeypatch.setattr(importer, "extract_track_geometry", _invalid_extract)

    result = importer.import_track_geometry_from_telemetry(
        "Spa__GP",
        telemetry_dir=telemetry_dir,
        storage_root=storage_root,
    )

    assert result.status == "invalid_ibt_geometry"
    assert result.matched_ibt_path == ibt_path
    assert result.geometry_path == storage_root / "track_geometries" / "Spa__GP" / "track_road_geometry.json"
    assert not result.geometry_path.exists()
