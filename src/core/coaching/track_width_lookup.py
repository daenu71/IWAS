"""Track half-width lookup from track_widths.json with fuzzy fallback."""

from __future__ import annotations

import difflib
import json
import logging
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

_DATA_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "data"
    / "coaching"
    / "track_geometries"
    / "track_widths.json"
)
_DEFAULT_FALLBACK: float = 5.5

_cache: dict[str, Any] | None = None


def _load_data() -> dict[str, Any]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            _LOG.warning("[track_width] Failed to load %s: %s", _DATA_PATH, exc)
            _cache = {}
    return _cache


def get_track_half_width_m(track_name: str) -> float:
    """Return track half-width in metres for *track_name*.

    Lookup order:
    1. Exact match (case-insensitive) in track_widths.json
    2. Fuzzy match via difflib (cutoff=0.6, best match only)
    3. Fallback from ``_meta.fallback_half_width_m`` (default 5.5 m)

    Never raises.
    """
    try:
        data = _load_data()
        tracks: dict[str, Any] = data.get("tracks", {})
        fallback: float = float(
            data.get("_meta", {}).get("fallback_half_width_m", _DEFAULT_FALLBACK)
        )

        if not track_name:
            return fallback

        name_lower = track_name.lower()

        # 1. Exact match (case-insensitive)
        for key, entry in tracks.items():
            if key.lower() == name_lower:
                return float(entry["half_width_m"])

        # 2. Fuzzy match
        candidates = list(tracks.keys())
        matches = difflib.get_close_matches(track_name, candidates, n=1, cutoff=0.6)
        if matches:
            matched = matches[0]
            half_w = float(tracks[matched]["half_width_m"])
            _LOG.info(
                "[track_width] fuzzy match: '%s' → '%s' (%.1fm)",
                track_name,
                matched,
                half_w,
            )
            return half_w

        # 3. Fallback
        _LOG.info(
            "[track_width] no match for '%s', using fallback %.1fm",
            track_name,
            fallback,
        )
        return fallback

    except Exception as exc:
        _LOG.warning("[track_width] error during lookup for '%s': %s", track_name, exc)
        return _DEFAULT_FALLBACK
