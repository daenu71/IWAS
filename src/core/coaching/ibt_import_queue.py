"""Startup discovery for IBT files and persistence of a pending import queue."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from core.persistence import get_iracing_telemetry_dir

from .storage import get_coaching_storage_dir


_LOG = logging.getLogger(__name__)

_QUEUE_FILENAME = "ibt_import_queue.json"
_REGISTRY_FILENAME = "ibt_import_registry.json"
_QUEUE_VERSION = 1
_REGISTRY_VERSION = 1
_FINGERPRINT_READ_BYTES = 1024 * 1024


@dataclass(frozen=True)
class IbtDiscoverySummary:
    telemetry_dir: Path
    storage_root: Path
    queue_path: Path
    registry_path: Path
    telemetry_dir_exists: bool
    scanned_file_count: int
    queued_count: int
    duplicate_count: int
    pending_count: int


def discover_and_queue_new_ibt_files(
    *,
    telemetry_dir: str | Path | None = None,
    storage_root: str | Path | None = None,
) -> IbtDiscoverySummary:
    """Scan the telemetry directory and append new IBTs to the pending queue."""
    telemetry_path = _resolve_telemetry_dir(telemetry_dir)
    storage_path = _resolve_storage_root(storage_root)
    queue_path = storage_path / _QUEUE_FILENAME
    registry_path = storage_path / _REGISTRY_FILENAME

    queue_doc = _load_queue_doc(queue_path)
    registry_doc = _load_registry_doc(registry_path)
    queue_entries = list(queue_doc["entries"])
    registry_sources = dict(registry_doc["sources"])

    pending_count_before = len(queue_entries)
    if not telemetry_path.exists() or not telemetry_path.is_dir():
        _LOG.info(
            "[ibt_import_discovery] telemetry directory unavailable telemetry_dir=%s",
            telemetry_path,
        )
        return IbtDiscoverySummary(
            telemetry_dir=telemetry_path,
            storage_root=storage_path,
            queue_path=queue_path,
            registry_path=registry_path,
            telemetry_dir_exists=False,
            scanned_file_count=0,
            queued_count=0,
            duplicate_count=0,
            pending_count=pending_count_before,
        )

    scanned_file_count = 0
    queued_count = 0
    duplicate_count = 0
    changed = False
    now_iso = _utc_now_iso()

    known_fingerprints = {
        _coerce_text(entry.get("fingerprint"))
        for entry in queue_entries
        if _coerce_text(entry.get("fingerprint"))
    }
    known_fingerprints.update(registry_sources.keys())

    for ibt_path in _iter_ibt_files(telemetry_path):
        scanned_file_count += 1
        fingerprint, stat_payload = _build_fingerprint_payload(ibt_path)
        if fingerprint is None or stat_payload is None:
            continue
        if fingerprint in known_fingerprints:
            duplicate_count += 1
            continue

        entry = {
            "queue_id": f"ibt-{fingerprint[:16]}",
            "fingerprint": fingerprint,
            "state": "pending",
            "source_path": str(ibt_path),
            "source_name": ibt_path.name,
            "size_bytes": int(stat_payload["size_bytes"]),
            "modified_ts": float(stat_payload["modified_ts"]),
            "discovered_at": now_iso,
            "discovery_reason": "app_start",
        }
        queue_entries.append(entry)
        registry_sources[fingerprint] = {
            "fingerprint": fingerprint,
            "state": "queued",
            "first_seen_at": now_iso,
            "source_path": str(ibt_path),
            "source_name": ibt_path.name,
            "size_bytes": int(stat_payload["size_bytes"]),
            "modified_ts": float(stat_payload["modified_ts"]),
        }
        known_fingerprints.add(fingerprint)
        queued_count += 1
        changed = True

    if changed:
        queue_doc = {
            "version": _QUEUE_VERSION,
            "updated_at": now_iso,
            "entries": queue_entries,
        }
        registry_doc = {
            "version": _REGISTRY_VERSION,
            "updated_at": now_iso,
            "sources": registry_sources,
        }
        _write_json(queue_path, queue_doc)
        _write_json(registry_path, registry_doc)

    pending_count = len(queue_entries)
    _LOG.info(
        "[ibt_import_discovery] telemetry_dir=%s scanned=%d queued=%d duplicates=%d pending=%d",
        telemetry_path,
        scanned_file_count,
        queued_count,
        duplicate_count,
        pending_count,
    )
    return IbtDiscoverySummary(
        telemetry_dir=telemetry_path,
        storage_root=storage_path,
        queue_path=queue_path,
        registry_path=registry_path,
        telemetry_dir_exists=True,
        scanned_file_count=scanned_file_count,
        queued_count=queued_count,
        duplicate_count=duplicate_count,
        pending_count=pending_count,
    )


def _resolve_telemetry_dir(telemetry_dir: str | Path | None) -> Path:
    if telemetry_dir is None:
        return Path(get_iracing_telemetry_dir())
    return Path(get_iracing_telemetry_dir(telemetry_dir))


def _resolve_storage_root(storage_root: str | Path | None) -> Path:
    if storage_root is None:
        return Path(get_coaching_storage_dir())
    return Path(storage_root)


def _iter_ibt_files(telemetry_dir: Path) -> list[Path]:
    candidates: list[tuple[float, str, Path]] = []
    for ibt_path in telemetry_dir.rglob("*.ibt"):
        try:
            stat = ibt_path.stat()
        except Exception as exc:
            _LOG.debug("[ibt_import_discovery] unreadable ibt=%s (%s)", ibt_path, exc)
            continue
        candidates.append((float(stat.st_mtime), str(ibt_path).lower(), ibt_path))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in candidates]


def _build_fingerprint_payload(ibt_path: Path) -> tuple[str | None, dict[str, float | int] | None]:
    try:
        stat = ibt_path.stat()
        digest = hashlib.sha256()
        digest.update(str(int(stat.st_size)).encode("ascii"))
        digest.update(b":")
        with ibt_path.open("rb") as fh:
            head = fh.read(_FINGERPRINT_READ_BYTES)
            digest.update(head)
            if stat.st_size > _FINGERPRINT_READ_BYTES:
                tail_size = min(int(stat.st_size), _FINGERPRINT_READ_BYTES)
                fh.seek(max(0, int(stat.st_size) - tail_size))
                digest.update(fh.read(tail_size))
    except Exception as exc:
        _LOG.debug("[ibt_import_discovery] fingerprint failed ibt=%s (%s)", ibt_path, exc)
        return (None, None)
    return (
        digest.hexdigest(),
        {
            "size_bytes": int(stat.st_size),
            "modified_ts": float(stat.st_mtime),
        },
    )


def _load_queue_doc(path: Path) -> dict[str, Any]:
    data = _read_json(path)
    entries = data.get("entries")
    if isinstance(entries, list):
        return {
            "version": _coerce_int(data.get("version"), _QUEUE_VERSION),
            "updated_at": _coerce_text(data.get("updated_at")),
            "entries": [entry for entry in entries if isinstance(entry, dict)],
        }
    return {
        "version": _QUEUE_VERSION,
        "updated_at": "",
        "entries": [],
    }


def _load_registry_doc(path: Path) -> dict[str, Any]:
    data = _read_json(path)
    raw_sources = data.get("sources")
    if not isinstance(raw_sources, dict):
        return {
            "version": _REGISTRY_VERSION,
            "updated_at": "",
            "sources": {},
        }
    sources: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in raw_sources.items():
        key = _coerce_text(raw_key)
        if not key or not isinstance(raw_value, dict):
            continue
        sources[key] = dict(raw_value)
    return {
        "version": _coerce_int(data.get("version"), _REGISTRY_VERSION),
        "updated_at": _coerce_text(data.get("updated_at")),
        "sources": sources,
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        return {}
    return {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""


def _coerce_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(fallback)
