"""track_key.py – Central helper for building coaching track keys.

Single source of truth for the canonical track-key format used across
all coaching modules (extractor, view-model, analyzer, UI).

Format: "<track_display_name>__<track_config_name>"
  - Double underscore as separator between name and config.
  - Spaces in names are preserved (directory names with spaces are OK).
  - Only filesystem-forbidden characters are replaced by "_":
    \\ / : * ? " < > |
  - If config_name is empty the separator is omitted:
    build_track_key("Sebring", "") -> "Sebring"

Examples
--------
>>> build_track_key("Misano World Circuit Marco Simoncelli", "Grand Prix")
'Misano World Circuit Marco Simoncelli__Grand Prix'
>>> build_track_key("Sebring", "")
'Sebring'
"""

from __future__ import annotations

import re

_FORBIDDEN = re.compile(r'[\\/:*?"<>|]')


def build_track_key(track_display_name: str, track_config_name: str) -> str:
    """Return the canonical coaching track key.

    Parameters
    ----------
    track_display_name:
        Human-readable track name, e.g. ``"Misano World Circuit Marco Simoncelli"``.
    track_config_name:
        Track configuration name, e.g. ``"Grand Prix"``.  Pass an empty string
        when the track has no configuration variant.

    Returns
    -------
    str
        Canonical key such as ``"Misano World Circuit Marco Simoncelli__Grand Prix"``.
    """
    name = _FORBIDDEN.sub("_", track_display_name.strip())
    config = _FORBIDDEN.sub("_", track_config_name.strip())
    if config:
        return f"{name}__{config}"
    return name
