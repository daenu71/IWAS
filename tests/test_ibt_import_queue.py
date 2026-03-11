from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.coaching.ibt_import_queue import discover_and_queue_new_ibt_files  # noqa: E402


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_discovery_queues_new_ibt_files_and_writes_registry(tmp_path: Path) -> None:
    telemetry_dir = tmp_path / "telemetry"
    storage_root = tmp_path / "storage"
    telemetry_dir.mkdir()

    first = telemetry_dir / "a_first.ibt"
    second = telemetry_dir / "b_second.ibt"
    first.write_bytes(b"first-ibt-content")
    second.write_bytes(b"second-ibt-content")
    os.utime(first, (100.0, 100.0))
    os.utime(second, (200.0, 200.0))

    result = discover_and_queue_new_ibt_files(
        telemetry_dir=telemetry_dir,
        storage_root=storage_root,
    )

    assert result.telemetry_dir_exists is True
    assert result.scanned_file_count == 2
    assert result.queued_count == 2
    assert result.duplicate_count == 0
    assert result.pending_count == 2

    queue_doc = _read_json(storage_root / "ibt_import_queue.json")
    registry_doc = _read_json(storage_root / "ibt_import_registry.json")

    assert len(queue_doc["entries"]) == 2
    assert queue_doc["entries"][0]["source_name"] == "a_first.ibt"
    assert queue_doc["entries"][1]["source_name"] == "b_second.ibt"
    assert queue_doc["entries"][0]["state"] == "pending"
    assert len(registry_doc["sources"]) == 2


def test_discovery_deduplicates_already_queued_and_same_content_duplicates(tmp_path: Path) -> None:
    telemetry_dir = tmp_path / "telemetry"
    storage_root = tmp_path / "storage"
    telemetry_dir.mkdir()

    original = telemetry_dir / "race_001.ibt"
    duplicate_copy = telemetry_dir / "race_001_copy.ibt"
    payload = b"same-ibt-payload"
    original.write_bytes(payload)
    duplicate_copy.write_bytes(payload)

    first_result = discover_and_queue_new_ibt_files(
        telemetry_dir=telemetry_dir,
        storage_root=storage_root,
    )
    second_result = discover_and_queue_new_ibt_files(
        telemetry_dir=telemetry_dir,
        storage_root=storage_root,
    )

    assert first_result.scanned_file_count == 2
    assert first_result.queued_count == 1
    assert first_result.duplicate_count == 1
    assert first_result.pending_count == 1

    assert second_result.scanned_file_count == 2
    assert second_result.queued_count == 0
    assert second_result.duplicate_count == 2
    assert second_result.pending_count == 1

    queue_doc = _read_json(storage_root / "ibt_import_queue.json")
    assert len(queue_doc["entries"]) == 1
    assert queue_doc["entries"][0]["source_name"] in {"race_001.ibt", "race_001_copy.ibt"}


def test_discovery_returns_without_writing_when_telemetry_dir_is_missing(tmp_path: Path) -> None:
    storage_root = tmp_path / "storage"

    result = discover_and_queue_new_ibt_files(
        telemetry_dir=tmp_path / "missing",
        storage_root=storage_root,
    )

    assert result.telemetry_dir_exists is False
    assert result.scanned_file_count == 0
    assert result.queued_count == 0
    assert result.pending_count == 0
    assert not (storage_root / "ibt_import_queue.json").exists()
    assert not (storage_root / "ibt_import_registry.json").exists()
